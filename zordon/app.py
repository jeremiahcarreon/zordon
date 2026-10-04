"""The Agent: one object that owns the bus, the transcript store, the providers
and the four worker threads, and implements ``AgentAPI`` for the transport.

Construction never fails because a key or a model file is missing: each
provider is built through its factory and degraded on ``ProviderNotConfigured``
(anthropic -> passthrough, kokoro -> silence, a missing Silero model -> voice
input disabled, jev/anthropic -> keyword). Every degradation is logged, kept in
``Agent.warnings`` for the CLI and published as a ``Notice`` on ``start()`` so
the client sees it. ``zordon doctor`` is where the user fixes it.

Spoken glue that no single thread owns lives here as well: permission, plan,
question and trust prompts are spoken when the session manager publishes
``PromptDetected`` (background sessions get a collapsed notice instead), a
session's permission summary is spoken once when it is first focused, and
``Notice(speak=True)`` events from the manager are read out. These run on a
small events thread fed by a tap on ``bus.publish`` so the publishing thread is
never blocked.

``AgentBus.publish`` also redacts ``PromptDetected`` (title, options, raw lines)
and ``Notice`` text before any tap, log line or client sees them: those are
built from raw pane captures and may hold a key that was on screen.
"""

from __future__ import annotations

import logging
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from zordon import __version__, paths
from zordon.agents import ADAPTERS, AgentAdapter, get_adapter
from zordon.bus import (
    Bus,
    Flush,
    LineKind,
    Notice,
    PromptDetected,
    PromptKind,
    SessionState,
    StateChanged,
    TranscriptRow,
    Utterance,
    drain,
)
from zordon.config import VERBOSITY_LEVELS, Config, ConfigError
from zordon.health import HealthReport
from zordon.health import collect as collect_health
from zordon.output.normalizer import PassthroughNormalizer, make_normalizer
from zordon.output.pipeline import PipelineThread
from zordon.output.tts import SilenceTTS, make_tts
from zordon.providers import (
    Normalizer,
    ProviderError,
    ProviderNotConfigured,
    Router,
    STTProvider,
    STTResult,
    TTSProvider,
)
from zordon.routing.dispatcher import DispatcherThread
from zordon.routing.select import make_router
from zordon.session.manager import SessionManager
from zordon.session.prompts import BYPASS_DIALOG_TITLE
from zordon.session.tmux import Tmux
from zordon.speech.audio_thread import AudioThread
from zordon.speech.stt import make_stt
from zordon.speech.vad import FakeVAD, make_vad
from zordon.transcript.redaction import redact
from zordon.transcript.store import TranscriptStore
from zordon.transport.protocol import Sessions, TunnelOut
from zordon.transport.qr import svg_qr
from zordon.transport.server import sanitize_filename
from zordon.transport.ws import settings_out, to_session_summary

log = logging.getLogger("zordon.app")

PROVIDER_KINDS = ("stt", "tts", "normalizer", "router")
UPLOAD_DIRNAME = ".zordon/uploads"
HEALTH_CACHE_S = 5.0  # ``Agent.health()`` re-collects at most this often
HEALTH_INTERVAL_S = 30.0  # the health thread re-collects this often
HEALTH_REPUBLISH_S = 60.0  # ... and publishes at least this often, changed or not
CALL_BUSY_TEXT = "Another device is already in the call. End it there first."
MAX_SPOKEN_OPTIONS = 6

# LineKind used for the transcript row of a spoken prompt, per prompt kind.
PROMPT_LINE_KINDS: dict[PromptKind, LineKind] = {
    PromptKind.PERMISSION: LineKind.PERMISSION_PROMPT,
    PromptKind.TRUST: LineKind.PERMISSION_PROMPT,
    PromptKind.PLAN: LineKind.PLAN,
    PromptKind.QUESTION: LineKind.QUESTION,
}


# ---- bus with a tap ------------------------------------------------------------------


class AgentBus(Bus):
    """A ``Bus`` whose ``publish`` also hands every client event to registered taps.

    Taps run on the publishing thread and must return at once (the agent's tap
    only enqueues). ``client_events`` receives every event as published, except
    that ``PromptDetected`` and ``Notice`` are redacted first (see
    :func:`redact_event`): they carry raw pane text, and the transport sends them
    to the browser verbatim.
    """

    def __init__(self) -> None:
        super().__init__()
        self._taps: list[Callable[[Any], None]] = []

    def tap(self, fn: Callable[[Any], None]) -> None:
        self._taps.append(fn)

    def publish(self, event: Any) -> None:
        event = redact_event(event)
        for fn in self._taps:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - a broken tap must not lose the event
                log.exception("bus tap failed")
        super().publish(event)


