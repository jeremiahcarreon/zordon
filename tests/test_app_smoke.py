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
    PaneLine,
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
    assert {k: s["providers"][k] for k in ("stt", "tts", "normalizer", "router")} == {"stt": "fake-stt", "tts": "fake-tts", "normalizer": "passthrough", "router": "fake-router"}
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
    assert "default mode" in row.text
    # DC-02: a statement that says how to change the mode, never a question: nothing owns
    # the answer, and a bare "yes" would be typed into Claude Code.
    assert "?" not in row.text
    assert "keep these settings" not in row.text.lower()
    assert "say switch to" in row.text and row.text.rstrip().endswith("to change it.")
    # A second IDLE transition or re-focus does not repeat it.
    agent.bus.publish(StateChanged(sid, SessionState.IDLE, "again"))
    agent.sessions.focus(sid)
    time.sleep(0.2)
    assert sum(1 for r in ev.spoken() if "say switch to" in r.text) == 1


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
        and "say switch to" in e.text
    )
    assert row.session_id == b != a
    assert "?" not in row.text
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
    assert not agent.audio.in_call


def test_only_one_client_is_in_the_call(agent: A.Agent):
    """CONC-9: a second start is refused with an error notice; a non-caller's end is ignored."""
    ev = Events(agent.bus)
    agent.call_state("phone", "start")
    assert agent.audio.in_call and agent.audio.client_id == "phone"
    agent.call_state("laptop", "start")
    notice = ev.wait(lambda e: isinstance(e, Notice) and e.level == "error")
    assert notice.text == A.CALL_BUSY_TEXT
    assert agent.audio.client_id == "phone" and agent.audio.in_call
    # The laptop tab closing (ws sends call end on disconnect) does not end the phone's call.
    agent.call_state("laptop", "end")
    agent.call_state("laptop", "pause")
    assert agent.audio.in_call and agent.audio.client_id == "phone"
    agent.call_state("phone", "end")
    assert not agent.audio.in_call
    agent.call_state("laptop", "start")
    assert agent.audio.client_id == "laptop"
    with pytest.raises(ValueError):
        agent.call_state("laptop", "dance")


# ---- focus: only the focused session is spoken ------------------------------------------------------


def test_unfocused_session_output_is_stored_but_not_spoken(agent: A.Agent, parts: dict[str, Any], tmp_path: Path):
    """DC-01 / CONC-4: two attached sessions, one focused. Prose from the other one gets a
    transcript row and no audio; a prompt there is a collapsed notice."""
    sid = focused_session(agent, tmp_path)
    other = agent.sessions.start(str(tmp_path / "proj"))
    assert wait_until(lambda: agent.manager.state_of(other) is SessionState.IDLE)
    assert agent.sessions.focused() == sid
    agent.config.voice.verbosity = "normal"
    ev = Events(agent.bus)
    tts: FakeTTS = parts["tts"]
    tts.calls.clear()
    for s, text in ((other, "Background session says this sentence."), (sid, "Focused session says this sentence.")):
        agent.bus.pane_lines.put(PaneLine(session_id=s, text=text, source="jsonl", block="text"))
        agent.bus.pane_lines.put(PaneLine(session_id=s, text="", source="jsonl", block="turn_end"))
    bg = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and e.session_id == other)
    fg = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and e.session_id == sid)
    assert wait_until(lambda: "Focused session says this sentence." in tts.calls)
    time.sleep(0.2)
    assert tts.calls == ["Focused session says this sentence."]
    assert agent.pipeline.stats()["unfocused_skips"] == 1
    assert [r.text for r in agent.transcript_tail(other, 5) if r.kind == "spoken"] == [bg.text]
    assert agent.transcript_tail(other, 5)[-1].spoken is False
    assert fg.sentence_id != bg.sentence_id
    chunks = [e for e in ev.pump() if hasattr(e, "sentence_id") and hasattr(e, "pcm")]
    assert any(c.sentence_id == fg.sentence_id for c in chunks)
    assert not any(c.sentence_id == bg.sentence_id for c in chunks)


