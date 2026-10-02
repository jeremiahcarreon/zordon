"""``zordon.app.Agent`` with every provider faked and a fake tmux. No network, no models,
no tmux server: the fake pane always shows the idle screen fixture, so the real
``SessionManager`` thread runs and keeps one focused session at IDLE."""

from __future__ import annotations

import queue
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from zordon import app as A
from zordon.bus import (
    Flush,
    LineKind,
    Notice,
    PromptDetected,
    PromptKind,
    SessionState,
    StateChanged,
    TranscriptRow,
)
from zordon.config import Config
from zordon.output.normalizer import PassthroughNormalizer
from zordon.output.tts import SilenceTTS
from zordon.providers import (
    ProviderNotConfigured,
    RouteContext,
    RouteResult,
    STTResult,
    YesNoResult,
)
from zordon.routing.keyword import KeywordRouter
from zordon.speech.vad import FakeVAD

from .conftest import read_fixture

IDLE_LINES = read_fixture("idle.txt").split("\n")


# ---- fakes ---------------------------------------------------------------------------------


class FakeTmux:
    """Enough of ``session.tmux.Tmux`` for the manager: one pane that is always idle."""

    def __init__(self) -> None:
        self.windows = 0
        self.sent: list[tuple[str, str, str]] = []
        self.killed: list[str] = []

    def binary_available(self) -> bool:
        return True

    def ensure_session(self, name: str, cwd: str | None = None, width: int = 160, height: int = 45) -> str:
        return name

    def new_window(self, session: str, name: str, cwd: str, command: Any, width: int = 160, height: int = 45) -> str:
        self.windows += 1
        return f"{session}:@{self.windows}.%{self.windows}"

    def list_panes(self) -> list[Any]:
        return []

    def pane_exists(self, target: str) -> bool:
        return target not in self.killed

    def alternate_on(self, target: str) -> bool:
        return True

    def capture(self, target: str, *a: Any, **k: Any) -> list[str]:
        return list(IDLE_LINES)

    def send_literal(self, target: str, text: str) -> None:
        self.sent.append(("literal", target, text))

    def send_enter(self, target: str) -> None:
        self.sent.append(("enter", target, ""))

    def send_key(self, target: str, key: str) -> None:
        self.sent.append(("key", target, key))

    def kill_window(self, target: str) -> None:
        self.killed.append(target)

    def kill_session(self, name: str) -> None:
        pass


class FakeTTS:
    name = "fake-tts"
    sample_rate = 16000

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> Iterator[bytes]:
        with self._lock:
            self.calls.append(text)
        yield b"\x00\x00" * 160
        yield b"\x00\x00" * 160


class FakeSTT:
    name = "fake-stt"

    def __init__(self, text: str = "hello") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        self.calls += 1
        return STTResult(text=self.text, confidence=0.9)


class FakeRouter:
    name = "fake-router"

    def __init__(self) -> None:
        self.routed: list[str] = []

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        self.routed.append(utterance)
        return RouteResult("claude_code", 0.99)

    def yes_no(self, utterance: str) -> YesNoResult:
        return YesNoResult("unclear", 0.0)

    def prompt_score(self, lines: list[str]) -> float:
        return 0.0