def redact_event(event: Any) -> Any:
    """Mask secrets in the events built from raw pane captures. Returns the event
    itself when nothing matched, otherwise a redacted copy."""
    if isinstance(event, PromptDetected):
        title, hit_t = redact(event.title or "")
        options = [redact(o or "")[0] for o in event.options]
        raw_lines = [redact(ln or "")[0] for ln in event.raw_lines]
        if hit_t or options != list(event.options) or raw_lines != list(event.raw_lines):
            return replace(event, title=title, options=options, raw_lines=raw_lines)
        return event
    if isinstance(event, Notice):
        text, hit = redact(event.text or "")
        return replace(event, text=text) if hit else event
    return event


# ---- providers with graceful degradation ---------------------------------------------


class UnavailableSTT:
    """Stands in for a speech-to-text provider that could not be built.

    Every utterance fails with ``ProviderNotConfigured`` carrying the reason, so
    the audio thread publishes a warning the user can see instead of speech
    silently going nowhere.
    """

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        raise ProviderNotConfigured(self.reason)


@dataclass
class Providers:
    normalizer: Normalizer
    tts: TTSProvider
    stt: STTProvider
    vad: Any  # SileroVAD, FakeVAD or a ready SpeechGate
    router: Router
    warnings: list[str] = field(default_factory=list)

    def names(self) -> dict[str, str]:
        return {
            "stt": str(getattr(self.stt, "name", "?")),
            "tts": str(getattr(self.tts, "name", "?")),
            "normalizer": str(getattr(self.normalizer, "name", "?")),
            "router": str(getattr(self.router, "name", "?")),
            "voice": str(getattr(self.tts, "voice", "") or ""),
        }