# ---- redaction of prompt cards and notices -----------------------------------------------------------


def test_prompt_cards_and_notices_are_redacted_before_clients_see_them(agent: A.Agent, tmp_path: Path):
    """SEC-3: PromptDetected title/options/raw_lines and Notice text are masked on publish."""
    sid = focused_session(agent, tmp_path)
    key = "sk-ant-api03-" + "A" * 40
    ev = Events(agent.bus)
    agent.bus.publish(
        PromptDetected(
            sid,
            PromptKind.PERMISSION,
            f"Bash command: curl -H 'Authorization: Bearer {key}'",
            ["Yes", f"Yes, and always allow {key}", "No"],
            [f"│ curl -H 'Authorization: Bearer {key}'", "│ Do you want to proceed?"],
        )
    )
    prompt = ev.wait(lambda e: isinstance(e, PromptDetected))
    assert key not in prompt.title and key not in " ".join(prompt.options) and key not in " ".join(prompt.raw_lines)
    assert "[redacted]" in prompt.title and prompt.options[0] == "Yes" and prompt.options[-1] == "No"
    assert len(prompt.raw_lines) == 2 and prompt.raw_lines[1] == "│ Do you want to proceed?"
    spoken = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and "shell command" in e.text)
    assert key not in spoken.text
    agent.bus.publish(Notice(text=f"Claude Code looks stuck. The last lines were: export ANTHROPIC_API_KEY={key}", level="warning", session_id=sid, speak=True))
    notice = ev.wait(lambda e: isinstance(e, Notice) and "looks stuck" in e.text)
    assert key not in notice.text and "[redacted]" in notice.text
    row = ev.wait(lambda e: isinstance(e, TranscriptRow) and e.kind == "spoken" and "looks stuck" in e.text)
    assert key not in row.text
    # Events without secrets pass through untouched (same object).
    clean = Notice(text="all good", level="info")
    agent.bus.publish(clean)
    assert ev.wait(lambda e: e is clean) is clean
    assert A.redact_event(clean) is clean


def test_redact_event_is_pure():
    key = "sk-ant-api03-" + "B" * 40
    p = PromptDetected("s", PromptKind.PERMISSION, f"Bash command: echo {key}", ["Yes", "No"], [key])
    out = A.redact_event(p)
    assert out is not p and out.prompt_id == p.prompt_id and out.session_id == "s"
    assert key not in out.title and out.raw_lines == ["[redacted]"]
    assert p.title.endswith(key)  # the original is untouched
    n = A.redact_event(Notice(text=f"token {key}"))
    assert key not in n.text


# ---- shutdown order ---------------------------------------------------------------------------------


class SlowTTS(FakeTTS):
    sample_rate = 16000

    def synthesize(self, text: str) -> Iterator[bytes]:
        with self._lock:
            self.calls.append(text)
        for _ in range(50):
            time.sleep(0.05)
            yield b"\x00\x00" * 160


def test_stop_joins_the_speaker_before_closing_the_store(cfg: Config, parts: dict[str, Any], tmp_path: Path):
    """CONC-10: a long synthesis in flight must not outlive stop(); the store is closed only
    after the speaker thread is gone."""
    parts = dict(parts, tts=SlowTTS())
    a = A.Agent(cfg, tmux=FakeTmux(), providers_override=parts, claude_home=tmp_path / "ch")  # type: ignore[arg-type]
    a.start()
    sid = focused_session(a, tmp_path)
    a.speak("A sentence that takes two and a half seconds to synthesize.", sid, LineKind.PROSE)
    assert wait_until(lambda: a.pipeline.stats()["chunks"] >= 2)
    t0 = time.monotonic()
    a.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"stop took {elapsed:.2f}s"
    assert not any(t.name == "zordon-speaker" and t.is_alive() for t in threading.enumerate())
    assert not a.pipeline.is_alive()
    assert a.pipeline.stats()["chunks"] < 50


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
