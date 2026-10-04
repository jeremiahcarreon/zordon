"""WebSocket wire protocol. One WebSocket per client; every message is a JSON
object with a ``type`` field. ``docs/protocol.md`` is the human description and
must be kept in step with this module (``tests/test_protocol.py`` checks that
every type listed here appears in the doc).

Inbound (client -> agent) messages are validated with ``parse_inbound`` before
anything looks at them. Outbound models are produced by ``zordon.transport.ws``
from bus events.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

PROTOCOL_VERSION = 1

# Closed set of client commands. The router may only *select* from this list;
# the client may only *send* from this list.
COMMANDS = (
    "list_sessions",
    "focus",
    "start",
    "resume",
    "attach",
    "detach",
    "delete",
    "send_text",
    "approve",
    "deny",
    "plan_approve",
    "plan_revise",
    "plan_deny",
    "answer",
    "stop",
    "mute",
    "unmute",
    "set_verbosity",
    "set_tool_chatter",
    "set_permission_mode",
    "set_provider",
    "repeat",
    "status",
    "upload",
    # projects, decision 0018
    "list_projects",
    "browse",
    "create_project",
    "open_project",
    "admin",
    "forget_project",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- inbound -------------------------------------------------------------------


class AudioIn(_Strict):
    """20 ms of 16 kHz mono int16 PCM, base64. Sent continuously during a call."""

    type: Literal["audio"]
    pcm: str = Field(max_length=4096)
    seq: int = Field(ge=0, default=0)

    @field_validator("pcm")
    @classmethod
    def _b64(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError("pcm is not valid base64") from e
        if len(raw) == 0 or len(raw) % 2:
            raise ValueError("pcm must be a non-empty whole number of int16 samples")
        if len(raw) > 3200:  # 100 ms at 16 kHz
            raise ValueError("pcm frame too large")
        return v

    def samples(self) -> bytes:
        return base64.b64decode(self.pcm)


class CommandIn(_Strict):
    type: Literal["command"]
    name: str
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in COMMANDS:
            raise ValueError(f"unknown command {v!r}")
        return v


class TextIn(_Strict):
    """Typed text. Goes through the router exactly like speech."""

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=8000)


class CallIn(_Strict):
    """Call lifecycle: the talk button."""

    type: Literal["call"]
    action: Literal["start", "end", "pause", "resume"]


class FlushAck(_Strict):
    type: Literal["flush_ack"]
    generation: int


class Ping(_Strict):
    type: Literal["ping"]
    ts: float | None = None


Inbound = Annotated[
    AudioIn | CommandIn | TextIn | CallIn | FlushAck | Ping,
    Field(discriminator="type"),
]
_inbound_adapter: TypeAdapter[Any] = TypeAdapter(Inbound)

MAX_INBOUND_BYTES = 64 * 1024


class ProtocolError(ValueError):
    pass


def parse_inbound(raw: str | bytes) -> AudioIn | CommandIn | TextIn | CallIn | FlushAck | Ping:
    if len(raw) > MAX_INBOUND_BYTES:
        raise ProtocolError("message too large")
    try:
        return _inbound_adapter.validate_json(raw)
    except ValidationError as e:
        raise ProtocolError(_short(e)) from e


def _short(e: ValidationError) -> str:
    errs = e.errors()
    if not errs:
        return "invalid message"
    first = errs[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc or 'message'}: {first.get('msg', 'invalid')}"


# ---- outbound ------------------------------------------------------------------


class Hello(_Strict):
    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    version: str
    focused_session: str | None
    verbosity: str
    tool_chatter: bool
    providers: dict[str, str]
    tts_sample_rate: int
    tunnel_url: str | None = None
    muted: bool = False  # the agent's mute state, so a fresh client renders the toggle right
    agents: dict[str, bool] = Field(default_factory=dict)  # adapter key -> installed
    default_agent: str = "claude-code"
    home: str | None = None  # the user's home directory: where projects live


class SessionSummary(_Strict):
    session_id: str
    directory: str
    title: str
    last_active: float | None
    attached: bool
    running: bool
    state: str
    permission_mode: str | None = None
    focused: bool = False
    agent: str = "claude-code"


class Sessions(_Strict):
    type: Literal["sessions"] = "sessions"
    sessions: list[SessionSummary]


class ProjectSummary(_Strict):
    id: str
    name: str
    directory: str
    agent: str = "claude-code"
    permission_mode: str = "default"
    scope_edits: bool = True
    talk_first: bool = True
    runner: str = "terminal"  # terminal | headless (decision 0020)
    running: bool = False
    session_id: str | None = None
    focused: bool = False
    state: str | None = None
    last_used: float | None = None
    exists: bool = True


class Projects(_Strict):
    """Every project, most recently used first. Sent after each project command and on
    connect; the client's "Continue a previous project" list."""

    type: Literal["projects"] = "projects"
    projects: list[ProjectSummary]
    focused_project: str | None = None


