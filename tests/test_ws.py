from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import queue
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from zordon.bus import (
    Flush,
    Notice,
    PromptCleared,
    PromptDetected,
    PromptKind,
    SessionState,
    SpeechChunk,
    StateChanged,
    TranscriptRow,
)
from zordon.health import HealthReport, Item, make_report
from zordon.transport import protocol as P
from zordon.transport import server as S
from zordon.transport import ws as W

from .fake_agent import FakeAgent

TOKEN = "ws-test-token-0123456789abcdef"
FRAME = b"\x01\x00" * 320  # 20 ms of 16 kHz int16


@pytest.fixture
def agent(tmp_path: Path) -> FakeAgent:
    return FakeAgent(tmp_path / "uploads", token=TOKEN)


@pytest.fixture
def client(agent: FakeAgent, tmp_path: Path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Zordon</title>")
    app = S.create_app(agent, static_dir=web)
    with TestClient(app) as c:
        assert c.post("/auth", json={"token": TOKEN}).status_code == 200
        yield c


@contextlib.contextmanager
def connected(client: TestClient, tail_rows: int = 1):
    """Open the socket and consume the opening hello/sessions/transcript burst."""
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        sessions = ws.receive_json()
        tail = [ws.receive_json() for _ in range(tail_rows)]
        yield ws, hello, sessions, tail


def recv_type(ws, wanted: str, tries: int = 20) -> dict:
    """Skip unrelated messages (e.g. broadcast state) until one of type ``wanted`` arrives."""
    for _ in range(tries):
        msg = ws.receive_json()
        if msg["type"] == wanted:
            return msg
    raise AssertionError(f"no {wanted} message received")


def audio_msg(pcm: bytes = FRAME, seq: int = 0) -> dict:
    return {"type": "audio", "pcm": base64.b64encode(pcm).decode(), "seq": seq}


def drain_audio(q: queue.Queue, timeout: float = 2.0) -> list[bytes]:
    out: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            out.append(q.get(timeout=0.05))
        except queue.Empty:
            if out:
                break
    return out


# ---- opening burst ---------------------------------------------------------------------


def test_hello_then_sessions_then_tail(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, hello, sessions, tail):
        assert hello["type"] == "hello"
        assert hello["protocol"] == P.PROTOCOL_VERSION
        assert hello["version"] == agent.version
        assert hello["focused_session"] == "sess-1"
        assert hello["verbosity"] == "minimal" and hello["tool_chatter"] is False
        assert hello["providers"]["tts"] == "kokoro"
        assert hello["tts_sample_rate"] == 24000
        assert hello["tunnel_url"] is None
        assert sessions["type"] == "sessions"
        assert [s["session_id"] for s in sessions["sessions"]] == ["sess-1", "sess-2"]
        assert sessions["sessions"][0]["focused"] is True
        assert sessions["sessions"][1]["focused"] is False
        assert sessions["sessions"][0]["state"] == "idle"
        assert sessions["sessions"][1]["permission_mode"] == "plan"
        assert tail[0]["type"] == "transcript" and tail[0]["kind"] == "spoken"
        assert tail[0]["raw_lines"] == ["⏺ Edited auth.py"]


def test_hello_carries_the_agent_mute_state(client: TestClient, agent: FakeAgent):
    """WEB-7: a fresh client must learn the mute state from hello, not assume unmuted."""
    with connected(client) as (ws, hello, _, _):
        assert hello["muted"] is False
    agent._settings["muted"] = True
    with connected(client) as (ws, hello, _, _):
        assert hello["muted"] is True


def test_hello_never_contains_keys(client: TestClient, agent: FakeAgent):
    agent._settings["providers"]["anthropic_api_key"] = "sk-ant-should-not-leak"
    with connected(client) as (ws, hello, _, _):
        assert "anthropic_api_key" not in hello["providers"]
        assert "sk-ant" not in json.dumps(hello)


# ---- commands --------------------------------------------------------------------------


def test_set_verbosity_returns_settings(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_verbosity", "args": {"level": "technical"}})
        msg = recv_type(ws, "settings")
        assert msg["verbosity"] == "technical"
        assert ("set_verbosity", "technical") in agent.calls

        ws.send_json({"type": "command", "name": "set_verbosity", "args": {"level": "loud"}})
        err = recv_type(ws, "error")
        assert err["code"] == "bad_argument"


def test_mute_and_tool_chatter(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "mute"})
        assert recv_type(ws, "settings")["muted"] is True
        ws.send_json({"type": "command", "name": "unmute"})
        assert recv_type(ws, "settings")["muted"] is False
        ws.send_json({"type": "command", "name": "set_tool_chatter", "args": {"enabled": True}})
        assert recv_type(ws, "settings")["tool_chatter"] is True
        assert ("set_tool_chatter", True) in agent.calls


def test_session_commands_dispatch(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "focus", "args": {"session_id": "sess-2"}})
        msg = recv_type(ws, "sessions")
        assert [s["focused"] for s in msg["sessions"]] == [False, True]

        ws.send_json({"type": "command", "name": "approve"})
        ws.send_json({"type": "command", "name": "deny"})
        ws.send_json({"type": "command", "name": "plan_approve"})
        ws.send_json({"type": "command", "name": "plan_revise", "args": {"text": "smaller steps"}})
        ws.send_json({"type": "command", "name": "plan_deny"})
        ws.send_json({"type": "command", "name": "answer", "args": {"option": 2}})
        ws.send_json({"type": "command", "name": "stop"})
        ws.send_json({"type": "command", "name": "send_text", "args": {"text": "run the tests"}})
        ws.send_json({"type": "command", "name": "repeat"})
        ws.send_json({"type": "command", "name": "status"})
        state = recv_type(ws, "state")
        assert state["session_id"] == "sess-2" and state["state"] == "detached"
        recv_type(ws, "sessions")

    names = agent.sessions.names()
    for expected in (
        ("focus", "sess-2"),
        ("approve", "sess-2"),
        ("deny", "sess-2"),
        ("plan_approve", "sess-2"),
        ("plan_revise", "sess-2", "smaller steps"),
        ("plan_deny", "sess-2"),
        ("answer_question", "sess-2", 2),
        ("send_escape", "sess-2"),
        ("send_text", "sess-2", "run the tests"),
    ):
        assert expected in agent.sessions.calls, (expected, agent.sessions.calls)
    assert ("repeat_last",) in agent.calls
    assert "list_sessions" in names


