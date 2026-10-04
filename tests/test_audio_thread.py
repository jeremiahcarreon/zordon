"""AudioThread end to end with a FakeVAD, a FakeSTT and a fake clock.

Frames are pushed as ``AudioFrame`` with explicit timestamps 20 ms apart while
the test sleeps 5 ms between them; the fake clock is advanced in lockstep so
playback timing and frame timing share one time base. Barge-in latency is
measured with ``time.perf_counter`` on the real wall clock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from zordon.bus import Bus, Flush, SpeechChunk, TranscriptRow, Utterance
from zordon.speech.audio_thread import AudioFrame, AudioThread
from zordon.speech.stt.fake import FakeSTT
from zordon.speech.vad import FakeVAD, SpeechGate

log = logging.getLogger("zordon.tests.audio_thread")

FRAME_S = 0.02
SPEECH = (np.full(320, 8000, dtype=np.int16)).tobytes()
SILENCE = bytes(640)
PUSH_SLEEP = 0.005


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.t

    def set(self, t: float) -> None:
        with self._lock:
            self.t = t


class FakeStore:
    def __init__(self) -> None:
        self.unspoken: list[int] = []
        self.events: list[tuple[str, str, str]] = []
        self._n = 0

    def mark_unspoken(self, sentence_id: int) -> None:
        self.unspoken.append(sentence_id)

    def add_event(self, session_id: str, kind: str, text: str, ts: float | None = None) -> int:
        self._n += 1
        self.events.append((session_id, kind, text))
        return self._n


class Collector:
    """Drains bus.client_events into a list and waits for predicates."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.events: list[Any] = []

    def pump(self) -> None:
        while not self.bus.client_events.empty():
            self.events.append(self.bus.client_events.get_nowait())

    def wait_for(self, pred: Callable[[Any], bool], timeout: float = 2.0) -> Any:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.pump()
            for ev in self.events:
                if pred(ev):
                    return ev
            time.sleep(0.001)
        self.pump()
        raise AssertionError(f"no event matched within {timeout}s; have {self.events!r}")

    def none_match(self, pred: Callable[[Any], bool], settle: float = 0.05) -> None:
        time.sleep(settle)
        self.pump()
        matched = [e for e in self.events if pred(e)]
        assert not matched, matched

    def of(self, typ: type) -> list[Any]:
        self.pump()
        return [e for e in self.events if isinstance(e, typ)]


def chunk(sentence_id: int, generation: int, seconds: float = 2.0, seq: int = 0) -> SpeechChunk:
    return SpeechChunk(
        sentence_id=sentence_id,
        seq=seq,
        generation=generation,
        pcm=bytes(int(seconds * 16000) * 2),
        sample_rate=16000,
    )


class Harness:
    def __init__(self, stt_text: str = "add retry logic to the upload handler") -> None:
        self.bus = Bus()
        self.clock = FakeClock(1000.0)
        self.vad = FakeVAD(lambda c: 1.0 if float(np.abs(c).max()) > 0.05 else 0.0)
        self.gate = SpeechGate(self.vad, onset_frames=3, end_ms=700, echo_guard_ms=120)
        self.stt = FakeSTT(stt_text, confidence=0.93)
        self.store = FakeStore()
        self.playback_states: list[bool] = []
        self.thread = AudioThread(
            self.bus,
            config=None,
            gate=self.gate,
            stt=self.stt,
            store=self.store,
            on_playback_state=self.playback_states.append,
            session_id_fn=lambda: "sess-1",
            clock=self.clock,
        )
        self.collector = Collector(self.bus)
        self.t = self.clock()

    def start(self) -> None:
        self.thread.start()
        self.thread.call_started("client-a")

    def stop(self) -> None:
        self.thread.stop(timeout=2.0)
        assert not self.thread.is_alive()

    def play(
        self, sentence_id: int, seconds: float = 2.0, generation: int | None = None
    ) -> SpeechChunk:
        c = chunk(sentence_id, self.bus.generation if generation is None else generation, seconds)
        self.bus.playback.put(c)
        return c

    def push(self, pcm: bytes) -> AudioFrame:
        """One 20 ms frame at the current fake time; then advance time by 20 ms."""
        frame = AudioFrame(pcm, ts=self.t)
        self.bus.inbound_audio.put(frame)
        self.t += FRAME_S
        time.sleep(PUSH_SLEEP)
        self.clock.set(self.t)
        return frame

    def push_many(self, pcm: bytes, n: int) -> None:
        for _ in range(n):
            self.push(pcm)

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.clock.set(self.t)


