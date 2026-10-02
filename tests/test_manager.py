"""SessionManager unit tests with a scripted FakeTmux (no tmux, no thread unless stated).

Screens come from ``eval/fixtures/pane``; each test sets the fake pane's current
capture and drives ``poll_once`` by hand, then inspects the bus and the exact
``send-keys`` calls.
"""

from __future__ import annotations

import json
import os
import queue
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from tests import fixtures_store as fs
from tests.conftest import read_fixture
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
from zordon.session import discovery, hooks
from zordon.session import manager as M
from zordon.session.manager import QUIET_IDLE_POLLS, SessionBusy, SessionManager, UnknownSession


def claude_argv(command: list[str]) -> list[str]:
    """The ``claude ...`` part of a launched command (after the ``env -u ...`` scrub prefix)."""
    return discovery.strip_env_prefix(command)


def lines_of(name: str) -> list[str]:
    text = read_fixture(name)
    out = text.split("\n")
    if out and out[-1] == "":
        out.pop()
    return [line.rstrip(" ") for line in out]


def with_mode(lines: list[str], mode: str) -> list[str]:
    return [line.replace("⏸ manual mode on", f"⏸ {mode} mode on") for line in lines]


class FakeTmux:
    """Scripted pane: ``screens[target]`` is what ``capture`` returns."""

    MODE_CYCLE = ("manual", "accept edits", "plan")

    def __init__(self) -> None:
        self.screens: dict[str, list[str]] = {}
        self.alt: dict[str, bool] = {}
        self.alive: dict[str, bool] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.sessions: set[str] = set()
        self.windows: list[tuple[str, str, str, list[str]]] = []
        self.btab_cycles = False
        self.bypass_in_cycle = False
        self._n = 0

    # scripting
    def set_screen(self, target: str, lines: list[str], *, alt: bool = True) -> None:
        self.screens[target] = list(lines)
        self.alt[target] = alt
        self.alive.setdefault(target, True)

    def keys(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "key"]

    # Tmux surface used by the manager
    def ensure_session(self, name: str, cwd: str | None = None, width: int = 160, height: int = 45) -> str:
        self.sessions.add(name)
        self.calls.append(("ensure_session", name))
        return name

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def new_window(self, session: str, name: str, cwd: str, command, width: int = 160, height: int = 45) -> str:
        """Like the real one: creates the session with this window first when it is missing."""
        kind = "new_window"
        if session not in self.sessions:
            self.sessions.add(session)
            kind = "new_session"
        self._n += 1
        target = f"{session}:@{self._n}.%{self._n}"
        self.windows.append((target, name, cwd, list(command)))
        self.alive[target] = True
        self.screens.setdefault(target, [])
        self.alt.setdefault(target, False)
        self.calls.append((kind, target, list(command)))
        return target

    def pane_exists(self, target: str) -> bool:
        return self.alive.get(target, False)

    def alternate_on(self, target: str) -> bool:
        return self.alt.get(target, False)

    def capture(self, target: str, ansi: bool = False, history_lines: int = 0, *, with_ansi: bool = False) -> list[str]:
        return list(self.screens.get(target, []))

    def send_literal(self, target: str, text: str) -> None:
        assert "\x03" not in text
        self.calls.append(("literal", target, text))

    def send_enter(self, target: str) -> None:
        self.calls.append(("enter", target))

    def send_key(self, target: str, key: str) -> None:
        self.calls.append(("key", target, key))
        if key == "BTab" and self.btab_cycles:
            self._cycle(target)

    def kill_window(self, target: str) -> None:
        self.alive[target] = False
        self.calls.append(("kill_window", target))

    def _cycle(self, target: str) -> None:
        lines = self.screens[target]
        cycle = list(self.MODE_CYCLE) + (["bypass permissions"] if self.bypass_in_cycle else [])
        for i, line in enumerate(lines):
            for j, word in enumerate(cycle):
                if f"⏸ {word} mode on" in line:
                    nxt = cycle[(j + 1) % len(cycle)]
                    lines[i] = line.replace(f"⏸ {word} mode on", f"⏸ {nxt} mode on")
                    return


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def env(tmp_path: Path):
    bus = Bus()
    cfg = Config()
    cfg.output.poll_interval_ms = 10
    cfg.voice.idle_watchdog_seconds = 20
    tmux = FakeTmux()
    clock = Clock()
    claude_home = fs.make_claude_home(tmp_path)
    zordon_home = tmp_path / "zordon-home"
    mgr = SessionManager(
        bus,
        cfg,
        tmux,  # type: ignore[arg-type]
        claude_home=claude_home,
        zordon_home=zordon_home,
        hook_port=8765,
        hook_secret="s3cr3t-s3cr3t-s3cr3t",
        clock=clock,
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    return mgr, bus, tmux, clock, proj


def drain(bus: Bus) -> list[Any]:
    out = []
    while True:
        try:
            out.append(bus.client_events.get_nowait())
        except queue.Empty:
            return out


def drain_lines(bus: Bus) -> list[PaneLine]:
    out = []
    while True:
        try:
            out.append(bus.pane_lines.get_nowait())
        except queue.Empty:
            return out


def states(events: list[Any]) -> list[SessionState]:
    return [e.state for e in events if isinstance(e, StateChanged)]


def started(env, screen: str | list[str] = "idle.txt", *, alt: bool = True):
    """Start a session on the fake and settle it on ``screen``. Returns (sid, target)."""
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    target = mgr.sessions[sid].target
    tmux.set_screen(target, lines_of(screen) if isinstance(screen, str) else screen, alt=alt)
    mgr.poll_once()
    drain(bus)
    drain_lines(bus)
    tmux.calls.clear()
    return sid, target


# ---- lifecycle ---------------------------------------------------------------------


def test_start_opens_pane_with_hooks_and_publishes_working(env):
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj), "plan")
    assert discovery.UUID_RE.match(sid)
    target, name, cwd, full = tmux.windows[0]
    assert cwd == str(proj)
    assert full[0] == "env" and "-u" in full  # RR-7 / SEC-2: scrub prefix
    command = claude_argv(full)
    assert command[:3] == ["claude", "--session-id", sid]
    assert "--permission-mode" in command and command[command.index("--permission-mode") + 1] == "plan"
    settings = Path(command[command.index("--settings") + 1])
    assert settings == discovery.hook_settings_path(mgr.zordon_home, sid)
    assert settings.is_file()
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == {"Notification", "UserPromptSubmit", "Stop"}
    assert "PermissionRequest" not in data["hooks"]
    hook_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "s3cr3t" not in hook_cmd and "s3cr3t" not in settings.read_text()  # SEC-6
    curlrc = discovery.hook_curl_config_path(settings)
    assert f"-K {curlrc}" in hook_cmd
    assert curlrc.is_file() and stat.S_IMODE(curlrc.stat().st_mode) == 0o600
    assert "X-Zordon-Hook-Secret: s3cr3t-s3cr3t-s3cr3t" in curlrc.read_text()
    assert "http://127.0.0.1:8765/hooks/claude" in hook_cmd  # SEC-8: the default bind
    assert mgr.state_of(sid) is SessionState.WORKING
    assert mgr.focused() == sid
    assert "zordon" in tmux.sessions
    ev = drain(bus)
    assert states(ev) == [SessionState.WORKING]
    assert ev[0].detail == "starting"


def test_start_refuses_bypass_mode(env):
    mgr, bus, tmux, clock, proj = env
    with pytest.raises(ValueError):
        mgr.start(str(proj), "bypassPermissions")
    assert tmux.windows == []


