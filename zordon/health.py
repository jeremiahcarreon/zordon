"""Runtime health: one cheap check per part Zordon needs, evaluated against the
*running* agent (not the config on disk, which is ``zordon doctor``'s job).

``collect(agent)`` returns a :class:`HealthReport` whose items map one to one
onto the ``health`` protocol message (``HealthOut``) and the header strip in
the web client. Every probe is cheap: file and attribute checks, plus two
network-ish probes (the tmux server and the Ollama server) that run on helper
threads and are abandoned after :data:`PROBE_TIMEOUT_S`. Nothing here makes a
paid API call.

Items marked ``optional`` (hooks, update, tunnel) never pull the overall status
below ``warn``: Zordon works without them.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zordon.transport.protocol import HealthItem, HealthOut

log = logging.getLogger("zordon.health")

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}
PROBE_TIMEOUT_S = 1.0
OPTIONAL_KEYS = frozenset({"hooks", "update", "tunnel"})

LABELS = {
    "account": "account",
    "tmux": "tmux",
    "agent": "agent",
    "sessions": "session",
    "normalizer": "normalizer",
    "tts": "speech out",
    "stt": "speech in",
    "vad": "voice detection",
    "router": "router",
    "threads": "threads",
    "hooks": "hooks",
    "update": "update",
    "tunnel": "tunnel",
}

# Fix wording shared with ``zordon doctor`` so the strip and the CLI agree.
FIX_DOWNLOAD = "zordon doctor --download"
FIX_ROOT = "create a normal user with sudo and run Zordon there: useradd -m -s /bin/bash <name>; usermod -aG sudo <name>; passwd <name>; su - <name>"
FIX_TMUX = "install tmux 3.2 or newer (or run `zordon setup`)"
FIX_GPU = "install the CUDA libraries into Zordon's environment: zordon setup (it offers GPU support) or `uv tool install --force 'zordon[gpu] @ https://github.com/jeremiahcarreon/zordon/archive/refs/heads/main.tar.gz'`, then restart"
FIX_CLAUDE = "install Claude Code and log in (or run `zordon setup`)"
FIX_CURL = "install curl (or run `zordon setup`)"
FIX_ANTHROPIC_KEY = "set providers.keys.anthropic in config.toml or export ANTHROPIC_API_KEY"
FIX_ROUTER = "set providers.keys.typesafe or providers.keys.anthropic, or providers.router = \"ollama\""
FIX_NORMALIZER = (
    "set providers.keys.anthropic, or install Ollama and `ollama pull <model>`, "
    "or install Claude Code (see docs/troubleshooting.md)"
)
FIX_RESTART = "restart `zordon serve` and check the log"
FIX_SESSION = "start or resume a session from the Sessions sheet, or say \"start a session in <folder>\""

Which = Callable[[str], str | None]


@dataclass(slots=True)
class Item:
    key: str
    status: str  # ok | warn | fail
    detail: str = ""
    fix: str = ""
    optional: bool = False

    @property
    def label(self) -> str:
        return LABELS.get(self.key, self.key)

    def to_out(self) -> HealthItem:
        return HealthItem(key=self.key, label=self.label, status=self.status, detail=self.detail, fix=self.fix)  # type: ignore[arg-type]


@dataclass
class HealthReport:
    status: str
    items: list[Item] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def degraded(self) -> list[Item]:
        return [i for i in self.items if i.status != OK]

    def to_out(self) -> HealthOut:
        return HealthOut(status=self.status, items=[i.to_out() for i in self.items], ts=self.ts)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "ts": self.ts, "items": [asdict(i) | {"label": i.label} for i in self.items]}

    def signature(self) -> tuple[tuple[str, str, str, str], ...]:
        """What "changed" means for the health thread: every field but ``ts``."""
        return tuple((i.key, i.status, i.detail, i.fix) for i in self.items)

    def summary_sentence(self) -> str:
        """One spoken sentence about the degraded items; empty when everything is fine."""
        bad = self.degraded()
        if not bad:
            return ""
        parts = [f"{i.label} {'failed' if i.status == FAIL else 'warning'}: {i.detail}".rstrip(": ") for i in bad]
        return "Needs attention: " + "; ".join(parts) + "."


def overall_status(items: list[Item]) -> str:
    worst = OK
    for i in items:
        s = i.status
        if i.optional and s == FAIL:
            s = WARN
        if _RANK.get(s, 0) > _RANK[worst]:
            worst = s
    return worst


def make_report(items: list[Item], ts: float | None = None) -> HealthReport:
    for i in items:
        if i.key in OPTIONAL_KEYS:
            i.optional = True
            if i.status == FAIL:
                i.status = WARN
    return HealthReport(status=overall_status(items), items=items, ts=ts if ts is not None else time.time())


# ---- collection -----------------------------------------------------------------------------


def collect(agent: Any, *, which: Which = shutil.which, timeout: float = PROBE_TIMEOUT_S) -> HealthReport:
    """Evaluate every item against ``agent`` (a ``zordon.app.Agent`` or anything with the
    same attributes). Never raises: a probe that blows up becomes a ``warn`` item."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="zordon-health-probe")
    try:
        tmux_future = pool.submit(_tmux_alive, agent)
        ollama_future = pool.submit(_ollama_probe, agent)
        items = [
            _guard("account", check_account),
            _guard("tmux", lambda: check_tmux(agent, tmux_future, which, timeout)),
            _guard("agent", lambda: check_agent(agent)),
            _guard("sessions", lambda: check_sessions(agent)),
            _guard("normalizer", lambda: check_normalizer(agent, ollama_future, timeout)),
            _guard("tts", lambda: check_tts(agent)),
            _guard("stt", lambda: check_stt(agent)),
            _guard("vad", lambda: check_vad(agent)),
            _guard("router", lambda: check_router(agent)),
            _guard("threads", lambda: check_threads(agent)),
            _guard("hooks", lambda: check_hooks(which)),
            _guard("update", lambda: check_update(agent)),
        ]
        tunnel = check_tunnel(agent)
        if tunnel is not None:
            items.append(tunnel)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return make_report(items)