def test_start_resume_detach_delete(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "start", "args": {"directory": "/tmp/p", "permission_mode": "plan"}})
        recv_type(ws, "sessions")
        ws.send_json({"type": "command", "name": "resume", "args": {"session_id": "sess-2"}})
        recv_type(ws, "sessions")
        ws.send_json({"type": "command", "name": "detach", "args": {"session_id": "sess-2"}})
        recv_type(ws, "sessions")
        # delete without confirm is refused and never reaches the manager
        ws.send_json({"type": "command", "name": "delete", "args": {"session_id": "sess-2"}})
        assert recv_type(ws, "error")["code"] == "confirm_required"
        ws.send_json({"type": "command", "name": "delete", "args": {"session_id": "sess-2", "confirm": True}})
        recv_type(ws, "sessions")
    calls = agent.sessions.calls
    assert ("start", "/tmp/p", "plan") in calls
    assert ("resume", "sess-2", None) in calls
    assert ("detach", "sess-2") in calls
    assert calls.count(("delete", "sess-2")) == 1


def test_permission_mode_bypass_rejected(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_permission_mode", "args": {"mode": "bypassPermissions"}})
        err = recv_type(ws, "error")
        assert err["code"] == "forbidden"
        ws.send_json({"type": "command", "name": "start", "args": {"directory": "/x", "permission_mode": "bypassPermissions"}})
        assert recv_type(ws, "error")["code"] == "forbidden"
        ws.send_json({"type": "command", "name": "set_permission_mode", "args": {"mode": "plan"}})
        assert recv_type(ws, "settings")["type"] == "settings"
    assert not any(c[0] == "start" for c in agent.sessions.calls)
    assert ("set_permission_mode", "sess-1", "plan") in agent.sessions.calls
    assert not any("bypass" in str(c) for c in agent.sessions.calls)


def test_set_provider(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_provider", "args": {"kind": "tts", "name": "openai"}})
        assert recv_type(ws, "settings")["providers"]["tts"] == "openai"
        ws.send_json({"type": "command", "name": "set_provider", "args": {"kind": "keys", "name": "x"}})
        assert recv_type(ws, "error")["code"] == "bad_argument"


def test_no_focused_session_errors(client: TestClient, agent: FakeAgent):
    agent.sessions.focused_id = None
    with connected(client, tail_rows=0) as (ws, hello, _, _):
        assert hello["focused_session"] is None
        ws.send_json({"type": "command", "name": "approve"})
        assert recv_type(ws, "error")["code"] == "no_session"


def test_prompt_action_without_prompt(client: TestClient, agent: FakeAgent):
    agent.sessions.prompt_result = False
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "approve"})
        assert recv_type(ws, "error")["code"] == "no_prompt"


