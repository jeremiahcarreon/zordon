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

from zordon import assets, manifest, paths, prereqs
from zordon.config import Config

Ask = Callable[[str], str]


class SetupAborted(Exception):
    """The user chose not to continue (e.g. no coding agent installed)."""


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
    agents: dict[str, str | None] = field(default_factory=dict)  # adapter key -> binary path
    python: str = f"{sys.version_info.major}.{sys.version_info.minor}"


def detect(ollama_url: str = "http://127.0.0.1:11434") -> Detected:
    d = Detected()
    d.tmux = shutil.which("tmux")
    d.claude = shutil.which("claude")
    try:
        from zordon.agents import available_agents  # noqa: PLC0415

        d.agents = available_agents()
    except Exception:  # noqa: BLE001
        d.agents = {"claude-code": d.claude, "generic": ""}
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
    install_gpu_libs: bool = False  # the CUDA libraries for recognition on an NVIDIA GPU (the gpu extra)
    ollama_model: str = "qwen2.5:3b-instruct"
    agent: str = "claude-code"


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
    installed = [k for k, v in d.agents.items() if v and k != "generic"]
    if "claude-code" in installed:
        c.agent = "claude-code"
    elif installed:
        c.agent = installed[0]
    else:
        c.agent = "generic"
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
    if hasattr(p, "agent"):
        p.agent = c.agent
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


AGENT_INTRO = """
0. Coding agent: which terminal agent will Zordon talk to?
"""

AGENT_LINES = {
    "claude-code": "Claude Code       Full support: prompts, plan approval, sessions, clean transcript.",
    "codex": "Codex (OpenAI)    Sessions and approval prompts from its source; live captures still wanted.",
    "generic": "Any tmux pane     Attach to a pane you already run (aider, gemini, q, ...). Prompts\n"
    "                            found by common cues only; prose read from the screen.",
}

INSTALL_LINES = {
    "claude-code": "npm install -g @anthropic-ai/claude-code   then run `claude` once to log in",
    "codex": "npm install -g @openai/codex               then run `codex` once to sign in",
}


def _ask_agent(d: Detected, ask: Ask, out: TextIO, c: Choices) -> None:
    keys = [k for k in ("claude-code", "codex", "generic") if k in d.agents or k == "generic"]
    installed = {k for k, v in d.agents.items() if v}
    out.write(AGENT_INTRO + "\n")
    for i, k in enumerate(keys, 1):
        state = "installed" if k in installed else ("" if k == "generic" else "NOT INSTALLED")
        out.write(f"  [{i}] {AGENT_LINES.get(k, k):<75} {state}\n")
    if not (installed - {"generic"}):
        out.write(
            "\n  No coding agent found on this machine. Pick the one you want; the prerequisites step\n"
            "  offers to install it (or shows the command if you would rather do it yourself).\n"
        )
    default = keys.index(c.agent) + 1 if c.agent in keys else 1
    pick = _pick(ask, out, "", default, len(keys))
    c.agent = keys[pick - 1]
    if c.agent != "generic" and c.agent not in installed:
        out.write(f"  {c.agent} is not installed yet; the prerequisites step will offer to install it.\n")
    if c.agent == "generic":
        out.write("  After `zordon serve`, use Attach in the web page with the pane target shown by `tmux list-panes -a`.\n")


def interview(d: Detected, ask: Ask, out: TextIO, defaults: Choices | None = None) -> Choices:
    c = defaults or recommend(d)
    out.write("\nZordon setup. A few questions; Enter takes the default in brackets.\n")
    out.write(_summary(d))
    _ask_agent(d, ask, out, c)

    speech = _pick(ask, out, SPEECH_TEXT, 1 if c.speech == "local" else (2 if c.speech == "cloud" else 3), 3)
    c.speech = {1: "local", 2: "cloud", 3: "later"}[speech]
    if c.speech == "cloud":
        c.keys["openai"] = _key(ask, out, "OpenAI", "OPENAI_API_KEY", d.openai_key_env)
        if _yes(ask, "Use ElevenLabs for the voice instead of OpenAI?", default=False):
            c.keys["elevenlabs"] = _key(ask, out, "ElevenLabs", "ELEVENLABS_API_KEY", d.elevenlabs_key_env)
        if _yes(ask, "Use Groq for transcription (faster than OpenAI)?", default=False):
            c.keys["groq"] = _key(ask, out, "Groq", "GROQ_API_KEY", False)
    c.download_models = c.speech == "local" and len(d.models_present) < 4
    if c.speech == "local" and d.gpu and not gpu_libs_present():
        c.install_gpu_libs = _yes(
            ask,
            f"GPU detected ({d.gpu}). Use it for speech recognition? Downloads about 900 MB of CUDA libraries; recognition then takes 20 ms instead of most of a second",
            default=True,
        )

    norm_default = {"ollama": 1, "anthropic": 2, "claude-cli": 3, "passthrough": 4}[c.normalizer]
    norm = _pick(ask, out, NORMALIZER_TEXT, norm_default, 4)
    c.normalizer = {1: "ollama", 2: "anthropic", 3: "claude-cli", 4: "passthrough"}[norm]
    if c.normalizer == "ollama":
        if not d.ollama_binary and not d.ollama_server:
            out.write("  Ollama is not installed; the prerequisites step will offer to install it.\n")
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