@pytest.fixture
def h():
    harness = Harness()
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


def is_flush(ev: Any) -> bool:
    return isinstance(ev, Flush)


# ---- barge-in ---------------------------------------------------------------------


def test_bargein_flushes_within_150ms_and_marks_sentence_unspoken(h: Harness):
    gen0 = h.bus.generation
    played = h.play(sentence_id=7, seconds=2.0)
    h.collector.wait_for(lambda e: e is played)
    assert h.thread.playback_active
    assert h.playback_states == [True]

    # Past the echo guard, then a burst of chunks the pipeline is still producing.
    h.advance(0.3)
    extra = [h.play(sentence_id=8, seconds=1.0) for _ in range(20)]
    h.advance(0.0)

    t_first_speech = time.perf_counter()
    h.push(SPEECH)
    flush = None
    for _ in range(10):
        h.push(SPEECH)
        h.collector.pump()
        flushes = h.collector.of(Flush)
        if flushes:
            flush = flushes[0]
            break
    assert flush is not None, "no Flush published"
    latency = time.perf_counter() - t_first_speech
    log.info("barge-in: first speech frame -> Flush in %.1f ms", latency * 1000)
    print(f"barge-in latency (first speech frame -> Flush): {latency * 1000:.1f} ms")
    assert latency < 0.15

    assert flush.generation == gen0 + 1 == h.bus.generation
    assert flush.interrupted_sentence_id == 7
    assert h.store.unspoken == [7]
    assert h.thread.last_bargein_latency_ms is not None
    assert h.thread.last_bargein_latency_ms < 150
    hist = h.thread.bargein_histogram()
    assert sum(hist.values()) == 1 and h.thread.bargein_count == 1

    # Playback queue was drained: every extra chunk was either forwarded before the
    # onset or dropped by the drain, and nothing is left.
    assert h.bus.playback.empty()
    forwarded_extra = [e for e in h.collector.of(SpeechChunk) if e in extra]
    assert len(forwarded_extra) + h.thread.drained_chunks == len(extra)
    assert not h.thread.playback_active
    assert h.playback_states == [True, False]

    # While the user is still speaking nothing is forwarded, old or new generation
    # (CONC-6): the new chunk stays queued instead of talking over the user.
    old = h.play(sentence_id=8, seconds=0.5, generation=gen0)
    new = h.play(sentence_id=9, seconds=0.5)
    h.push_many(SPEECH, 5)
    h.collector.none_match(lambda e: e is new or e is old)
    assert h.gate.speaking and not h.thread.playback_active
    # The utterance ends (700 ms of silence) and the hold lifts: the old chunk is dropped,
    # the new one passes, after the Flush.
    h.push_many(SILENCE, 40)
    h.advance(0.2)
    h.collector.wait_for(lambda e: e is new)
    assert old not in h.collector.events
    assert h.thread.dropped_chunks >= 1
    assert h.collector.events.index(flush) < h.collector.events.index(new)


def test_no_playback_while_the_user_speaks_and_a_second_bargein_can_fire(h: Harness):
    """CONC-6: after a barge-in, chunks of the new generation wait until the utterance
    ends, so Claude does not talk over the user and the next sentence can be barged
    into as well."""
    first = h.play(sentence_id=1, seconds=2.0)
    h.collector.wait_for(lambda e: e is first)
    h.advance(0.3)
    h.push_many(SPEECH, 5)
    flush1 = h.collector.wait_for(is_flush)
    assert flush1.interrupted_sentence_id == 1 and h.thread.bargein_count == 1
    assert h.gate.speaking

    # The pipeline keeps producing: a new-generation sentence lands while the user talks.
    second = h.play(sentence_id=2, seconds=2.0)
    h.push_many(SPEECH, 10)
    h.collector.none_match(lambda e: e is second)
    assert not h.thread.playback_active
    assert h.thread.held_chunks > 0
    assert h.bus.generation == flush1.generation  # no spurious second bump

    # The user stops; after the end of the utterance plus the short grace the chunk plays.
    h.push_many(SILENCE, 40)
    h.advance(0.2)
    h.collector.wait_for(lambda e: e is second)
    assert h.thread.playback_active
    utt = _wait_queue(h.bus.utterances)
    assert utt.text

    # The user speaks again over sentence 2: a second barge-in fires.
    h.advance(0.3)
    h.push_many(SPEECH, 8)
    flushes = [e for e in h.collector.of(Flush)]
    assert len(flushes) >= 2, "second barge-in did not fire"
    assert flushes[-1].interrupted_sentence_id == 2
    assert h.thread.bargein_count == 2
    assert h.store.unspoken == [1, 2]


