"""PipelineThread tests with a fake normalizer and a fake TTS. No network, no models."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from zordon.bus import Bus, LineKind, PaneLine, SpeechChunk, TranscriptRow
from zordon.config import Config
from zordon.output.pipeline import PipelineThread
from zordon.providers import ProviderError
from zordon.transcript.store import TranscriptStore

SID = "sess-1"


class FakeNormalizer:
    """Uppercases. Can be told to raise ProviderError or to sleep past the timeout."""

    name = "fake"

    def __init__(self) -> None:
        self.inputs: list[tuple[str, list[str]]] = []
        self.fail = False
        self.sleep = 0.0
        self._lock = threading.Lock()

    def normalize(self, sentence: str, context: list[str]) -> str:
        with self._lock:
            self.inputs.append((sentence, list(context)))
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail:
            raise ProviderError("boom")
        return sentence.upper()


class FakeTTS:
    """Yields ``chunks`` blocks of silence per sentence, optionally slowly."""

    name = "fake-tts"
    sample_rate = 24000

    def __init__(self, chunks: int = 3, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> Iterator[bytes]:
        with self._lock:
            self.calls.append(text)
        for _ in range(self.chunks):
            if self.delay:
                time.sleep(self.delay)
            yield b"\x00\x00" * 480


def wait_until(pred: Callable[[], bool], timeout: float = 3.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def spoken_rows(bus: Bus) -> list[TranscriptRow]:
    return [e for e in drain(bus.client_events) if isinstance(e, TranscriptRow)]


class Harness:
    def __init__(
        self, tmp_path: Path, verbosity: str = "minimal", prebuffer: int = 1, **kw
    ) -> None:
        self.bus = Bus()
        self.config = Config()
        self.config.voice.verbosity = verbosity
        self.config.voice.prebuffer_sentences = prebuffer
        self.config.providers.normalizer_timeout_seconds = 0.5
        self.store = TranscriptStore(tmp_path / "t.db")
        self.normalizer = FakeNormalizer()
        self.tts = FakeTTS(**{k: v for k, v in kw.items() if k in ("chunks", "delay")})
        self.muted = threading.Event()
        self.pipeline = PipelineThread(
            self.bus,
            self.config,
            self.normalizer,
            self.tts,
            self.store,
            muted=self.muted,
            lull_seconds=kw.get("lull", 0.15),
            prebuffer_max_wait=kw.get("prebuffer_max_wait", 5.0),
        )
        self.pipeline.start()

    def line(
        self, text: str, source: str = "pane", block: str = "", meta: dict | None = None
    ) -> None:
        self.bus.pane_lines.put(
            PaneLine(session_id=SID, text=text, source=source, block=block, meta=meta or {})
        )

    def jsonl(self, text: str) -> None:
        self.line(text, source="jsonl", block="text")

    def turn_end(self) -> None:
        self.line("", source="jsonl", block="turn_end")

    def tool_use(self, name: str, inp: dict) -> None:
        self.line(name, source="jsonl", block="tool_use", meta={"name": name, "input": inp})

    def chunks(self) -> list[SpeechChunk]:
        return drain(self.bus.playback)

    def wait_synth(self, n: int, timeout: float = 3.0) -> bool:
        return wait_until(lambda: len(self.tts.calls) >= n, timeout)

    def close(self) -> None:
        self.pipeline.stop()
        self.pipeline.join(timeout=3.0)
        self.store.close()


@pytest.fixture
def make(tmp_path: Path):
    made: list[Harness] = []

    def _make(**kw) -> Harness:
        h = Harness(tmp_path, **kw)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.close()


# ---- verbosity -------------------------------------------------------------------


def test_minimal_speaks_intent_and_summary_only(make):
    h = make(verbosity="minimal")
    h.jsonl(
        "Adding retry logic to the upload handler. First I read the handler. "
        "Then I added a loop with backoff. Finally I ran the tests. All tests pass."
    )
    h.turn_end()
    assert h.wait_synth(3)
    time.sleep(0.2)
    assert h.tts.calls == [
        "ADDING RETRY LOGIC TO THE UPLOAD HANDLER.",
        "FINALLY I RAN THE TESTS.",
        "ALL TESTS PASS.",
    ]
    rows = spoken_rows(h.bus)
    kinds = {r.text: h.store.tail(SID, 10) for r in rows}
    assert kinds  # rows were published
    tail = h.store.tail(SID, 10)
    assert [r.text for r in tail] == h.tts.calls
    stats = h.pipeline.stats()
    assert stats["dropped"] >= 2 and stats["turns"] == 1


def test_normal_speaks_all_prose_in_order(make):
    h = make(verbosity="normal")
    h.jsonl("One is first. Two is second. Three is third. Four is fourth.")
    h.turn_end()
    assert h.wait_synth(4)
    assert h.tts.calls == ["ONE IS FIRST.", "TWO IS SECOND.", "THREE IS THIRD.", "FOUR IS FOURTH."]


def test_summary_is_reemitted_after_lull_at_minimal(make):
    """Trailing prose released at a lull but dropped by the filter is still the outcome."""
    h = make(verbosity="minimal", lull=0.1)
    h.jsonl("Starting the refactor. Moved the helper. Done, tests pass.")
    assert h.wait_synth(1)
    assert wait_until(
        lambda: h.pipeline.stats()["dropped"] >= 2, 2.0
    )  # lull released them, filter dropped them
    h.turn_end()
    assert h.wait_synth(3)
    assert h.tts.calls == ["STARTING THE REFACTOR.", "MOVED THE HELPER.", "DONE, TESTS PASS."]


def test_technical_speaks_tool_calls_minimal_does_not(make):
    h = make(verbosity="technical")
    h.tool_use("Edit", {"file_path": "/x/auth.py", "old_string": "a", "new_string": "b"})
    h.tool_use("Bash", {"command": "pytest -q", "description": "Run the tests"})
    assert h.wait_synth(2)
    assert h.tts.calls == ["EDITING AUTH.PY", "RUNNING: RUN THE TESTS"]
    h.close()

    m = make(verbosity="minimal")
    m.tool_use("Edit", {"file_path": "/x/auth.py"})
    m.tool_use("Bash", {"command": "pytest -q"})
    time.sleep(0.3)
    assert m.tts.calls == []
    assert m.pipeline.stats()["dropped"] == 2


def test_normal_speaks_file_edits_not_shell(make):
    h = make(verbosity="normal")
    h.tool_use("Bash", {"command": "ls"})
    h.tool_use("Write", {"file_path": "notes.md", "content": "x"})
    assert h.wait_synth(1)
    time.sleep(0.2)
    assert h.tts.calls == ["WRITING NOTES.MD"]


def test_tool_chatter_toggle(make):
    h = make(verbosity="minimal")
    h.config.voice.tool_chatter = True
    h.tool_use("Grep", {"pattern": "x"})
    assert h.wait_synth(1)
    assert h.tts.calls == ["SEARCHING THE CODEBASE"]


# ---- priority / speak_now --------------------------------------------------------------


def test_speak_now_jumps_the_queue(make):
    h = make(verbosity="normal", delay=0.03, chunks=3)
    h.jsonl(
        "Sentence one here. Sentence two here. Sentence three here. Sentence four here. Sentence five here."
    )
    assert h.wait_synth(1)
    sid = h.pipeline.speak_now("Do you want to create probe.txt?", SID, LineKind.PERMISSION_PROMPT)
    assert h.wait_synth(6)
    calls = h.tts.calls
    idx = calls.index("Do you want to create probe.txt?")
    assert 0 < idx < len(calls) - 1, f"prompt did not jump the queue: {calls}"
    # Not normalized, not filtered, stored as a prompt row with its own sentence id.
    assert all("Do you want" not in s for s, _ in h.normalizer.inputs)
    tail = h.store.tail(SID, 20)
    row = next(r for r in tail if r.sentence_id == sid)
    assert row.text == "Do you want to create probe.txt?"
    assert h.pipeline.stats()["priority"] == 1


def test_speak_now_is_spoken_at_minimal_even_as_question_or_error(make):
    h = make(verbosity="minimal")
    h.pipeline.speak_now("Tabs or spaces?", SID, LineKind.QUESTION)
    h.pipeline.speak_now("Claude Code looks stuck", SID, LineKind.ERROR)
    assert h.wait_synth(2)
    assert h.tts.calls == ["Tabs or spaces?", "Claude Code looks stuck"]


def test_muted_writes_transcript_but_no_audio(make):
    h = make(verbosity="normal")
    h.muted.set()
    h.jsonl("Hello there friend.")
    h.turn_end()
    h.pipeline.speak_now("Do you want to proceed?", SID)
    assert wait_until(lambda: len(spoken := h.store.tail(SID, 10)) >= 2 and spoken is not None, 3.0)
    time.sleep(0.2)
    assert h.tts.calls == []
    assert h.chunks() == []
    texts = [r.text for r in h.store.tail(SID, 10)]
    assert "HELLO THERE FRIEND." in texts and "Do you want to proceed?" in texts
    rows = spoken_rows(h.bus)
    assert {r.text for r in rows} >= {"HELLO THERE FRIEND.", "Do you want to proceed?"}
    assert h.pipeline.stats()["muted_skips"] == 2


# ---- prebuffer ---------------------------------------------------------------------------


def test_prebuffer_holds_three_then_releases(make):
    h = make(verbosity="normal", prebuffer=3, prebuffer_max_wait=10.0)
    h.jsonl("First sentence here.")
    h.jsonl("Second sentence here.")
    time.sleep(0.4)
    assert h.tts.calls == [], "audio must not start before three normalized sentences are ready"
    assert h.pipeline.stats()["normalized"] == 0
    h.jsonl("Third sentence here.")
    assert h.wait_synth(3)
    assert h.tts.calls == ["FIRST SENTENCE HERE.", "SECOND SENTENCE HERE.", "THIRD SENTENCE HERE."]
    # The prebuffer only applies at the start of a turn.
    h.jsonl("Fourth sentence here.")
    assert h.wait_synth(4, 1.0)


def test_prebuffer_releases_on_turn_end(make):
    h = make(verbosity="normal", prebuffer=3, prebuffer_max_wait=10.0)
    h.jsonl("Only one sentence.")
    time.sleep(0.3)
    assert h.tts.calls == []
    h.turn_end()
    assert h.wait_synth(1)
    assert h.tts.calls == ["ONLY ONE SENTENCE."]
    # Next turn prebuffers again.
    h.jsonl("Second turn starts.")
    time.sleep(0.3)
    assert h.tts.calls == ["ONLY ONE SENTENCE."]


def test_prebuffer_releases_on_priority(make):
    h = make(verbosity="normal", prebuffer=3, prebuffer_max_wait=10.0)
    h.jsonl("Held sentence.")
    time.sleep(0.2)
    assert h.tts.calls == []
    h.pipeline.speak_now("Do you want to proceed?", SID)
    assert h.wait_synth(2)
    assert h.tts.calls == ["Do you want to proceed?", "HELD SENTENCE."]


def test_prebuffer_gives_up_after_max_wait(make):
    h = make(verbosity="normal", prebuffer=3, prebuffer_max_wait=0.3)
    h.jsonl("Lonely sentence.")
    assert h.wait_synth(1, 2.0)


# ---- normalizer failure modes -------------------------------------------------------------


def test_normalizer_error_falls_back_to_raw_text(make):
    h = make(verbosity="normal")
    h.normalizer.fail = True
    h.jsonl("Keep this as is.")
    assert h.wait_synth(1)
    assert h.tts.calls == ["Keep this as is."]
    assert h.pipeline.stats()["normalizer_failures"] == 1


def test_normalizer_timeout_falls_back_to_raw_text(make):
    h = make(verbosity="normal")
    h.config.providers.normalizer_timeout_seconds = 0.2
    h.normalizer.sleep = 0.8
    h.jsonl("Slow path sentence.")
    assert h.wait_synth(1, 2.0)
    assert h.tts.calls == ["Slow path sentence."]
    assert h.pipeline.stats()["normalizer_failures"] == 1
    time.sleep(0.8)  # let the sleeping worker finish before teardown


def test_normalizer_gets_previous_two_sentences_as_context(make):
    h = make(verbosity="normal")
    h.jsonl("Alpha one. Beta two. Gamma three. Delta four.")
    assert h.wait_synth(4)
    inputs = dict(h.normalizer.inputs)
    assert inputs["Alpha one."] == []
    assert inputs["Beta two."] == ["Alpha one."] or inputs["Beta two."] == ["ALPHA ONE."]
    ctx = inputs["Delta four."]
    assert len(ctx) == 2 and ctx[0].lower() == "beta two." and ctx[1].lower() == "gamma three."


# ---- generation / barge-in ------------------------------------------------------------------


def test_generation_bump_drops_remaining_chunks(make):
    h = make(verbosity="normal", chunks=12, delay=0.03)
    h.jsonl("A long sentence being spoken.")
    assert wait_until(lambda: h.bus.playback.qsize() >= 2, 3.0)
    h.bus.next_generation()
    assert wait_until(lambda: h.pipeline.stats()["dropped_by_generation"] >= 1, 3.0)
    time.sleep(0.2)
    chunks = h.chunks()
    assert 0 < len(chunks) < 12
    assert all(c.generation == 1 for c in chunks)
    assert not any(c.final for c in chunks)
    rows = h.store.tail(SID, 5)
    assert rows and rows[-1].spoken is False
    # New sentences after the barge-in are spoken under the new generation.
    h.jsonl("Fresh sentence after barge in.")
    assert wait_until(
        lambda: any(c.generation == 2 and c.final for c in drain(h.bus.playback)), 3.0
    )


def test_queued_sentences_from_old_generation_are_not_spoken(make):
    h = make(verbosity="normal", chunks=2, delay=0.05)
    h.jsonl("First one. Second one. Third one. Fourth one.")
    assert h.wait_synth(1)
    h.bus.next_generation()
    assert wait_until(lambda: len(h.store.tail(SID, 10)) >= 4, 3.0)
    time.sleep(0.3)
    assert len(h.tts.calls) < 4
    tail = h.store.tail(SID, 10)
    assert len(tail) == 4  # transcript is complete even though speech was cut
    assert any(r.spoken is False for r in tail)


def test_speech_chunks_are_well_formed(make):
    h = make(verbosity="normal", chunks=3)
    h.jsonl("Chunk shape check.")
    assert h.wait_synth(1)
    assert wait_until(lambda: h.bus.playback.qsize() >= 3, 2.0)
    chunks = h.chunks()
    assert [c.seq for c in chunks] == [0, 1, 2]
    assert [c.final for c in chunks] == [False, False, True]
    assert all(c.sample_rate == 24000 and c.pcm and len(c.pcm) % 2 == 0 for c in chunks)
    assert len({c.sentence_id for c in chunks}) == 1


# ---- transcript, raw links, redaction --------------------------------------------------------


def test_transcript_rows_link_raw_ids_and_redaction_precedes_normalizer(make):
    h = make(verbosity="normal")
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    h.line(f"● Set ANTHROPIC_API_KEY={secret} in the environment and restart.")
    h.line("  Then the normalizer works.")
    h.line("")
    h.turn_end()
    assert h.wait_synth(2)
    # The normalizer never saw the secret.
    for sent, ctx in h.normalizer.inputs:
        assert secret not in sent and "sk-ant" not in sent
        assert all(secret not in c for c in ctx)
    # Neither did the TTS, the client rows or the store.
    assert all(secret not in s for s in h.tts.calls)
    rows = spoken_rows(h.bus)
    assert rows
    for r in rows:
        assert secret not in r.text
        assert all(secret not in raw for raw in r.raw_lines)
        assert r.raw_lines, "every spoken row links to the raw lines it came from"
        assert r.kind == "spoken" and r.sentence_id is not None
    first = next(r for r in rows if "ANTHROPIC_API_KEY" in r.text.upper())
    assert "[redacted]" in first.raw_lines[0].lower() or "[REDACTED]" in first.raw_lines[0]
    linked = h.store.raw_for(first.sentence_id)
    assert linked and all(secret not in x for x in linked)
    assert any("[redacted]" in x for x in linked)


def test_pane_source_turn_boundaries(make):
    h = make(verbosity="minimal")
    h.line("❯ do the thing")
    h.line("● Adding the retry logic.")
    h.line("  First the handler, then the tests.")
    h.line("")
    h.line("  tmux new -s work")
    h.line("  tmux attach -t work")
    h.line("")
    h.line("  Done, tests pass.")
    h.line("")
    h.line("✻ Worked for 4s · done 8:33 PM")
    assert h.wait_synth(2)
    time.sleep(0.2)
    assert h.tts.calls == ["ADDING THE RETRY LOGIC.", "DONE, TESTS PASS."]
    tail = h.store.tail(SID, 10)
    kinds = {r.text: r for r in tail}
    assert set(kinds) == set(h.tts.calls)
    # the raw lines under each spoken sentence are the pane lines it came from
    assert any(
        "Adding the retry logic" in x
        for x in h.store.raw_for(kinds["ADDING THE RETRY LOGIC."].sentence_id)
    )
    assert h.pipeline.stats()["turns"] == 1


def test_pane_code_block_spoken_at_technical_with_raw_links(make):
    h = make(verbosity="technical")
    h.line("● Here is the snippet.")
    h.line("")
    h.line("    def a():")
    h.line("        return 1")
    h.line("")
    h.line("  That is all.")
    h.line("✻ Worked for 1s · done 8:33 PM")
    assert h.wait_synth(3)
    assert h.tts.calls == ["HERE IS THE SNIPPET.", "a code block, 2 lines", "THAT IS ALL."]
    code = next(r for r in h.store.tail(SID, 10) if r.text == "a code block, 2 lines")
    raws = h.store.raw_for(code.sentence_id)
    assert any("def a():" in x for x in raws) and any("return 1" in x for x in raws)
    assert "a code block, 2 lines" not in [
        s for s, _ in h.normalizer.inputs
    ]  # literal: no model call


def test_placeholders_are_never_spoken(make):
    h = make(verbosity="technical")
    h.config.voice.tool_chatter = True
    h.tool_use("AskUserQuestion", {"questions": [{"question": "Tabs or spaces?"}]})
    h.tool_use("ExitPlanMode", {"plan": "..."})
    h.tool_use("Read", {"file_path": "x/y.py"})
    assert h.wait_synth(1)
    time.sleep(0.2)
    assert h.tts.calls == ["READING Y.PY"]


def test_tool_result_blocks(make):
    h = make(verbosity="technical")
    h.line(
        "===== 12 passed in 0.3s =====",
        source="jsonl",
        block="tool_result",
        meta={"is_error": False},
    )
    h.line(
        "The user doesn't want to proceed with this tool use.",
        source="jsonl",
        block="tool_result",
        meta={"rejected": True},
    )
    assert h.wait_synth(2)
    assert h.tts.calls == ["tests passed", "that was denied"]


def test_errors_are_spoken_at_minimal(make):
    h = make(verbosity="minimal")
    h.line(
        "Error: ENOENT: no such file", source="jsonl", block="tool_result", meta={"is_error": True}
    )
    assert h.wait_synth(1)
    assert h.tts.calls == ["error: ENOENT: no such file"]


def test_thinking_and_user_prompt_blocks_are_never_spoken(make):
    h = make(verbosity="technical")
    h.config.voice.tool_chatter = True
    h.line("Let me think about the retry logic here.", source="jsonl", block="thinking")
    h.line("add retry logic", source="jsonl", block="user_prompt")
    h.line("plan", source="jsonl", block="permission_mode", meta={"mode": "plan"})
    h.line("Retry logic", source="jsonl", block="title")
    h.jsonl("Adding the retry logic now.")
    assert h.wait_synth(1)
    time.sleep(0.2)
    assert h.tts.calls == ["ADDING THE RETRY LOGIC NOW."]
    # Everything still lands in the raw transcript.
    assert len(h.store.raw_tail(SID, 10)) == 5


def test_user_prompt_block_starts_a_new_turn(make):
    h = make(verbosity="minimal")
    h.jsonl("First turn intent. First turn middle. First turn end.")
    h.turn_end()
    assert h.wait_synth(3)
    h.line("next task", source="jsonl", block="user_prompt")
    h.jsonl("Second turn intent. Second turn end.")
    h.turn_end()
    assert h.wait_synth(5)
    assert h.tts.calls[3:] == ["SECOND TURN INTENT.", "SECOND TURN END."]


def test_stats_shape(make):
    h = make()
    s = h.pipeline.stats()
    for k in (
        "lines_in",
        "tagged",
        "kept",
        "dropped",
        "sentences",
        "normalized",
        "normalizer_failures",
        "synthesized",
        "chunks",
        "priority",
        "dropped_by_generation",
        "muted_skips",
        "queued",
        "kinds",
        "verbosity",
        "tool_chatter",
        "muted",
    ):
        assert k in s
    assert s["verbosity"] == "minimal" and s["muted"] is False


def test_stop_is_prompt(make):
    h = make()
    h.jsonl("Hello.")
    t0 = time.monotonic()
    h.pipeline.stop()
    h.pipeline.join(timeout=3.0)
    assert not h.pipeline.is_alive()
    assert time.monotonic() - t0 < 2.0
