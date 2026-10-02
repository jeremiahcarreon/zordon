"""A stand-in for ``zordon.app.Agent`` that satisfies ``AgentAPI`` (docs/architecture.md).

Real ``Bus`` and ``Config.default()`` (ZORDON_HOME is isolated by conftest); the
session surface records every call so transport tests can assert on them.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zordon.bus import Bus, PromptDetected, SessionState, TranscriptRow, Utterance
from zordon.config import Config


@dataclass
class FakeSessionInfo:
    session_id: str
    directory: str
    title: str
    last_active: float | None
    attached: bool
    running: bool
    state: SessionState
    permission_mode: str | None = "default"


@dataclass
class FakeSessions:
    """Implements ``SessionControl``. Every call lands in ``calls`` as ``(name, *args)``."""

    calls: list[tuple[Any, ...]] = field(default_factory=list)
    hook_events: list[dict[str, Any]] = field(default_factory=list)
    focused_id: str | None = "sess-1"
    prompt_result: bool = True
    infos: list[FakeSessionInfo] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.infos:
            now = time.time()
            self.infos = [
                FakeSessionInfo(
                    "sess-1", "/home/user/proj-a", "proj-a", now - 60, True, True, SessionState.IDLE
                ),
                FakeSessionInfo(
                    "sess-2",
                    "/home/user/proj-b",
                    "proj-b",
                    now - 3600,
                    False,
                    False,
                    SessionState.DETACHED,
                    "plan",
                ),
            ]

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # ---- SessionControl ------------------------------------------------------------

    def list_sessions(self) -> list[FakeSessionInfo]:
        self._rec("list_sessions")
        return list(self.infos)

    def focused(self) -> str | None:
        return self.focused_id

    def focus(self, session_id: str) -> None:
        self._rec("focus", session_id)
        self.focused_id = session_id

    def state_of(self, session_id: str) -> SessionState:
        for s in self.infos:
            if s.session_id == session_id:
                return s.state
        return SessionState.DETACHED

    def current_prompt(self, session_id: str) -> PromptDetected | None:
        return None

    def start(self, directory: str, permission_mode: str | None = None) -> str:
        self._rec("start", directory, permission_mode)
        return "sess-new"

    def resume(self, session_id: str, permission_mode: str | None = None) -> None:
        self._rec("resume", session_id, permission_mode)

    def detach(self, session_id: str) -> None:
        self._rec("detach", session_id)

    def delete(self, session_id: str) -> None:
        self._rec("delete", session_id)

    def send_text(self, session_id: str, text: str) -> None:
        self._rec("send_text", session_id, text)

    def send_escape(self, session_id: str) -> None:
        self._rec("send_escape", session_id)

    def approve(self, session_id: str) -> bool:
        self._rec("approve", session_id)
        return self.prompt_result

    def deny(self, session_id: str) -> bool:
        self._rec("deny", session_id)
        return self.prompt_result

    def plan_approve(self, session_id: str) -> bool:
        self._rec("plan_approve", session_id)
        return self.prompt_result

    def plan_revise(self, session_id: str, feedback: str) -> bool:
        self._rec("plan_revise", session_id, feedback)
        return self.prompt_result

    def plan_deny(self, session_id: str) -> bool:
        self._rec("plan_deny", session_id)
        return self.prompt_result

    def answer_question(self, session_id: str, option: int | str) -> bool:
        self._rec("answer_question", session_id, option)
        return self.prompt_result

    def accept_trust(self, session_id: str) -> bool:
        self._rec("accept_trust", session_id)
        return True

    def decline_trust(self, session_id: str) -> bool:
        self._rec("decline_trust", session_id)
        return True

    def set_permission_mode(self, session_id: str, mode: str) -> bool:
        self._rec("set_permission_mode", session_id, mode)
        return True

    def permission_summary(self, session_id: str) -> str:
        return "default mode, no extra rules"

    def last_pane_lines(self, session_id: str, n: int = 10) -> list[str]:
        return ["> "]

    def hook_event(self, payload: dict[str, Any]) -> None:
        self._rec("hook_event", payload)
        self.hook_events.append(payload)


class FakeAgent:
    """``AgentAPI`` with recording setters. ``upload_dir`` is where uploads land."""

    def __init__(self, upload_dir: Path, *, token: str | None = None) -> None:
        self.config = Config.default()
        if token is not None:
            self.config.server.token = token
        self.bus = Bus()
        self.sessions = FakeSessions()
        self.version = "0.1.0-test"
        self.hook_secret = secrets.token_hex(16)
        self.tunnel_url: str | None = None
        self.tts_sample_rate = 24000
        self.upload_dir = Path(upload_dir)
        self.calls: list[tuple[Any, ...]] = []
        self.tail_rows: list[TranscriptRow] = [
            TranscriptRow(
                row_id=1,
                session_id="sess-1",
                kind="spoken",
                text="Earlier I edited auth dot p y.",
                raw_lines=["⏺ Edited auth.py"],
                ts=time.time() - 10,
                sentence_id=1,
                spoken=True,
            )
        ]
        self._settings: dict[str, Any] = {
            "verbosity": "minimal",
            "tool_chatter": False,
            "muted": False,
            "providers": {
                "stt": "faster-whisper",
                "tts": "kokoro",
                "normalizer": "anthropic",
                "router": "jev",
            },
            "permission_mode": "default",
        }

    # ---- AgentAPI ----------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        out = dict(self._settings)
        out["providers"] = dict(self._settings["providers"])
        return out

    def set_verbosity(self, level: str) -> None:
        self.calls.append(("set_verbosity", level))
        self._settings["verbosity"] = level

    def set_tool_chatter(self, enabled: bool) -> None:
        self.calls.append(("set_tool_chatter", enabled))
        self._settings["tool_chatter"] = enabled

    def set_muted(self, muted: bool) -> None:
        self.calls.append(("set_muted", muted))
        self._settings["muted"] = muted

    def set_provider(self, kind: str, name: str) -> None:
        self.calls.append(("set_provider", kind, name))
        self._settings["providers"][kind] = name

    def submit_text(self, text: str, client_id: str) -> None:
        self.calls.append(("submit_text", text, client_id))
        self.bus.utterances.put(Utterance(text=text, source="text", client_id=client_id))

    def call_state(self, client_id: str, action: str) -> None:
        self.calls.append(("call_state", client_id, action))

    def repeat_last(self) -> None:
        self.calls.append(("repeat_last",))

    def upload_path(self, filename: str) -> Path:
        self.calls.append(("upload_path", filename))
        return self.upload_dir / filename

    # Optional extra the transport uses when present.
    def transcript_tail(self, session_id: str, n: int) -> list[TranscriptRow]:
        return [r for r in self.tail_rows if r.session_id == session_id][-n:]

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]