def test_start_without_hook_config_passes_no_settings(env):
    mgr, bus, tmux, clock, proj = env
    mgr.hook_port = None
    sid = mgr.start(str(proj))
    command = claude_argv(tmux.windows[0][3])
    assert "--settings" not in command
    assert command == ["claude", "--session-id", sid, "--permission-mode", "default"]


def test_start_and_resume_without_a_mode_never_inherit_auto(env):
    """RR-2: Claude Code 2.1.x starts in auto when no mode is given; Zordon always names one."""
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    command = claude_argv(tmux.windows[0][3])
    assert command[command.index("--permission-mode") + 1] == "default"
    other = fs.sid(8)
    fs.write_session(mgr.claude_home, str(proj), other, [fs.user_prompt(other, str(proj), time.time() - 60, "hello")])
    mgr.resume(other)
    command = claude_argv(tmux.windows[1][3])
    assert command[:3] == ["claude", "--resume", other]
    assert command[command.index("--permission-mode") + 1] == "default"
    # An explicit mode is still honoured (narrowing only; bypass is refused by the builder).
    sid3 = mgr.start(str(proj), "plan")
    assert claude_argv(tmux.windows[2][3])[-1] == "plan"
    assert sid3 != sid
    with pytest.raises(ValueError):
        mgr.start(str(proj), "bypassPermissions")


def test_hook_url_follows_the_server_bind(env):
    """SEC-8: a server bound to one address only listens there, so the pane posts there."""
    mgr, bus, tmux, clock, proj = env
    mgr.config.server.bind = "100.101.102.103"
    mgr.config.server.token = "t" * 32
    sid = mgr.start(str(proj))
    settings = mgr.sessions[sid].settings_path
    cmd = json.loads(settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "http://100.101.102.103:8765/hooks/claude" in cmd
    mgr.config.server.bind = "0.0.0.0"
    sid2 = mgr.start(str(proj))
    cmd2 = json.loads(mgr.sessions[sid2].settings_path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "http://127.0.0.1:8765/hooks/claude" in cmd2


def test_first_pane_is_the_first_window_of_the_tmux_session(env):
    """RR-8: no stray shell window; the Claude Code pane creates the session."""
    mgr, bus, tmux, clock, proj = env
    a = mgr.start(str(proj))
    b = mgr.start(str(proj))
    kinds = [c[0] for c in tmux.calls if c[0] in ("ensure_session", "new_session", "new_window")]
    assert kinds == ["new_session", "new_window"]
    assert "zordon" in tmux.sessions
    assert mgr.sessions[a].target != mgr.sessions[b].target


def test_idle_screen_moves_to_idle(env):
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    target = mgr.sessions[sid].target
    drain(bus)
    tmux.set_screen(target, lines_of("idle.txt"), alt=True)
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.IDLE]
    assert mgr.state_of(sid) is SessionState.IDLE
    assert mgr.sessions[sid].permission_mode == "default"
    mgr.poll_once()
    assert states(drain(bus)) == []  # stable: no repeated event


def test_normal_screen_startup_stays_working_until_tui_is_up(env):
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    target = mgr.sessions[sid].target
    drain(bus)
    tmux.set_screen(target, ["$ claude --session-id x", "Permission deny rule (...)"], alt=False)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.WORKING
    assert drain_lines(bus) == []  # the shell is not Claude Code output
    tmux.set_screen(target, lines_of("idle.txt"), alt=True)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE


def test_pane_gone_detaches_and_removes_hook_settings(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    settings = mgr.sessions[sid].settings_path
    assert settings and settings.exists()
    curlrc = discovery.hook_curl_config_path(settings)
    assert curlrc.exists()
    tmux.alive[target] = False
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.DETACHED]
    assert mgr.sessions[sid].attached is False
    assert not settings.exists() and not curlrc.exists()
    assert mgr.focused() is None
    # Not polled any more, but still listed for resume.
    mgr.poll_once()
    assert drain(bus) == []
    assert any(r.session_id == sid and r.state is SessionState.DETACHED for r in mgr.list_sessions())


def test_exit_line_on_normal_screen_detaches_with_notice(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    tmux.set_screen(target, lines_of("exit.txt"), alt=False)
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.DETACHED]
    notices = [e for e in ev if isinstance(e, Notice)]
    assert notices and notices[0].speak and "exited" in notices[0].text
    assert mgr.sessions[sid].attached is False
    mgr.poll_once()
    assert drain(bus) == []  # notice only once; pane is left alone
    assert tmux.alive[target]


SHELL_ONLY = [
    "user@host:~/proj$ claude --resume 11111111-2222-4333-8444-555555555555",
    "No conversation found with session ID: 11111111-2222-4333-8444-555555555555",
    "user@host:~/proj$",
]
CRASH_NO_RESUME_LINE = [
    "node:internal/process/promises:289",
    "    triggerUncaughtException(err, true /* fromPromise */);",
    "Error: boom",
    "    at main (file:///x/cli.js:12:3)",
    "user@host:~/proj$",
]


def test_shell_prompt_after_failed_start_detaches_and_refuses_text(env):
    """PR-2: a bare shell is never Claude Code; voice text must not become a shell command."""
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    target = mgr.sessions[sid].target
    drain(bus)
    tmux.set_screen(target, SHELL_ONLY, alt=False)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.WORKING  # one poll is not enough
    assert mgr.sessions[sid].exit_polls == 1
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.DETACHED]
    assert any(isinstance(e, Notice) and e.speak and "exited" in e.text for e in ev)
    assert mgr.sessions[sid].attached is False
    with pytest.raises(M.SessionError):
        mgr.send_text(sid, "delete the build directory and rerun the tests")
    assert not any(c[0] in ("literal", "enter", "key") for c in tmux.calls)


def test_leaving_the_alternate_screen_without_the_resume_line_detaches(env):
    """PR-2: a crash shows no 'claude --resume' line; having left the TUI is enough."""
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)  # alt screen seen
    assert mgr.sessions[sid].seen_alternate
    tmux.set_screen(target, CRASH_NO_RESUME_LINE[:-1], alt=False)  # no shell prompt yet
    mgr.poll_once()
    assert mgr.state_of(sid) is not SessionState.DETACHED  # one poll is not enough
    drain(bus)
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.DETACHED]
    assert mgr.sessions[sid].attached is False
    assert mgr.focused() is None
    # The closed-session marker lets the pipeline flush its buffers (CONC-8).
    markers = [ln for ln in drain_lines(bus) if ln.block == "turn_end"]
    assert len(markers) == 1 and markers[0].session_id == sid and markers[0].meta["reason"] == "session_closed"