def test_command_exception_becomes_error(client: TestClient, agent: FakeAgent):
    def boom(level: str) -> None:
        raise RuntimeError("kaboom")

    agent.set_verbosity = boom  # type: ignore[assignment]
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_verbosity", "args": {"level": "normal"}})
        err = recv_type(ws, "error")
        assert err["code"] == "command" and "kaboom" in err["message"]
        ws.send_json({"type": "ping", "ts": 1.5})
        assert recv_type(ws, "pong")["ts"] == 1.5


def test_upload_command_size_check(client: TestClient):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "upload", "args": {"name": "a.bin", "size": 10**9}})
        assert recv_type(ws, "error")["code"] == "too_large"


# ---- text, audio, call ----------------------------------------------------------------


def test_text_goes_to_submit_text(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "text", "text": "what did you change"})
        ws.send_json({"type": "ping"})
        recv_type(ws, "pong")
    calls = [c for c in agent.calls if c[0] == "submit_text"]
    assert len(calls) == 1 and calls[0][1] == "what did you change"
    assert isinstance(calls[0][2], str) and calls[0][2]
    utt = agent.bus.utterances.get_nowait()
    assert utt.text == "what did you change" and utt.source == "text"


def test_audio_ignored_before_call_start(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json(audio_msg())
        ws.send_json({"type": "ping"})
        recv_type(ws, "pong")
        assert agent.bus.inbound_audio.empty()


def test_audio_after_call_start_reaches_bus(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "call", "action": "start"})
        ws.send_json(audio_msg(seq=1))
        frames = drain_audio(agent.bus.inbound_audio)
        assert len(frames) == 1 and len(frames[0]) == 640 and frames[0] == FRAME
        ws.send_json({"type": "call", "action": "pause"})
        ws.send_json(audio_msg(seq=2))
        ws.send_json({"type": "ping"})
        recv_type(ws, "pong")
        assert agent.bus.inbound_audio.empty()
    actions = [c[2] for c in agent.calls if c[0] == "call_state"]
    assert actions == ["start", "pause"]


def test_call_ended_when_socket_drops(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "call", "action": "start"})
        ws.send_json({"type": "ping"})
        recv_type(ws, "pong")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        actions = [c[2] for c in agent.calls if c[0] == "call_state"]
        if actions == ["start", "end"]:
            break
        time.sleep(0.02)
    assert actions == ["start", "end"]


def test_inbound_audio_drops_oldest_when_full(client: TestClient, agent: FakeAgent):
    q = agent.bus.inbound_audio
    while True:
        try:
            q.put_nowait(b"old" * 10)
        except queue.Full:
            break
    with connected(client) as (ws, *_):
        ws.send_json({"type": "call", "action": "start"})
        ws.send_json(audio_msg())
        ws.send_json({"type": "ping"})
        recv_type(ws, "pong")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items[-1] == FRAME and len(items) == q.maxsize


# ---- bus -> client -------------------------------------------------------------------