def _guard(key: str, fn: Callable[[], Item]) -> Item:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one broken probe must not hide the others
        log.debug("health probe %s failed", key, exc_info=True)
        return Item(key, WARN, f"check failed: {_short(e)}", FIX_RESTART)


def _result(future: concurrent.futures.Future[Any], timeout: float) -> Any:
    """The probe's value, or ``TimeoutError`` after ``timeout`` seconds."""
    return future.result(timeout=timeout)


# ---- the probes -----------------------------------------------------------------------------


def _tmux_alive(agent: Any) -> bool | None:
    """True/False from ``server_alive``; None when the tmux object has no such probe."""
    tmux = getattr(getattr(agent, "manager", None), "tmux", None)
    alive = getattr(tmux, "server_alive", None)
    if not callable(alive):
        return None
    return bool(alive())


def check_tmux(agent: Any, future: concurrent.futures.Future[Any], which: Which, timeout: float) -> Item:
    tmux = getattr(getattr(agent, "manager", None), "tmux", None)
    binary = str(getattr(tmux, "binary", "tmux") or "tmux")
    present = getattr(tmux, "binary_available", None)
    has_binary = bool(present()) if callable(present) else which(binary) is not None
    if not has_binary:
        return Item("tmux", FAIL, f"{binary} not found on PATH", FIX_TMUX)
    try:
        alive = _result(future, timeout)
    except concurrent.futures.TimeoutError:
        return Item("tmux", WARN, "tmux did not answer within a second", "check `tmux list-sessions` by hand")
    except Exception as e:  # noqa: BLE001
        return Item("tmux", WARN, f"tmux probe failed: {_short(e)}", "check `tmux list-sessions` by hand")
    if alive is None:
        return Item("tmux", OK, f"{binary} available")
    if alive:
        return Item("tmux", OK, "server running")
    return Item("tmux", WARN, "no tmux server yet; the first session starts one", "")


def check_agent(agent: Any) -> Item:
    cfg = getattr(agent, "config", None)
    key = str(getattr(getattr(cfg, "providers", None), "agent", "") or "claude-code")
    probe = getattr(agent, "available_agents", None)
    found: dict[str, str | None]
    if callable(probe):
        found = dict(probe() or {})
    else:
        from zordon.agents import available_agents  # noqa: PLC0415

        found = dict(available_agents(cfg))
    display = _agent_display(agent, key)
    if key == "generic":
        return Item("agent", OK, "generic adapter (attach to a tmux pane)")
    path = found.get(key)
    if path is None:
        fix = FIX_CLAUDE if key == "claude-code" else f"install {display} so its binary is on PATH"
        return Item("agent", FAIL, f"{display} not found on PATH", fix)
    adapters = getattr(agent, "adapters", None) or {}
    adapter = adapters.get(key) if isinstance(adapters, dict) else None
    logged = getattr(adapter, "logged_in", None)
    try:
        state = logged() if callable(logged) else None
    except Exception:  # noqa: BLE001
        state = None
    if state is False:
        return Item("agent", FAIL, f"{display} is installed but not logged in", f"run `{found.get(key) and key.split('-')[0] or 'claude'}` once in a terminal and sign in (or `tmux attach -t zordon` when a session is waiting on it)")
    return Item("agent", OK, f"{display} at {path}" if path else display)