class BrowseEntry(_Strict):
    name: str
    path: str
    has_git: bool = False
    project_id: str | None = None


class BrowseOut(_Strict):
    """One level of the folder picker, never outside the user's home directory."""

    type: Literal["browse"] = "browse"
    path: str
    parent: str | None
    home: str
    entries: list[BrowseEntry]
    can_create: bool


class SpeechOut(_Strict):
    type: Literal["speech"] = "speech"
    sentence_id: int
    seq: int
    generation: int
    sample_rate: int
    pcm: str  # base64 int16 mono
    final: bool = False


class FlushOut(_Strict):
    type: Literal["flush"] = "flush"
    generation: int
    # The sentence that was playing when the barge-in happened, when known, so the
    # client can mark it (and every unfinished sentence after it) as cut off.
    sentence_id: int | None = None


class TranscriptOut(_Strict):
    type: Literal["transcript"] = "transcript"
    row_id: int
    session_id: str
    kind: Literal["spoken", "user", "notice", "raw"]
    text: str
    raw_lines: list[str]
    ts: float
    sentence_id: int | None = None
    spoken: bool | None = None


class StateOut(_Strict):
    type: Literal["state"] = "state"
    session_id: str
    state: str
    detail: str = ""
    ts: float


class PromptOut(_Strict):
    type: Literal["prompt"] = "prompt"
    prompt_id: int
    session_id: str
    kind: Literal["permission", "plan", "question", "trust"]
    title: str
    options: list[str]
    raw_lines: list[str]
    cleared: bool = False


class SettingsOut(_Strict):
    type: Literal["settings"] = "settings"
    verbosity: str
    tool_chatter: bool
    muted: bool
    providers: dict[str, str]
    permission_mode: str | None = None
    launch_mode: str | None = None  # sessions.permission_mode: what "New session" preselects


class ErrorOut(_Strict):
    type: Literal["error"] = "error"
    message: str
    code: str = "error"


class Pong(_Strict):
    type: Literal["pong"] = "pong"
    ts: float | None = None


class UpdateOut(_Strict):
    """A newer Zordon is available (or was just installed and needs a restart)."""

    type: Literal["update"] = "update"
    current: str
    latest: str
    command: str  # what to run, e.g. "zordon update" or "restart zordon serve"
    auto: bool = False  # True when the update was already installed automatically
    notes_url: str | None = None


class HealthItem(_Strict):
    key: str  # tmux | agent | sessions | normalizer | tts | stt | vad | router | threads | update | hooks
    label: str
    status: Literal["ok", "warn", "fail"]
    detail: str = ""
    fix: str = ""


class HealthOut(_Strict):
    type: Literal["health"] = "health"
    status: Literal["ok", "warn", "fail"]
    items: list[HealthItem]
    ts: float


class TunnelOut(_Strict):
    type: Literal["tunnel"] = "tunnel"
    url: str | None
    qr_svg: str | None = None


OUTBOUND_TYPES = (
    "hello",
    "sessions",
    "projects",
    "browse",
    "speech",
    "flush",
    "transcript",
    "state",
    "prompt",
    "settings",
    "error",
    "pong",
    "tunnel",
    "update",
    "health",
)
INBOUND_TYPES = ("audio", "command", "text", "call", "flush_ack", "ping")


def dump(msg: BaseModel) -> str:
    return msg.model_dump_json()
