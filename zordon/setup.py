"""Guided first-run setup: ``zordon setup``, and ``zordon serve`` when no config exists.

Four questions, each with the trade-offs spelled out, then the wizard does the
work (writes the config, downloads models, pulls the Ollama model, fetches
cloudflared) and hands over to ``zordon serve``. Everything it chooses is an
ordinary config.toml value, so the file stays the source of truth and the
wizard can be re-run or skipped (``--yes`` takes the detected defaults).

Design rule kept: the wizard never widens Claude Code's permissions and never
writes a bypass mode anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from zordon import assets, paths
from zordon.config import Config

Ask = Callable[[str], str]


# ---- detection -----------------------------------------------------------------------


@dataclass(slots=True)
class Detected:
    tmux: str | None = None
    claude: str | None = None
    curl: str | None = None
    ollama_binary: str | None = None
    ollama_server: bool = False
    ollama_models: list[str] = field(default_factory=list)
    gpu: str | None = None
    anthropic_key_env: bool = False
    typesafe_key_env: bool = False
    openai_key_env: bool = False
    elevenlabs_key_env: bool = False
    cloudflared: str | None = None
    tailscale: str | None = None
    models_present: list[str] = field(default_factory=list)
    python: str = f"{sys.version_info.major}.{sys.version_info.minor}"


def detect(ollama_url: str = "http://127.0.0.1:11434") -> Detected:
    d = Detected()
    d.tmux = shutil.which("tmux")
    d.claude = shutil.which("claude")
    d.curl = shutil.which("curl")
    d.ollama_binary = shutil.which("ollama")
    d.cloudflared = assets.find_binary("cloudflared")
    d.tailscale = shutil.which("tailscale")
    d.anthropic_key_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    d.typesafe_key_env = bool(os.environ.get("TYPESAFE_API_KEY"))
    d.openai_key_env = bool(os.environ.get("OPENAI_API_KEY"))
    d.elevenlabs_key_env = bool(os.environ.get("ELEVENLABS_API_KEY"))
    try:
        from zordon.output.normalizer.ollama import server_models  # noqa: PLC0415

        d.ollama_models = server_models(ollama_url, timeout=0.8)
        d.ollama_server = True
    except Exception:  # noqa: BLE001 - any failure means "no server"
        d.ollama_server = False
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                d.gpu = out.stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            pass
    for a in (assets.SILERO_VAD, assets.KOKORO_MODEL, assets.KOKORO_VOICES):
        if assets.is_present(a):
            d.models_present.append(a.filename)
    if (paths.models_dir() / assets.WHISPER_DIRNAME / "model.bin").exists():
        d.models_present.append(assets.WHISPER_DIRNAME)
    return d


# ---- choices -------------------------------------------------------------------------


@dataclass(slots=True)
class Choices:
    speech: str = "local"  # local | cloud | later
    normalizer: str = "ollama"  # ollama | anthropic | claude-cli | passthrough
    router: str = "keyword"  # keyword | jev | anthropic
    access: str = "local"  # local | tunnel | tailscale | lan
    keys: dict[str, str] = field(default_factory=dict)
    install_ollama: bool = False
    pull_ollama_model: bool = False
    download_models: bool = True
    download_cloudflared: bool = False
    ollama_model: str = "qwen2.5:3b-instruct"


def recommend(d: Detected) -> Choices:
    """Defaults from what the machine has. No network, no prompts."""
    c = Choices()
    c.speech = "local"
    if d.anthropic_key_env:
        c.normalizer = "anthropic"
    elif d.ollama_server or d.ollama_binary:
        c.normalizer = "ollama"
        c.pull_ollama_model = not any(m.startswith(c.ollama_model) for m in d.ollama_models)
    elif d.claude:
        c.normalizer = "claude-cli"
    else:
        c.normalizer = "passthrough"
    if d.typesafe_key_env:
        c.router = "jev"
    elif d.anthropic_key_env:
        c.router = "anthropic"
    c.download_models = len(d.models_present) < 4
    return c


def apply(c: Choices, cfg: Config) -> Config:
    p = cfg.providers
    if c.speech == "local":
        p.stt, p.tts = "faster-whisper", "kokoro"
    elif c.speech == "cloud":
        p.stt = "groq" if c.keys.get("groq") else "openai"
        p.tts = "elevenlabs" if c.keys.get("elevenlabs") else "openai"
    p.normalizer = c.normalizer
    p.ollama_model = c.ollama_model
    p.router = c.router
    for k, v in c.keys.items():
        if v:
            p.keys[k] = v
    if c.access == "lan":
        cfg.server.bind = "0.0.0.0"
    else:
        cfg.server.bind = "127.0.0.1"
    cfg.tunnel.provider = "cloudflared"
    cfg.validate()
    return cfg


# ---- the interview ----------------------------------------------------------------------

SPEECH_TEXT = """
1. Speech: how do you want to listen and talk?

  [1] Local (recommended)   Kokoro voice + faster-whisper, both on this machine.
                            One-time download of about 820 MB. Private: audio never
                            leaves the box. CPU is fine (about 1 s per utterance to
                            transcribe); a GPU makes transcription ~15x faster.
  [2] Cloud                 OpenAI (voice + transcription), ElevenLabs (voice), Groq
                            (fast transcription). Lowest latency, best voices, pay per
                            use, your audio goes to those services. Needs API keys.
  [3] Decide later          Start without speech; text in the browser still works.
