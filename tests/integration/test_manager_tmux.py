"""The session core against a real tmux pane running ``tests/fake_claude.py``.

A private tmux server (``tmux -L zordon-test-<pid>``) hosts a ``zordon`` session;
``discovery.new_session_command`` is patched so the pane runs the fake TUI with
the jsonl path the manager expects. The manager thread runs for real at a 100 ms
poll with a 2 s idle watchdog. The user's own tmux server is never touched.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from zordon.bus import (
    Bus,
    Notice,
    PaneLine,
    PromptCleared,
    PromptDetected,
    PromptKind,
    SessionState,
    StateChanged,
)
from zordon.config import Config
from zordon.session import discovery, prompts
from zordon.session.manager import SessionManager
from zordon.session.tmux import Tmux

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent.parent
FAKE = ROOT / "tests" / "fake_claude.py"
PYTHON = sys.executable


def _socket_path(name: str) -> str:
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.join(base, f"tmux-{os.getuid()}", name)


@pytest.fixture
def private_tmux():
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    name = f"zordon-test-{os.getpid()}"
    t = Tmux(socket=name)
    try:
        yield t
    finally:
        t.kill_server()
        try:
            os.unlink(_socket_path(name))
        except OSError:
            pass


class Events:
    """Drains ``bus.client_events`` and ``bus.pane_lines`` and waits for predicates."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.client: list[Any] = []
        self.lines: list[PaneLine] = []

    def pump(self) -> None:
        while True:
            try:
                self.client.append(self.bus.client_events.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                self.lines.append(self.bus.pane_lines.get_nowait())
            except queue.Empty:
                break

    def wait(self, pred, timeout: float = 5.0, what: str = "event"):  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while True:
            self.pump()
            for ev in self.client:
                if pred(ev):
                    return ev
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}; saw {self.describe()}")
            time.sleep(0.05)

    def wait_state(self, sid: str, state: SessionState, timeout: float = 5.0) -> StateChanged:
        return self.wait(
            lambda e: isinstance(e, StateChanged) and e.session_id == sid and e.state is state,
            timeout,
            f"state {state.value}",
        )

    def wait_line(self, pred, timeout: float = 5.0, what: str = "pane line"):  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while True:
            self.pump()
            for line in self.lines:
                if pred(line):
                    return line
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}; lines: {[ln.text for ln in self.lines]}")
            time.sleep(0.05)

    def clear(self) -> None:
        self.pump()
        self.client.clear()
        self.lines.clear()

    def states(self, sid: str) -> list[SessionState]:
        self.pump()
        return [e.state for e in self.client if isinstance(e, StateChanged) and e.session_id == sid]

    def describe(self) -> str:
        return ", ".join(
            f"{type(e).__name__}({getattr(e, 'state', getattr(e, 'text', ''))})" for e in self.client[-8:]
        )


def _wait(pred, timeout: float = 5.0, what: str = "condition") -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