def _agent_display(agent: Any, key: str) -> str:
    adapters = getattr(agent, "adapters", None) or {}
    adapter = adapters.get(key) if isinstance(adapters, dict) else None
    info = getattr(adapter, "info", None)
    return str(getattr(info, "display_name", None) or key)


def check_sessions(agent: Any) -> Item:
    sessions = getattr(agent, "sessions", None)
    focused = sessions.focused() if sessions is not None and callable(getattr(sessions, "focused", None)) else None
    if not focused:
        return Item("sessions", WARN, "no session focused", FIX_SESSION)
    title = focused[:8]
    manager = getattr(agent, "manager", None)
    live = getattr(manager, "sessions", None)
    s = live.get(focused) if isinstance(live, dict) else None
    if s is not None:
        title = str(getattr(s, "title", "") or Path(str(getattr(s, "cwd", "") or "")).name or focused[:8])
    return Item("sessions", OK, f"{title} focused")


def _ollama_probe(agent: Any) -> tuple[bool, list[str]]:
    """``(reachable, models)`` for the configured Ollama server; only awaited when the
    normalizer is the Ollama one."""
    norm = getattr(getattr(agent, "providers", None), "normalizer", None)
    if getattr(norm, "name", "") != "ollama":
        return (False, [])
    from zordon.output.normalizer.ollama import server_models  # noqa: PLC0415

    cfg = getattr(getattr(agent, "config", None), "providers", None)
    url = str(getattr(norm, "url", "") or getattr(cfg, "ollama_url", "") or "http://127.0.0.1:11434")
    return (True, server_models(url, timeout=PROBE_TIMEOUT_S))


def check_normalizer(agent: Any, ollama_future: concurrent.futures.Future[Any], timeout: float) -> Item:
    norm = getattr(getattr(agent, "providers", None), "normalizer", None)
    name = str(getattr(norm, "name", "") or "?")
    cfg = getattr(getattr(agent, "config", None), "providers", None)
    configured = str(getattr(cfg, "normalizer", "auto") or "auto")
    if name == "passthrough":
        if configured == "passthrough":
            return Item("normalizer", WARN, "passthrough: output is spoken terse, as written", "set providers.normalizer = \"auto\"")
        return Item("normalizer", WARN, f"{configured!r} is not configured; passthrough speaks terse output", FIX_NORMALIZER)
    if name == "ollama":
        model = str(getattr(norm, "model", "") or getattr(cfg, "ollama_model", ""))
        url = str(getattr(norm, "url", "") or getattr(cfg, "ollama_url", ""))
        try:
            _wanted, models = _result(ollama_future, timeout)
        except concurrent.futures.TimeoutError:
            return Item("normalizer", FAIL, f"ollama at {url} did not answer within a second", "run `ollama serve`")
        except Exception as e:  # noqa: BLE001 - ProviderNotConfigured when unreachable
            return Item("normalizer", FAIL, f"ollama: no server at {url} ({_short(e)})", "run `ollama serve`")
        from zordon.output.normalizer.ollama import has_model  # noqa: PLC0415

        if not has_model(models, model):
            return Item("normalizer", FAIL, f"ollama server up, model {model!r} not pulled", f"ollama pull {model}")
        return Item("normalizer", OK, f"ollama {model} at {url}")
    if name == "anthropic":
        creds = getattr(norm, "credentials_configured", None)
        if callable(creds) and not creds():
            return Item("normalizer", FAIL, "anthropic: no credentials", FIX_ANTHROPIC_KEY)
        return Item("normalizer", OK, f"anthropic {getattr(norm, 'model', '')}".strip())
    if name == "claude-cli":
        binary = str(getattr(norm, "binary", "") or "claude")
        if not (os.path.isabs(binary) and os.access(binary, os.X_OK)) and not shutil.which(binary):
            return Item("normalizer", FAIL, f"claude-cli: {binary!r} is not runnable", FIX_CLAUDE)
        return Item("normalizer", OK, f"claude-cli {getattr(norm, 'model', '')} (whole turns)".replace("  ", " "))
    return Item("normalizer", OK, name)


