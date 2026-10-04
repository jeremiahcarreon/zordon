"""Configuration: ``~/.zordon/config.toml``.

The file is the single source of configuration. Environment variables are a
fallback for API keys only (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
``ELEVENLABS_API_KEY``, ``GROQ_API_KEY``, ``TYPESAFE_API_KEY``) so a key never has
to be written to disk if the user prefers not to.

Safety rules enforced here, not left to callers:

* ``bind`` other than loopback requires a token.
* The token is generated on first run and the file is written ``0600``.
* ``bypassPermissions`` is not a value Zordon will ever write anywhere; it is not
  a config option at all.
"""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomli_w

from zordon import paths

Verbosity = Literal["minimal", "normal", "technical"]
VERBOSITY_LEVELS: tuple[Verbosity, ...] = ("minimal", "normal", "technical")

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "groq": "GROQ_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
}

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ConfigError(ValueError):
    pass


@dataclass
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    # WebSocket idle disconnect. Enforced as a hard requirement under --tunnel.
    idle_disconnect_minutes: int = 30
    # Failed token attempts per IP per minute before 429.
    auth_rate_limit_per_minute: int = 5


@dataclass
class ProvidersConfig:
    # The coding agent Zordon drives by default (``zordon.agents.ADAPTERS``):
    # claude-code | codex | generic. A session may pick another at start/attach.
    agent: str = "claude-code"
    stt: str = "faster-whisper"  # faster-whisper | openai | groq
    tts: str = "kokoro"  # kokoro | elevenlabs | openai
    normalizer: str = "auto"  # auto | anthropic | ollama | claude-cli | passthrough
    router: str = "jev"  # jev | anthropic | ollama | keyword
    keys: dict[str, str] = field(default_factory=lambda: {k: "" for k in ENV_KEYS})

    # Per-provider knobs. All have working defaults.
    stt_model: str = "small.en"
    stt_device: str = "cpu"  # cpu | cuda
    tts_voice: str = "af_heart"
    tts_speed: float = 1.15  # a little faster than Kokoro's default reads as natural speech
    normalizer_model: str = "claude-haiku-4-5"
    router_model: str = "claude-haiku-4-5"
    normalizer_timeout_seconds: float = 1.5
    # Headless Claude Code normalizer (no API key): model alias and per-turn timeout.
    claude_cli_model: str = "claude-haiku-4-5"
    # Local Ollama server (no key): normalizer and router fallback.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b-instruct"
    claude_cli_timeout_seconds: float = 30.0

    def key(self, name: str) -> str:
        """Config value first, environment second, empty string if neither."""
        v = (self.keys or {}).get(name, "") or ""
        if v:
            return v
        env = ENV_KEYS.get(name)
        return os.environ.get(env, "") if env else ""


@dataclass
class VoiceConfig:
    verbosity: str = "minimal"
    tool_chatter: bool = False
    idle_watchdog_seconds: int = 20
    router_confidence: float = 0.85
    yes_no_confidence: float = 0.95
    # VAD / barge-in
    speech_onset_frames: int = 3  # 3 x 20 ms = 60 ms
    speech_end_ms: int = 700
    echo_guard_ms: int = 120
    # Deferred submit (decision 0019): speech is typed into the agent's input box as it
    # is transcribed and sent only after this much quiet with nothing more said, or on
    # "go ahead". 0 sends every utterance at once (the old behaviour).
    submit_quiet_ms: int = 2500
    # Plain spoken-style prose (no code, paths, identifiers or symbols) is spoken as written
    # instead of waiting for the rewriter (decision 0019). true sends everything through it.
    normalize_conversational: bool = False
    # How many normalized sentences to buffer before playback starts.
    prebuffer_sentences: int = 3


@dataclass
class TunnelConfig:
    provider: str = "cloudflared"  # cloudflared | ngrok


@dataclass
class UpdateConfig:
    check: bool = True  # look for a newer Zordon when serve starts (cached, at most every 6 h)
    auto: bool = True  # install it when found; a restart picks it up
    channel: str = "main"  # git branch or tag the install tracks


# Permission modes a new session may be launched in. Claude Code's names; the
# adapter maps them for other agents. ``bypassPermissions`` is deliberately absent:
# Zordon never launches an agent with permission checks switched off (decision 0007).
LAUNCH_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "dontAsk")


@dataclass
class SessionsConfig:
    # The mode new sessions start in when the client does not pick one. "auto" lets
    # Claude Code's auto mode handle routine permission prompts itself, so the voice
    # loop is not interrupted for every command. Changeable per session from the
    # web client (New session / Switch mode) or by voice ("switch to plan mode").
    permission_mode: str = "default"