def test_brief_normal_screen_flicker_does_not_detach(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    tmux.set_screen(target, ["", ""], alt=False)
    mgr.poll_once()
    tmux.set_screen(target, lines_of("idle.txt"), alt=True)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE
    assert mgr.sessions[sid].exit_polls == 0
    assert SessionState.DETACHED not in states(drain(bus))
    assert mgr.sessions[sid].attached


def test_trust_dialog_on_the_normal_screen_is_not_an_exit_and_accepts_keys(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "trust_dialog.txt", alt=False)
    for _ in range(4):
        mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.AWAITING_PERMISSION
    assert mgr.sessions[sid].exit_polls == 0
    assert mgr.accept_trust(sid) is True  # the one prompt that lives off the alternate screen
    assert tmux.keys() == ["Down", "Enter"]


def test_keystrokes_refused_off_the_alternate_screen_unless_trust_dialog(env):
    """PR-2: no send_text / Escape / menu selection into a pane that is not showing the TUI."""
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission.txt")
    assert mgr.current_prompt(sid) is not None
    tmux.alt[target] = False  # the TUI vanished between the poll and the keystroke
    with pytest.raises(M.SessionError, match="won't type"):
        mgr.send_text(sid, "yes")
    with pytest.raises(M.SessionError):
        mgr.send_escape(sid)
    with pytest.raises(M.SessionError):
        mgr.approve(sid)
    with pytest.raises(M.SessionError):
        mgr.deny(sid)
    assert tmux.calls == []
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert notices and all(n.speak and "won't type" in n.text for n in notices)
    tmux.alt[target] = True
    assert mgr.approve(sid) is True
    assert tmux.keys() == ["Enter"]


def test_looks_like_shell_prompt_regex():
    yes = ["user@host:~/proj$", "user@host:~/proj$ ", "(venv) user@host:~$", "host% ", "# ", "$", "~/proj %", "/root#"]
    no = ["$ claude --session-id x", "Permission deny rule (...)", "100%", "Downloading 45%", "", "   ", "❯ ", "No conversation found"]
    for line in yes:
        assert M.looks_like_shell_prompt(["other", line, ""]), line
    for line in no:
        assert not M.looks_like_shell_prompt(["other", line]), line
    assert not M.looks_like_shell_prompt([])


def test_detach_and_delete(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.detach(sid)
    assert mgr.state_of(sid) is SessionState.DETACHED
    assert states(drain(bus)) == [SessionState.DETACHED]
    assert tmux.alive[target]  # pane keeps running
    with pytest.raises(M.SessionError):
        mgr.send_text(sid, "hi")
    # CONC-8: the pipeline is told the stream ended so its buffers flush now.
    closed = [ln for ln in drain_lines(bus) if ln.block == "turn_end"]
    assert len(closed) == 1 and closed[0].session_id == sid and closed[0].source == "jsonl"
    mgr.delete(sid)
    assert ("kill_window", target) in tmux.calls
    assert sid not in mgr.sessions
    assert mgr.state_of(sid) is SessionState.DETACHED
    closed = [ln for ln in drain_lines(bus) if ln.block == "turn_end"]
    assert len(closed) == 1 and closed[0].session_id == sid
    with pytest.raises(UnknownSession):
        mgr.delete(sid)


def test_automatic_refocus_publishes_a_sessions_snapshot(env):
    """CONC-11: pane died / delete / exit move focus; every client's picker must learn it."""
    mgr, bus, tmux, clock, proj = env
    a, ta = started(env)
    b = mgr.start(str(proj))
    tb = mgr.sessions[b].target
    tmux.set_screen(tb, lines_of("idle.txt"))
    mgr.poll_once()
    drain(bus)
    assert mgr.focused() == a
    published: list[str | None] = []
    mgr.publish_sessions = lambda: published.append(mgr.focused())
    # Explicit focus is the agent's business (it publishes itself): no snapshot from here.
    mgr.focus(b)
    mgr.focus(a)
    assert published == []
    # Pane a dies: focus moves to b automatically.
    tmux.alive[ta] = False
    mgr.poll_once()
    assert mgr.focused() == b and published == [b]
    # Deleting the focused session moves focus (to None here).
    mgr.delete(b)
    assert mgr.focused() is None and published == [b, None]
    # Deleting an unfocused session changes nothing.
    c = mgr.start(str(proj))
    d = mgr.start(str(proj))
    assert mgr.focused() == c
    mgr.delete(d)
    assert published == [b, None]
    # Exit line on the focused pane: snapshot again.
    tmux.set_screen(mgr.sessions[c].target, lines_of("exit.txt"), alt=False)
    mgr.poll_once()
    assert published == [b, None, None]


def test_default_sessions_snapshot_is_the_transport_message(env):
    from zordon.transport.protocol import Sessions

    mgr, bus, tmux, clock, proj = env
    a, ta = started(env)
    b = mgr.start(str(proj))
    drain(bus)
    tmux.alive[ta] = False
    mgr.poll_once()
    snaps = [e for e in drain(bus) if isinstance(e, Sessions)]
    assert len(snaps) == 1
    rows = {r.session_id: r for r in snaps[0].sessions}
    assert rows[b].focused and not rows[a].focused


def test_focus_switches_between_sessions(env):
    mgr, bus, tmux, clock, proj = env
    a = mgr.start(str(proj))
    b = mgr.start(str(proj))
    assert mgr.focused() == a
    mgr.focus(b)
    assert mgr.focused() == b
    assert mgr.sessions[b].focused and not mgr.sessions[a].focused
    with pytest.raises(UnknownSession):
        mgr.focus("nope")
    rows = {r.session_id: r for r in mgr.list_sessions()}
    assert rows[b].focused and not rows[a].focused


# ---- output ------------------------------------------------------------------------


def test_pane_lines_published_only_without_jsonl(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    tmux.set_screen(target, lines_of("prose_output.txt"))
    mgr.poll_once()
    lines = drain_lines(bus)
    assert lines and all(line.source == "pane" and line.session_id == sid for line in lines)
    texts = [line.text for line in lines]
    assert not any(line.startswith("❯ ") for line in texts)  # input box is never content
    assert any("✻" in t for t in texts)  # the done line is content (turn terminator)
    # Nothing new on an identical screen.
    mgr.poll_once()
    assert drain_lines(bus) == []
    # Once the jsonl appears (auto mode), pane prose stops and jsonl events flow.
    path = discovery.jsonl_path_for(str(proj), sid, mgr.claude_home)
    path.parent.mkdir(parents=True)
    records = [
        fs.permission_mode(sid, "acceptEdits"),
        fs.assistant_record(sid, str(proj), time.time(), [fs.text_block("Hello from jsonl.")], stop_reason="end_turn"),
    ]
    path.write_bytes(fs.jsonl_bytes(records))
    # The status row is authoritative when visible; here it agrees with the jsonl.
    tmux.set_screen(target, with_mode(lines_of("working_no_spinner.txt"), "accept edits"))
    mgr.poll_once()
    lines = drain_lines(bus)
    assert [line.source for line in lines] == ["jsonl"] * len(lines)
    blocks = [line.block for line in lines]
    assert "text" in blocks and "turn_end" in blocks and "permission_mode" in blocks
    assert mgr.sessions[sid].permission_mode == "acceptEdits"
    assert mgr.sessions[sid].jsonl is not None
    # Screen wins over a stale jsonl record.
    tmux.set_screen(target, lines_of("working_no_spinner.txt"))
    mgr.poll_once()
    assert mgr.sessions[sid].permission_mode == "default"


def test_output_source_pane_keeps_pane_lines_even_with_jsonl(env):
    mgr, bus, tmux, clock, proj = env
    mgr.config.output.source = "pane"
    sid, target = started(env)
    path = discovery.jsonl_path_for(str(proj), sid, mgr.claude_home)
    path.parent.mkdir(parents=True)
    path.write_bytes(fs.jsonl_bytes([fs.assistant_record(sid, str(proj), time.time(), [fs.text_block("x")])]))
    tmux.set_screen(target, lines_of("prose_output.txt"))
    mgr.poll_once()
    sources = {line.source for line in drain_lines(bus)}
    assert sources == {"pane"}


def test_last_pane_lines_and_permission_summary(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "denial.txt")
    tail = mgr.last_pane_lines(sid, 3)
    assert len(tail) == 3
    assert tail[-1].startswith("✻ Sautéed")
    assert "Interrupted" in tail[-2]
    assert mgr.last_pane_lines("unknown") == []
    sentence = mgr.permission_summary(sid)
    assert "default mode" in sentence
    assert "bypass" not in sentence.lower() or "never" in sentence.lower()
    fs.write_settings(proj / ".claude" / "settings.local.json", {"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Edit(.env)"]}})
    mgr.sessions[sid].permission_mode = "plan"
    sentence = mgr.permission_summary(sid)
    assert "plan mode" in sentence and "1 allow rule" in sentence and "1 deny rule" in sentence


# ---- prompts -----------------------------------------------------------------------


def test_permission_prompt_detected_once_then_cleared(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    tmux.set_screen(target, lines_of("bash_permission.txt"))
    mgr.poll_once()
    ev = drain(bus)
    prompts_ = [e for e in ev if isinstance(e, PromptDetected)]
    assert len(prompts_) == 1
    p = prompts_[0]
    assert p.kind is PromptKind.PERMISSION and len(p.options) == 4
    assert p.options[0] == "Yes" and p.options[-1] == "No"
    assert states(ev) == [SessionState.AWAITING_PERMISSION]
    assert mgr.current_prompt(sid) is p
    m = mgr.current_match(sid)
    assert m is not None and [o.unsafe for o in m.options] == [False, True, True, False]
    # Same prompt, pointer moved: no second PromptDetected.
    mgr.poll_once()
    tmux.set_screen(target, lines_of("bash_permission_no_selected.txt"))
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, PromptDetected | PromptCleared)] == []
    assert mgr.current_match(sid).selected.index == 4
    # Prompt answered in the pane: cleared, state back to idle.
    tmux.set_screen(target, lines_of("denial.txt"))
    mgr.poll_once()
    ev = drain(bus)
    cleared = [e for e in ev if isinstance(e, PromptCleared)]
    assert cleared and cleared[0].prompt_id == p.prompt_id
    assert states(ev) == [SessionState.IDLE]
    assert mgr.current_prompt(sid) is None