PREREQ_TEXT = """
5. Prerequisites: Zordon assumes nothing about this machine. Missing pieces for the
   choices above are installed in dependency order with as few commands as possible,
   so sudo asks for your password once.
"""


def prerequisites(
    c: Choices,
    ask: Ask,
    out: TextIO,
    *,
    env: prereqs.Environment | None = None,
    runner: Callable[..., Any] = subprocess.run,
    assume_yes: bool = False,
) -> list[str]:
    """Offer to install everything the chosen path needs, as one batch. Returns what is still missing."""
    want_agents = (c.agent,) if c.agent != "generic" else ()
    env = env or prereqs.detect(want_agents=want_agents, want_ollama=(c.normalizer == "ollama"))
    missing = env.missing(required_only=True)
    if not missing:
        return []
    out.write(PREREQ_TEXT)
    if env.package_manager:
        out.write(f"  Package manager: {env.package_manager}\n")
    else:
        out.write("  No known package manager found (apt, dnf, pacman, zypper, apk, brew); commands are shown for you to adapt.\n")
    for p in missing:
        out.write(f"\n  {p.label}: {p.why}.\n")
        if p.detail:
            out.write(f"    {p.detail}\n")
    steps = prereqs.plan_steps(env, missing)
    unplanned = [p for p in missing if not any(p.key in st.keys for st in steps)]
    still: list[str] = [f"{p.label}: install it by hand ({p.detail or 'no command known for this system'})" for p in unplanned]
    if not steps:
        return still
    out.write("\n  Plan:\n")
    for i, st in enumerate(steps, 1):
        out.write(f"    {i}. {st.command}\n")
    if assume_yes or not _yes(ask, f"Install all {len(steps)} step{'s' if len(steps) != 1 else ''} now?", default=True):
        for st in steps:
            for k in st.keys:
                p = env.get(k)
                if p is not None:
                    still.append(f"{p.label}: {st.command}" + (f"; then {p.after}" if p.after else ""))
        return still
    results = prereqs.run_steps(steps, env, run=runner, log=lambda line: out.write(f"\n  {line}\n"))
    for k, (ok, msg) in results.items():
        p = env.get(k)
        out.write(f"    {'✓' if ok else '✗'} {msg}\n")
        if ok and p is not None:
            try:
                manifest.record("system", p.key, command=p.command or "", note=p.label)
            except OSError:
                pass
            if p.after:
                out.write(f"      Next: {p.after}\n")
        elif p is not None:
            still.append(f"{p.label}: {p.command}" + (f"; then {p.after}" if p.after else ""))
    for p in missing:
        if p.present and prereqs.login_command(p.key) and _yes(ask, f"Open {p.label} now to sign in (it closes by itself once you are signed in)?", default=True):
            if runner is not subprocess.run:
                prereqs.open_for_login(p.key, run=runner)  # tests: a fake runner, no process to watch
            elif not prereqs.login_and_wait(p.key):
                still.append(f"{p.label}: not signed in yet; run `{prereqs.login_command(p.key)[0]}` once in a terminal")
    return still


def _summary(d: Detected) -> str:
    rows = [
        ("tmux", d.tmux or "MISSING (install tmux)"),
        ("Claude Code", d.claude or "not installed"),
        ("Codex", d.agents.get("codex") or "not installed"),
        ("Ollama", "server running" if d.ollama_server else (d.ollama_binary or "not installed")),
        ("GPU", d.gpu or "none detected"),
        ("Models", ", ".join(d.models_present) or "none downloaded yet"),
    ]
    return "\nFound on this machine:\n" + "".join(f"  {k:<12} {v}\n" for k, v in rows)


# ---- actions ---------------------------------------------------------------------------


GPU_PACKAGES = ("nvidia-cublas-cu12>=12.1", "nvidia-cudnn-cu12>=9")


def gpu_libs_present() -> bool:
    try:
        from zordon.speech.stt.faster_whisper import nvidia_libs_present  # noqa: PLC0415

        return nvidia_libs_present()
    except Exception:  # noqa: BLE001
        return False


