"""Headless Claude Code (decision 0020): stream-json events, the MCP permission tool and
the manager's process-backed sessions, driven by ``tests/fake_claude_headless.py``."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests import fixtures_store as fs
from tests.test_manager import Clock, FakeTmux, drain
from zordon import mcp_permission as M
from zordon.agents import headless as H
from zordon.bus import Bus, PromptDetected, SessionState, StateChanged
from zordon.config import Config
from zordon.session import discovery
from zordon.session.manager import SessionManager

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_claude_headless.py"
SID = "0a0a0a0a-0000-4000-8000-00000000000a"

# ---- stream-json -> pane lines --------------------------------------------------------------


def test_translate_events():
    events = [
        {"type": "system", "subtype": "init", "session_id": SID, "permissionMode": "default", "tools": []},
        {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Working on it."}], "stop_reason": None}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}], "stop_reason": None}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py\nb.py", "is_error": False}]}},
        {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "system", "subtype": "commands_changed"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": SID, "stop_reason": "end_turn"},
        {"type": "_exited", "code": 0},
    ]
    got = H.translate(events, SID, now=5.0)
    assert got.session_id == SID and got.permission_mode == "default" and got.turn_ended and got.exited == 0 and got.turn_error is None
    blocks = [(ln.block, ln.text) for ln in got.lines]
    assert blocks[0] == ("text", "Working on it.")
    assert blocks[1][0] == "tool_use" and got.lines[1].meta["name"] == "Bash"
    assert blocks[2][0] == "tool_result" and blocks[2][1].startswith("a.py")
    assert blocks[-1][0] == "turn_end" and all(ln.source == "jsonl" and ln.session_id == SID for ln in got.lines)
    err = H.translate([{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}], SID)
    assert err.turn_ended and err.turn_error == "boom"


# ---- the MCP permission tool ------------------------------------------------------------------


def test_decision_translation_and_secret_file(tmp_path: Path):
    assert M.decision_to_result({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}) == {"behavior": "allow"}
    upd = M.decision_to_result({"hookSpecificOutput": {"decision": {"behavior": "allow", "updatedInput": {"answers": {"q": "a"}}}}})
    assert upd == {"behavior": "allow", "updatedInput": {"answers": {"q": "a"}}}
    assert M.decision_to_result({"hookSpecificOutput": {"decision": {"behavior": "deny", "message": "nope"}}}) == {"behavior": "deny", "message": "nope"}
    assert M.decision_to_result({}) == {"behavior": "deny", "message": M.NO_ANSWER}  # no opinion is never an allow
    assert M.decision_to_result("garbage")["behavior"] == "deny"
    rc = tmp_path / "s.curlrc"
    rc.write_text(discovery.hook_curl_config_text("s3cr3t-s3cr3t-s3cr3t"))
    assert M.read_secret(rc) == "s3cr3t-s3cr3t-s3cr3t"
    bare = tmp_path / "hook.secret"
    bare.write_text("bare-secret-value-123\n")
    assert M.read_secret(bare) == "bare-secret-value-123"


def test_mcp_server_speaks_jsonrpc_over_pipes():
    seen: list[dict[str, Any]] = []

    def ask(payload):
        seen.append(payload)
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "The user said no."}}}

    inp = io.StringIO(
        "\n".join(
            json.dumps(m)
            for m in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2.1.288"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "permission", "arguments": {"tool_name": "Write", "input": {"file_path": "/tmp/x", "content": "hi"}, "tool_use_id": "t"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "other", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 5, "method": "nope"},
            ]
        )
        + "\nnot json at all\n"
    )
    out = io.StringIO()
    M.PermissionServer(ask, session_id=SID, cwd="/home/u/proj", permission_mode="default", inp=inp, out=out).serve()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "zordon" and replies[0]["result"]["capabilities"] == {"tools": {}}
    assert replies[1]["result"]["tools"][0]["name"] == "permission"
    assert json.loads(replies[2]["result"]["content"][0]["text"]) == {"behavior": "deny", "message": "The user said no."}
    assert replies[3]["error"]["code"] == -32602 and replies[4]["error"]["code"] == -32601 and replies[5]["error"]["code"] == -32700
    assert seen == [{"hook_event_name": "PermissionRequest", "session_id": SID, "cwd": "/home/u/proj", "tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "hi"}, "permission_mode": "default"}]


def test_mcp_config_shape(tmp_path: Path):
    cfg = M.mcp_config(zordon_argv=["/usr/bin/python", "-m", "zordon"], port=8765, host="127.0.0.1", secret_file=tmp_path / "s", session_id=SID, cwd="/p", permission_mode="auto")
    srv = cfg["mcpServers"]["zordon"]
    assert srv["command"] == "/usr/bin/python" and srv["args"] == ["-m", "zordon", "mcp-permission"]
    assert srv["env"]["ZORDON_SESSION_ID"] == SID and srv["env"]["ZORDON_PERMISSION_MODE"] == "auto" and srv["env"]["ZORDON_HOOK_PORT"] == "8765"
    assert M.PERMISSION_TOOL == "mcp__zordon__permission"


# ---- the adapter's command line -----------------------------------------------------------------


def test_headless_command_line(tmp_path: Path):
    a = H.HeadlessAdapter(None)
    argv = a.command(SID, resume=False, permission_mode="auto", settings_path=tmp_path / "h.json", mcp_config=tmp_path / "m.json", system_prompt="speak plainly")
    assert argv[:7] == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__zordon__permission" and "--strict-mcp-config" in argv
    assert argv[argv.index("--session-id") + 1] == SID and argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--append-system-prompt") + 1] == "speak plainly"
    resumed = a.command(SID, resume=True, permission_mode="default", settings_path=None, mcp_config=tmp_path / "m.json")
    assert "--resume" in resumed and "--session-id" not in resumed and "--settings" not in resumed
    with pytest.raises(ValueError):
        a.command(SID, resume=False, permission_mode="bypassPermissions", settings_path=None, mcp_config=tmp_path / "m.json")
    assert "bypassPermissions" in a.command(SID, resume=False, permission_mode="bypassPermissions", settings_path=None, mcp_config=tmp_path / "m.json", allow_bypass=True)


# ---- the manager with a fake process -------------------------------------------------------------


class _HookRelay:
    """A loopback HTTP server standing in for Zordon's transport: forwards /hooks/permission
    to the manager, the way server.py does, so the real `zordon mcp-permission` subprocess
    launched by the fake claude reaches the manager."""

    def __init__(self, mgr: SessionManager, secret: str) -> None:
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("content-length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if self.headers.get("X-Zordon-Hook-Secret") != secret:
                    self.send_response(403)
                    self.end_headers()
                    return
                answer = mgr.permission_request(payload, timeout_s=20.0) if self.path == "/hooks/permission" else {}
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        relay.mgr = mgr

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def headless_env(tmp_path: Path, monkeypatch):
    bus = Bus()
    cfg = Config()
    cfg.output.poll_interval_ms = 10
    tmux = FakeTmux()
    claude_home = fs.make_claude_home(tmp_path)
    zordon_home = tmp_path / "zordon-home"
    secret = "s3cr3t-s3cr3t-s3cr3t"
    holder: dict[str, Any] = {}

    def permission_request(payload, *, timeout_s=870.0):
        return holder["mgr"].permission_request(payload, timeout_s=timeout_s)

    class Relay(_HookRelay):
        pass

    # The relay needs the manager and the manager needs the relay's port: resolve the port first.
    probe = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    mgr = SessionManager(bus, cfg, tmux, claude_home=claude_home, zordon_home=zordon_home, hook_port=port, hook_secret=secret, clock=Clock())  # type: ignore[arg-type]
    holder["mgr"] = mgr
    relay = _HookRelay(mgr, secret)
    mgr.hook_port = relay.port  # the probe port may have been taken; use what the relay got
    adapter = mgr.adapters["claude-headless"]
    monkeypatch.setattr(type(adapter), "available", lambda self: "/usr/bin/claude")
    real_command = type(adapter).command

    def fake_command(self, *a, **kw):
        argv = real_command(self, *a, **kw)
        return [sys.executable, str(FAKE), *argv[1:]]

    monkeypatch.setattr(type(adapter), "command", fake_command)
    proj = tmp_path / "home" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    try:
        yield mgr, bus, proj
    finally:
        for s in list(mgr.sessions.values()):
            if s.headless is not None:
                s.headless.close()
        relay.close()


def _poll_until(mgr: SessionManager, pred, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        mgr.poll_once()
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_headless_project_round_trip(headless_env):
    """create (headless) -> prompt arrives through the MCP tool -> approve by the usual API ->
    the fake proceeds -> spoken lines and idle; close and reopen resumes with --resume."""
    mgr, bus, proj = headless_env
    row = mgr.create_project(str(proj.parent), "", existing=True, permission_mode="default", talk_first=False, runner="headless")
    sid = row["session_id"]
    s = mgr.sessions[sid]
    assert s.headless is not None and s.headless.alive and row["runner"] == "headless" and row["running"]
    assert s.target.startswith("headless:") and s.state is SessionState.IDLE
    assert _poll_until(mgr, lambda: s.permission_mode == "default", 5.0)  # the init record was read
    assert mgr.projects.get(row["id"]).session_id == sid
    drain(bus)

    mgr.send_text(sid, "please say pong")
    assert s.state is SessionState.WORKING
    assert _poll_until(mgr, lambda: s.state is SessionState.IDLE)
    lines = [ln for ln in _drain_lines(bus) if ln.block == "text"]
    assert lines and lines[-1].text == "pong: please say pong"

    # A tool use: the fake launches `zordon mcp-permission`, which posts to the relay, which asks
    # the manager; the prompt is an ordinary hook prompt and approve answers it.
    mgr.send_text(sid, "touch the marker")
    assert _poll_until(mgr, lambda: s.state is SessionState.AWAITING_PERMISSION, 20.0)
    ev = drain(bus)
    prompt = next(e for e in ev if isinstance(e, PromptDetected))
    assert prompt.kind.value == "permission" and prompt.title.startswith("Bash command: touch marker.txt")
    assert mgr.current_match(sid).description == "Create a marker file"
    assert mgr.approve(sid)
    assert _poll_until(mgr, lambda: s.state is SessionState.IDLE, 20.0)
    texts = [ln.text for ln in _drain_lines(bus) if ln.block == "text"]
    assert texts[-1] == "I created the marker file."

    # Compose/submit (deferred submit) and "scratch that" work without a pane.
    mgr.compose(sid, "say")
    mgr.compose(sid, "pong again")
    assert mgr.is_composing(sid) and mgr.submit(sid)
    assert _poll_until(mgr, lambda: s.state is SessionState.IDLE)
    assert [ln.text for ln in _drain_lines(bus) if ln.block == "text"][-1] == "pong: say pong again"
    mgr.compose(sid, "never mind")
    assert mgr.clear_input(sid) and not mgr.is_composing(sid)

    # Pause (admin) keeps it running; detach closes the process; open again resumes it.
    mgr.admin()
    assert mgr.focused() is None and s.headless.alive
    mgr.detach(sid)
    assert s.headless is None and s.state is SessionState.DETACHED
    fs.write_session(mgr.claude_home, str(proj), sid, [fs.user_prompt(sid, str(proj), time.time() - 60, "hello")])  # the agent's own record
    again = mgr.open_project(row["id"])
    assert again["session_id"] == sid and mgr.sessions[sid].headless is not None
    argv = mgr.sessions[sid].headless.argv
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == sid
    mgr.delete(sid)
    assert sid not in mgr.sessions


def test_headless_process_exit_marks_the_session_exited(headless_env):
    mgr, bus, proj = headless_env
    row = mgr.create_project(str(proj.parent), "", existing=True, talk_first=False, runner="headless")
    sid = row["session_id"]
    s = mgr.sessions[sid]
    drain(bus)
    mgr.send_text(sid, "exit now")
    assert _poll_until(mgr, lambda: s.state is SessionState.DETACHED, 10.0)
    assert not s.attached and any(isinstance(e, StateChanged) and e.state is SessionState.DETACHED for e in drain(bus))
    assert mgr.list_projects()[0]["running"] is False


def test_headless_needs_claude_code_and_the_hook_port(headless_env):
    mgr, bus, proj = headless_env
    from zordon.session.manager import SessionError

    with pytest.raises(SessionError, match="Only Claude Code"):
        mgr.create_project(str(proj.parent), "", existing=True, agent="generic", runner="headless")
    mgr.hook_port = None
    with pytest.raises(SessionError, match="hook port"):
        mgr.create_project(str(proj.parent), "", existing=True, runner="headless")


def _drain_lines(bus: Bus):
    out = []
    while True:
        try:
            out.append(bus.pane_lines.get_nowait())
        except Exception:  # noqa: BLE001
            return out


def test_headless_process_ends_with_the_server_and_orphans_are_terminated(headless_env):
    """Seen live: zordon restart left the claude -p process running, and opening the project
    resumed the same conversation in a second process. The pid is recorded on the project,
    stop() closes the process, and a relaunch or a fresh server terminates a leftover first."""

    mgr, bus, proj = headless_env
    row = mgr.create_project(str(proj.parent), "orphan", runner="headless", talk_first=False)
    sid = row["session_id"]
    s = mgr.sessions[sid]
    pid = s.headless.proc.pid
    project = mgr.projects.get(row["id"])
    assert project.headless_pid == pid and s.headless.alive
    # Stop the server: the process goes with it and the pid is cleared.
    mgr.stop()
    assert not s.headless.alive and mgr.projects.get(row["id"]).headless_pid is None

    # A pid left behind by a previous server, still alive and still claude for this session:
    # terminated on the next open (the fake claude carries the session id on its command line).
    import subprocess
    import sys

    from tests.test_headless import FAKE

    stray = subprocess.Popen([sys.executable, str(FAKE), "--session-id", sid, "--input-format", "stream-json"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, start_new_session=True)
    try:
        mgr.projects.update(project, headless_pid=stray.pid, session_id=sid)
        assert mgr.sweep_orphan_headless() == 1
        for _ in range(50):
            if stray.poll() is not None:
                break
            time.sleep(0.1)
        assert stray.poll() is not None and mgr.projects.get(row["id"]).headless_pid is None
        # A pid that is not a claude process is left alone.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True)
        try:
            mgr.projects.update(project, headless_pid=other.pid)
            assert mgr.sweep_orphan_headless() == 0 and other.poll() is None
        finally:
            other.kill()
            other.wait()
    finally:
        if stray.poll() is None:
            stray.kill()
            stray.wait()
