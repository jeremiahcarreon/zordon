"""The whole agent against a real tmux pane running ``tests/fake_claude.py``, driven
through the FastAPI app and its WebSocket exactly like the browser does.

Providers are local fakes (passthrough normalizer, silent TTS, fake STT, scripted
VAD, keyword router); the session manager, pipeline, audio thread, dispatcher,
transport and the tmux pane are real. A private tmux server is used and killed
on teardown; the user's own tmux server is never touched.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zordon.app import Agent
from zordon.bus import SpeechChunk
from zordon.config import Config
from zordon.output.normalizer import PassthroughNormalizer
from zordon.output.tts import SilenceTTS
from zordon.providers import STTResult
from zordon.routing.keyword import KeywordRouter
from zordon.session import discovery
from zordon.session.tmux import Tmux
from zordon.speech.vad import FakeVAD
from zordon.transport.server import create_app

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent.parent
FAKE = ROOT / "tests" / "fake_claude.py"
PYTHON = sys.executable
TOKEN = "e2e-token-0123456789abcdef0123456789"
FRAME_SAMPLES = 320  # 20 ms at 16 kHz


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


class FakeSTT:
    name = "fake"

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        self.calls += 1
        return STTResult(text=self.text, confidence=0.9)


def loud_vad(chunk: np.ndarray) -> float:
    """Speech when the chunk is loud, silence otherwise."""
    return 1.0 if float(np.abs(chunk).max()) > 0.1 else 0.0


@pytest.fixture
def stack(private_tmux: Tmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    claude_home = tmp_path / "claude-home"
    (claude_home / "projects").mkdir(parents=True)
    (claude_home / "sessions").mkdir()
    zordon_home = tmp_path / "zordon-home"
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("ZORDON_HOME", str(zordon_home))

    def fake_command(session_id: str, settings_path: Path | None = None, permission_mode: str | None = None, **_kw: object) -> list[str]:
        jsonl = discovery.jsonl_path_for(str(project), session_id, claude_home)
        return [PYTHON, str(FAKE), str(jsonl)]

    monkeypatch.setattr(discovery, "new_session_command", fake_command)
    monkeypatch.setattr(discovery, "resume_command", fake_command)

    cfg = Config.default()
    cfg.server.token = TOKEN
    cfg.output.poll_interval_ms = 100
    cfg.voice.idle_watchdog_seconds = 5
    cfg.voice.prebuffer_sentences = 1
    providers = {
        "normalizer": PassthroughNormalizer(),
        "tts": SilenceTTS(sample_rate=16000),
        "stt": FakeSTT(""),
        "vad": FakeVAD(loud_vad),
        "router": KeywordRouter(),
    }
    agent = Agent(
        cfg,
        tmux=private_tmux,
        providers_override=providers,
        claude_home=claude_home,
        zordon_home=zordon_home,
        tmux_session="zordon",
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Zordon</title>")
    app = create_app(agent, static_dir=web)
    agent.start()
    try:
        with TestClient(app, client=("127.0.0.1", 40000)) as client:
            assert client.post("/auth", json={"token": TOKEN}).status_code == 200
            yield agent, client, project, claude_home
    finally:
        agent.stop()


class Socket:
    """A WebSocket test session with a reader thread, so waits can time out."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while True:
            try:
                msg = self.ws.receive_json()
            except Exception:  # noqa: BLE001 - the socket closed
                return
            with self._lock:
                self.messages.append(msg)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.messages)

    def mark(self) -> int:
        with self._lock:
            return len(self.messages)

    def wait(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 8.0, since: int = 0, what: str = "message") -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for m in self.all()[since:]:
                if pred(m):
                    return m
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}; last messages: {self.describe()}")
            time.sleep(0.05)

    def wait_state(self, sid: str, state: str, since: int = 0, timeout: float = 10.0) -> dict[str, Any]:
        return self.wait(
            lambda m: m["type"] == "state" and m["session_id"] == sid and m["state"] == state,
            timeout,
            since,
            f"state {state}",
        )

    def spoken(self, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in self.all()[since:] if m["type"] == "transcript" and m["kind"] == "spoken"]

    def describe(self) -> str:
        out = []
        for m in self.all()[-10:]:
            if m["type"] == "transcript":
                out.append(f"transcript/{m['kind']}:{m['text'][:50]!r}")
            elif m["type"] == "state":
                out.append(f"state:{m['state']}")
            elif m["type"] == "prompt":
                out.append(f"prompt:{m['kind']}{'/cleared' if m.get('cleared') else ''}")
            else:
                out.append(m["type"])
        return ", ".join(out)

    def send(self, msg: dict[str, Any]) -> None:
        self.ws.send_json(msg)

    def text(self, text: str) -> None:
        self.send({"type": "text", "text": text})

    def command(self, name: str, **args: Any) -> None:
        self.send({"type": "command", "name": name, "args": args})