def test_a_different_prompt_replaces_the_current_one(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission.txt")
    first = mgr.current_prompt(sid)
    tmux.set_screen(target, lines_of("write_permission.txt"))
    mgr.poll_once()
    ev = drain(bus)
    assert [type(e) for e in ev if not isinstance(e, StateChanged)] == [PromptCleared, PromptDetected]
    assert ev[0].prompt_id == first.prompt_id
    assert mgr.current_prompt(sid).title == "Create file probe.txt"


def test_approve_sends_enter_on_the_plain_yes(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission.txt")
    assert mgr.approve(sid) is True
    assert tmux.keys() == ["Enter"]
    assert not any(c[0] in ("literal", "enter") for c in tmux.calls)


def test_approve_navigates_back_when_pointer_moved(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission_no_selected.txt")
    assert mgr.approve(sid) is True
    assert tmux.keys() == ["Up", "Up", "Up", "Enter"]


def test_deny_moves_to_the_last_no_option(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission.txt")
    assert mgr.deny(sid) is True
    assert tmux.keys() == ["Down", "Down", "Down", "Enter"]
    tmux.calls.clear()
    tmux.set_screen(target, lines_of("write_permission.txt"))
    mgr.poll_once()
    assert mgr.deny(sid) is True
    assert tmux.keys() == ["Down", "Down", "Enter"]


def test_deny_without_a_no_option_sends_escape(env):
    mgr, bus, tmux, clock, proj = env
    screen = lines_of("bash_permission.txt")
    screen = [line for line in screen if not line.strip().startswith("4. No")]
    sid, target = started(env, screen)
    assert mgr.current_prompt(sid) is not None
    assert mgr.deny(sid) is True
    assert tmux.keys() == ["Escape"]


def test_approve_refuses_when_no_plain_yes(env):
    mgr, bus, tmux, clock, proj = env
    screen = [line.replace("❯ 1. Yes", "❯ 1. Yes, and always allow everything") for line in lines_of("bash_permission.txt")]
    sid, target = started(env, screen)
    assert mgr.current_prompt(sid) is not None
    assert mgr.approve(sid) is False
    assert tmux.keys() == []


def test_approve_and_deny_return_false_without_a_prompt(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "idle.txt")
    assert mgr.approve(sid) is False
    assert mgr.deny(sid) is False
    assert mgr.plan_approve(sid) is False
    assert mgr.plan_deny(sid) is False
    assert mgr.answer_question(sid, 1) is False
    assert mgr.accept_trust(sid) is False
    assert tmux.keys() == []


def test_plan_prompt_controls(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "MENU_SETTLE", 0.0)
    sid, target = started(env, "plan_approval.txt")
    assert mgr.state_of(sid) is SessionState.AWAITING_PLAN_APPROVAL
    p = mgr.current_prompt(sid)
    assert p.kind is PromptKind.PLAN and p.options[0] == "Yes, and use auto mode"
    assert mgr.approve(sid) is False  # approve() is for permission prompts only
    assert mgr.plan_approve(sid) is True
    assert tmux.keys() == ["Down", "Enter"]  # option 2, never option 1
    tmux.calls.clear()
    assert mgr.plan_deny(sid) is True
    assert tmux.keys() == ["Escape"]
    tmux.calls.clear()
    assert mgr.plan_revise(sid, "add tests\x1b first") is True
    assert tmux.calls == [
        ("key", target, "Down"),
        ("key", target, "Down"),
        ("key", target, "Enter"),
        ("literal", target, "add tests first"),
        ("enter", target),
    ]


def test_answer_question_by_number_or_label(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "ask_user_question.txt")
    assert mgr.state_of(sid) is SessionState.AWAITING_QUESTION
    assert mgr.current_prompt(sid).options == ["Tabs", "Spaces", "Type something.", "Chat about this"]
    assert mgr.answer_question(sid, 2) is True
    assert tmux.keys() == ["Down", "Enter"]
    tmux.calls.clear()
    assert mgr.answer_question(sid, "tabs") is True
    assert tmux.keys() == ["Enter"]
    tmux.calls.clear()
    assert mgr.answer_question(sid, 9) is False
    assert mgr.answer_question(sid, "nope") is False
    assert tmux.keys() == []


def test_trust_dialog_on_the_normal_screen(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "trust_dialog.txt", alt=False)
    p = mgr.current_prompt(sid)
    assert p is not None and p.kind is PromptKind.TRUST
    assert mgr.state_of(sid) is SessionState.AWAITING_PERMISSION
    assert mgr.approve(sid) is True  # trust: the "Yes, I trust this folder" option
    assert tmux.keys() == ["Down", "Enter"]
    tmux.calls.clear()
    assert mgr.accept_trust(sid) is True
    assert tmux.keys() == ["Down", "Enter"]
    tmux.calls.clear()
    tmux.set_screen(target, lines_of("trust_dialog_yes_selected.txt"), alt=False)
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, PromptDetected)] == []  # same prompt
    assert mgr.accept_trust(sid) is True
    assert tmux.keys() == ["Enter"]
    tmux.calls.clear()
    assert mgr.decline_trust(sid) is True
    assert tmux.keys() == ["Up", "Enter"]
    assert mgr.state_of(sid) is SessionState.DETACHED
    ev = drain(bus)
    assert any(isinstance(e, PromptCleared) for e in ev)
    assert states(ev) == [SessionState.DETACHED]


# ---- keystrokes -----------------------------------------------------------------------


def test_send_text_strips_control_characters_and_sends_enter_separately(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.send_text(sid, "run the tests\x03 now\x1b[A please\n")
    assert tmux.calls == [("literal", target, "run the tests now[A please"), ("enter", target)]


def test_send_text_with_only_control_characters_sends_nothing(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.send_text(sid, "\x03\x1b  ")
    assert tmux.calls == []
    ev = drain(bus)
    assert any(isinstance(e, Notice) and "nothing" in e.text.lower() for e in ev)


def test_send_text_nothing_changed_notice(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.send_text(sid, "hello")
    mgr.poll_once()
    clock.advance(1.0)
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, Notice)] == []
    clock.advance(0.6)
    mgr.poll_once()
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].speak and "nothing changed" in notices[0].text
    assert mgr.state_of(sid) is SessionState.IDLE  # state never follows what we sent
    mgr.poll_once()
    clock.advance(5)
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, Notice)] == []  # only once