def wait_until(pred: Callable[[], bool], timeout: float = 3.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


class Events:
    def __init__(self, bus: Any) -> None:
        self.bus = bus
        self.seen: list[Any] = []

    def pump(self) -> list[Any]:
        while True:
            try:
                self.seen.append(self.bus.client_events.get_nowait())
            except queue.Empty:
                return self.seen

    def wait(self, pred: Callable[[Any], bool], timeout: float = 3.0) -> Any:
        found: list[Any] = []

        def check() -> bool:
            found[:] = [e for e in self.pump() if pred(e)]
            return bool(found)

        assert wait_until(check, timeout), f"no matching event; saw {[type(e).__name__ for e in self.seen][-12:]}"
        return found[0]

    def spoken(self) -> list[TranscriptRow]:
        return [e for e in self.pump() if isinstance(e, TranscriptRow) and e.kind == "spoken"]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config.default()
    c.output.poll_interval_ms = 20
    c.voice.prebuffer_sentences = 1
    return c


@pytest.fixture
def parts(cfg: Config, tmp_path: Path) -> dict[str, Any]:
    return {
        "normalizer": PassthroughNormalizer(),
        "tts": FakeTTS(),
        "stt": FakeSTT(),
        "vad": FakeVAD(lambda _c: 0.0),
        "router": FakeRouter(),
    }


@pytest.fixture
def agent(cfg: Config, parts: dict[str, Any], tmp_path: Path):
    tmux = FakeTmux()
    a = A.Agent(
        cfg,
        tmux=tmux,  # type: ignore[arg-type]
        providers_override=parts,
        claude_home=tmp_path / "claude-home",
        zordon_home=tmp_path / "zordon-home",
    )
    a.fake_tmux = tmux  # type: ignore[attr-defined]
    a.start()
    try:
        yield a
    finally:
        a.stop()


def focused_session(agent: A.Agent, tmp_path: Path) -> str:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    sid = agent.sessions.start(str(project))
    assert wait_until(lambda: agent.manager.state_of(sid) is SessionState.IDLE)
    assert agent.sessions.focused() == sid
    return sid


# ---- construction and lifecycle ----------------------------------------------------------------


def test_start_and_stop_within_two_seconds(cfg: Config, parts: dict[str, Any], tmp_path: Path):
    a = A.Agent(cfg, tmux=FakeTmux(), providers_override=parts, claude_home=tmp_path / "ch")  # type: ignore[arg-type]
    t0 = time.monotonic()
    a.start()
    assert a.manager.is_alive() and a.pipeline.is_alive() and a.audio.is_alive() and a.dispatcher.is_alive()
    a.stop()
    assert time.monotonic() - t0 < 2.0
    assert not a.pipeline.is_alive() and not a.audio.is_alive() and not a.dispatcher.is_alive()
    assert not a.manager.is_alive()
    a.stop()  # idempotent


def test_agent_api_surface(agent: A.Agent):
    for name in (
        "config", "bus", "sessions", "version", "hook_secret", "tunnel_url", "settings", "set_verbosity",
        "set_tool_chatter", "set_muted", "set_provider", "submit_text", "call_state", "repeat_last",
        "upload_path", "transcript_tail",
    ):
        assert hasattr(agent, name), name
    assert len(agent.hook_secret) >= 32
    assert agent.version == "0.1.0"
    assert agent.warnings == []


def test_settings_round_trip(agent: A.Agent):
    s = agent.settings()
    assert s["verbosity"] == "minimal" and s["tool_chatter"] is False and s["muted"] is False
    assert s["providers"] == {"stt": "fake-stt", "tts": "fake-tts", "normalizer": "passthrough", "router": "fake-router"}
    assert s["tts_sample_rate"] == 16000
    agent.set_verbosity("technical")
    agent.set_tool_chatter(True)
    agent.set_muted(True)
    s = agent.settings()
    assert s["verbosity"] == "technical" and s["tool_chatter"] is True and s["muted"] is True
    assert agent.config.voice.verbosity == "technical"
    agent.set_muted(False)
    assert agent.settings()["muted"] is False
    with pytest.raises(ValueError):
        agent.set_verbosity("loud")
    with pytest.raises(ValueError):
        agent.set_provider("vad", "silero")
    with pytest.raises(ValueError):
        agent.call_state("c1", "hangup")
    assert "sk-" not in repr(agent.settings())


def test_set_provider_keeps_old_one_when_new_fails(agent: A.Agent, monkeypatch: pytest.MonkeyPatch):
    def boom(config: Config) -> Any:
        raise ProviderNotConfigured("no kokoro files")

    monkeypatch.setattr(A, "make_tts", boom)
    before = agent.providers.tts
    with pytest.raises(ProviderNotConfigured):
        agent.set_provider("tts", "kokoro")
    assert agent.providers.tts is before and agent.pipeline.tts is before
    assert agent.config.providers.tts == "kokoro"  # unchanged default
    monkeypatch.undo()
    agent.set_provider("tts", "silence")
    assert isinstance(agent.providers.tts, SilenceTTS)
    assert agent.pipeline.tts is agent.providers.tts
    assert agent.settings()["providers"]["tts"] == "silence"


def test_mute_flushes_playback(agent: A.Agent):
    ev = Events(agent.bus)
    gen = agent.bus.generation
    agent.set_muted(True)
    flush = ev.wait(lambda e: isinstance(e, Flush))
    assert flush.generation == gen + 1 == agent.bus.generation
    agent.set_muted(True)  # no second flush
    time.sleep(0.05)
    assert sum(1 for e in ev.pump() if isinstance(e, Flush)) == 1


# ---- text input -> dispatcher -> pane ------------------------------------------------------------


def test_submit_text_reaches_dispatcher_and_pane(agent: A.Agent, parts: dict[str, Any], tmp_path: Path):
    sid = focused_session(agent, tmp_path)
    tmux: FakeTmux = agent.fake_tmux  # type: ignore[attr-defined]
    agent.submit_text("add a retry to the upload handler", "client-1")
    router: FakeRouter = parts["router"]
    assert wait_until(lambda: router.routed == ["add a retry to the upload handler"])
    assert wait_until(lambda: ("literal", agent.manager.sessions[sid].target, "add a retry to the upload handler") in tmux.sent)
    kinds = [k for k, _t, _x in tmux.sent]
    assert kinds[-2:] == ["literal", "enter"]
    agent.submit_text("   ", "client-1")
    time.sleep(0.1)
    assert router.routed == ["add a retry to the upload handler"]


def test_no_focused_session_speaks_an_error(agent: A.Agent):
    ev = Events(agent.bus)
    agent.submit_text("hello", "c")
    row = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken")
    assert "No session is focused" in row.text


# ---- prompts are spoken ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "title", "options", "kw", "expected"),
    [
        (
            PromptKind.PERMISSION,
            "Bash command: touch probe && echo done",
            ["Yes", "Yes, and always allow", "No"],
            {},
            "Claude Code wants to run a shell command: touch probe && echo done. Yes or no?",
        ),
        (
            PromptKind.PERMISSION,
            "whatever",
            ["Yes", "No"],
            {"command": "pytest -q"},
            "Claude Code wants to run a shell command: pytest -q. Yes or no?",
        ),
        (
            PromptKind.PERMISSION,
            "Create file probe.txt",
            ["Yes", "Yes, and switch to accept edits", "No"],
            {},
            "Claude Code wants to create probe.txt. Yes or no?",
        ),
        (
            PromptKind.PERMISSION,
            "Do you want to proceed?",
            ["Yes", "No"],
            {},
            "Claude Code is asking for permission: Do you want to proceed? Yes or no?",
        ),
        (
            PromptKind.PLAN,
            "Plan ready: Plan: write README.md for Zordon",
            ["Yes, and use auto mode", "Yes, manually approve edits", "Tell Claude what to change"],
            {},
            "Claude Code has a plan ready: Plan: write README.md for Zordon. Approve, revise, or deny?",
        ),
        (
            PromptKind.QUESTION,
            "Do you prefer tabs or spaces for indentation?",
            ["Tabs", "Spaces", "Type something.", "Chat about this"],
            {},
            "Claude Code is asking: Do you prefer tabs or spaces for indentation? "
            "Options are Tabs, Spaces, Type something., or Chat about this.",
        ),
        (
            PromptKind.TRUST,
            "Trust this folder: /home/u/proj",
            ["No, exit", "Yes, I trust this folder"],
            {},
            "This folder is not trusted yet. Say yes to trust it, or no.",
        ),
    ],
)
def test_prompt_speech_wording(kind, title, options, kw, expected):
    assert A.prompt_speech(kind, title, options, **kw) == expected