def test_speech_without_playback_holds_new_chunks_until_the_utterance_ends(h: Harness):
    h.push_many(SPEECH, 10)
    assert h.gate.speaking
    c = h.play(sentence_id=5, seconds=1.0)
    h.push_many(SPEECH, 5)
    h.collector.none_match(lambda e: e is c)
    h.push_many(SILENCE, 40)
    h.advance(0.2)
    h.collector.wait_for(lambda e: e is c)


def test_onset_without_playback_is_not_a_bargein(h: Harness):
    gen0 = h.bus.generation
    h.push_many(SPEECH, 10)
    h.collector.none_match(is_flush)
    assert h.bus.generation == gen0
    assert h.store.unspoken == []
    assert h.gate.speaking


# ---- utterance -> STT -> bus ---------------------------------------------------------


def test_end_of_speech_publishes_utterance_but_no_user_row(h: Harness):
    h.push_many(SPEECH, 25)  # 500 ms of speech
    h.push_many(SILENCE, 40)  # 800 ms of silence -> END
    utt = _wait_queue(h.bus.utterances)
    assert isinstance(utt, Utterance)
    assert utt.text == "add retry logic to the upload handler"
    assert utt.confidence == pytest.approx(0.93)
    assert utt.source == "voice"
    assert utt.client_id == "client-a"

    # No transcript row yet: the utterance is a draft until it is sent (decision 0019).
    time.sleep(0.1)
    assert not any(isinstance(e, TranscriptRow) for e in h.collector.events)
    assert h.store.events == []
    # The STT saw the utterance: pre-roll + speech + trailing silence, as float32.
    assert h.stt.call_count == 1
    audio = h.stt.calls[0]
    assert audio.dtype == np.float32
    assert 0.5 + 0.7 <= audio.shape[0] / 16000 <= 0.5 + 0.3 + 0.8 + 0.05
    assert float(np.abs(audio).max()) == pytest.approx(8000 / 32768, abs=1e-4)
    assert h.thread.utterances_sent == 1


def test_end_requires_700ms_of_silence(h: Harness):
    h.push_many(SPEECH, 25)
    h.push_many(SILENCE, 30)  # 600 ms: not yet
    time.sleep(0.05)
    assert h.bus.utterances.empty()
    assert h.stt.call_count == 0
    assert h.gate.speaking
    h.push_many(SILENCE, 8)  # 760 ms total (the last speech chunk may close up to 40 ms late)
    utt = _wait_queue(h.bus.utterances)
    assert utt.text
    assert not h.gate.speaking


def test_empty_transcription_publishes_nothing():
    harness = Harness(stt_text="")
    harness.start()
    try:
        harness.push_many(SPEECH, 25)
        harness.push_many(SILENCE, 40)
        _wait_until(lambda: harness.stt.call_count == 1)
        time.sleep(0.05)
        assert harness.bus.utterances.empty()
        assert harness.collector.of(TranscriptRow) == []
    finally:
        harness.stop()


def test_frames_stopping_mid_utterance_still_ends_it(h: Harness):
    h.push_many(SPEECH, 25)
    assert h.gate.speaking
    # No more frames (muted); only time passes.
    h.advance(0.8)
    utt = _wait_queue(h.bus.utterances)
    assert utt.text


def test_call_ended_resets_gate(h: Harness):
    h.push_many(SPEECH, 25)
    assert h.gate.speaking
    h.thread.call_ended()
    _wait_until(lambda: not h.gate.speaking)
    h.advance(1.0)
    time.sleep(0.05)
    assert h.bus.utterances.empty()
    assert h.stt.call_count == 0


# ---- one caller at a time -----------------------------------------------------------------