"""

NORMALIZER_TEXT = """
2. Spoken English: Claude Code's output is terse. Who rewrites it for speech?

  [1] Local model (Ollama)  Free, private, ~200 ms per sentence, speaks as Claude
                            types. Needs Ollama (ollama.com) and a 2 GB model.
                            Small models sometimes pad; Zordon guards against it.
  [2] Anthropic API key     Best quality, ~200 ms per sentence, speaks as Claude
                            types. About $0.001 per sentence (a few cents an hour).
  [3] Your Claude login     No key, no extra install: uses Claude Code itself,
                            headless. Slow (5-15 s per response), so Zordon waits
                            for a whole response before speaking it.
  [4] None                  Speak the cleaned-up text as is. Readable, robotic.
"""

ROUTER_TEXT = """
3. Routing: who decides whether you are talking to Claude Code, asking about the
   transcript ("what did it just change?"), or controlling Zordon ("mute")?

  [1] Built-in rules (recommended to start)  Free, instant. Commands and yes/no by
                            phrase table; everything else goes to Claude Code. Safe
                            default: a misheard question reaches Claude Code at worst.
  [2] TypeSafe Jev          Calibrated classifier, sub-second, about $0.00003 per
                            utterance. Needs a TypeSafe key.
  [3] Anthropic API key     Haiku classifies with a confidence; ~1 s. Needs the key.
"""

ACCESS_TEXT = """
4. Reach: where will you open Zordon?

  [1] This machine only     http://127.0.0.1:8765. Nothing exposed.
  [2] Phone anywhere        Public tunnel (cloudflared, downloaded now). Random
                            https URL each run, shown as a QR code. The token is
                            required, failed logins are rate-limited, idle
                            connections drop after 30 minutes.
  [3] Tailscale             Your tailnet only. `zordon serve --bind tailscale`.
  [4] Same Wi-Fi            Bind to all interfaces. Browsers need https for the
                            microphone, so phones will not get a mic this way.
