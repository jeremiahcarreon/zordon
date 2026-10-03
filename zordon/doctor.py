"""``zordon doctor``: dependency and provider checks, model and binary download.

Every check is a small function returning a :class:`Check` with ``OK``, ``WARN``,
``FAIL`` or ``SKIP`` and a one-line fix. Nothing here makes a paid API call
unless ``--probe`` is passed; the default run only looks at files, binaries and
importability. ``--download`` fetches the local models (Silero VAD, Kokoro,
faster-whisper) and, when ``--tunnel`` is given, the cloudflared binary.

The functions take their collaborators (``which``, ``run``, ``find_spec``) as
arguments so tests can run the doctor against fake binaries without touching
the machine.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zordon import __version__, assets, paths
from zordon.config import ENV_KEYS, LOOPBACK, Config, ConfigError

log = logging.getLogger("zordon.doctor")

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
MIN_PYTHON = (3, 12)
MIN_TMUX = (3, 2)
ESPEAK_PATH_LIMIT = 160
ESPEAK_DATA_DIRNAME = "espeak-ng-data"
CLAUDE_TIMEOUT = 15.0
TMUX_TIMEOUT = 5.0

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_MISSING = 3

# Which optional distribution each provider needs, and the extra that installs it.
OPTIONAL_MODULES: dict[str, tuple[str, str]] = {
    "faster_whisper": ("faster-whisper", "local"),
    "kokoro_onnx": ("kokoro-onnx", "local"),
    "typesafe_sdk": ("typesafe-sdk", "jev"),
    "onnxruntime": ("onnxruntime", ""),
    "anthropic": ("anthropic", ""),
}
# What each extra pulls in, for the pipx hint.
EXTRA_PACKAGES: dict[str, tuple[str, ...]] = {
    "local": ("faster-whisper", "kokoro-onnx"),
    "jev": ("typesafe-sdk",),
}
KOKORO_MAX_PYTHON = (3, 14)  # kokoro-onnx declares <3.14


def installed_with_pipx(prefix: str | None = None, environ: dict[str, str] | None = None) -> bool:
    """True when the running interpreter lives in a pipx-managed venv."""
    prefix = prefix if prefix is not None else sys.prefix
    env = os.environ if environ is None else environ
    parts = Path(prefix).parts
    if "pipx" in parts and "venvs" in parts:
        return True
    home = env.get("PIPX_HOME")
    return bool(home) and Path(prefix).is_relative_to(Path(home))


def install_hint(extra: str, package: str = "", *, pipx: bool | None = None) -> str:
    """The command that installs ``extra`` (or ``package``) into the interpreter that
    runs zordon: ``pipx inject`` for a pipx install, otherwise ``pip install``."""
    pipx = installed_with_pipx() if pipx is None else pipx
    if extra == "local":
        # The local speech stack is part of the core install; a broken one means a broken install.
        return "pipx reinstall zordon" if pipx else "pip install --upgrade --force-reinstall zordon"
    if extra:
        if pipx:
            return "pipx inject zordon " + " ".join(EXTRA_PACKAGES.get(extra, (package,)))
        return f'pip install "zordon[{extra}]"'
    return f"pipx inject zordon {package}" if pipx else f"pip install {package}"

Which = Callable[[str], str | None]
Run = Callable[..., subprocess.CompletedProcess[str]]
FindSpec = Callable[[str], Any]


@dataclass(slots=True)
class Check:
    name: str
    status: str  # OK | WARN | FAIL | SKIP
    detail: str = ""
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass
class DoctorOptions:
    download: bool = False
    probe: bool = False
    tunnel: bool = False
    verify_hashes: bool = False
    config_path: Path | None = None


@dataclass
class Report:
    version: str
    config_path: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "config_path": self.config_path,
            "ok": self.ok,
            "checks": [asdict(c) for c in self.checks],
        }


# ---- individual checks -------------------------------------------------------------------


def check_python() -> Check:
    v = sys.version_info
    have = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        return Check("python", OK, have)
    return Check("python", FAIL, have, f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")


def _prereq_fix(key: str, fallback: str) -> str:
    """The exact install command for this machine, or a generic hint."""
    try:
        from zordon import prereqs  # noqa: PLC0415

        p = prereqs.detect(want_agents=("claude-code", "codex"), want_ollama=True).get(key)
        if p is not None and p.command:
            return f"{p.command}  (or run `zordon setup`)"
    except Exception:  # noqa: BLE001
        pass
    return fallback + " (or run `zordon setup`)"


def check_tmux(which: Which = shutil.which, run: Run = subprocess.run) -> Check:
    binary = which("tmux")
    if not binary:
        return Check("tmux", FAIL, "not found on PATH", _prereq_fix("tmux", "install tmux 3.2 or newer"))
    out = _run_text(run, [binary, "-V"], TMUX_TIMEOUT)
    if out is None:
        return Check("tmux", FAIL, f"{binary} did not answer -V", "reinstall tmux")
    version = parse_version(out)
    if version is None:
        return Check("tmux", WARN, out.strip() or "unknown version", "could not parse the version; 3.2 or newer is needed")
    if version >= MIN_TMUX:
        return Check("tmux", OK, out.strip())
    return Check("tmux", FAIL, out.strip(), f"upgrade tmux to {MIN_TMUX[0]}.{MIN_TMUX[1]} or newer")


def check_claude(which: Which = shutil.which, run: Run = subprocess.run) -> Check:
    binary = which("claude")
    if not binary:
        return Check("claude", FAIL, "not found on PATH", _prereq_fix("claude-code", "install Claude Code and log in"))
    out = _run_text(run, [binary, "--version"], CLAUDE_TIMEOUT)
    if out is None:
        return Check("claude", WARN, f"{binary} did not answer --version", "run `claude --version` by hand")
    line = out.strip().splitlines()[0] if out.strip() else binary
    return compare_prompts_version(line)


def check_claude_login() -> Check | None:
    """Credentials present for Claude Code (``.credentials.json`` or an API key in the env)."""
    try:
        from zordon.agents.claude_code import ClaudeCodeAdapter  # noqa: PLC0415

        state = ClaudeCodeAdapter(None).logged_in()
    except Exception:  # noqa: BLE001
        return None
    if state is None:
        return None
    if state:
        return Check("claude login", OK, "credentials found")
    return Check("claude login", FAIL, "Claude Code is installed but not logged in", "run `claude` once in a terminal and sign in, then exit it")


def compare_prompts_version(version_line: str, prompts_version: str | None = None) -> Check:
    """OK when the installed Claude Code matches the release the prompt regexes were
    captured from (``session/prompts.PROMPTS_VERSION``), WARN otherwise: most releases
    do not change the prompt text, but a mismatch is the first thing to suspect when
    prompts go undetected."""
    if prompts_version is None:
        from zordon.session.prompts import PROMPTS_VERSION  # noqa: PLC0415

        prompts_version = PROMPTS_VERSION
    installed = parse_version(version_line)
    expected = parse_version(prompts_version)
    detail = f"{version_line} (prompts verified against {prompts_version})"
    if installed is None or expected is None or installed == expected:
        return Check("claude", OK, detail)
    return Check(
        "claude",
        WARN,
        detail,
        "prompt detection was verified against a different release; see docs/prompts-version.md if prompts go undetected",
    )


def check_ollama(cfg: Config) -> Check | None:
    """Local Ollama: the zero-key per-sentence normalizer (and opt-in router). Only
    reported when the config can use it (normalizer auto/ollama or router ollama)."""
    p = cfg.providers
    wants = p.normalizer in ("auto", "ollama") or p.router == "ollama"
    if not wants:
        return None
    if p.normalizer == "auto" and p.key("anthropic"):
        return None  # the API normalizer wins; Ollama is irrelevant
    from zordon.output.normalizer.ollama import has_model, server_models  # noqa: PLC0415
    from zordon.providers import ProviderNotConfigured  # noqa: PLC0415

    required = p.normalizer == "ollama" or p.router == "ollama"
    level = FAIL if required else WARN
    try:
        models = server_models(p.ollama_url)
    except ProviderNotConfigured:
        return Check(
            "ollama",
            level,
            f"no server at {p.ollama_url}; the normalizer falls back to headless Claude Code (slower, per turn)",
            "install Ollama (https://ollama.com), run `ollama serve`, then `ollama pull " + p.ollama_model + "`",
        )
    if not has_model(models, p.ollama_model):
        return Check("ollama", level, f"server up, model {p.ollama_model!r} not pulled", f"ollama pull {p.ollama_model}")
    return Check("ollama", OK, f"{p.ollama_model} at {p.ollama_url}")


def check_curl(which: Which = shutil.which) -> Check:
    """The Notification/Stop hook handlers Zordon writes into a session's settings are
    ``curl`` command lines (decision 0009); without curl the second prompt signal is
    silently absent."""
    found = which("curl")
    if found:
        return Check("curl", OK, found)
    return Check(
        "curl",
        WARN,
        "not found on PATH; Claude Code hook signals are disabled (prompt detection falls back to the pane regexes only)",
        _prereq_fix("curl", "install curl"),
    )


CUDA_MODULES = ("nvidia.cublas", "nvidia.cudnn")


def check_cuda(cfg: Config, find_spec: FindSpec = importlib.util.find_spec) -> Check | None:
    """Only when ``stt_device = "cuda"``: the pip-installed CUDA runtime libraries that
    faster-whisper (CTranslate2) loads must be importable packages."""
    if cfg.providers.stt_device != "cuda":
        return None
    missing = []
    for mod in CUDA_MODULES:
        try:
            if find_spec(mod) is None:
                missing.append(mod)
        except (ImportError, ValueError):
            missing.append(mod)
    if missing:
        return Check(
            "cuda libraries",
            FAIL,
            "missing " + ", ".join(missing),
            "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 (or set stt_device = \"cpu\")",
        )
    return Check("cuda libraries", OK, "cublas and cudnn packages present")


def check_claude_home() -> Check:
    home = paths.claude_home()
    if home.is_dir():
        projects = (home / "projects").is_dir()
        detail = str(home) + ("" if projects else " (no projects yet)")
        return Check("claude home", OK, detail)
    return Check(
        "claude home", WARN, f"{home} does not exist", "run `claude` once in a project so the session store exists"
    )


def check_config_file(cfg_path: Path) -> Check:
    if not cfg_path.exists():
        return Check("config", WARN, f"{cfg_path} not written yet", "`zordon serve` writes it on first run")
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    if mode & 0o077:
        return Check("config", WARN, f"{cfg_path} mode {mode:o}", f"chmod 600 {cfg_path}")
    return Check("config", OK, f"{cfg_path} (0600)")


def check_token(cfg: Config) -> Check:
    if cfg.server.token:
        return Check("token", OK, "set")
    if cfg.server.bind not in LOOPBACK:
        return Check("token", FAIL, f"empty while bind={cfg.server.bind}", "set server.token or `zordon token rotate`")
    return Check("token", WARN, "empty (loopback only)", "`zordon token rotate` generates one")


def required_keys(cfg: Config) -> dict[str, str]:
    """Key name -> why the config needs it."""
    p = cfg.providers
    needs: dict[str, str] = {}
    if p.normalizer == "anthropic":
        needs["anthropic"] = "normalizer"
    elif p.normalizer == "auto" and not p.key("anthropic"):
        # Not required: without a key the normalizer runs headless Claude Code per turn.
        pass
    if p.router == "anthropic":
        needs["anthropic"] = (needs.get("anthropic", "") + " router").strip()
    if p.router == "jev":
        needs["typesafe"] = "router"
        needs.setdefault("anthropic", "router fallback")
    if p.stt == "openai" or p.tts == "openai":
        needs["openai"] = " ".join(k for k, v in (("stt", p.stt == "openai"), ("tts", p.tts == "openai")) if v)
    if p.stt == "groq":
        needs["groq"] = "stt"
    if p.tts == "elevenlabs":
        needs["elevenlabs"] = "tts"
    return needs


def check_keys(cfg: Config) -> list[Check]:
    out: list[Check] = []
    for name, why in required_keys(cfg).items():
        env = ENV_KEYS.get(name, "")
        present = bool(cfg.providers.key(name))
        label = f"key {name}"
        if present:
            out.append(Check(label, OK, f"set (used by {why})"))
        elif "fallback" in why:
            out.append(Check(label, WARN, f"not set ({why})", f"optional: set providers.keys.{name} or {env}"))
        else:
            out.append(
                Check(
                    label,
                    WARN,
                    f"not set (needed by {why}); Zordon degrades to a local fallback",
                    f"set providers.keys.{name} in config.toml or export {env}",
                )
            )
    return out


def required_modules(cfg: Config) -> list[str]:
    p = cfg.providers
    mods = ["onnxruntime"]
    if p.stt == "faster-whisper":
        mods.append("faster_whisper")
    if p.tts == "kokoro":
        mods.append("kokoro_onnx")
    if p.router == "jev":
        mods.append("typesafe_sdk")
    if p.normalizer in ("anthropic", "auto") or p.router in ("anthropic", "jev"):
        mods.append("anthropic")
    return mods


def check_modules(cfg: Config, find_spec: FindSpec = importlib.util.find_spec) -> list[Check]:
    out: list[Check] = []
    for mod in required_modules(cfg):
        dist, extra = OPTIONAL_MODULES.get(mod, (mod, ""))
        fix = install_hint(extra, dist)
        if mod == "kokoro_onnx" and sys.version_info[:2] >= KOKORO_MAX_PYTHON:
            fix = (
                "kokoro-onnx needs Python 3.12 or 3.13: install zordon with one of those, "
                'or pick another tts provider ([providers] tts = "openai" or "elevenlabs")'
            )
        try:
            found = find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            out.append(Check(f"module {dist}", OK, "importable"))
        else:
            status = WARN if mod == "typesafe_sdk" else FAIL
            out.append(Check(f"module {dist}", status, "not installed", fix))
    return out


def model_checks(cfg: Config, opts: DoctorOptions, downloader: Callable[..., Path] | None = None) -> list[Check]:
    """Silero VAD always; Kokoro when tts=kokoro; faster-whisper when stt=faster-whisper."""
    from zordon.speech.vad import models_dir_override  # noqa: PLC0415

    downloader = downloader or assets.download
    out: list[Check] = []
    models = models_dir_override()
    wanted: list[assets.Asset] = [assets.SILERO_VAD]
    if cfg.providers.tts == "kokoro":
        wanted += [assets.KOKORO_MODEL, assets.KOKORO_VOICES]
    for asset in wanted:
        out.append(_asset_check(asset, models / asset.filename, opts, downloader))
    if cfg.providers.stt == "faster-whisper":
        out.append(_whisper_check(cfg, models, opts))
    return out


def _asset_check(
    asset: assets.Asset, path: Path, opts: DoctorOptions, downloader: Callable[..., Path]
) -> Check:
    name = f"model {asset.filename}"
    present = _present(asset, path, opts.verify_hashes)
    if not present and opts.download:
        try:
            print(f"downloading {asset.filename} ({_mb(asset.size)})...", file=sys.stderr)
            downloader(asset)
            path = assets.path_for(asset)
            present = _present(asset, path, opts.verify_hashes)
        except Exception as e:  # noqa: BLE001 - network errors are many
            return Check(name, FAIL, f"download failed: {e}", "check the network and retry `zordon doctor --download`")
    if present:
        size = path.stat().st_size if path.exists() else 0
        hashed = " (sha256 verified)" if opts.verify_hashes and asset.sha256 else ""
        return Check(name, OK, f"{path} {_mb(size)}{hashed}")
    if path.exists():
        return Check(name, FAIL, f"{path} has the wrong size or hash", "`zordon doctor --download` re-downloads it")
    return Check(name, FAIL, f"missing from {path.parent}", "`zordon doctor --download`")


def _present(asset: assets.Asset, path: Path, verify: bool) -> bool:
    if path == assets.path_for(asset):
        return assets.is_present(asset, verify_hash=verify)
    if not path.is_file():
        return False
    if asset.size is not None and path.stat().st_size != asset.size:
        return False
    if verify and asset.sha256 and assets.sha256_of(path) != asset.sha256:
        return False
    return True


def _whisper_check(cfg: Config, models: Path, opts: DoctorOptions) -> Check:
    from zordon.speech.stt.faster_whisper import model_dir_for  # noqa: PLC0415

    model = cfg.providers.stt_model or "small.en"
    folder = model_dir_for(model, models)
    name = f"model {folder.name}"
    if (folder / "model.bin").is_file():
        size = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
        return Check(name, OK, f"{folder} {_mb(size)}")
    if opts.download:
        try:
            from zordon.speech.stt.faster_whisper import ensure_model  # noqa: PLC0415

            print(f"downloading faster-whisper {model} into {folder}...", file=sys.stderr)
            ensure_model(models, model)
        except Exception as e:  # noqa: BLE001
            return Check(name, FAIL, f"download failed: {e}", "check the network and retry `zordon doctor --download`")
        if (folder / "model.bin").is_file():
            return Check(name, OK, str(folder))
    return Check(name, FAIL, f"missing from {folder}", "`zordon doctor --download`")


def check_espeak(cfg: Config, find_spec: FindSpec = importlib.util.find_spec) -> Check:
    if cfg.providers.tts != "kokoro":
        return Check("espeak-ng data", SKIP, "only needed by kokoro")
    try:
        if find_spec("espeakng_loader") is None:
            return Check("espeak-ng data", WARN, "espeakng_loader not installed", install_hint("local"))
        import espeakng_loader  # noqa: PLC0415

        data = str(espeakng_loader.get_data_path())
    except Exception as e:  # noqa: BLE001
        return Check("espeak-ng data", WARN, f"could not locate: {e}", "reinstall with " + install_hint("local"))
    length = len(data.encode())
    if length < ESPEAK_PATH_LIMIT:
        return Check("espeak-ng data", OK, f"path is {length} characters")
    copy = paths.zordon_home() / ESPEAK_DATA_DIRNAME
    if (copy / "phontab").exists():
        return Check("espeak-ng data", OK, f"bundled path is {length} characters; short copy at {copy} in use")
    return Check(
        "espeak-ng data",
        WARN,
        f"bundled path is {length} characters (limit {ESPEAK_PATH_LIMIT}); Kokoro copies it to {copy} on first use",
        "nothing to do unless ZORDON_HOME is also long; then set a shorter ZORDON_HOME",
    )


def check_tunnel_binary(cfg: Config, opts: DoctorOptions, downloader: Callable[..., Path] | None = None) -> Check:
    downloader = downloader or assets.download
    provider = cfg.tunnel.provider
    found = assets.find_binary(provider)
    if found:
        return Check(provider, OK, found)
    if provider == "ngrok":
        status = FAIL if opts.tunnel else WARN
        return Check("ngrok", status, "not found", "install ngrok (https://ngrok.com/download) or set [tunnel] provider = \"cloudflared\"")
    if opts.download and opts.tunnel:
        try:
            found = download_cloudflared(downloader)
        except Exception as e:  # noqa: BLE001
            return Check("cloudflared", FAIL, f"download failed: {e}", "retry, or install cloudflared yourself")
        return Check("cloudflared", OK, found)
    status = FAIL if opts.tunnel else WARN
    return Check(
        "cloudflared",
        status,
        "not found",
        "`zordon serve --tunnel` downloads it on first use; `zordon doctor --download --tunnel` fetches it now",
    )


def download_cloudflared(downloader: Callable[..., Path] | None = None) -> str:
    """Fetch cloudflared into ``paths.bin_dir()`` and return the executable's path.

    Used by ``zordon doctor --download --tunnel`` and by ``zordon serve --tunnel`` on
    first use. Raises ``OSError`` (or the downloader's error) when it cannot.
    """
    downloader = downloader or assets.download
    print("downloading cloudflared...", file=sys.stderr)
    dest = downloader(assets.CLOUDFLARED)
    dest = extract_cloudflared(dest)
    found = assets.find_binary("cloudflared")
    if not found:
        raise OSError(f"downloaded {dest} but cloudflared is still not runnable")
    return found


def extract_cloudflared(downloaded: Path) -> Path:
    """macOS releases are a .tgz; unpack the binary next to it."""
    if downloaded.suffix != ".tgz":
        return downloaded
    dest = downloaded.with_suffix("")
    with tarfile.open(downloaded) as tar:
        member = next((m for m in tar.getmembers() if Path(m.name).name == "cloudflared"), None)
        if member is None:
            raise OSError("archive has no cloudflared binary")
        with tar.extractfile(member) as src, open(dest, "wb") as out:  # type: ignore[union-attr]
            shutil.copyfileobj(src, out)
    os.chmod(dest, 0o755)
    downloaded.unlink(missing_ok=True)
    return dest


def check_port(cfg: Config) -> Check:
    bind, port = cfg.server.bind, cfg.server.port
    host = "127.0.0.1" if bind in ("localhost", "tailscale") else bind
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as e:
        return Check("port", FAIL, f"{host}:{port} is not free ({e.strerror or e})", "stop the other process or change server.port")
    return Check("port", OK, f"{host}:{port} is free")


def probe_checks(cfg: Config) -> list[Check]:
    """``--probe`` only: one cheap authenticated call per configured API."""
    out: list[Check] = []
    key = cfg.providers.key("anthropic")
    if cfg.providers.normalizer == "anthropic" or cfg.providers.router in ("anthropic", "jev"):
        if not key:
            out.append(Check("probe anthropic", SKIP, "no key"))
        else:
            try:
                import anthropic  # noqa: PLC0415

                client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=10.0)
                model = client.models.retrieve(cfg.providers.normalizer_model)
                out.append(Check("probe anthropic", OK, f"{getattr(model, 'id', cfg.providers.normalizer_model)} reachable"))
            except Exception as e:  # noqa: BLE001
                out.append(Check("probe anthropic", FAIL, _short_error(e), "check the key and the network"))
    if cfg.providers.router == "jev":
        key = cfg.providers.key("typesafe")
        if not key:
            out.append(Check("probe typesafe", SKIP, "no key"))
        else:
            try:
                import typesafe_sdk  # noqa: PLC0415

                client = typesafe_sdk.TypeSafe(api_key=key)
                models = client.models.list()
                count = len(getattr(models, "data", None) or list(models) or [])
                out.append(Check("probe typesafe", OK, f"{count} models listed"))
            except Exception as e:  # noqa: BLE001
                out.append(Check("probe typesafe", FAIL, _short_error(e), "check the key and the network"))
    return out


# ---- the run -------------------------------------------------------------------------------


def run_checks(
    cfg: Config,
    opts: DoctorOptions | None = None,
    *,
    which: Which = shutil.which,
    run: Run = subprocess.run,
    find_spec: FindSpec = importlib.util.find_spec,
    downloader: Callable[..., Path] | None = None,
) -> Report:
    opts = opts or DoctorOptions()
    downloader = downloader or assets.download
    cfg_path = opts.config_path or cfg.path or paths.config_path()
    report = Report(version=__version__, config_path=str(cfg_path))
    checks = report.checks
    checks.append(check_python())
    checks.append(check_tmux(which, run))
    checks.append(check_claude(which, run))
    login = check_claude_login()
    if login is not None:
        checks.append(login)
    checks.append(check_curl(which))
    checks.append(check_claude_home())
    checks.append(check_config_file(Path(cfg_path)))
    checks.append(check_token(cfg))
    checks.append(Check("providers", OK, ", ".join(f"{k}={getattr(cfg.providers, k)}" for k in ("stt", "tts", "normalizer", "router"))))
    checks.extend(check_keys(cfg))
    checks.extend(check_modules(cfg, find_spec))
    ollama = check_ollama(cfg)
    if ollama is not None:
        checks.append(ollama)
    cuda = check_cuda(cfg, find_spec)
    if cuda is not None:
        checks.append(cuda)
    checks.extend(model_checks(cfg, opts, downloader))
    checks.append(check_espeak(cfg, find_spec))
    checks.append(check_tunnel_binary(cfg, opts, downloader))
    checks.append(check_port(cfg))
    if opts.probe:
        checks.extend(probe_checks(cfg))
    return report


def format_table(report: Report) -> str:
    width = max((len(c.name) for c in report.checks), default=10)
    lines = [f"zordon {report.version} doctor  (config: {report.config_path})", ""]
    for c in report.checks:
        line = f"  {c.status:<4} {c.name:<{width}}  {c.detail}"
        if c.fix and c.status in (WARN, FAIL):
            line += f"\n       {'':<{width}}  fix: {c.fix}"
        lines.append(line)
    lines.append("")
    n = len(report.failures)
    if n:
        lines.append(f"{n} problem{'s' if n != 1 else ''} to fix.")
    else:
        lines.append("Everything Zordon needs is in place.")
    return "\n".join(lines)


def load_config(path: Path | None) -> Config:
    """The config for the doctor: the file when it exists, defaults otherwise."""
    cfg_path = path or paths.config_path()
    if cfg_path.exists():
        return Config.load(cfg_path)
    cfg = Config()
    cfg.path = cfg_path
    return cfg


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="zordon doctor", description="Check every dependency and provider.")
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run(args)


def add_arguments(parser: Any) -> None:
    parser.add_argument("--download", action="store_true", help="fetch missing models (and cloudflared with --tunnel)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--probe", action="store_true", help="make one small authenticated call per configured API (may cost money)")
    parser.add_argument("--tunnel", action="store_true", help="also require the tunnel binary (for `zordon serve --tunnel`)")
    parser.add_argument("--verify", action="store_true", help="verify model file hashes, not just sizes")
    parser.add_argument("--config", type=Path, default=None, help="config file (default: $ZORDON_HOME/config.toml)")


def run(args: Any) -> int:
    opts = DoctorOptions(
        download=bool(args.download),
        probe=bool(args.probe),
        tunnel=bool(args.tunnel),
        verify_hashes=bool(getattr(args, "verify", False)),
        config_path=args.config,
    )
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError, ValueError) as e:
        if args.json:
            print(json.dumps({"ok": False, "error": f"config: {e}"}))
        else:
            print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    report = run_checks(cfg, opts)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_table(report))
    return EXIT_OK if report.ok else EXIT_MISSING


# ---- helpers -------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _run_text(run: Run, argv: list[str], timeout: float) -> str | None:
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    return out if proc.returncode == 0 or out.strip() else None


def _mb(size: int | None) -> str:
    if not size:
        return "unknown size"
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    return f"{size / 1024:.0f} KB"


def _short_error(e: Exception) -> str:
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:160]


__all__ = [
    "Check",
    "DoctorOptions",
    "Report",
    "add_arguments",
    "format_table",
    "load_config",
    "main",
    "run",
    "run_checks",
]