def install_gpu_libs(*, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """Install the ``gpu`` extra's packages into this interpreter's environment.
    Returns a problem string, or None when done. Uses ``uv pip`` when uv is around
    (the installer's environments have no pip), else ``python -m pip``."""
    uv = shutil.which("uv")
    if uv:
        argv = [uv, "pip", "install", "--python", sys.executable, *GPU_PACKAGES]
    else:
        argv = [sys.executable, "-m", "pip", "install", "--quiet", *GPU_PACKAGES]
    try:
        res = runner(argv, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not install the CUDA libraries: {e}"
    if getattr(res, "returncode", 1) != 0:
        return "the CUDA libraries did not install; later: " + " ".join(argv)
    return None


def run_actions(
    c: Choices,
    cfg: Config,
    out: TextIO,
    *,
    runner: Callable[..., Any] = subprocess.run,
    downloader: Callable[..., Path] | None = None,
) -> list[str]:
    """Do the downloads and pulls the choices imply. Returns human-readable problems.

    ``downloader`` replaces :func:`zordon.assets.download` for the model and cloudflared
    fetches (the TUI passes one that reports progress); ``None`` uses the default.
    """
    problems: list[str] = []
    if c.normalizer == "ollama" and c.pull_ollama_model:
        binary = shutil.which("ollama")
        if binary:
            if not ensure_ollama_server(cfg.providers.ollama_url, binary, out):
                problems.append("Ollama is installed but its server is not running; start it with `ollama serve`, then `ollama pull " + c.ollama_model + "`")
                return problems
            out.write(f"\nPulling {c.ollama_model} with Ollama (one time)...\n")
            try:
                res = runner([binary, "pull", c.ollama_model], check=False)
                if getattr(res, "returncode", 1) != 0:
                    problems.append(f"`ollama pull {c.ollama_model}` failed; run it by hand")
                else:
                    manifest.record("ollama-model", c.ollama_model, command=f"ollama pull {c.ollama_model}")
            except (OSError, subprocess.SubprocessError) as e:
                problems.append(f"could not run ollama pull: {e}")
        else:
            problems.append(f"Ollama is not installed; later: `ollama pull {c.ollama_model}`")
    if c.download_models and c.speech == "local":
        out.write("\nDownloading the local speech models (about 820 MB)...\n")
        try:
            from zordon.doctor import DoctorOptions, model_checks  # noqa: PLC0415

            for chk in model_checks(cfg, DoctorOptions(download=True), downloader):
                if chk.status not in ("OK", "SKIP"):
                    problems.append(f"{chk.name}: {chk.detail}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"model download failed: {e}")
    if c.install_gpu_libs:
        out.write("\nInstalling the CUDA libraries for GPU speech recognition (about 900 MB)...\n")
        err = install_gpu_libs(runner=runner)
        if err:
            problems.append(err)
        else:
            manifest.record("python", "zordon[gpu]", command="uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
    if c.download_cloudflared:
        out.write("\nDownloading cloudflared...\n")
        try:
            from zordon.doctor import download_cloudflared  # noqa: PLC0415

            download_cloudflared(downloader)
        except Exception as e:  # noqa: BLE001
            problems.append(f"cloudflared download failed: {e}")
    return problems


def ensure_ollama_server(url: str, binary: str, out: TextIO, *, wait_s: float = 15.0) -> bool:
    """Ollama normally runs as a service; where it does not (containers, no systemd) start it
    detached and wait until it answers. Returns whether the server is reachable."""
    import time  # noqa: PLC0415

    from zordon.output.normalizer.ollama import server_models  # noqa: PLC0415

    def up() -> bool:
        try:
            server_models(url, timeout=1.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    if up():
        return True
    out.write("\nStarting the Ollama server (it was not running)...\n")
    try:
        log_path = paths.zordon_home() / "ollama-serve.log"
        paths.ensure_private_dir(log_path.parent)
        with open(log_path, "ab") as log:
            subprocess.Popen([binary, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)  # noqa: S603
    except OSError as e:
        out.write(f"  could not start it: {e}\n")
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if up():
            out.write("  Ollama server is up.\n")
            return True
        time.sleep(0.5)
    return False


def next_steps(c: Choices, cfg: Config) -> str:
    cmd = "zordon start"
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
    hint = path_hint()
    if hint:
        lines += ["", hint]
    return "\n".join(lines) + "\n"


def path_hint() -> str:
    """When the installer had to add zordon's directory to PATH itself, the user's own shell
    does not have it yet. ZORDON_PATH_HINT carries the directory from install.sh."""
    d = os.environ.get("ZORDON_PATH_HINT", "").strip()
    if not d:
        return ""
    env_file = Path(d) / "env"
    if env_file.exists():
        return f"This shell cannot see `zordon` yet. Run:  source {env_file}   (or open a new terminal)."
    return f'This shell cannot see `zordon` yet. Run:  export PATH="{d}:$PATH"   (or open a new terminal).'


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
    if os.environ.get("ZORDON_INSTALLED_UV"):
        # install.sh put uv on this machine for us; uninstall may offer to take it away.
        try:
            manifest.record("uv", "uv", command="install.sh", removal=os.environ.get("UV_INSTALL_DIR", "") or str(Path.home() / ".local" / "share" / "uv"))
        except OSError:
            pass
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
    problems: list[str] = []
    if do_actions:
        problems += prerequisites(c, ask, out, assume_yes=assume_yes)
        d = detect(cfg.providers.ollama_url)  # re-detect: installs above may have changed the picture
        if c.normalizer == "ollama":
            c.pull_ollama_model = not any(m.startswith(c.ollama_model) for m in d.ollama_models)
        problems += run_actions(c, cfg, out)
    out.write(next_steps(c, cfg))
    if problems:
        out.write("\nStill to do:\n" + "".join(f"  - {p}\n" for p in problems))
    return cfg, c, problems