@pytest.fixture
def manager(private_tmux: Tmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    claude_home = tmp_path / "claude-home"
    (claude_home / "projects").mkdir(parents=True)
    (claude_home / "sessions").mkdir()
    zordon_home = tmp_path / "zordon-home"
    project = tmp_path / "proj"
    project.mkdir()

    def fake_command(session_id: str, settings_path: Path | None = None, permission_mode: str | None = None) -> list[str]:
        jsonl = discovery.jsonl_path_for(str(project), session_id, claude_home)
        return [PYTHON, str(FAKE), str(jsonl)]

    monkeypatch.setattr(discovery, "new_session_command", fake_command)
    monkeypatch.setattr(discovery, "resume_command", fake_command)

    cfg = Config()
    cfg.output.poll_interval_ms = 100
    cfg.voice.idle_watchdog_seconds = 2
    bus = Bus()
    mgr = SessionManager(
        bus,
        cfg,
        private_tmux,
        claude_home=claude_home,
        zordon_home=zordon_home,
        hook_port=8765,
        hook_secret="integration-test-secret-0001",
        tmux_session="zordon",
    )
    mgr.start_thread()
    try:
        yield mgr, bus, project
    finally:
        mgr.stop()


def test_session_core_against_fake_tui(manager, private_tmux: Tmux):
    mgr, bus, project = manager
    ev = Events(bus)
    t = private_tmux

    # ---- start: a pane running the fake, hooks file written, IDLE once the TUI is up
    sid = mgr.start(str(project))
    assert discovery.UUID_RE.match(sid)
    target = mgr.sessions[sid].target
    assert t.pane_exists(target)
    assert t.window_size(target) == (160, 45)
    settings = mgr.sessions[sid].settings_path
    assert settings is not None and settings.is_file()
    ev.wait_state(sid, SessionState.WORKING)
    ev.wait_state(sid, SessionState.IDLE, timeout=10)
    assert mgr.state_of(sid) is SessionState.IDLE
    assert mgr.focused() == sid
    _wait(lambda: mgr.sessions[sid].jsonl is not None, what="jsonl located")
    assert mgr.sessions[sid].permission_mode == "default"
    ev.clear()

    # ---- say: prose comes from the jsonl, state goes WORKING then IDLE
    mgr.send_text(sid, "say hello world")
    line = ev.wait_line(lambda ln: ln.source == "jsonl" and "hello world" in ln.text and ln.block == "text")
    assert line.session_id == sid
    ev.wait_line(lambda ln: ln.source == "jsonl" and ln.block == "turn_end")
    ev.wait_state(sid, SessionState.IDLE)
    assert ev.states(sid) == [SessionState.WORKING, SessionState.IDLE]
    assert not any(ln.source == "pane" for ln in ev.lines)
    assert not any(isinstance(e, Notice) and "nothing changed" in e.text for e in ev.client)
    assert any("hello world" in ln for ln in mgr.last_pane_lines(sid))
    ev.clear()

    # ---- perm: permission prompt detected with its unsafe options, deny() clears it
    mgr.send_text(sid, "perm")
    p = ev.wait(lambda e: isinstance(e, PromptDetected) and e.session_id == sid, what="PromptDetected")
    assert p.kind is PromptKind.PERMISSION
    assert len(p.options) == 4 and p.options[0] == "Yes" and p.options[3] == "No"
    assert [prompts.is_unsafe_label(o) for o in p.options] == [False, True, True, False]
    m = mgr.current_match(sid)
    assert [o.unsafe for o in m.options] == [False, True, True, False]
    ev.wait_state(sid, SessionState.AWAITING_PERMISSION)
    assert mgr.current_prompt(sid) is p
    assert mgr.deny(sid) is True
    cleared = ev.wait(lambda e: isinstance(e, PromptCleared) and e.prompt_id == p.prompt_id, timeout=2, what="PromptCleared")
    assert cleared.session_id == sid
    ev.wait_state(sid, SessionState.IDLE, timeout=2)
    assert mgr.current_prompt(sid) is None
    assert any("Interrupted" in ln for ln in mgr.last_pane_lines(sid))
    ev.clear()

    # ---- perm again, approve(): Enter on the plain Yes
    mgr.send_text(sid, "perm")
    p2 = ev.wait(lambda e: isinstance(e, PromptDetected) and e.session_id == sid, what="PromptDetected")
    assert p2.prompt_id != p.prompt_id
    assert mgr.approve(sid) is True
    ev.wait(lambda e: isinstance(e, PromptCleared) and e.prompt_id == p2.prompt_id, timeout=2, what="PromptCleared")
    ev.wait_state(sid, SessionState.IDLE, timeout=2)
    _wait(lambda: any(ln == "● Ran 1 shell command" for ln in mgr.last_pane_lines(sid)), what="approved output")
    ev.clear()

    # ---- plan: plan_approve() picks "Yes, manually approve edits", never auto mode
    mgr.send_text(sid, "plan")
    p3 = ev.wait(lambda e: isinstance(e, PromptDetected) and e.session_id == sid, what="plan prompt")
    assert p3.kind is PromptKind.PLAN
    assert p3.options == ["Yes, and use auto mode", "Yes, manually approve edits", "Tell Claude what to change"]
    ev.wait_state(sid, SessionState.AWAITING_PLAN_APPROVAL)
    assert mgr.plan_approve(sid) is True
    ev.wait(lambda e: isinstance(e, PromptCleared) and e.prompt_id == p3.prompt_id, timeout=2, what="PromptCleared")
    ev.wait_state(sid, SessionState.IDLE, timeout=2)
    tail = mgr.last_pane_lines(sid)
    assert "● Approved: manual" in tail
    assert "● Approved: auto" not in tail
    ev.clear()

    # ---- ask: answer_question picks by number
    mgr.send_text(sid, "ask")
    p4 = ev.wait(lambda e: isinstance(e, PromptDetected) and e.kind is PromptKind.QUESTION, what="question prompt")
    assert p4.options[:2] == ["Tabs", "Spaces"]
    ev.wait_state(sid, SessionState.AWAITING_QUESTION)
    assert mgr.answer_question(sid, "Spaces") is True
    ev.wait_state(sid, SessionState.IDLE, timeout=2)
    assert any("→ Spaces" in ln for ln in mgr.last_pane_lines(sid))
    ev.clear()

    # ---- hang: the watchdog stalls the session after ~2 s with the last lines, Escape recovers
    mgr.send_text(sid, "hang")
    ev.wait_state(sid, SessionState.WORKING)
    t0 = time.monotonic()
    stalled = ev.wait_state(sid, SessionState.STALLED, timeout=6)
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed <= 5.0, elapsed
    assert "no output" in stalled.detail
    notice = ev.wait(lambda e: isinstance(e, Notice) and e.session_id == sid and "waiting on something" in e.text, what="stall notice")
    assert notice.speak and notice.level == "warning"
    assert "❯ hang" in notice.text
    time.sleep(0.5)
    ev.pump()
    assert sum(1 for e in ev.client if isinstance(e, Notice) and "waiting on something" in e.text) == 1
    mgr.send_escape(sid)
    ev.wait_state(sid, SessionState.IDLE, timeout=3)
    assert any("Interrupted" in ln for ln in mgr.last_pane_lines(sid))
    ev.clear()

    # ---- permission mode: Shift+Tab until the status row shows the target; bypass refused
    assert mgr.set_permission_mode(sid, "acceptEdits") is True
    _wait(lambda: any("accept edits mode on" in line for line in t.capture(target)), what="status row")
    assert mgr.sessions[sid].permission_mode == "acceptEdits"
    assert mgr.set_permission_mode(sid, "default") is True
    _wait(lambda: any("manual mode on" in line for line in t.capture(target)), what="status row back")
    with pytest.raises(ValueError):
        mgr.set_permission_mode(sid, "bypassPermissions")
    assert any("manual mode on" in line for line in t.capture(target))
    time.sleep(0.3)
    ev.clear()

    # ---- control characters never reach the pane
    mgr.send_text(sid, "say ok\x03ay \x1b[A done")
    ev.wait_line(lambda ln: ln.source == "jsonl" and ln.block == "text" and "okay [A done" in ln.text)
    ev.wait_state(sid, SessionState.IDLE)
    assert not any("control character received" in ln for ln in mgr.last_pane_lines(sid, 20))
    ev.clear()

    # ---- the picker row for a live session
    rows = mgr.list_sessions()
    assert any(r.session_id == sid and r.attached and r.running and r.state is SessionState.IDLE for r in rows)

    # ---- /exit: the TUI leaves the alternate screen; the session is DETACHED
    mgr.send_text(sid, "/exit")
    detached = ev.wait_state(sid, SessionState.DETACHED, timeout=8)
    assert detached.session_id == sid
    assert mgr.state_of(sid) is SessionState.DETACHED
    assert mgr.sessions[sid].attached is False
    assert mgr.focused() is None
    _wait(lambda: not t.pane_exists(target), timeout=6, what="pane to close")
    assert not settings.exists()


def test_two_sessions_only_one_focused_and_delete_kills_pane(manager, private_tmux: Tmux):
    mgr, bus, project = manager
    ev = Events(bus)
    a = mgr.start(str(project))
    b = mgr.start(str(project))
    ev.wait_state(a, SessionState.IDLE, timeout=10)
    ev.wait_state(b, SessionState.IDLE, timeout=10)
    assert mgr.focused() == a
    rows = {r.session_id: r for r in mgr.list_sessions()}
    assert rows[a].focused and not rows[b].focused
    mgr.focus(b)
    assert mgr.focused() == b
    ta, tb = mgr.sessions[a].target, mgr.sessions[b].target
    mgr.delete(b)
    _wait(lambda: not private_tmux.pane_exists(tb), what="pane b killed")
    assert b not in mgr.sessions
    assert mgr.focused() == a
    assert private_tmux.pane_exists(ta)
    # Resuming the still-attached session is a no-op; resuming an unknown id raises.
    mgr.resume(a)
    assert mgr.sessions[a].target == ta
    with pytest.raises(Exception):  # noqa: B017 - UnknownSession or SessionError
        mgr.resume("11111111-2222-4333-8444-555555555555")