def check_tts(agent: Any) -> Item:
    tts = getattr(getattr(agent, "providers", None), "tts", None)
    name = str(getattr(tts, "name", "") or "?")
    cfg = getattr(getattr(agent, "config", None), "providers", None)
    configured = str(getattr(cfg, "tts", "kokoro") or "kokoro")
    if name == "silence":
        if configured in ("silence", "none", "off"):
            return Item("tts", FAIL, "silence: nothing will be spoken", "set providers.tts = \"kokoro\"")
        fix = FIX_DOWNLOAD if configured == "kokoro" else _key_fix(configured)
        return Item("tts", FAIL, f"nothing will be spoken: {configured} could not start", fix)
    if name == "kokoro":
        paths_ = [getattr(tts, "model_path", None), getattr(tts, "voices_path", None)]
        missing = [str(p) for p in paths_ if p is not None and not Path(p).is_file()]
        if missing:
            return Item("tts", FAIL, f"kokoro model files missing ({', '.join(Path(m).name for m in missing)})", FIX_DOWNLOAD)
        voice = str(getattr(tts, "voice", "") or "")
        return Item("tts", OK, f"kokoro voice {voice}".strip())
    if name in ("openai", "elevenlabs"):
        if cfg is not None and not cfg.key(name):
            return Item("tts", FAIL, f"{name}: no API key", _key_fix(name))
        return Item("tts", OK, name)
    return Item("tts", OK, name)


def check_stt(agent: Any) -> Item:
    stt = getattr(getattr(agent, "providers", None), "stt", None)
    name = str(getattr(stt, "name", "") or "?")
    cfg = getattr(getattr(agent, "config", None), "providers", None)
    configured = str(getattr(cfg, "stt", "faster-whisper") or "faster-whisper")
    if name == "unavailable":
        reason = str(getattr(stt, "reason", "") or "provider could not start")
        fix = FIX_DOWNLOAD if configured in ("faster-whisper", "faster_whisper") else _key_fix(configured)
        return Item("stt", FAIL, f"voice input is unavailable: {reason}", fix)
    if name == "faster-whisper":
        device = str(getattr(stt, "device", "cpu") or "cpu")
        if device == "cpu" and _gpu_without_libs():
            # Recognition is ~40x slower here than it could be: a sentence takes most of a
            # second instead of 20 ms, which is the whole wait before "Heard" appears.
            return Item("stt", WARN, "faster-whisper on the CPU although an NVIDIA GPU is present (~0.8 s per sentence; ~20 ms on the GPU)", FIX_GPU)
        if getattr(stt, "loaded", False):
            return Item("stt", OK, f"faster-whisper loaded on {device}")
        spec = Path(str(getattr(stt, "model_spec", "") or "")).expanduser()
        if spec.is_dir() and (spec / "model.bin").is_file():
            return Item("stt", OK, f"faster-whisper {spec.name} on {device}")
        model = str(getattr(cfg, "stt_model", "") or spec.name or "model")
        return Item("stt", WARN, f"faster-whisper {model} not downloaded yet; the first utterance fetches it", FIX_DOWNLOAD)
    if name in ("openai", "groq"):
        if cfg is not None and not cfg.key(name):
            return Item("stt", FAIL, f"{name}: no API key", _key_fix(name))
        return Item("stt", OK, name)
    return Item("stt", OK, name)


def _gpu_without_libs() -> bool:
    try:
        from zordon.speech.stt.faster_whisper import (  # noqa: PLC0415
            gpu_present,
            nvidia_libs_present,
        )

        return gpu_present() and not nvidia_libs_present()
    except Exception:  # noqa: BLE001
        return False


def check_vad(agent: Any) -> Item:
    vad = getattr(getattr(agent, "providers", None), "vad", None)
    inner = getattr(vad, "vad", None)  # a SpeechGate wraps the VAD
    if inner is not None:
        vad = inner
    kind = type(vad).__name__ if vad is not None else "none"
    if vad is None or kind == "FakeVAD":
        return Item("vad", FAIL, "voice input is off: Silero VAD model missing", FIX_DOWNLOAD)
    if kind == "SileroVAD":
        return Item("vad", OK, f"silero ({Path(str(getattr(vad, 'model_path', 'silero_vad.onnx'))).name})")
    return Item("vad", OK, kind)