def test_speech_chunk_broadcast(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        pcm = bytes(range(256)) * 4
        agent.bus.publish(SpeechChunk(sentence_id=7, seq=3, generation=2, pcm=pcm, sample_rate=24000, final=True))
        msg = recv_type(ws, "speech")
        assert msg["sentence_id"] == 7 and msg["seq"] == 3 and msg["generation"] == 2
        assert msg["sample_rate"] == 24000 and msg["final"] is True
        assert base64.b64decode(msg["pcm"]) == pcm


def test_flush_and_state_and_prompt_and_notice(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        agent.bus.publish(Flush(generation=2))
        assert recv_type(ws, "flush")["generation"] == 2

        agent.bus.publish(StateChanged("sess-1", SessionState.WORKING, "producing output"))
        st = recv_type(ws, "state")
        assert st["state"] == "working" and st["detail"] == "producing output"

        p = PromptDetected("sess-1", PromptKind.PERMISSION, "Edit auth.py?", ["Yes", "No"], ["raw"])
        agent.bus.publish(p)
        pm = recv_type(ws, "prompt")
        assert pm["kind"] == "permission" and pm["options"] == ["Yes", "No"] and pm["cleared"] is False
        assert pm["prompt_id"] == p.prompt_id

        agent.bus.publish(PromptCleared("sess-1", p.prompt_id))
        pc = recv_type(ws, "prompt")
        assert pc["cleared"] is True and pc["prompt_id"] == p.prompt_id

        agent.bus.publish(TranscriptRow(5, "sess-1", "user", "hello", [], 1.0))
        tr = recv_type(ws, "transcript")
        assert tr["kind"] == "user" and tr["row_id"] == 5

        agent.bus.publish(Notice("STT provider is down", level="error", session_id="sess-1"))
        n = recv_type(ws, "transcript")
        assert n["kind"] == "notice" and n["text"] == "STT provider is down" and n["row_id"] < 0
        e = recv_type(ws, "error")
        assert e["code"] == "notice"

        agent.bus.publish(P.Sessions(sessions=[]))
        assert recv_type(ws, "sessions")["sessions"] == []
        agent.bus.publish({"type": "tunnel", "url": "https://x.trycloudflare.com", "qr_svg": None})
        assert recv_type(ws, "tunnel")["url"] == "https://x.trycloudflare.com"


def _fake_report(status: str = "fail") -> HealthReport:
    return make_report(
        [
            Item("tmux", "ok", "server running"),
            Item("vad", status, "voice input is off: Silero VAD model missing", "zordon doctor --download"),
            Item("hooks", "warn", "curl not found", "install curl"),
        ],
        ts=123.0,
    )


def test_health_sent_after_hello_and_sessions(client: TestClient, agent: FakeAgent):
    """A fresh client learns the health of every part before the transcript tail."""
    agent.health = lambda: _fake_report()  # type: ignore[attr-defined]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "sessions"
        h = ws.receive_json()
        assert h["type"] == "health" and h["status"] == "fail" and h["ts"] == 123.0
        assert [i["key"] for i in h["items"]] == ["tmux", "vad", "hooks"]
        vad = h["items"][1]
        assert vad["label"] == "voice detection" and vad["status"] == "fail"
        assert vad["fix"] == "zordon doctor --download"
        assert ws.receive_json()["type"] == "transcript"
        assert P.HealthOut.model_validate(h)


def test_no_health_message_without_a_probe(client: TestClient, agent: FakeAgent):
    """An agent without ``health()`` (older or minimal) still gets the plain opening burst."""
    with connected(client) as (ws, hello, sessions, tail):
        assert tail[0]["type"] == "transcript"


def test_health_and_update_events_pass_through(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        agent.bus.publish(_fake_report("warn").to_out())
        h = recv_type(ws, "health")
        assert h["status"] == "warn" and h["items"][1]["status"] == "warn"
        agent.bus.publish(P.UpdateOut(current="0.1.0", latest="0.2.0", command="zordon update", notes_url="https://example.com/x"))
        u = recv_type(ws, "update")
        assert u == {"type": "update", "current": "0.1.0", "latest": "0.2.0", "command": "zordon update", "auto": False, "notes_url": "https://example.com/x"}
    assert W.to_outbound(P.UpdateOut(current="1", latest="2", command="zordon update")) == [
        P.UpdateOut(current="1", latest="2", command="zordon update")
    ]


def test_two_clients_both_receive(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws1, *_), connected(client) as (ws2, *_):
        agent.bus.publish(Flush(generation=9))
        assert recv_type(ws1, "flush")["generation"] == 9
        assert recv_type(ws2, "flush")["generation"] == 9


# ---- protocol errors and idle -----------------------------------------------------------


def test_invalid_json_gives_protocol_error(client: TestClient):
    with connected(client) as (ws, *_):
        ws.send_text("{not json")
        err = recv_type(ws, "error")
        assert err["code"] == "protocol"
        ws.send_json({"type": "command", "name": "rm_rf"})
        assert recv_type(ws, "error")["code"] == "protocol"
        ws.send_json({"type": "ping"})
        assert recv_type(ws, "pong")["type"] == "pong"


def test_too_many_protocol_errors_closes_1008(client: TestClient):
    with connected(client) as (ws, *_):
        for _ in range(W.MAX_PROTOCOL_ERRORS):
            ws.send_text("garbage")
        with pytest.raises(WebSocketDisconnect) as ei:
            for _ in range(W.MAX_PROTOCOL_ERRORS + 1):
                ws.receive_json()
        assert ei.value.code == 1008


def test_idle_disconnect(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(W.ClientConnection, "IDLE_SECONDS_OVERRIDE", 0.3)
    with connected(client) as (ws, *_):
        t0 = time.monotonic()
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 1000
        assert ei.value.reason == "idle"
        assert 0.2 <= time.monotonic() - t0 <= 3.0


# ---- pure pieces --------------------------------------------------------------------------


def test_out_queue_drops_speech_only():
    async def run():
        q = W.OutQueue(cap=3)
        q.push("speech", 1, "s1")
        q.push("speech", 1, "s2")
        q.push("state", 0, "st")
        q.push("speech", 1, "s3")  # full: oldest speech (s1) goes
        assert [x[2] for x in q._dq] == ["s2", "st", "s3"]
        q.push("transcript", 0, "t")  # full: s2 goes, control message kept
        assert [x[2] for x in q._dq] == ["st", "s3", "t"]
        q.push("error", 0, "e")  # no speech left to drop: control messages still kept
        assert [x[2] for x in q._dq] == ["st", "t", "e", ] or len(q._dq) == 4
        assert q.dropped >= 2
        first = await q.get()
        assert first == "st"

    asyncio.run(run())


def test_out_queue_flush_purges_old_speech():
    async def run():
        q = W.OutQueue()
        q.push("speech", 1, "old")
        q.push("speech", 2, "current")
        q.push("flush", 2, "flush")
        assert [x[2] for x in q._dq] == ["current", "flush"]

    asyncio.run(run())


def test_to_outbound_unknown_dropped():
    assert W.to_outbound(object()) == []
    assert W.to_outbound({"type": "nope"}) == []


def test_session_summary_from_dict_and_object():
    d = {"session_id": "a", "directory": "/d", "title": "t", "last_active": 5, "attached": True, "running": False, "state": SessionState.WORKING}
    s = W.to_session_summary(d, "a")
    assert s.focused and s.state == "working" and s.last_active == 5.0
    s2 = W.to_session_summary(type("O", (), {"session_id": "b"})(), "a")
    assert not s2.focused and s2.state == "detached" and s2.directory == ""


# ---- SEC-7 / CONC-2: commands run off the event loop ----------------------------------------


def test_slow_command_does_not_stall_the_loop(client: TestClient, agent: FakeAgent):
    """A blocking SessionControl call must not hold the asyncio loop: another client's
    ping is answered while the first client's `start` is still sleeping."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_start(directory: str, permission_mode=None) -> str:
        started.set()
        release.wait(5.0)
        return "sess-new"

    agent.sessions.start = slow_start  # type: ignore[method-assign]
    with connected(client) as (a, *_), connected(client) as (b, *_):
        a.send_json({"type": "command", "name": "start", "args": {"directory": "/tmp/x"}})
        assert started.wait(2.0)
        t0 = time.monotonic()
        b.send_json({"type": "ping", "ts": 7.0})
        assert recv_type(b, "pong")["ts"] == 7.0
        assert time.monotonic() - t0 < 1.0
        # the first client's own ping is answered too: its receive loop is not blocked
        a.send_json({"type": "ping", "ts": 8.0})
        assert recv_type(a, "pong")["ts"] == 8.0
        release.set()
        assert recv_type(a, "sessions")["type"] == "sessions"


def test_command_timeout_replies_error(client: TestClient, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(W, "COMMAND_TIMEOUT_S", 0.2)

    def hang(session_id: str) -> None:
        release.wait(5.0)

    agent.sessions.focus = hang  # type: ignore[method-assign]
    try:
        with connected(client) as (ws, *_):
            ws.send_json({"type": "command", "name": "focus", "args": {"session_id": "sess-2"}})
            err = recv_type(ws, "error")
            assert err["code"] == "timeout"
            assert "focus" in err["message"]
            # the connection is still usable afterwards
            ws.send_json({"type": "ping", "ts": 1.0})
            assert recv_type(ws, "pong")["ts"] == 1.0
    finally:
        release.set()


def test_commands_of_one_connection_run_in_order(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        for level in ("normal", "technical", "minimal"):
            ws.send_json({"type": "command", "name": "set_verbosity", "args": {"level": level}})
        seen = [recv_type(ws, "settings")["verbosity"] for _ in range(3)]
        assert seen == ["normal", "technical", "minimal"]
    assert [c[1] for c in agent.calls if c[0] == "set_verbosity"] == ["normal", "technical", "minimal"]


# ---- WEB-6: keepalive pings do not reset the idle disconnect -----------------------------------


def test_ping_does_not_defeat_idle_disconnect(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(W.ClientConnection, "IDLE_SECONDS_OVERRIDE", 0.6)
    with connected(client) as (ws, *_):
        t0 = time.monotonic()
        closed = False
        try:
            # ping faster than the idle timeout for longer than the idle timeout
            for _ in range(8):
                ws.send_json({"type": "ping", "ts": 1.0})
                msg = ws.receive_json()
                assert msg["type"] == "pong"
                time.sleep(0.15)
            ws.receive_json()
        except WebSocketDisconnect as e:
            closed = True
            assert e.code == 1000 and e.reason == "idle"
        assert closed, "pings alone kept the connection open"
        assert 0.5 <= time.monotonic() - t0 <= 3.0


def test_real_activity_resets_idle_timer(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(W.ClientConnection, "IDLE_SECONDS_OVERRIDE", 0.6)
    with connected(client) as (ws, *_):
        for _ in range(4):
            time.sleep(0.3)
            ws.send_json({"type": "flush_ack", "generation": 1})
        # 1.2 s of wall time with activity every 0.3 s: still connected
        ws.send_json({"type": "ping", "ts": 2.0})
        assert recv_type(ws, "pong")["ts"] == 2.0


# ---- SEC-10: control / bidi characters never reach the pane ----------------------------------


def test_text_and_send_text_are_sanitized(client: TestClient, agent: FakeAgent):
    from zordon.transport.sanitize import sanitize_keystrokes

    dirty = "A\u009b31mB‮text​\x1b[31m\x7fC⁦D﻿"
    assert sanitize_keystrokes(dirty) == "A31mBtext[31mCD"
    assert sanitize_keystrokes("tabs\tand\nnewlines stay") == "tabs\tand\nnewlines stay"
    with connected(client) as (ws, *_):
        ws.send_json({"type": "text", "text": dirty})
        ws.send_json({"type": "command", "name": "send_text", "args": {"text": dirty}})
        ws.send_json({"type": "command", "name": "plan_revise", "args": {"text": "ok‮"}})
        ws.send_json({"type": "command", "name": "answer", "args": {"option": "two​"}})
        # only control characters: nothing is sent, the argument is invalid
        ws.send_json({"type": "command", "name": "send_text", "args": {"text": "​\u009b"}})
        assert recv_type(ws, "error")["code"] == "bad_argument"
        ws.send_json({"type": "ping", "ts": 3.0})
        recv_type(ws, "pong")
    assert ("submit_text", "A31mBtext[31mCD", agent.calls[0][2]) == agent.calls[0]
    assert ("send_text", "sess-1", "A31mBtext[31mCD") in agent.sessions.calls
    assert ("plan_revise", "sess-1", "ok") in agent.sessions.calls
    assert ("answer_question", "sess-1", "two") in agent.sessions.calls
    assert len([c for c in agent.sessions.calls if c[0] == "send_text"]) == 1


# ---- WEB-2: the voice selector ---------------------------------------------------------------


def test_set_provider_voice_uses_agent_set_voice(client: TestClient, agent: FakeAgent):
    def set_voice(name: str) -> None:
        agent.calls.append(("set_voice", name))
        agent._settings["providers"]["voice"] = name

    agent.set_voice = set_voice  # type: ignore[attr-defined]
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_provider", "args": {"kind": "voice", "name": "am_adam"}})
        assert recv_type(ws, "settings")["providers"]["voice"] == "am_adam"
    assert ("set_voice", "am_adam") in agent.calls


def test_set_provider_voice_without_agent_support_is_a_clear_error(client: TestClient, agent: FakeAgent):
    def set_provider(kind: str, name: str) -> None:
        if kind not in W.PROVIDER_KINDS:
            raise ValueError("kind must be one of stt, tts, normalizer, router")
        agent._settings["providers"][kind] = name

    agent.set_provider = set_provider  # type: ignore[method-assign]
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "set_provider", "args": {"kind": "voice", "name": "am_adam"}})
        err = recv_type(ws, "error")
        assert err["code"] == "unsupported"
        assert "voice" in err["message"]


# ---- agent adapters ------------------------------------------------------------------------------


def test_hello_lists_agents_and_sessions_carry_the_agent(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, hello, sessions, _):
        assert set(hello["agents"]) >= {"claude-code", "generic"}
        assert all(isinstance(v, bool) for v in hello["agents"].values())
        assert hello["agents"]["generic"] is True  # needs no binary
        assert hello["default_agent"] == "claude-code"
        assert all(s["agent"] == "claude-code" for s in sessions["sessions"])  # FakeSessionInfo has no agent field
    agent.available_agents = lambda: {"claude-code": None, "codex": "/usr/bin/codex", "generic": ""}  # type: ignore[attr-defined]
    agent.config.providers.agent = "codex"
    agent.sessions.infos[0].agent = "generic"  # type: ignore[attr-defined]
    with connected(client) as (ws, hello, sessions, _):
        assert hello["agents"] == {"claude-code": False, "codex": True, "generic": True}
        assert hello["default_agent"] == "codex"
        assert sessions["sessions"][0]["agent"] == "generic"


def test_attach_command_and_agent_on_start_resume(client: TestClient, agent: FakeAgent):
    attached: list[tuple[str, str]] = []

    def attach(target: str, agent_key: str = "generic") -> str:
        attached.append((target, agent_key))
        return "sess-attached"

    agent.sessions.attach = attach  # type: ignore[attr-defined]
    seen: list[tuple] = []
    agent.sessions.start = lambda directory, permission_mode=None, agent=None: seen.append(("start", directory, permission_mode, agent)) or "sess-new"  # type: ignore[method-assign]
    agent.sessions.resume = lambda session_id, permission_mode=None, agent=None: seen.append(("resume", session_id, permission_mode, agent))  # type: ignore[method-assign]
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "attach", "args": {"target": "work:@1.%3"}})
        assert recv_type(ws, "sessions")["type"] == "sessions"
        ws.send_json({"type": "command", "name": "attach", "args": {"target": "work:@1.%3", "agent": "codex"}})
        recv_type(ws, "sessions")
        ws.send_json({"type": "command", "name": "attach", "args": {}})
        assert recv_type(ws, "error")["code"] == "bad_argument"
        ws.send_json({"type": "command", "name": "start", "args": {"directory": "/tmp/p", "agent": "generic"}})
        recv_type(ws, "sessions")
        ws.send_json({"type": "command", "name": "start", "args": {"directory": "/tmp/q"}})
        recv_type(ws, "sessions")
        ws.send_json({"type": "command", "name": "resume", "args": {"session_id": "sess-2", "agent": "codex"}})
        recv_type(ws, "sessions")
    assert attached == [("work:@1.%3", "generic"), ("work:@1.%3", "codex")]
    assert ("start", "/tmp/p", None, "generic") in seen
    assert ("start", "/tmp/q", None, None) in seen  # no agent field: the manager's default
    assert ("resume", "sess-2", None, "codex") in seen


def test_attach_without_manager_support_is_a_clean_error(client: TestClient, agent: FakeAgent):
    with connected(client) as (ws, *_):
        ws.send_json({"type": "command", "name": "attach", "args": {"target": "work:@1.%3"}})
        assert recv_type(ws, "error")["code"] == "unsupported"