def user_records(jsonl: Path) -> int:
    """How many prompts reached the fake Claude Code (it logs one user record each)."""
    if not jsonl.exists():
        return 0
    n = 0
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "user":
            n += 1
    return n


def _wait(pred: Callable[[], bool], timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def test_voice_interface_end_to_end(stack):
    agent, client, project, claude_home = stack
    with client.websocket_connect("/ws") as raw:
        s = Socket(raw)

        # ---- opening burst: hello, sessions, no focused session yet
        hello = s.wait(lambda m: m["type"] == "hello", what="hello")
        assert hello["focused_session"] is None
        assert {k: hello["providers"][k] for k in ("stt", "tts", "normalizer", "router")} == {"stt": "fake", "tts": "silence", "normalizer": "passthrough", "router": "keyword"}
        assert hello["tts_sample_rate"] == 16000
        assert TOKEN not in json.dumps(hello)
        sessions = s.wait(lambda m: m["type"] == "sessions", what="sessions")
        assert sessions["sessions"] == []

        # ---- start a session: the fake TUI comes up, the state reaches idle, the summary is spoken
        mark = s.mark()
        s.command("start", directory=str(project))
        reply = s.wait(lambda m: m["type"] == "sessions" and m["sessions"], since=mark, what="sessions after start")
        sid = reply["sessions"][0]["session_id"]
        assert discovery.UUID_RE.match(sid) and reply["sessions"][0]["focused"] is True
        s.wait_state(sid, "working", since=mark)
        s.wait_state(sid, "idle", since=mark, timeout=15)
        summary = s.wait(
            lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and "say switch to" in m["text"],
            since=mark,
            what="permission summary",
        )
        assert summary["session_id"] == sid and "default mode" in summary["text"]
        assert "?" not in summary["text"]  # a statement: nothing owns a yes/no answer here
        s.wait(lambda m: m["type"] == "speech" and m["sentence_id"] == summary["sentence_id"], since=mark, what="summary audio")
        jsonl = discovery.jsonl_path_for(str(project), sid, claude_home)
        _wait(lambda: agent.manager.sessions[sid].jsonl is not None, what="jsonl located")

        # ---- typed text goes through the router to the pane; the reply is spoken from the jsonl
        mark = s.mark()
        before = user_records(jsonl)
        s.text("say hello from zordon")
        row = s.wait(
            lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and "hello from zordon" in m["text"],
            since=mark,
            what="spoken reply",
        )
        assert row["session_id"] == sid and row["raw_lines"]
        speech = s.wait(lambda m: m["type"] == "speech" and m["sentence_id"] == row["sentence_id"], since=mark, what="speech")
        assert speech["sample_rate"] == 16000 and base64.b64decode(speech["pcm"])
        assert any(m["type"] == "speech" and m["final"] for m in s.all()[mark:])
        s.wait_state(sid, "idle", since=mark)
        _wait(lambda: user_records(jsonl) == before + 1, what="fake received the prompt")

        # ---- a permission prompt: card with 4 options, state awaiting_permission, spoken form
        mark = s.mark()
        s.text("perm")
        prompt = s.wait(lambda m: m["type"] == "prompt" and not m["cleared"], since=mark, what="prompt card")
        assert prompt["kind"] == "permission" and prompt["session_id"] == sid
        assert len(prompt["options"]) == 4 and prompt["options"][0] == "Yes" and prompt["options"][-1] == "No"
        assert prompt["title"].startswith("Bash command:")
        s.wait_state(sid, "awaiting_permission", since=mark)
        spoken_prompt = s.wait(
            lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and "wants to run a command" in m["text"],
            since=mark,
            what="spoken prompt",
        )
        # The dialog's description line is spoken, never the command itself.
        assert spoken_prompt["text"].startswith("Claude Code wants to run a command: Create probe marker file")
        assert "touch " not in spoken_prompt["text"]
        assert spoken_prompt["text"].endswith("Yes or no?")

        # ---- "yes" passes the strict gate: the plain Yes is selected and the card clears
        mark = s.mark()
        before = user_records(jsonl)
        s.text("yes")
        cleared = s.wait(lambda m: m["type"] == "prompt" and m["cleared"] and m["prompt_id"] == prompt["prompt_id"], since=mark, what="prompt cleared")
        assert cleared["session_id"] == sid
        s.wait_state(sid, "idle", since=mark)
        s.wait(lambda m: m["type"] == "transcript" and m["text"] == "Approved.", since=mark, what="ack")
        _wait(lambda: any("● Ran 1 shell command" == ln for ln in agent.manager.last_pane_lines(sid, 20)), what="approved output")
        assert user_records(jsonl) == before  # "yes" was a menu selection, not typed into the pane

        # ---- "always allow" is refused by voice; "no" denies
        mark = s.mark()
        s.text("perm")
        prompt2 = s.wait(lambda m: m["type"] == "prompt" and not m["cleared"], since=mark, what="second prompt")
        assert prompt2["prompt_id"] != prompt["prompt_id"]
        s.wait_state(sid, "awaiting_permission", since=mark)
        mark2 = s.mark()
        s.text("always allow")
        refusal = s.wait(
            lambda m: m["type"] == "transcript" and "always-allow" in m["text"].lower().replace(" ", "-"),
            since=mark2,
            what="refusal row",
        )
        assert "can't" in refusal["text"] or "cannot" in refusal["text"]
        time.sleep(0.4)
        assert not any(m["type"] == "prompt" and m["cleared"] for m in s.all()[mark2:])
        assert agent.manager.state_of(sid).value == "awaiting_permission"
        mark3 = s.mark()
        s.text("no")
        s.wait(lambda m: m["type"] == "prompt" and m["cleared"] and m["prompt_id"] == prompt2["prompt_id"], since=mark3, what="denied")
        s.wait(lambda m: m["type"] == "transcript" and m["text"] == "Denied.", since=mark3, what="deny ack")
        s.wait_state(sid, "idle", since=mark3)
        _wait(lambda: any("Interrupted" in ln for ln in agent.manager.last_pane_lines(sid, 20)), what="denied output")

        # ---- "mute" is a shim command: settings say muted, nothing is typed into the pane
        mark = s.mark()
        before = user_records(jsonl)
        s.text("mute")
        settings = s.wait(lambda m: m["type"] == "settings" and m["muted"] is True, since=mark, what="muted settings")
        assert settings["verbosity"] == "normal"
        assert agent.settings()["muted"] is True
        s.wait(lambda m: m["type"] == "flush", since=mark, what="flush on mute")
        # CONC-12: the acknowledgement is still heard, after the flush, under the new generation.
        ack = s.wait(
            lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and m["text"] == "Muted.",
            since=mark,
            what="mute ack row",
        )
        ack_audio = s.wait(
            lambda m: m["type"] == "speech" and m["sentence_id"] == ack["sentence_id"], since=mark, what="mute ack audio"
        )
        assert ack_audio["generation"] == agent.bus.generation
        time.sleep(0.3)
        assert user_records(jsonl) == before

        # ---- a transcript question is answered from the transcript, Claude Code is not touched
        mark = s.mark()
        before = user_records(jsonl)
        s.text("what did it just say")
        answer = s.wait(
            lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and m["session_id"] == sid,
            since=mark,
            what="transcript answer",
        )
        assert answer["text"].strip()
        time.sleep(0.5)
        assert user_records(jsonl) == before
        # muted: rows, no audio (the "Muted." ack is the one sentence allowed through)
        assert not any(m["type"] == "speech" and m["sentence_id"] != ack["sentence_id"] for m in s.all()[mark:])

        # ---- unmute, then barge in: speech frames during playback flush the queue
        mark = s.mark()
        s.text("unmute")
        s.wait(lambda m: m["type"] == "settings" and m["muted"] is False, since=mark, what="unmuted settings")
        s.send({"type": "call", "action": "start"})
        _wait(lambda: agent.audio.client_id != "", what="call started")
        mark = s.mark()
        generation = agent.bus.generation
        long_pcm = b"\x00\x00" * (16000 * 3)  # 3 s of silence: playback stays active while we speak
        agent.bus.playback.put(SpeechChunk(sentence_id=999_001, seq=0, generation=generation, pcm=long_pcm, sample_rate=16000, final=True))
        s.wait(lambda m: m["type"] == "speech" and m["sentence_id"] == 999_001, since=mark, what="forwarded chunk")
        _wait(lambda: agent.audio.playback_active, what="playback active")
        time.sleep(0.25)  # past the 120 ms echo guard
        loud = (np.ones(FRAME_SAMPLES, dtype="<i2") * 12000).tobytes()
        for seq in range(15):
            s.send({"type": "audio", "pcm": base64.b64encode(loud).decode("ascii"), "seq": seq})
        flush = s.wait(lambda m: m["type"] == "flush", since=mark, what="flush after barge-in")
        assert flush["generation"] == generation + 1 == agent.bus.generation
        assert agent.audio.bargein_count == 1
        assert agent.audio.last_bargein_latency_ms is not None and agent.audio.last_bargein_latency_ms < 500
        s.send({"type": "call", "action": "end"})

        # ---- the transcript tail is replayed to a new connection
        rows = agent.transcript_tail(sid, 50)
        assert any(r.kind == "spoken" and "hello from zordon" in r.text for r in rows)
        assert any(r.kind == "user" and r.text == "say hello from zordon" for r in rows)

    with client.websocket_connect("/ws") as raw2:
        s2 = Socket(raw2)
        hello2 = s2.wait(lambda m: m["type"] == "hello", what="hello 2")
        assert hello2["focused_session"] == sid
        s2.wait(lambda m: m["type"] == "transcript" and "hello from zordon" in m["text"], what="replayed tail")


def test_hook_endpoint_feeds_the_session_manager(stack):
    agent, client, project, claude_home = stack
    with client.websocket_connect("/ws") as raw:
        s = Socket(raw)
        mark = s.mark()
        s.command("start", directory=str(project))
        reply = s.wait(lambda m: m["type"] == "sessions" and m["sessions"], since=mark, what="sessions")
        sid = reply["sessions"][0]["session_id"]
        s.wait_state(sid, "idle", since=mark, timeout=15)
        settings_file = agent.manager.sessions[sid].settings_path
        assert settings_file is not None and settings_file.is_file()
        hooks = json.loads(settings_file.read_text())["hooks"]
        assert set(hooks) == {"Notification", "UserPromptSubmit", "Stop", "PermissionRequest"}
        assert agent.hook_secret not in settings_file.read_text()
        assert agent.hook_secret in discovery.hook_curl_config_path(settings_file).read_text()
        bad = client.post("/hooks/claude", json={"session_id": sid, "hook_event_name": "Stop"}, headers={"X-Zordon-Hook-Secret": "nope"})
        assert bad.status_code == 403
        ok = client.post(
            "/hooks/claude",
            json={"session_id": sid, "hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "idle"},
            headers={"X-Zordon-Hook-Secret": agent.hook_secret},
        )
        assert ok.status_code == 200
        # The session is idle on screen, so the hint changes nothing visible; it must not break polling.
        time.sleep(0.4)
        assert agent.manager.state_of(sid).value == "idle"

        # The PermissionRequest hook (decision 0019): the POST waits for the user's answer,
        # the client sees the prompt card and the spoken question, approve answers the hook.
        import threading

        answers: list = []
        payload = {
            "session_id": sid,
            "cwd": str(project),
            "permission_mode": "default",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "touch marker.txt", "description": "Create a marker file"},
        }

        def post():
            answers.append(client.post("/hooks/permission", json=payload, headers={"X-Zordon-Hook-Secret": agent.hook_secret}))

        mark = s.mark()
        t = threading.Thread(target=post, daemon=True)
        t.start()
        prompt = s.wait(lambda m: m["type"] == "prompt" and not m.get("cleared"), since=mark, what="hook prompt")
        assert prompt["kind"] == "permission" and prompt["options"] == ["Yes", "No"]
        s.wait_state(sid, "awaiting_permission", since=mark)
        spoken = s.wait(lambda m: m["type"] == "transcript" and m["kind"] == "spoken" and "wants to run a command" in m["text"], since=mark, what="spoken hook prompt")
        assert spoken["text"] == "Claude Code wants to run a command: Create a marker file. Yes or no?"
        mark = s.mark()
        s.command("approve")
        t.join(10)
        assert answers and answers[0].status_code == 200
        assert answers[0].json() == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
        s.wait(lambda m: m["type"] == "prompt" and m.get("cleared"), since=mark, what="hook prompt cleared")
        forbidden = client.post("/hooks/permission", json=payload, headers={"X-Zordon-Hook-Secret": "nope"})
        assert forbidden.status_code == 403

        mark = s.mark()
        s.command("delete", session_id=sid, confirm=True)
        s.wait(lambda m: m["type"] == "state" and m["session_id"] == sid and m["state"] == "detached", since=mark, what="deleted")