def build_providers(config: Config, overrides: dict[str, Any] | None = None) -> Providers:
    """Build every provider the config asks for, degrading instead of raising."""
    o = dict(overrides or {})
    warnings: list[str] = []

    normalizer = o.get("normalizer")
    if normalizer is None:
        try:
            normalizer = make_normalizer(config)
        except Exception as e:  # noqa: BLE001 - never crash at startup
            log.exception("normalizer could not be built")
            warnings.append(f"normalizer: {e}; speaking pre-passed text as is")
            normalizer = PassthroughNormalizer()
        if config.providers.normalizer != "passthrough" and normalizer.name == "passthrough":
            warnings.append(
                f"normalizer {config.providers.normalizer!r} is not configured; "
                "using passthrough (output will be terse). Run `zordon doctor`."
            )

    tts = o.get("tts")
    if tts is None:
        try:
            tts = make_tts(config)
        except ProviderError as e:
            warnings.append(f"tts: {e}; no speech will be produced")
            tts = SilenceTTS()
        except Exception as e:  # noqa: BLE001
            log.exception("tts could not be built")
            warnings.append(f"tts: {e}; no speech will be produced")
            tts = SilenceTTS()

    stt = o.get("stt")
    if stt is None:
        try:
            stt = make_stt(config)
        except ProviderError as e:
            warnings.append(f"stt: {e}; voice input is unavailable")
            stt = UnavailableSTT(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("stt could not be built")
            warnings.append(f"stt: {e}; voice input is unavailable")
            stt = UnavailableSTT(str(e))

    vad = o.get("gate", o.get("vad"))
    if vad is None:
        try:
            vad = make_vad()
        except ProviderError as e:
            warnings.append(f"vad: {e}; voice input is disabled until the model is downloaded")
            vad = FakeVAD(lambda _chunk: 0.0)
        except Exception as e:  # noqa: BLE001
            log.exception("vad could not be built")
            warnings.append(f"vad: {e}; voice input is disabled")
            vad = FakeVAD(lambda _chunk: 0.0)

    router = o.get("router")
    if router is None:
        try:
            router = make_router(config)
        except Exception as e:  # noqa: BLE001
            log.exception("router could not be built")
            warnings.append(f"router: {e}; using the keyword router only")
            from zordon.routing.keyword import KeywordRouter  # noqa: PLC0415

            router = KeywordRouter()

    for w in warnings:
        log.warning("%s", w)
    return Providers(normalizer=normalizer, tts=tts, stt=stt, vad=vad, router=router, warnings=warnings)


# ---- spoken forms (pure) -------------------------------------------------------------


DEFAULT_AGENT_NAME = "Claude Code"


def prompt_speech(
    kind: PromptKind | str,
    title: str,
    options: list[str] | None = None,
    *,
    command: str | None = None,
    target_file: str | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    description: str | None = None,
    plan: str | None = None,
) -> str:
    """The short sentence Zordon speaks when a prompt appears on the focused session.

    ``agent_name`` is the adapter's display name ("Claude Code", "Codex", "the agent").
    ``description`` is the agent's own one-line summary of a shell command (Claude
    Code prints it in the permission dialog). It is spoken in place of the command:
    a raw command line is unreadable aloud and, for a long chain, takes half a
    minute. Without one, a short command is read as is and a long one is reduced
    to the programs it calls (``command_gist``).
    """
    k = kind.value if isinstance(kind, PromptKind) else str(kind)
    title = (title or "").strip()
    who = (agent_name or DEFAULT_AGENT_NAME).strip()
    who = who[0].upper() + who[1:] if who else DEFAULT_AGENT_NAME
    if k == PromptKind.TRUST.value:
        if title == BYPASS_DIALOG_TITLE:
            return (
                f"{who} warns that this project runs without permission checks, as you chose when creating it. "
                "Say yes to accept and continue, or no to exit."
            )
        return "This folder is not trusted yet. Say yes to trust it, or no."
    if k == PromptKind.PLAN.value:
        summary = _strip_prefix(title, "Plan ready:").strip() or "a plan"
        if plan:
            gist = plan_gist(plan)
            if gist:
                summary = gist
        return f"{who} has a plan ready: {_sentence(summary)} Approve, revise, or deny?"
    if k == PromptKind.QUESTION.value:
        question = _sentence(title or "a question")
        labels = [o for o in (options or []) if o.strip()][:MAX_SPOKEN_OPTIONS]
        if labels:
            return f"{who} is asking: {question} Options are {_join(labels)}."
        return f"{who} is asking: {question}"
    # permission
    if command or title.startswith("Bash command:"):
        desc = (description or "").strip()
        if desc:
            return f"{who} wants to run a command: {_sentence(desc)} Yes or no?"
        cmd = (command or _strip_prefix(title, "Bash command:")).strip()
        if len(cmd) <= SHORT_COMMAND_CHARS and "\n" not in cmd:
            return f"{who} wants to run a shell command: {_sentence(cmd)} Yes or no?"
        return f"{who} wants to run a shell command that uses {_join(command_gist(cmd), 'and')}. Yes or no?"
    if target_file or title.startswith(("Create file", "Write file", "Write to")):
        name = (target_file or _strip_prefix(_strip_prefix(title, "Create file"), "Write file")).strip()
        return f"{who} wants to create {_sentence(name)} Yes or no?"
    if title.startswith(("Edit file", "Update file", "Modify file")):
        name = title.split(" ", 2)[-1].strip()
        return f"{who} wants to edit {_sentence(name)} Yes or no?"
    return f"{who} is asking for permission: {_sentence(title or 'to continue')} Yes or no?"


SHORT_COMMAND_CHARS = 60  # a command this short is read out; longer ones are summarised
PLAN_GIST_WORDS = 45  # how much of a plan is read with the approval question


def plan_gist(plan: str) -> str:
    """The first sentences of a plan as speech: headings and list markers dropped, cut at
    about ``PLAN_GIST_WORDS`` words on a sentence boundary when possible."""
    words: list[str] = []
    for line in plan.splitlines():
        t = line.strip().lstrip("#*-•").strip()
        t = re.sub(r"^\d+[.)]\s*", "", t)
        if not t or t.lower().startswith(("verification", "```")):
            continue
        words += t.split()
        if len(words) >= PLAN_GIST_WORDS:
            break
    if not words:
        return ""
    text = " ".join(words[: PLAN_GIST_WORDS + 15])
    cut = max(text.rfind(". "), text.rfind("? "), text.rfind("! "))
    if len(words) > PLAN_GIST_WORDS and cut > 40:
        text = text[: cut + 1]
    elif len(words) > PLAN_GIST_WORDS:
        text = " ".join(words[:PLAN_GIST_WORDS]) + "..."
    return text
_GIST_SKIP = frozenset({"sudo", "env", "exec", "time", "nohup", "nice", "command", "builtin", "then", "do", "else", "elif"})
_GIST_MAX = 4


def command_gist(cmd: str) -> list[str]:
    """The distinct programs a shell command line calls, in order, at most ``_GIST_MAX``
    (the last entry becomes "more" when there are others). ``git init && apt-get
    install gh && gh --version`` -> ["git", "apt-get", "gh"]."""
    seen: list[str] = []
    for seg in re.split(r"\|\||&&|[;|\n]|\$\(|`", cmd):
        toks = seg.strip().lstrip("({ ").split()
        while toks and (toks[0] in _GIST_SKIP or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]) or toks[0].startswith("-")):
            toks = toks[1:]
        if not toks:
            continue
        prog = toks[0].rsplit("/", 1)[-1].strip("'\"")
        if not prog or prog.startswith("$") or prog in ("for", "while", "if", "fi", "done", "[", "[[", "!"):
            continue
        if prog not in seen:
            seen.append(prog)
    if len(seen) > _GIST_MAX:
        seen = seen[: _GIST_MAX - 1] + ["more"]
    return seen or ["a shell command"]