def test_prompt_on_focused_session_is_spoken(agent: A.Agent, parts: dict[str, Any], tmp_path: Path):
    sid = focused_session(agent, tmp_path)
    ev = Events(agent.bus)
    agent.bus.publish(
        PromptDetected(sid, PromptKind.PERMISSION, "Bash command: rm -rf build", ["Yes", "No"], ["│ rm -rf build"])
    )
    row = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and "shell command" in e.text)
    assert row.text == "Claude Code wants to run a shell command: rm -rf build. Yes or no?"
    assert row.session_id == sid
    tts: FakeTTS = parts["tts"]
    assert wait_until(lambda: any("rm -rf build" in t for t in tts.calls))
    # Permission prompts are spoken even when the verbosity filter is at minimal and muted is off.
    assert agent.config.voice.verbosity == "minimal"


def test_prompt_on_background_session_is_a_notice(agent: A.Agent, tmp_path: Path):
    sid = focused_session(agent, tmp_path)
    other = agent.sessions.start(str(tmp_path / "proj"))
    assert agent.sessions.focused() == sid and other != sid
    ev = Events(agent.bus)
    agent.bus.publish(PromptDetected(other, PromptKind.PLAN, "Plan ready: do things", ["Yes, manually approve edits"], []))
    notice = ev.wait(lambda e: isinstance(e, Notice) and e.session_id == other)
    assert notice.level == "info" and not notice.speak
    assert "Background session" in notice.text and "plan" in notice.text
    time.sleep(0.1)
    assert not any("plan ready" in r.text.lower() for r in ev.spoken())