def check_router(agent: Any) -> Item:
    router = getattr(getattr(agent, "providers", None), "router", None)
    chain = getattr(router, "chain", None)
    if isinstance(chain, list) and chain:
        names = [str(getattr(r, "name", "?")) for r in chain]
    else:
        names = [str(getattr(router, "name", "?") or "?")]
    if names == ["keyword"]:
        skipped = getattr(router, "skipped", None) or []
        pref = getattr(router, "preference", None)
        why = ("; ".join(str(r) for r in skipped)) if skipped else ""
        detail = "keyword router only: shim commands and yes/no work; everything else is typed to the agent"
        if why:
            detail += f" (configured {pref or 'router'} unavailable: {why})"
        return Item("router", WARN, detail, FIX_ROUTER)
    errors = getattr(router, "last_errors", None) or {}
    if isinstance(errors, dict) and errors:
        name, err = next(iter(errors.items()))
        low = str(err).lower()
        if "401" in low or "authenticat" in low or "unauthorized" in low or "invalid api key" in low:
            return Item(
                "router",
                WARN,
                f"{name} rejected the API key on the last request; routing fell back to keywords. {_short(str(err))}",
                f"check providers.keys.{'typesafe' if name == 'jev' else name} in config.toml (zordon setup re-enters it)",
            )
        return Item("router", WARN, f"{name} failed on the last request; routing fell back to keywords. {_short(str(err))}", FIX_ROUTER)
    return Item("router", OK, " -> ".join(names))


def _short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


THREAD_ATTRS = ("manager", "pipeline", "audio", "dispatcher")
THREAD_LABELS = {"manager": "session manager", "pipeline": "pipeline", "audio": "audio", "dispatcher": "dispatcher"}


def check_threads(agent: Any) -> Item:
    dead: list[str] = []
    seen = 0
    for attr in THREAD_ATTRS:
        t = getattr(agent, attr, None)
        alive = getattr(t, "is_alive", None)
        if not callable(alive):
            continue
        seen += 1
        if not alive():
            dead.append(THREAD_LABELS[attr])
    if not seen:
        return Item("threads", WARN, "no worker threads to check", "")
    if dead:
        return Item("threads", FAIL, f"{', '.join(dead)} thread{'s' if len(dead) > 1 else ''} not running", FIX_RESTART)
    return Item("threads", OK, f"{seen} worker threads running")


def check_account() -> Item:
    """Zordon should run as a normal user: every project acts with this account's power,
    and Claude Code refuses bypass-permissions mode under root (decision 0018)."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return Item(
            "account",
            WARN,
            "running as root; Claude Code refuses bypass mode and every project runs with root's power",
            FIX_ROOT,
        )
    return Item("account", OK, f"running as {_username()}")


def _username() -> str:
    for key in ("USER", "LOGNAME"):
        if os.environ.get(key):
            return os.environ[key]
    try:
        import pwd  # noqa: PLC0415

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return "an unprivileged user"


def check_hooks(which: Which) -> Item:
    found = which("curl")
    if found:
        return Item("hooks", OK, f"curl at {found}", optional=True)
    return Item(
        "hooks",
        WARN,
        "curl not found; Claude Code hook signals are off (prompt detection uses the pane only)",
        FIX_CURL,
        optional=True,
    )


def check_update(agent: Any) -> Item:
    st = getattr(agent, "update_status", None)
    version = str(getattr(agent, "version", "") or "")
    if not isinstance(st, dict):
        return Item("update", OK, f"zordon {version}".strip(), optional=True)
    current = str(st.get("current") or version)
    latest = str(st.get("latest") or "")
    if not st.get("available"):
        return Item("update", OK, f"zordon {current} is current", optional=True)
    if st.get("installed"):
        return Item("update", WARN, f"zordon {latest} installed; this process still runs {current}", "restart zordon serve", optional=True)
    return Item("update", WARN, f"zordon {latest} is available (you have {current})", "zordon update", optional=True)


def check_tunnel(agent: Any) -> Item | None:
    url = getattr(agent, "tunnel_url", None)
    if not url:
        return None
    return Item("tunnel", OK, str(url), optional=True)


# ---- helpers ---------------------------------------------------------------------------------


def _key_fix(provider: str) -> str:
    from zordon.config import ENV_KEYS  # noqa: PLC0415

    env = ENV_KEYS.get(provider, f"{provider.upper()}_API_KEY")
    return f"set providers.keys.{provider} in config.toml or export {env}"


def _short(e: BaseException) -> str:
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:120]


__all__ = [
    "FAIL",
    "OK",
    "OPTIONAL_KEYS",
    "PROBE_TIMEOUT_S",
    "WARN",
    "HealthReport",
    "Item",
    "collect",
    "make_report",
    "overall_status",
]