def _strip_prefix(text: str, prefix: str) -> str:
    return text[len(prefix) :] if text.startswith(prefix) else text


def _sentence(text: str) -> str:
    """End ``text`` with a terminator so the following sentence is spoken separately."""
    t = text.strip()
    if not t:
        return t
    return t if t[-1] in ".!?" else t + "."


def _join(items: list[str], word: str = "or") -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", {word} {items[-1]}"


# ---- the session surface the agent hands out -----------------------------------------


class _Sessions:
    """Forwards every ``SessionControl`` call to the manager; ``focus`` also tells the
    agent so the permission summary can be spoken the first time a session is focused."""

    def __init__(self, manager: SessionManager, on_focus: Callable[[str], None]) -> None:
        self._manager = manager
        self._on_focus = on_focus

    def focus(self, session_id: str) -> None:
        self._manager.focus(session_id)
        self._on_focus(session_id)

    def open_project(self, project_id: str) -> dict[str, Any]:
        row = self._manager.open_project(project_id)
        sid = row.get("session_id") if isinstance(row, dict) else None
        if sid:
            self._on_focus(sid)
        return row

    def create_project(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        row = self._manager.create_project(*args, **kwargs)
        sid = row.get("session_id") if isinstance(row, dict) else None
        if sid:
            self._on_focus(sid)
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


# ---- the agent ---------------------------------------------------------------------------


class Agent:
    """Implements ``AgentAPI`` (docs/architecture.md). Build it, ``start()`` it, hand it
    to ``transport.server.create_app``."""

    version = __version__

    def __init__(
        self,
        config: Config,
        *,
        tmux: Tmux | None = None,
        providers_override: dict[str, Any] | None = None,
        store: TranscriptStore | None = None,
        claude_home: Path | None = None,
        zordon_home: Path | None = None,
        tmux_session: str | None = None,
        hook_port: int | None = None,
        health_interval: float = HEALTH_INTERVAL_S,
        health_republish: float = HEALTH_REPUBLISH_S,
    ) -> None:
        self.config = config
        self.bus = AgentBus()
        self.store = store or TranscriptStore(paths.db_path())
        self.hook_secret = secrets.token_urlsafe(32)
        self.tunnel_url: str | None = None
        # Set by the update check (zordon.cli.start_update_check): {current, latest, available, installed}.
        self.update_status: dict[str, Any] | None = None
        self.started_at: float | None = None
        self._health_interval = float(health_interval)
        self._health_republish = float(health_republish)
        self._health_lock = threading.Lock()
        self._health_cache: HealthReport | None = None
        self._health_wake = threading.Event()
        self._health_thread = threading.Thread(target=self._health_loop, name="zordon-health", daemon=True)
        self.health_published = 0
        self._muted = threading.Event()
        self._lock = threading.RLock()
        self._summarized: set[str] = set()
        self._events: queue.Queue[Any] = queue.Queue()
        self._events_thread = threading.Thread(target=self._events_loop, name="zordon-agent", daemon=True)
        self._started = False
        self._stopped = False

        self.providers = build_providers(config, providers_override)
        self.warnings: list[str] = list(self.providers.warnings)

        manager_kwargs: dict[str, Any] = {
            "hook_port": hook_port if hook_port is not None else config.server.port,
            "hook_secret": self.hook_secret,
        }
        if claude_home is not None:
            manager_kwargs["claude_home"] = claude_home
        if zordon_home is not None:
            manager_kwargs["zordon_home"] = zordon_home
        if tmux_session is not None:
            manager_kwargs["tmux_session"] = tmux_session
        self.adapters = self._build_adapters(config)
        manager_kwargs["adapters"] = self.adapters
        manager_kwargs["default_agent"] = config.providers.agent
        self.manager = SessionManager(self.bus, config, tmux, **manager_kwargs)
        self.sessions = _Sessions(self.manager, self._on_focus)

        self.pipeline = PipelineThread(
            self.bus,
            config,
            self.providers.normalizer,
            self.providers.tts,
            self.store,
            muted=self._muted,
            focused_fn=self.manager.focused,
        )
        self.audio = AudioThread(
            self.bus,
            config,
            self.providers.vad,
            self.providers.stt,
            self.store,
            session_id_fn=self.manager.focused,
        )
        self.dispatcher = DispatcherThread(
            self.bus,
            config,
            self.providers.router,
            self.sessions,
            self.store,
            self.speak,
            self,
            normalizer=self.providers.normalizer,
        )
        self.bus.tap(self._tap)

    # ---- lifecycle ---------------------------------------------------------------------

    def start(self, *, warm_up: bool = False) -> None:
        """Start every thread in order: sessions, pipeline, audio, dispatcher, events."""
        with self._lock:
            if self._started:
                return
            self._started = True
        if warm_up:
            self._warm_up()
        self.manager.start_thread()
        self.pipeline.start()
        self.audio.start()
        self.dispatcher.start()
        self._events_thread.start()
        self._health_thread.start()
        self.started_at = time.time()
        for w in self.warnings:
            self.bus.publish(Notice(text=w, level="warning"))
        log.info(
            "agent started (stt=%s tts=%s normalizer=%s router=%s)",
            *(self.providers.names()[k] for k in PROVIDER_KINDS),
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop and join every thread; safe to call twice."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self.bus.stop.set()
        deadline = time.monotonic() + timeout
        self.dispatcher.stop()
        self.audio.stop(timeout=_left(deadline))
        self.pipeline.stop()
        if self.pipeline.ident is not None:
            # Joins the speaker thread too: no transcript write may be in flight when
            # the store is closed below.
            self.pipeline.join(_left(deadline))
        if self.dispatcher.is_alive():
            self.dispatcher.join(_left(deadline))
        self.manager.stop(timeout=_left(deadline))
        self._events.put(None)
        self._health_wake.set()
        if self._events_thread.is_alive():
            self._events_thread.join(_left(deadline))
        if self._health_thread.is_alive():
            self._health_thread.join(_left(deadline))
        for prov in (self.providers.normalizer, self.providers.tts):
            close_prov = getattr(prov, "close", None)
            if callable(close_prov):
                try:
                    close_prov()
                except Exception:  # noqa: BLE001
                    log.debug("provider close failed", exc_info=True)
        close = getattr(self.providers.tts, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                log.debug("tts close failed", exc_info=True)
        self.store.close()
        log.info("agent stopped")

    def _warm_up(self) -> None:
        warm = getattr(self.providers.tts, "warm_up", None)
        if not callable(warm):
            return
        t0 = time.monotonic()
        try:
            warm()
            log.info("tts warmed up in %.0f ms", (time.monotonic() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"tts warm-up failed: {e}")
            log.warning("tts warm-up failed: %s", e)

    # ---- agent adapters ----------------------------------------------------------------

    @staticmethod
    def _build_adapters(config: Config) -> dict[str, AgentAdapter]:
        """Every registered adapter that imports; the configured default must be among them."""
        out: dict[str, AgentAdapter] = {}
        for key in ADAPTERS:
            try:
                out[key] = get_adapter(key, config)
            except ImportError as e:
                log.debug("agent adapter %s not present: %s", key, e)
            except Exception as e:  # noqa: BLE001 - one broken adapter must not stop the others
                log.warning("agent adapter %s unavailable: %s", key, e)
        if config.providers.agent not in out:
            raise ConfigError(f"providers.agent={config.providers.agent!r} has no working adapter")
        return out

    def available_agents(self) -> dict[str, str | None]:
        """Adapter key -> binary path (None when not installed; '' for the generic adapter)."""
        out: dict[str, str | None] = {}
        for key, adapter in self.adapters.items():
            try:
                out[key] = adapter.available()
            except Exception:  # noqa: BLE001
                out[key] = None
        return out

    def _agent_name_of(self, session_id: str) -> str:
        s = self.manager.sessions.get(session_id)
        if s is not None:
            return s.adapter.info.display_name
        adapter = self.adapters.get(self.config.providers.agent)
        return adapter.info.display_name if adapter is not None else DEFAULT_AGENT_NAME

    # ---- AgentAPI -----------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        sid = self.manager.focused()
        mode = None
        if sid:
            s = self.manager.sessions.get(sid)
            mode = s.permission_mode if s is not None else None
        return {
            "verbosity": self.config.voice.verbosity,
            "tool_chatter": bool(self.config.voice.tool_chatter),
            "muted": self._muted.is_set(),
            "providers": self.providers.names(),
            "permission_mode": mode,
            "launch_mode": self.config.sessions.permission_mode,
            "tts_sample_rate": self.tts_sample_rate,
        }

    @property
    def tts_sample_rate(self) -> int:
        return int(getattr(self.providers.tts, "sample_rate", 24000))

    @property
    def muted(self) -> bool:
        return self._muted.is_set()

    def set_verbosity(self, level: str) -> None:
        if level not in VERBOSITY_LEVELS:
            raise ValueError(f"verbosity must be one of {', '.join(VERBOSITY_LEVELS)}")
        self.config.voice.verbosity = level
        log.info("verbosity -> %s", level)
        self._publish_settings()

    def set_tool_chatter(self, enabled: bool) -> None:
        self.config.voice.tool_chatter = bool(enabled)
        log.info("tool chatter -> %s", bool(enabled))
        self._publish_settings()

    def set_muted(self, muted: bool) -> None:
        if muted:
            if not self._muted.is_set():
                self._muted.set()
                # Stop what is playing now as well: "mute" means quiet, not "finish this one".
                generation = self.bus.next_generation()
                drain(self.bus.playback)
                self.bus.publish(Flush(generation=generation))
        else:
            self._muted.clear()
        log.info("muted -> %s", bool(muted))
        self._publish_settings()

    def set_provider(self, kind: str, name: str) -> None:
        """Swap one provider at runtime. Raises ``ValueError`` for an unknown kind and
        ``ProviderError`` when the new provider cannot be built (the old one stays)."""
        if kind not in PROVIDER_KINDS:
            raise ValueError(f"kind must be one of {', '.join(PROVIDER_KINDS)}")
        name = (name or "").strip()
        if not name:
            raise ValueError("provider name is empty")
        previous = getattr(self.config.providers, kind)
        setattr(self.config.providers, kind, name)
        try:
            if kind == "stt":
                new = make_stt(self.config)
                self.providers.stt = new
                self.audio.stt = new
            elif kind == "tts":
                new = make_tts(self.config)
                self.providers.tts = new
                self.pipeline.tts = new
            elif kind == "normalizer":
                new = make_normalizer(self.config)
                if name != "passthrough" and new.name == "passthrough":
                    raise ProviderNotConfigured(f"normalizer {name!r} is not configured")
                old_close = getattr(self.providers.normalizer, "close", None)
                self.providers.normalizer = new
                self.pipeline.normalizer = new
                if callable(old_close):
                    old_close()
            else:
                new = make_router(self.config)
                self.providers.router = new
                self.dispatcher.router = new
        except Exception:
            setattr(self.config.providers, kind, previous)
            raise
        log.info("provider %s -> %s", kind, getattr(new, "name", name))
        self._publish_settings()

    def set_voice(self, name: str) -> None:
        """Change the TTS voice at runtime by rebuilding the TTS provider."""
        name = (name or "").strip()
        if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            raise ValueError("voice name is invalid")
        previous = self.config.providers.tts_voice
        self.config.providers.tts_voice = name
        try:
            new = make_tts(self.config)
            self.providers.tts = new
            self.pipeline.tts = new
        except Exception:
            self.config.providers.tts_voice = previous
            raise
        log.info("tts voice -> %s", name)
        self._publish_settings()

    def _publish_sessions(self) -> None:
        """A voice-driven focus change reaches every client's picker, not just the caller."""
        try:
            focused = self.manager.focused()
            rows = [to_session_summary(s, focused) for s in self.manager.list_sessions()]
            self.bus.publish(Sessions(sessions=rows))
        except Exception:  # noqa: BLE001
            log.exception("could not publish sessions")

    def _publish_settings(self) -> None:
        """Every client learns about a change, whichever client or voice command made it."""
        try:
            self.bus.publish(settings_out(self.settings()))
        except Exception:  # noqa: BLE001
            log.exception("could not publish settings")

    def submit_text(self, text: str, client_id: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.bus.utterances.put(Utterance(text=text, source="text", client_id=client_id))

    def call_state(self, client_id: str, action: str) -> None:
        """One client at a time is in the call. A second ``start`` is refused with an
        error notice (the ``ErrorOut`` reaches the client as well), and ``end`` /
        ``pause`` / ``resume`` from a client that is not the caller change nothing."""
        if action == "start":
            if not self.audio.call_started(client_id):
                log.info("call start from %s refused: %s is in the call", client_id, self.audio.client_id)
                self.bus.publish(Notice(text=CALL_BUSY_TEXT, level="error"))
        elif action == "end":
            self.audio.call_ended(client_id)
        elif action == "pause":
            self.audio.call_paused(client_id)
        elif action == "resume":
            self.audio.call_resumed(client_id)
        else:
            raise ValueError(f"unknown call action {action!r}")

    def repeat_last(self) -> None:
        sid = self.manager.focused()
        last = self.store.last_spoken(sid) if sid else None
        self.speak(last or "Nothing to repeat yet.", sid or "", LineKind.PROSE)

    def upload_path(self, filename: str) -> Path:
        safe = sanitize_filename(filename)
        root = self._focused_directory() or paths.zordon_home()
        folder = Path(root) / UPLOAD_DIRNAME
        if folder.is_symlink() or folder.parent.is_symlink():
            raise PermissionError(f"{folder} is a symlink; refusing to upload there")
        paths.ensure_private_dir(folder)
        return folder / safe

    # ---- extras the transport and the CLI use ------------------------------------------

    def transcript_tail(self, session_id: str, n: int = 30) -> list[TranscriptRow]:
        return self.store.tail(session_id, n)

    def speak(
        self, text: str, session_id: str = "", kind: LineKind = LineKind.PROSE, *, bypass_mute: bool = False
    ) -> int:
        """Speak ``text`` ahead of everything else (the pipeline's ``speak_now``)."""
        sid = session_id or self.manager.focused() or ""
        return self.pipeline.speak_now(text, sid, kind, bypass_mute=bypass_mute)

    def set_tunnel_url(self, url: str | None) -> None:
        self.tunnel_url = url
        self.bus.publish(TunnelOut(url=url, qr_svg=svg_qr(url) if url else None))

    def stats(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "started_at": self.started_at,
            "providers": self.providers.names(),
            "warnings": list(self.warnings),
            "sessions": len(self.manager.sessions),
            "focused": self.manager.focused(),
            "pipeline": self.pipeline.stats(),
            "audio": self.audio.stats(),
            "dispatched": self.dispatcher.handled,
            "health": self._health_cache.status if self._health_cache is not None else None,
        }

    # ---- health ----------------------------------------------------------------------------

    def health(self, *, max_age: float = HEALTH_CACHE_S) -> HealthReport:
        """The runtime health report (``zordon.health.collect``), re-collected when the
        cached one is older than ``max_age`` seconds. Safe from any thread."""
        with self._health_lock:
            cached = self._health_cache
            if cached is not None and time.time() - cached.ts < max_age:
                return cached
            report = collect_health(self)
            self._health_cache = report
            return report

    def health_summary_sentence(self) -> str:
        """Spoken by the ``status`` shim command after the session's state; empty when
        every part is working so the dispatcher adds nothing."""
        try:
            return self.health().summary_sentence()
        except Exception:  # noqa: BLE001
            log.exception("health summary failed")
            return ""

    def publish_health(self, report: HealthReport | None = None) -> HealthReport:
        """Send a ``health`` message to every client now."""
        report = report or self.health(max_age=0.0)
        self.bus.publish(report.to_out())
        self.health_published += 1
        return report

    def _health_loop(self) -> None:
        """Re-collect every ``health_interval`` seconds; publish when any item changed and
        at least every ``health_republish`` seconds so a client's strip never goes stale."""
        last_sig: tuple[Any, ...] | None = None
        last_published = 0.0
        while not self.bus.stop.is_set():
            if self._health_wake.wait(self._health_interval):
                return
            try:
                report = self.health(max_age=self._health_interval / 2)
                sig = report.signature()
                now = time.monotonic()
                if sig != last_sig or now - last_published >= self._health_republish:
                    self.publish_health(report)
                    last_sig = sig
                    last_published = now
            except Exception:  # noqa: BLE001
                log.exception("health check failed")

    # ---- spoken glue ---------------------------------------------------------------------

    def _tap(self, event: Any) -> None:
        if isinstance(event, (PromptDetected, StateChanged, Notice)):
            self._events.put(event)

    def _events_loop(self) -> None:
        while True:
            try:
                ev = self._events.get(timeout=0.25)
            except queue.Empty:
                if self.bus.stop.is_set():
                    return
                continue
            if ev is None:
                return
            try:
                self._handle_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("agent event handling failed")

    def _handle_event(self, ev: Any) -> None:
        if isinstance(ev, PromptDetected):
            self._on_prompt(ev)
        elif isinstance(ev, StateChanged):
            self._on_state(ev)
        elif isinstance(ev, Notice) and ev.speak:
            self._on_notice(ev)

    def _on_prompt(self, ev: PromptDetected) -> None:
        focused = self.manager.focused()
        if ev.session_id != focused:
            title = self._title_of(ev.session_id)
            self.bus.publish(
                Notice(
                    text=f"Background session {title} is waiting on a {ev.kind.value} prompt: {ev.title}",
                    level="info",
                    session_id=ev.session_id,
                )
            )
            return
        command = target_file = description = plan = None
        current_match = getattr(self.manager, "current_match", None)
        if callable(current_match):
            m = current_match(ev.session_id)
            if m is not None and getattr(m, "kind", None) == ev.kind:
                command = getattr(m, "command", None)
                target_file = getattr(m, "target_file", None)
                description = getattr(m, "description", None)
                plan = (getattr(m, "extra", None) or {}).get("plan")
        text = prompt_speech(
            ev.kind,
            ev.title,
            ev.options,
            command=command,
            target_file=target_file,
            agent_name=self._agent_name_of(ev.session_id),
            description=description,
            plan=plan,
        )
        self.pipeline.speak_now(text, ev.session_id, PROMPT_LINE_KINDS.get(ev.kind, LineKind.PERMISSION_PROMPT))

    def _on_state(self, ev: StateChanged) -> None:
        if ev.state is SessionState.IDLE and ev.session_id == self.manager.focused():
            self._summarize_once(ev.session_id)

    def _on_notice(self, ev: Notice) -> None:
        text = ev.text
        if ev.session_id and ev.session_id != self.manager.focused():
            text = f"In {self._title_of(ev.session_id)}: {text}"
        kind = LineKind.ERROR if ev.level in ("warning", "error") else LineKind.PROSE
        self.pipeline.speak_now(text, ev.session_id or self.manager.focused() or "", kind)

    def _on_focus(self, session_id: str) -> None:
        self._publish_sessions()
        state = self.manager.state_of(session_id)
        if state in (SessionState.DETACHED, SessionState.WORKING):
            return  # the IDLE transition will do it once the TUI is readable
        self._summarize_once(session_id)

    def _summarize_once(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._summarized:
                return
            self._summarized.add(session_id)
        try:
            summary = str(self.manager.permission_summary(session_id) or "").strip()
        except Exception:  # noqa: BLE001
            log.exception("permission summary failed")
            return
        if not summary:
            return
        # A statement, not a question: the summary already says how to change the mode
        # ("say switch to ... mode to change it"). A question here would invite a yes/no
        # that nothing owns and that the router would type into Claude Code.
        self.pipeline.speak_now(_sentence(summary), session_id, LineKind.SUMMARY)

    # ---- helpers -------------------------------------------------------------------------

    def _focused_directory(self) -> str | None:
        sid = self.manager.focused()
        if not sid:
            return None
        s = self.manager.sessions.get(sid)
        return s.cwd if s is not None else None

    def _title_of(self, session_id: str) -> str:
        s = self.manager.sessions.get(session_id)
        if s is not None:
            return s.title or Path(s.cwd).name or session_id[:8]
        return session_id[:8]


def _left(deadline: float) -> float:
    return max(0.05, deadline - time.monotonic())


__all__ = [
    "Agent",
    "AgentBus",
    "CALL_BUSY_TEXT",
    "Providers",
    "UnavailableSTT",
    "build_providers",
    "prompt_speech",
    "redact_event",
]