def test_send_text_echo_seen_means_no_notice(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.send_text(sid, "hello")
    tmux.set_screen(target, lines_of("working_no_spinner.txt"))
    mgr.poll_once()
    clock.advance(2.0)
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, Notice)] == []
    assert mgr.state_of(sid) is SessionState.WORKING


def test_send_escape(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.send_escape(sid)
    assert tmux.calls == [("key", target, "Escape")]


# ---- state / watchdog ------------------------------------------------------------------


def test_working_then_idle_from_frames(env):
    mgr, bus, tmux, clock, proj = env
    from zordon.session.screen import split_frames

    sid, target = started(env)
    frames = split_frames(read_fixture("spinner_frames.txt"))
    seen: list[SessionState] = []
    for _, _, lines in frames:
        tmux.set_screen(target, lines)
        clock.advance(0.1)
        mgr.poll_once()
        seen += states(drain(bus))
    assert seen[0] is SessionState.WORKING
    assert SessionState.STALLED not in seen
    assert SessionState.AWAITING_PERMISSION not in seen
    lines = drain_lines(bus)
    assert lines and not any("Thinking…" in line.text or "Perambulating…" in line.text for line in lines)


def test_watchdog_stalls_once_with_last_lines_then_recovers(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    from zordon.session.screen import split_frames

    frames = split_frames(read_fixture("spinner_frames.txt"))
    spinner_screen = frames[2][2]
    tmux.set_screen(target, spinner_screen)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.WORKING
    drain(bus)
    # A spinner that keeps spinning without any content or token progress is a stall.
    for _ in range(12):
        clock.advance(2.0)
        glyph_rotated = [line.replace("✢ ", "✻ ").replace("* ", "✽ ") if "…" in line else line for line in spinner_screen]
        tmux.set_screen(target, glyph_rotated)
        mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.STALLED]
    notices = [e for e in ev if isinstance(e, Notice)]
    assert len(notices) == 1
    assert notices[0].speak and notices[0].text.startswith(M.STALL_TEXT)
    tail = mgr.last_pane_lines(sid, 10)
    assert tail and all(t in notices[0].text for t in tail[-2:])
    assert notices[0].level == "warning"
    # Still stalled: no second notice.
    clock.advance(5)
    mgr.poll_once()
    assert drain(bus) == []
    # Output resumes: WORKING again, then idle.
    tmux.set_screen(target, lines_of("working_no_spinner.txt"))
    mgr.poll_once()
    assert states(drain(bus)) == [SessionState.WORKING]
    tmux.set_screen(target, lines_of("prose_output.txt"))
    mgr.poll_once()
    assert states(drain(bus)) == [SessionState.IDLE]


def test_spinner_token_progress_is_not_a_stall(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    base = lines_of("working_no_spinner.txt")
    # Put a thinking spinner with a growing token count above the input-box rule.
    idx = max(i for i, line in enumerate(base) if line.startswith("❯ ")) - 1
    for n in range(12):
        screen = list(base)
        screen[idx - 1] = f"✶ Thinking… ({n}s · ↓ {n * 100 + 5} tokens)"
        tmux.set_screen(target, screen)
        clock.advance(2.5)
        mgr.poll_once()
    seen = states(drain(bus))
    assert SessionState.STALLED not in seen
    assert mgr.state_of(sid) is SessionState.WORKING


def test_registry_waiting_status_counts_as_prompt_score(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "working_no_spinner.txt")
    assert mgr.state_of(sid) is SessionState.WORKING
    fs.write_registry_entry(mgr.claude_home, os.getpid(), sid, str(proj), status="waiting")
    clock.advance(1.1)
    mgr.poll_once()
    assert mgr.sessions[sid].registry_status == "waiting"
    clock.advance(25)
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.AWAITING_PERMISSION]
    changed = next(e for e in ev if isinstance(e, StateChanged))
    assert "looks like a prompt" in changed.detail
    notices = [e for e in ev if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].speak and "can't read the prompt" in notices[0].text
    assert mgr.current_prompt(sid) is None  # nothing to offer buttons for
    # The belief holds while the screen stays put (next_state alone would flip back).
    for _ in range(5):
        clock.advance(0.1)
        mgr.poll_once()
    assert drain(bus) == []
    assert mgr.state_of(sid) is SessionState.AWAITING_PERMISSION
    # The screen moves on: regex is in charge again.
    tmux.set_screen(target, lines_of("idle.txt"))
    mgr.poll_once()
    assert states(drain(bus)) == [SessionState.IDLE]
    assert mgr.sessions[sid].believed_prompt is None


# ---- hooks ---------------------------------------------------------------------------------


def hook_payload(sid: str, **kw) -> dict[str, Any]:
    base = {
        "session_id": sid,
        "transcript_path": "/x/y.jsonl",
        "cwd": "/x",
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs permission to run a Bash command",
        "title": "Permission Required",
    }
    base.update(kw)
    return base


def test_hook_event_hint_flags_an_unreadable_prompt(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "working_no_spinner.txt")
    mgr.poll_once()
    drain(bus)
    mgr.hook_event(hook_payload(sid))
    assert mgr.sessions[sid].hook_hint is not None
    mgr.poll_once()
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.AWAITING_PERMISSION]
    notices = [e for e in ev if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].speak
    assert "Claude needs permission to run a Bash command" in notices[0].text
    assert "last lines" in notices[0].text
    assert mgr.sessions[sid].hook_hint is None  # spent after two polls
    for _ in range(3):
        mgr.poll_once()
    assert drain(bus) == []
    assert mgr.state_of(sid) is SessionState.AWAITING_PERMISSION
    # When the regex finally sees the prompt the card appears and the state holds.
    tmux.set_screen(target, lines_of("bash_permission.txt"))
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [] and [type(e) for e in ev] == [PromptDetected]
    assert mgr.sessions[sid].believed_prompt is None


def test_hook_event_when_regex_sees_the_prompt_adds_nothing(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "bash_permission.txt")
    mgr.hook_event(hook_payload(sid))
    mgr.poll_once()
    mgr.poll_once()
    assert [e for e in drain(bus) if isinstance(e, Notice | PromptDetected)] == []
    assert mgr.state_of(sid) is SessionState.AWAITING_PERMISSION