def test_spoken_notices_are_read_out(agent: A.Agent, tmp_path: Path):
    sid = focused_session(agent, tmp_path)
    ev = Events(agent.bus)
    agent.bus.publish(Notice(text="Claude Code looks like it is waiting on something.", level="warning", session_id=sid, speak=True))
    row = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and "waiting on something" in e.text)
    assert row.session_id == sid
    agent.bus.publish(Notice(text="silent", level="info", session_id=sid, speak=False))
    time.sleep(0.1)
    assert not any("silent" == r.text for r in ev.spoken())


# ---- first focus speaks the permission summary once ----------------------------------------------


def test_first_focus_speaks_permission_summary_once(agent: A.Agent, tmp_path: Path):
    ev = Events(agent.bus)
    sid = focused_session(agent, tmp_path)
    row = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and "mode" in e.text)
    assert row.session_id == sid
    assert row.text.endswith(A.KEEP_SETTINGS_QUESTION)
    assert "default mode" in row.text
    # A second IDLE transition or re-focus does not repeat it.
    agent.bus.publish(StateChanged(sid, SessionState.IDLE, "again"))
    agent.sessions.focus(sid)
    time.sleep(0.2)
    assert sum(1 for r in ev.spoken() if A.KEEP_SETTINGS_QUESTION in r.text) == 1


def test_focus_switch_speaks_summary_for_the_other_session(agent: A.Agent, tmp_path: Path):
    a = focused_session(agent, tmp_path)
    b = agent.sessions.start(str(tmp_path / "proj"))
    assert wait_until(lambda: agent.manager.state_of(b) is SessionState.IDLE)
    ev = Events(agent.bus)
    agent.sessions.focus(b)
    row = ev.wait(
        lambda e: isinstance(e, TranscriptRow)
        and e.kind == "spoken"
        and e.session_id == b
        and A.KEEP_SETTINGS_QUESTION in e.text
    )
    assert row.session_id == b != a
    snapshot = ev.wait(lambda e: getattr(e, "type", None) == "sessions")
    assert [r.session_id for r in snapshot.sessions if r.focused] == [b]