@dataclass
class OutputConfig:
    # Where prose comes from. "auto" tails the Claude Code session jsonl when it
    # can be found and falls back to pane capture otherwise.
    source: str = "auto"  # auto | jsonl | pane
    poll_interval_ms: int = 100


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    path: Path | None = field(default=None, compare=False, repr=False)

    # ---- construction -------------------------------------------------------

    @classmethod
    def default(cls) -> Config:
        cfg = cls()
        cfg.server.token = generate_token()
        return cfg

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> Config:
        def section(name: str, typ: type) -> Any:
            raw = dict(data.get(name) or {})
            if name == "providers":
                keys = dict(raw.pop("keys", {}) or {})
                obj = _build(typ, raw)
                merged = {k: "" for k in ENV_KEYS}
                merged.update({str(k): str(v) for k, v in keys.items()})
                obj.keys = merged
                return obj
            return _build(typ, raw)

        cfg = cls(
            server=section("server", ServerConfig),
            providers=section("providers", ProvidersConfig),
            voice=section("voice", VoiceConfig),
            tunnel=section("tunnel", TunnelConfig),
            output=section("output", OutputConfig),
            update=section("update", UpdateConfig),
            sessions=section("sessions", SessionsConfig),
            path=path,
        )
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or paths.config_path()
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return cls.from_dict(data, path)

    @classmethod
    def load_or_create(cls, path: Path | None = None) -> tuple[Config, bool]:
        """Return (config, created). Creates a default file on first run."""
        path = path or paths.config_path()
        if path.exists():
            return cls.load(path), False
        cfg = cls.default()
        cfg.path = path
        cfg.save(path)
        return cfg, True

    # ---- persistence --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = {
            "server": asdict(self.server),
            "providers": asdict(self.providers),
            "voice": asdict(self.voice),
            "tunnel": asdict(self.tunnel),
            "output": asdict(self.output),
            "update": asdict(self.update),
            "sessions": asdict(self.sessions),
        }
        return d

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or paths.config_path()
        paths.ensure_private_dir(path.parent)
        tmp = path.with_suffix(".toml.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(CONFIG_HEADER.encode())
            tomli_w.dump(self.to_dict(), fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        self.path = path
        return path

    # ---- validation ---------------------------------------------------------

    def validate(self) -> None:
        if self.voice.verbosity not in VERBOSITY_LEVELS:
            raise ConfigError(f"voice.verbosity must be one of {VERBOSITY_LEVELS}")
        if not (0.0 <= self.voice.router_confidence <= 1.0):
            raise ConfigError("voice.router_confidence must be between 0 and 1")
        if not (0.0 <= self.voice.yes_no_confidence <= 1.0):
            raise ConfigError("voice.yes_no_confidence must be between 0 and 1")
        if not (1 <= self.server.port <= 65535):
            raise ConfigError("server.port out of range")
        if self.server.bind not in LOOPBACK and not self.server.token:
            raise ConfigError(
                f"server.bind={self.server.bind!r} is not loopback; a server.token is required"
            )
        if self.output.source not in ("auto", "jsonl", "pane"):
            raise ConfigError("output.source must be auto, jsonl or pane")
        if self.tunnel.provider not in ("cloudflared", "ngrok"):
            raise ConfigError("tunnel.provider must be cloudflared or ngrok")
        if self.sessions.permission_mode not in LAUNCH_MODES:
            hint = " (Zordon never launches with permissions bypassed)" if "bypass" in self.sessions.permission_mode.lower() else ""
            raise ConfigError(f"sessions.permission_mode must be one of {LAUNCH_MODES}{hint}")
        from zordon.agents import ADAPTERS  # noqa: PLC0415 - avoid an import cycle at module load

        if self.providers.agent not in ADAPTERS:
            raise ConfigError(f"providers.agent must be one of {tuple(ADAPTERS)}")

    def is_loopback(self) -> bool:
        return self.server.bind in LOOPBACK


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def _build(typ: type, raw: dict[str, Any]) -> Any:
    allowed = {f for f in typ.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in [{typ.__name__}]: {sorted(unknown)}")
    return typ(**raw)


CONFIG_HEADER = """# Zordon configuration. This file is read on every start.
# Keys may be left empty and supplied through the environment instead:
#   ANTHROPIC_API_KEY, OPENAI_API_KEY, ELEVENLABS_API_KEY, GROQ_API_KEY, TYPESAFE_API_KEY
# server.bind other than 127.0.0.1 requires server.token. Zordon never writes
# or honours a bypass-permissions setting; permissions belong to Claude Code.

"""