def test_hook_event_ignores_bad_or_unknown_payloads(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    mgr.hook_event({"nope": 1})
    mgr.hook_event(hook_payload("not-a-session"))
    mgr.hook_event(hook_payload(sid, notification_type=None))
    assert mgr.sessions[sid].hook_hint is None
    mgr.hook_event(hook_payload(sid, hook_event_name="Stop", notification_type=""))
    assert mgr.sessions[sid].hook_hint.kind == "stop"
    mgr.poll_once()
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE
    assert [e for e in drain(bus) if isinstance(e, Notice)] == []


def test_hooks_module_helpers():
    settings = hooks.build_hook_settings(8765, "abcdefghijklmnopqrstuvwxyz")
    assert settings["hooks"]["Notification"][0]["matcher"] == discovery.HOOK_MATCHER
    assert hooks.verify_hook_payload(hook_payload("abc"), "secret-secret-123", "secret-secret-123")
    assert not hooks.verify_hook_payload(hook_payload("abc"), "wrong", "secret-secret-123")
    assert not hooks.verify_hook_payload(hook_payload("abc"), "secret-secret-123", "")
    assert not hooks.verify_hook_payload(["list"], "s", "s")
    assert not hooks.verify_hook_payload({"session_id": ""}, "s", "s")
    assert not hooks.verify_hook_payload({"session_id": "x", "hook_event_name": "Notification"}, "s", "s")
    n = hooks.payload_to_notice(hook_payload("abc"))
    assert n.speak and n.session_id == "abc" and "permission" in n.text.lower()
    assert not hooks.payload_to_notice(hook_payload("abc", hook_event_name="Stop")).speak
    assert hooks.hint_for(hook_payload("abc")).kind == "prompt"
    assert hooks.hint_for(hook_payload("abc", notification_type="idle_prompt")).kind == "idle"
    assert hooks.hint_for(hook_payload("abc", hook_event_name="UserPromptSubmit")).kind == "working"
    with pytest.raises(ValueError):
        hooks.build_hook_settings(8765, "short")


# ---- permission mode -------------------------------------------------------------------------


def test_set_permission_mode_cycles_btab_until_target(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    tmux.btab_cycles = True
    sid, target = started(env)
    assert mgr.set_permission_mode(sid, "acceptEdits") is True
    assert tmux.keys() == ["BTab"]
    assert mgr.sessions[sid].permission_mode == "acceptEdits"
    tmux.calls.clear()
    assert mgr.set_permission_mode(sid, "default") is True
    assert tmux.keys() == ["BTab", "BTab"]
    tmux.calls.clear()
    assert mgr.set_permission_mode(sid, "default") is True
    assert tmux.keys() == []


def test_set_permission_mode_refuses_bypass_and_steps_past_it(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    tmux.btab_cycles = True
    tmux.bypass_in_cycle = True
    sid, target = started(env, with_mode(lines_of("idle.txt"), "plan"))
    with pytest.raises(ValueError):
        mgr.set_permission_mode(sid, "bypassPermissions")
    with pytest.raises(ValueError):
        mgr.set_permission_mode(sid, "bypass permissions")
    assert tmux.keys() == []
    # plan -> bypass (stepped past at once) -> manual
    assert mgr.set_permission_mode(sid, "default") is True
    assert tmux.keys() == ["BTab", "BTab"]
    assert mgr.sessions[sid].permission_mode == "default"


def test_set_permission_mode_refuses_while_a_prompt_is_up(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    tmux.btab_cycles = True
    sid, target = started(env, "plan_approval.txt")
    assert mgr.set_permission_mode(sid, "acceptEdits") is False
    assert tmux.keys() == []  # Shift+Tab on a plan prompt would approve it


def test_set_permission_mode_unreachable_target_returns_to_the_start_mode(env, monkeypatch):
    """CONC-1: a full cycle without the target stops at the starting mode, never two steps away."""
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    tmux.btab_cycles = True
    sid, target = started(env)
    assert mgr.set_permission_mode(sid, "auto") is False
    # manual -> accept edits -> plan -> manual: back where it started after one cycle.
    assert tmux.keys() == ["BTab"] * len(FakeTmux.MODE_CYCLE)
    assert len(tmux.keys()) <= M.MAX_BTAB_PRESSES
    assert mgr.sessions[sid].permission_mode == "default"
    assert any("manual mode on" in line for line in tmux.capture(target))
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert len(notices) == 1 and "auto" in notices[0].text and "back in default mode" in notices[0].text


def test_set_permission_mode_gives_up_when_bypass_keeps_showing(env, monkeypatch):
    """CONC-1: a status row stuck on bypass must not spin the session thread forever."""
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    sid, target = started(env)
    stuck = with_mode(lines_of("idle.txt"), "bypass permissions")
    tmux.set_screen(target, stuck)  # every capture after a press keeps saying bypass
    mgr.poll_once()
    drain(bus)
    tmux.calls.clear()
    assert mgr.set_permission_mode(sid, "plan") is False
    assert 1 <= len(tmux.keys()) <= M.MAX_BYPASS_STEPS + 1
    assert set(tmux.keys()) == {"BTab"}
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].speak and "couldn't switch to plan mode" in notices[0].text
    # The thread is not lost: polling and commands still work.
    mgr.poll_once()
    assert mgr.state_of(sid) is not None


def test_set_permission_mode_stops_when_the_status_row_does_not_react(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "BTAB_SETTLE", 0.0)
    sid, target = started(env)  # manual mode, and BTab changes nothing (btab_cycles False)
    assert mgr.set_permission_mode(sid, "plan") is False
    assert tmux.keys() == ["BTab"] * M.MAX_STUCK_READS
    assert mgr.sessions[sid].permission_mode == "default"
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert len(notices) == 1 and "status row did not change" in notices[0].text


# ---- resume / discovery -----------------------------------------------------------------------


def test_resume_opens_window_with_resume_command(env):
    mgr, bus, tmux, clock, proj = env
    sid = fs.sid(7)
    fs.write_session(mgr.claude_home, str(proj), sid, [fs.user_prompt(sid, str(proj), time.time() - 60, "hello")])
    mgr.resume(sid, "acceptEdits")
    target, name, cwd, command = tmux.windows[0]
    command = claude_argv(command)
    assert command[:3] == ["claude", "--resume", sid]
    assert "--permission-mode" in command and "acceptEdits" in command
    assert cwd == str(proj)
    assert mgr.state_of(sid) is SessionState.WORKING
    assert mgr.sessions[sid].jsonl_from_start is False
    tmux.set_screen(target, lines_of("idle.txt"))
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE
    # Resuming a live attached session is a no-op (focus only).
    mgr.resume(sid)
    assert len(tmux.windows) == 1


def test_resume_unknown_session_raises(env):
    mgr, bus, tmux, clock, proj = env
    with pytest.raises(UnknownSession):
        mgr.resume(fs.sid(9))
    assert tmux.windows == []


def test_resume_refuses_a_session_running_elsewhere(env):
    mgr, bus, tmux, clock, proj = env
    sid = fs.sid(3)
    fs.write_session(mgr.claude_home, str(proj), sid, [fs.user_prompt(sid, str(proj), time.time() - 60, "hi")])
    fs.write_registry_entry(mgr.claude_home, os.getpid(), sid, str(proj), status="idle")
    with pytest.raises(SessionBusy):
        mgr.resume(sid)
    assert tmux.windows == []


def test_resume_attaches_to_a_running_registry_pane(env):
    mgr, bus, tmux, clock, proj = env
    sid = fs.sid(4)
    fs.write_session(mgr.claude_home, str(proj), sid, [fs.user_prompt(sid, str(proj), time.time() - 60, "hi")])
    target = "other:@0.%0"
    tmux.set_screen(target, lines_of("idle.txt"))
    fs.write_registry_entry(mgr.claude_home, os.getpid(), sid, str(proj), status="idle", tmux=target)
    mgr.resume(sid)
    assert tmux.windows == []  # no new pane
    s = mgr.sessions[sid]
    assert s.target == target and s.owned is False and s.attached
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE
    mgr.delete(sid)
    assert ("kill_window", target) in tmux.calls  # delete is explicit: the user confirmed


def test_resume_after_exit_replaces_the_dead_pane(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    fs.write_session(mgr.claude_home, str(proj), sid, [fs.user_prompt(sid, str(proj), time.time() - 60, "hi")])
    tmux.set_screen(target, lines_of("exit.txt"), alt=False)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.DETACHED
    drain(bus)
    mgr.resume(sid)
    assert ("kill_window", target) in tmux.calls
    new_target = mgr.sessions[sid].target
    assert new_target != target
    assert claude_argv(tmux.windows[-1][3])[:3] == ["claude", "--resume", sid]
    assert states(drain(bus)) == [SessionState.WORKING]


def test_list_sessions_merges_live_and_store(env):
    mgr, bus, tmux, clock, proj = env
    other = fs.sid(5)
    fs.write_session(mgr.claude_home, "/some/where", other, [fs.user_prompt(other, "/some/where", time.time() - 3600, "write docs")])
    sid, target = started(env)
    rows = mgr.list_sessions()
    by_id = {r.session_id: r for r in rows}
    assert by_id[sid].attached and by_id[sid].running and by_id[sid].state is SessionState.IDLE
    assert by_id[sid].directory == str(proj) and by_id[sid].title == proj.name
    assert by_id[other].attached is False and by_id[other].state is SessionState.DETACHED
    assert by_id[other].directory == "/some/where" and by_id[other].title == "write docs"
    assert rows[0].session_id == sid  # attached first


# ---- thread -----------------------------------------------------------------------------------


def test_timed_out_command_is_cancelled_and_never_runs_later(env, monkeypatch):
    """CONC-7: a command the thread did not reach in time must not type into the pane later."""
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    monkeypatch.setattr(M, "COMMAND_TIMEOUT", 0.2)
    import threading

    release = threading.Event()
    orig_capture = tmux.capture

    def slow_capture(target_: str, *a, **k):
        release.wait(2.0)  # the session thread is stuck in a poll
        return orig_capture(target_, *a, **k)

    tmux.capture = slow_capture  # type: ignore[method-assign]
    mgr.start_thread()
    try:
        deadline = time.time() + 2
        while mgr.polls < 2 and time.time() < deadline:
            time.sleep(0.01)  # the thread is inside the slow capture now
        t0 = time.monotonic()
        with pytest.raises(M.CommandTimeout) as info:
            mgr.send_text(sid, "do the thing", )
        assert time.monotonic() - t0 < 1.5
        assert info.value.executed is False
        assert "dropped" in str(info.value)
        release.set()
        time.sleep(0.4)  # the thread drains the queue: the cancelled command is skipped
        assert not any(c[0] in ("literal", "enter") for c in tmux.calls)
        notices = [e for e in drain(bus) if isinstance(e, Notice) and "dropped" in e.text]
        assert notices and notices[0].speak
        # The thread is healthy: a fresh command goes through.
        mgr.send_text(sid, "second")
        assert ("literal", target, "second") in tmux.calls
    finally:
        release.set()
        mgr.stop()


def test_timed_out_running_command_reports_it_may_still_execute(env, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    monkeypatch.setattr(M, "COMMAND_TIMEOUT", 0.2)
    import threading

    release = threading.Event()
    orig = tmux.send_literal

    def slow_literal(target_: str, text: str) -> None:
        release.wait(2.0)
        orig(target_, text)

    tmux.send_literal = slow_literal  # type: ignore[method-assign]
    mgr.start_thread()
    try:
        with pytest.raises(M.CommandTimeout) as info:
            mgr.send_text(sid, "slow one")
        assert info.value.executed is None
        assert "may still" in str(info.value)
        release.set()
        deadline = time.time() + 2
        while ("literal", target, "slow one") not in tmux.calls and time.time() < deadline:
            time.sleep(0.01)
        assert ("literal", target, "slow one") in tmux.calls  # it did run, as the message warned
    finally:
        release.set()
        mgr.stop()


def test_permission_summary_waits_for_the_observed_mode(env, monkeypatch):
    """RR-2: the summary names the mode the pane really shows, never an assumed default."""
    mgr, bus, tmux, clock, proj = env
    monkeypatch.setattr(M, "MODE_OBSERVE_TIMEOUT", 0.6)
    sid = mgr.start(str(proj))
    target = mgr.sessions[sid].target
    assert mgr.sessions[sid].permission_mode is None
    # Not running: nothing to wait for, and no mode is invented.
    sentence = mgr.permission_summary(sid)
    assert sentence.startswith("I can't tell which permission mode")
    assert "default mode" not in sentence
    # Running: the status row shows auto a moment later and the summary says so.
    import threading

    tmux.set_screen(target, ["", "no status row yet"], alt=True)
    mgr.start_thread()
    try:
        threading.Timer(0.15, lambda: tmux.set_screen(target, with_mode(lines_of("idle.txt"), "auto"))).start()
        t0 = time.monotonic()
        sentence = mgr.permission_summary(sid)
        assert time.monotonic() - t0 < 0.6
        assert sentence.startswith("This session is in auto mode")
        # Never observed within the timeout: honest sentence, no guess.
        sid2 = mgr.start(str(proj))
        tmux.set_screen(mgr.sessions[sid2].target, ["", "nothing readable"], alt=True)
        t0 = time.monotonic()
        sentence = mgr.permission_summary(sid2)
        assert 0.5 <= time.monotonic() - t0 < 1.5
        assert sentence.startswith("I can't tell which permission mode")
    finally:
        mgr.stop()


def test_commands_run_on_the_session_thread(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    import threading

    seen: dict[str, Any] = {}
    orig = tmux.send_literal

    def record(target_: str, text: str) -> None:
        seen["thread"] = threading.current_thread().name
        orig(target_, text)

    tmux.send_literal = record  # type: ignore[method-assign]
    mgr.start_thread()
    assert mgr.is_alive()
    try:
        mgr.send_text(sid, "from another thread")
        assert seen["thread"] == "SessionThread"
        assert ("literal", target, "from another thread") in tmux.calls
        with pytest.raises(UnknownSession):
            mgr.send_text("missing", "x")
        deadline = time.time() + 2
        while mgr.polls < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert mgr.polls >= 3
    finally:
        mgr.stop()
    assert not mgr.is_alive()


def test_quiet_input_box_becomes_idle_without_a_completion_row(env):
    """showTurnDuration off / unknown wording: a quiet input box with unchanged content
    for a few seconds is idle, but a believed prompt is never overridden."""
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env, "working_no_spinner.txt")
    assert mgr.state_of(sid) is SessionState.WORKING
    for _ in range(QUIET_IDLE_POLLS - 1):
        clock.advance(0.1)
        mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.WORKING
    clock.advance(0.1)
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE


# ---- agent adapters: attach to an existing pane --------------------------------------------------


GENERIC_YN = ["$ deploy-tool run", "About to push 3 commits to origin/main.", "Proceed? (y/n) "]
GENERIC_MENU = ["Allow network access for the build?", "❯ 1. Yes", "  2. Yes, always allow", "  3. No"]
GENERIC_IDLE = ["Build finished in 4.2 s.", "> "]


def attach_generic(env, screen: list[str], *, target: str = "work:@3.%7", agent: str = "generic") -> str:
    """Attach the manager to a scripted plain-screen pane showing ``screen``."""
    mgr, bus, tmux, clock, proj = env
    tmux.set_screen(target, screen, alt=False)
    tmux.cwds = {target: str(proj)}
    sid = mgr.attach(target, agent)
    drain(bus)
    drain_lines(bus)
    tmux.calls.clear()
    return sid


def test_manager_builds_every_importable_adapter_and_binds_homes(env):
    mgr, bus, tmux, clock, proj = env
    assert "claude-code" in mgr.adapters and "generic" in mgr.adapters
    assert mgr.default_agent == "claude-code"
    claude = mgr.adapters["claude-code"]
    assert claude.claude_home == mgr.claude_home and claude.tmux is tmux  # type: ignore[attr-defined]
    assert mgr.adapter_for(None) is claude and mgr.adapter_for(" GENERIC ") is mgr.adapters["generic"]
    with pytest.raises(M.SessionError):
        mgr.adapter_for("vim")
    sid = mgr.start(str(proj))
    assert mgr.sessions[sid].agent == "claude-code" and mgr.sessions[sid].adapter is claude
    with pytest.raises(M.SessionError):
        mgr.start(str(proj), agent="generic")  # the generic adapter cannot launch anything
    with pytest.raises(M.SessionError):
        mgr.start(str(proj), agent="vim")


def test_attach_follows_a_pane_and_detects_an_inline_yn_prompt(env):
    mgr, bus, tmux, clock, proj = env
    FakeTmux.pane_cwd = lambda self, target: getattr(self, "cwds", {}).get(target)  # type: ignore[attr-defined]
    try:
        sid = attach_generic(env, GENERIC_YN)
    finally:
        del FakeTmux.pane_cwd  # type: ignore[attr-defined]
    s = mgr.sessions[sid]
    assert discovery.UUID_RE.match(sid)
    assert s.agent == "generic" and s.owned is False and s.target == "work:@3.%7"
    assert s.cwd == str(proj)  # derived from the pane's current path
    assert s.settings_paths == [] and s.transcript is None
    assert tmux.windows == []  # nothing was launched
    assert mgr.focused() == sid
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.AWAITING_PERMISSION]
    prompt = next(e for e in ev if isinstance(e, PromptDetected))
    assert prompt.kind is PromptKind.PERMISSION and prompt.title == "Proceed?"
    assert prompt.options == ["Yes", "No"]
    assert mgr.current_match(sid).extra["inline_yn"] == "1"
    # approve: an inline (y/n) prompt gets a literal "y" and a separate Enter, never Down/Enter
    assert mgr.approve(sid) is True
    assert tmux.calls == [("literal", "work:@3.%7", "y"), ("enter", "work:@3.%7")]
    tmux.calls.clear()
    assert mgr.deny(sid) is True
    assert tmux.calls == [("literal", "work:@3.%7", "n"), ("enter", "work:@3.%7")]
    rows = mgr.list_sessions()
    row = next(r for r in rows if r.session_id == sid)
    assert row.agent == "generic" and row.attached and row.running


def test_attach_menu_prompt_uses_pointer_navigation_and_refuses_widening_options(env):
    mgr, bus, tmux, clock, proj = env
    sid = attach_generic(env, GENERIC_MENU)
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev) == [SessionState.AWAITING_PERMISSION]
    m = mgr.current_match(sid)
    assert m is not None and [o.unsafe for o in m.options] == [False, True, False]
    assert mgr.approve(sid) is True
    assert tmux.keys() == ["Enter"]  # pointer already on Yes
    tmux.calls.clear()
    assert mgr.deny(sid) is True
    assert tmux.keys() == ["Down", "Down", "Enter"]
    # a menu whose only yes widens permissions is never approved
    tmux.set_screen("work:@3.%7", ["Allow?", "❯ 1. Yes, always allow", "  2. No"], alt=False)
    mgr.poll_once()
    drain(bus)
    tmux.calls.clear()
    assert mgr.approve(sid) is False
    assert tmux.calls == []
    # lettered menus get the letter typed, with no Enter
    tmux.set_screen("work:@3.%7", ["Apply the patch?", "[a] Approve", "[d] Deny"], alt=False)
    mgr.poll_once()
    drain(bus)
    tmux.calls.clear()
    assert mgr.approve(sid) is True
    assert tmux.calls == [("literal", "work:@3.%7", "a")]


def test_attached_pane_idle_text_exit_and_mode_switch(env):
    mgr, bus, tmux, clock, proj = env
    sid = attach_generic(env, GENERIC_IDLE)
    target = "work:@3.%7"
    mgr.poll_once()
    assert mgr.state_of(sid) is SessionState.IDLE
    assert drain_lines(bus) == []  # what was on the pane before the attach is history, not speech
    tmux.set_screen(target, GENERIC_IDLE[:-1] + ["Tests: 12 passed.", "> "], alt=False)
    mgr.poll_once()
    assert [ln.text for ln in drain_lines(bus)] == ["Tests: 12 passed."]  # new prose comes from the pane
    # keystrokes are allowed on the normal screen for a plain-screen agent
    mgr.send_text(sid, "run the tests")
    assert ("literal", target, "run the tests") in tmux.calls and ("enter", target) in tmux.calls
    # no permission modes to switch: refused with a spoken notice, no keys sent
    tmux.calls.clear()
    drain(bus)
    assert mgr.set_permission_mode(sid, "plan") is False
    notices = [e for e in drain(bus) if isinstance(e, Notice)]
    assert notices and "no permission modes" in notices[0].text and tmux.keys() == []
    assert "permission settings" in mgr.permission_summary(sid)
    # the agent exits: a shell prompt at the bottom for EXIT_CONFIRM_POLLS polls
    tmux.set_screen(target, ["Build finished in 4.2 s.", "user@host:~/proj$ "], alt=False)
    drain(bus)
    mgr.poll_once()
    assert mgr.state_of(sid) is not SessionState.DETACHED  # one poll is not enough
    mgr.poll_once()
    ev = drain(bus)
    assert states(ev)[-1] is SessionState.DETACHED
    assert "the agent exited" in mgr.sessions[sid].detail.lower()
    notice = next(e for e in ev if isinstance(e, Notice))
    assert "attach to the pane again" in notice.text.lower() and "resume" not in notice.text.lower()
    with pytest.raises(M.SessionError):
        mgr.send_text(sid, "hello?")
    # resume is not a thing for the generic adapter
    tmux.set_screen(target, GENERIC_IDLE, alt=False)
    with pytest.raises(M.SessionError):
        mgr.resume(sid)


def test_attach_validates_the_target_and_reuses_an_existing_session(env):
    mgr, bus, tmux, clock, proj = env
    with pytest.raises(UnknownSession):
        mgr.attach("nowhere:@9.%9", "generic")
    with pytest.raises(M.SessionError):
        mgr.attach("", "generic")
    tmux.set_screen("work:@1.%1", GENERIC_IDLE, alt=False)
    with pytest.raises(M.SessionError):
        mgr.attach("work:@1.%1", "vim")
    a = mgr.attach("work:@1.%1", "generic", cwd=str(proj))
    b = mgr.attach("work:@1.%1", "generic")
    assert a == b and mgr.sessions[a].cwd == str(proj)
    # the Claude Code adapter can follow a pane too; it then polls like a started session
    tmux.set_screen("work:@2.%2", lines_of("idle.txt"), alt=True)
    c = mgr.attach("work:@2.%2", "claude-code", cwd=str(proj))
    assert mgr.sessions[c].agent == "claude-code" and mgr.sessions[c].owned is False
    mgr.poll_once()
    assert mgr.state_of(c) is SessionState.IDLE
    assert mgr.sessions[c].permission_mode == "default"


def test_prompt_and_stall_wording_names_the_agent(env):
    mgr, bus, tmux, clock, proj = env
    assert M.STALL_TEXT.startswith("Claude Code") and M.NO_TUI_TEXT.startswith("Claude Code")
    assert M.stall_text("Codex").startswith("Codex looks like")
    sid = attach_generic(env, ["⠋ building..."])
    mgr.poll_once()
    clock.advance(float(mgr.config.voice.idle_watchdog_seconds) + 1)
    mgr.poll_once()
    ev = drain(bus)
    assert SessionState.STALLED in states(ev) and mgr.state_of(sid) is SessionState.STALLED
    stall = next(e for e in ev if isinstance(e, Notice))
    assert stall.text.startswith("the agent looks like it is waiting on something")