# ---- misc AgentAPI ---------------------------------------------------------------------------------


def test_upload_path_under_focused_cwd(agent: A.Agent, tmp_path: Path):
    focused_session(agent, tmp_path)
    p = agent.upload_path("../../etc/pass wd.txt")
    assert p.parent == tmp_path / "proj" / ".zordon" / "uploads"
    assert p.name == "pass_wd.txt"
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_upload_path_without_session_falls_back_to_home(agent: A.Agent, tmp_path: Path):
    p = agent.upload_path("photo.jpg")
    assert p.name == "photo.jpg" and ".zordon/uploads" in str(p)


def test_repeat_last_and_transcript_tail(agent: A.Agent, tmp_path: Path):
    sid = focused_session(agent, tmp_path)
    ev = Events(agent.bus)
    agent.speak("Tests pass.", sid, LineKind.SUMMARY)
    ev.wait(lambda e: isinstance(e, TranscriptRow) and e.text == "Tests pass.")
    agent.repeat_last()
    assert wait_until(lambda: sum(1 for r in ev.spoken() if r.text == "Tests pass.") == 2)
    rows = agent.transcript_tail(sid, 10)
    assert [r.text for r in rows if r.kind == "spoken"][-2:] == ["Tests pass.", "Tests pass."]


def test_call_state_drives_the_audio_thread(agent: A.Agent):
    agent.call_state("c9", "start")
    assert wait_until(lambda: agent.audio.client_id == "c9")
    for action in ("pause", "resume", "end"):
        agent.call_state("c9", action)


def test_tunnel_url_is_published(agent: A.Agent):
    ev = Events(agent.bus)
    agent.set_tunnel_url("https://abc.trycloudflare.com")
    msg = ev.wait(lambda e: getattr(e, "type", None) == "tunnel")
    assert msg.url == "https://abc.trycloudflare.com" and msg.qr_svg.startswith("<svg")
    assert agent.tunnel_url == "https://abc.trycloudflare.com"


# ---- graceful degradation --------------------------------------------------------------------------


def test_degrades_when_every_factory_fails(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def nope(*a: Any, **k: Any) -> Any:
        raise ProviderNotConfigured("not here; run zordon doctor")

    monkeypatch.setattr(A, "make_tts", nope)
    monkeypatch.setattr(A, "make_stt", nope)
    monkeypatch.setattr(A, "make_vad", nope)
    monkeypatch.setattr(A, "make_router", nope)
    a = A.Agent(cfg, tmux=FakeTmux(), claude_home=tmp_path / "ch")  # type: ignore[arg-type]
    try:
        names = a.providers.names()
        assert names["tts"] == "silence" and names["stt"] == "unavailable" and names["normalizer"] == "passthrough"
        assert names["router"] == "keyword" and isinstance(a.providers.router, KeywordRouter)
        assert isinstance(a.providers.vad, FakeVAD)
        assert len(a.warnings) >= 5
        assert all("zordon doctor" in w or "not here" in w or "not configured" in w for w in a.warnings)
        ev = Events(a.bus)
        a.start()
        ev.wait(lambda e: isinstance(e, Notice) and e.level == "warning" and "tts" in e.text)
        with pytest.raises(ProviderNotConfigured):
            a.providers.stt.transcribe(np.zeros(16000, dtype=np.float32))
    finally:
        a.stop()


def test_default_config_without_keys_or_models_degrades_to_local(cfg: Config, tmp_path: Path):
    """The real factories with an isolated home: no keys, no models, no crash."""
    a = A.Agent(cfg, tmux=FakeTmux(), claude_home=tmp_path / "ch")  # type: ignore[arg-type]
    try:
        names = a.providers.names()
        assert names["normalizer"] == "passthrough"
        assert names["tts"] == "silence"
        assert names["router"].startswith("fallback(keyword")
        assert any("vad" in w for w in a.warnings)
        assert any("tts" in w for w in a.warnings)
    finally:
        a.stop()