def test_second_client_cannot_join_the_call_or_reset_the_gate(h: Harness):
    """CONC-9: client-a is in the call; client-b's start is refused and its end/pause/resume
    do not touch the gate under client-a's utterance."""
    assert h.thread.in_call and h.thread.client_id == "client-a"
    assert h.thread.call_started("client-b") is False
    assert h.thread.client_id == "client-a"
    h.push_many(SPEECH, 25)
    assert h.gate.speaking
    h.thread.call_ended("client-b")
    h.thread.call_paused("client-b")
    h.thread.call_resumed("client-b")
    time.sleep(0.05)
    assert h.gate.speaking, "a non-caller reset the gate"
    assert h.thread.in_call
    # The caller's own utterance completes normally.
    h.push_many(SILENCE, 40)
    utt = _wait_queue(h.bus.utterances)
    assert utt.client_id == "client-a"
    # The caller may re-start (reconnect) and may end; then another client may join.
    assert h.thread.call_started("client-a") is True
    h.thread.call_ended("client-a")
    assert not h.thread.in_call
    assert h.thread.call_started("client-b") is True
    assert h.thread.client_id == "client-b"


# ---- echo guard -------------------------------------------------------------------


def test_echo_guard_suppresses_onset_in_first_120ms_of_playback(h: Harness):
    played = h.play(sentence_id=3, seconds=3.0)
    h.collector.wait_for(lambda e: e is played)
    started = h.thread.playback_started_at
    assert started == pytest.approx(h.clock())

    # Speech right as playback starts: 6 frames = 120 ms, all inside the guard.
    h.push_many(SPEECH, 6)
    h.collector.none_match(is_flush)
    assert not h.gate.speaking

    # Keep talking: two more chunks past the guard -> barge-in.
    h.push_many(SPEECH, 6)
    flush = h.collector.wait_for(is_flush)
    assert flush.interrupted_sentence_id == 3
    assert h.store.unspoken == [3]


def test_plain_bytes_frames_are_accepted(h: Harness):
    for _ in range(10):
        h.bus.inbound_audio.put(SPEECH)
        time.sleep(PUSH_SLEEP)
    _wait_until(lambda: h.gate.speaking)


def test_playback_active_expires_after_chunk_duration(h: Harness):
    played = h.play(sentence_id=1, seconds=0.5)
    h.collector.wait_for(lambda e: e is played)
    assert h.thread.playback_active
    h.advance(0.6)
    _wait_until(lambda: not h.thread.playback_active)
    assert h.playback_states == [True, False]
    # Speech now is a normal onset, not a barge-in.
    gen0 = h.bus.generation
    h.push_many(SPEECH, 6)
    h.collector.none_match(is_flush)
    assert h.bus.generation == gen0


def test_stats_snapshot(h: Harness):
    s = h.thread.stats()
    assert set(s) >= {
        "forwarded_chunks",
        "dropped_chunks",
        "bargein_count",
        "bargein_histogram",
        "playback_active",
        "held_chunks",
        "in_call",
    }
    assert list(s["bargein_histogram"]) == [
        "<=5ms",
        "<=10ms",
        "<=25ms",
        "<=50ms",
        "<=100ms",
        "<=150ms",
        ">150ms",
    ]


# ---- helpers ----------------------------------------------------------------------


def _wait_queue(q, timeout: float = 2.0):
    import queue

    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("nothing arrived on the queue") from None


def _wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return
        time.sleep(0.002)
    raise AssertionError("condition not met in time")


def test_partial_transcripts_while_still_talking():
    """With partials on, the utterance so far is transcribed every interval and published as
    a partial Heard; the final Heard follows at the end; a CPU recogniser gets none in auto."""
    from zordon.bus import Heard
    from zordon.config import Config

    cfg = Config()
    cfg.voice.partial_transcripts = "on"
    cfg.voice.partial_interval_ms = 300
    harness = Harness()
    harness.thread.config = cfg
    harness.start()
    try:
        harness.push_many(SPEECH, 60)  # 1.2 s of speech: at least one partial pass
        heard = harness.collector.wait_for(lambda e: isinstance(e, Heard) and e.partial)
        assert heard.text == "add retry logic to the upload handler" and harness.thread.partials_sent >= 1
        harness.push_many(SILENCE, 40)
        final = harness.collector.wait_for(lambda e: isinstance(e, Heard) and not e.partial)
        assert final.text == heard.text
        assert harness.stt.call_count >= 2
    finally:
        harness.stop()
    # auto + a CPU recogniser: no partials (a pass costs most of a second there).
    cfg.voice.partial_transcripts = "auto"
    harness2 = Harness()
    harness2.thread.config = cfg
    assert not harness2.thread.partials_enabled()
    harness2.stt.device = "cuda"
    assert harness2.thread.partials_enabled()