"""


def _pick(ask: Ask, out: TextIO, text: str, default: int, n: int) -> int:
    out.write(text)
    while True:
        raw = ask(f"  Choice [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= n:
            return int(raw)
        out.write(f"  Please answer 1-{n}.\n")


def _yes(ask: Ask, prompt: str, default: bool = True) -> bool:
    raw = ask(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _key(ask: Ask, out: TextIO, name: str, env: str, present: bool) -> str:
    if present:
        out.write(f"  {env} is already set in your environment; keeping it there.\n")
        return ""
    val = ask(f"  Paste your {name} key (stored 0600 in config.toml; blank to add later): ").strip()
    return val


def interview(d: Detected, ask: Ask, out: TextIO, defaults: Choices | None = None) -> Choices:
    c = defaults or recommend(d)
    out.write("\nZordon setup. Four questions; Enter takes the default in brackets.\n")
    out.write(_summary(d))

    speech = _pick(ask, out, SPEECH_TEXT, 1 if c.speech == "local" else (2 if c.speech == "cloud" else 3), 3)
    c.speech = {1: "local", 2: "cloud", 3: "later"}[speech]
    if c.speech == "cloud":
        c.keys["openai"] = _key(ask, out, "OpenAI", "OPENAI_API_KEY", d.openai_key_env)
        if _yes(ask, "Use ElevenLabs for the voice instead of OpenAI?", default=False):
            c.keys["elevenlabs"] = _key(ask, out, "ElevenLabs", "ELEVENLABS_API_KEY", d.elevenlabs_key_env)
        if _yes(ask, "Use Groq for transcription (faster than OpenAI)?", default=False):
            c.keys["groq"] = _key(ask, out, "Groq", "GROQ_API_KEY", False)
    c.download_models = c.speech == "local" and len(d.models_present) < 4

    norm_default = {"ollama": 1, "anthropic": 2, "claude-cli": 3, "passthrough": 4}[c.normalizer]
    norm = _pick(ask, out, NORMALIZER_TEXT, norm_default, 4)
    c.normalizer = {1: "ollama", 2: "anthropic", 3: "claude-cli", 4: "passthrough"}[norm]
    if c.normalizer == "ollama":
        if not d.ollama_binary and not d.ollama_server:
            out.write("  Ollama is not installed.\n")
            c.install_ollama = _yes(ask, "Run the official installer now (curl -fsSL https://ollama.com/install.sh | sh)?", default=False)
            if not c.install_ollama:
                out.write("  Install it later from https://ollama.com, then run `zordon setup` again.\n")
        has = any(m.startswith(c.ollama_model) for m in d.ollama_models)
        c.pull_ollama_model = not has
        if d.gpu and _yes(ask, f"GPU detected ({d.gpu}). Use the larger qwen2.5:14b-instruct (9 GB, better wording)?", default=False):
            c.ollama_model = "qwen2.5:14b-instruct"
            c.pull_ollama_model = not any(m.startswith(c.ollama_model) for m in d.ollama_models)
    elif c.normalizer == "anthropic":
        c.keys["anthropic"] = _key(ask, out, "Anthropic", "ANTHROPIC_API_KEY", d.anthropic_key_env)
    elif c.normalizer == "claude-cli" and not d.claude:
        out.write("  `claude` is not on PATH; install Claude Code and log in first.\n")

    router_default = {"keyword": 1, "jev": 2, "anthropic": 3}[c.router]
    r = _pick(ask, out, ROUTER_TEXT, router_default, 3)
    c.router = {1: "keyword", 2: "jev", 3: "anthropic"}[r]
    if c.router == "jev":
        c.keys["typesafe"] = _key(ask, out, "TypeSafe", "TYPESAFE_API_KEY", d.typesafe_key_env)
    if c.router == "anthropic" and "anthropic" not in c.keys:
        c.keys["anthropic"] = _key(ask, out, "Anthropic", "ANTHROPIC_API_KEY", d.anthropic_key_env)

    a = _pick(ask, out, ACCESS_TEXT, 1, 4)
    c.access = {1: "local", 2: "tunnel", 3: "tailscale", 4: "lan"}[a]
    c.download_cloudflared = c.access == "tunnel" and not d.cloudflared
    return c


def _summary(d: Detected) -> str:
    rows = [
        ("tmux", d.tmux or "MISSING (install tmux)"),
        ("Claude Code", d.claude or "MISSING (install and log in)"),
        ("Ollama", "server running" if d.ollama_server else (d.ollama_binary or "not installed")),
        ("GPU", d.gpu or "none detected"),
        ("Models", ", ".join(d.models_present) or "none downloaded yet"),
    ]
    return "\nFound on this machine:\n" + "".join(f"  {k:<12} {v}\n" for k, v in rows)


# ---- actions ---------------------------------------------------------------------------


def run_actions(c: Choices, cfg: Config, out: TextIO, *, runner: Callable[..., Any] = subprocess.run) -> list[str]:
    """Do the downloads and pulls the choices imply. Returns human-readable problems."""
    problems: list[str] = []
    if c.install_ollama:
        out.write("\nInstalling Ollama (official installer)...\n")
        try:
            res = runner(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], check=False)
            if getattr(res, "returncode", 1) != 0:
                problems.append("Ollama installer exited with an error; install from https://ollama.com")
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"could not run the Ollama installer: {e}")
    if c.normalizer == "ollama" and c.pull_ollama_model:
        binary = shutil.which("ollama")
        if binary:
            out.write(f"\nPulling {c.ollama_model} with Ollama (one time)...\n")
            try:
                res = runner([binary, "pull", c.ollama_model], check=False)
                if getattr(res, "returncode", 1) != 0:
                    problems.append(f"`ollama pull {c.ollama_model}` failed; run it by hand")
            except (OSError, subprocess.SubprocessError) as e:
                problems.append(f"could not run ollama pull: {e}")
        else:
            problems.append(f"Ollama is not installed; later: `ollama pull {c.ollama_model}`")
    if c.download_models and c.speech == "local":
        out.write("\nDownloading the local speech models (about 820 MB)...\n")
        try:
            from zordon.doctor import DoctorOptions, model_checks  # noqa: PLC0415

            for chk in model_checks(cfg, DoctorOptions(download=True)):
                if chk.level not in ("OK", "SKIP"):
                    problems.append(f"{chk.name}: {chk.detail}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"model download failed: {e}")
    if c.download_cloudflared:
        out.write("\nDownloading cloudflared...\n")
        try:
            from zordon.doctor import download_cloudflared  # noqa: PLC0415

            download_cloudflared()
        except Exception as e:  # noqa: BLE001
            problems.append(f"cloudflared download failed: {e}")
    return problems


def next_steps(c: Choices, cfg: Config) -> str:
    cmd = "zordon serve"
    if c.access == "tunnel":
        cmd += " --tunnel"
    elif c.access == "tailscale":
        cmd += " --bind tailscale"
    lines = [
        "",
        f"Config written to {cfg.path} (mode 0600).",
        f"Your session token is: {cfg.server.token}",
        "Type it into the browser once; `zordon token show` prints it again.",
        "",
        f"Start with:  {cmd}",
        "Then open the page, pick or start a session, tap Talk.",
        "Re-run this any time with `zordon setup`; `zordon doctor` checks everything.",
    ]
    return "\n".join(lines) + "\n"


def run(
    config_path: Path | None = None,
    *,
    ask: Ask = input,
    out: TextIO | None = None,
    assume_yes: bool = False,
    do_actions: bool = True,
) -> tuple[Config, Choices, list[str]]:
    out = out if out is not None else sys.stdout
    cfg, _created = Config.load_or_create(config_path)
    inner = ask

    def ask(prompt: str) -> str:  # noqa: F811 - EOF (piped stdin ran dry) means "take the default"
        try:
            return inner(prompt)
        except EOFError:
            out.write("\n")
            return ""

    d = detect(cfg.providers.ollama_url)
    if assume_yes:
        c = recommend(d)
        c.download_cloudflared = False
    else:
        c = interview(d, ask, out)
    apply(c, cfg)
    cfg.save()
    problems = run_actions(c, cfg, out) if do_actions else []
    out.write(next_steps(c, cfg))
    if problems:
        out.write("\nStill to do:\n" + "".join(f"  - {p}\n" for p in problems))
    return cfg, c, problems
