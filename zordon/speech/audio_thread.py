"""AudioThread: inbound frames -> VAD gate -> utterance -> STT -> ``bus.utterances``,
plus the barge-in kill switch.

Owns ``bus.inbound_audio``, ``bus.playback``, the playback generation and the
``playback_active`` estimate. Everything latency-critical (barge-in) happens on
this thread with no network round trip; STT runs on a one-worker executor so
the loop never blocks on it.

Loop, each iteration (about 5 ms when idle):

1. forward ``SpeechChunk`` from ``bus.playback`` to ``bus.client_events``, dropping
   chunks whose generation is older than ``bus.generation``;
2. take every waiting frame from ``bus.inbound_audio`` and feed the gate with
   the current ``playback_active`` flag;
   - ONSET while playback is active -> barge-in: bump the generation, drain
     ``bus.playback``, publish ``Flush``, mark the interrupted sentence unspoken;
   - END -> hand the utterance to the STT worker;
3. refresh ``playback_active`` and let the gate time out an utterance when frames
   stopped arriving (mute, pause).

``playback_active`` is an estimate of what the client is hearing: chunks are
forwarded faster than real time, so the thread keeps a playback timeline
(cumulative chunk durations from the first forward) and the flag stays true
until that timeline runs out.

Frames on ``bus.inbound_audio`` are normally plain ``bytes`` (stamped with the
clock when dequeued); an ``AudioFrame`` carries its own timestamp so tests can
drive the gate deterministically.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from zordon.bus import Bus, Flush, Notice, SpeechChunk, TranscriptRow, Utterance, drain
from zordon.providers import ProviderError, STTProvider
from zordon.speech.resample import pcm16_to_float32
from zordon.speech.vad import GateKind, SpeechGate

log = logging.getLogger("zordon.speech.audio_thread")

IDLE_WAIT_S = 0.005
MAX_FORWARD_PER_LOOP = 64
LATENCY_BUCKETS_MS = (5.0, 10.0, 25.0, 50.0, 100.0, 150.0, float("inf"))


@dataclass(slots=True)
class AudioFrame:
    """A frame with an explicit timestamp (same clock as the thread's ``clock``)."""

    pcm: bytes
    ts: float | None = None
    arrived: float = field(default_factory=time.perf_counter)


class AudioThread(threading.Thread):
    def __init__(
        self,
        bus: Bus,
        config: Any,
        gate: SpeechGate | Any,
        stt: STTProvider,
        store: Any,
        on_playback_state: Callable[[bool], None] | None = None,
        *,
        session_id_fn: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="zordon-audio", daemon=True)
        self.bus = bus
        self.config = config
        self.gate = _as_gate(gate, config)
        self.stt = stt
        self.store = store
        self.on_playback_state = on_playback_state
        self._session_id_fn = session_id_fn
        self._clock = clock
        self._stop_event = threading.Event()
        self._reset_gate = threading.Event()
        self._client_id = ""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zordon-stt")
        # Playback estimate.
        self._playback_until = 0.0
        self._playback_started_at: float | None = None
        self._playback_active = False
        self._timeline: deque[tuple[float, int]] = deque()  # (ends_at, sentence_id)
        self._last_sentence_id: int | None = None
        # Counters (read from other threads; ints are fine).
        self.forwarded_chunks = 0
        self.dropped_chunks = 0
        self.drained_chunks = 0
        self.bargein_count = 0
        self.utterances_sent = 0
        self.pending_transcriptions = 0
        self.last_bargein_latency_ms: float | None = None
        self._latency_hist: Counter[float] = Counter()

    # ---- public control (any thread) ----------------------------------------------

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)
        self._executor.shutdown(wait=False)

    def call_started(self, client_id: str) -> None:
        self._client_id = client_id
        self._reset_gate.set()

    def call_ended(self) -> None:
        self._reset_gate.set()

    def call_paused(self) -> None:
        self._reset_gate.set()

    def call_resumed(self) -> None:
        self._reset_gate.set()

    @property
    def playback_active(self) -> bool:
        return self._playback_active

    @property
    def playback_started_at(self) -> float | None:
        return self._playback_started_at

    @property
    def client_id(self) -> str:
        return self._client_id

    def bargein_histogram(self) -> dict[str, int]:
        """Flush latency histogram, bucket upper bound in ms -> count."""
        out: dict[str, int] = {}
        for upper in LATENCY_BUCKETS_MS:
            label = f"<={upper:g}ms" if upper != float("inf") else ">150ms"
            out[label] = self._latency_hist.get(upper, 0)
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "forwarded_chunks": self.forwarded_chunks,
            "dropped_chunks": self.dropped_chunks,
            "drained_chunks": self.drained_chunks,
            "bargein_count": self.bargein_count,
            "last_bargein_latency_ms": self.last_bargein_latency_ms,
            "bargein_histogram": self.bargein_histogram(),
            "utterances_sent": self.utterances_sent,
            "pending_transcriptions": self.pending_transcriptions,
            "playback_active": self._playback_active,
        }

    # ---- loop ---------------------------------------------------------------------

    def run(self) -> None:
        log.info("audio thread started")
        try:
            while not self._stopping():
                self._service_requests()
                forwarded = self._forward_playback()
                frame = self._next_frame(0.0 if forwarded else IDLE_WAIT_S)
                while frame is not None:
                    self._handle_frame(frame)
                    frame = self._next_frame(0.0)
                self._refresh_playback_state()
                self._tick_gate()
        except Exception:  # noqa: BLE001 - keep the agent alive, report the bug
            log.exception("audio thread crashed")
            self.bus.publish(Notice(text="Audio thread stopped unexpectedly", level="error"))
        finally:
            self._executor.shutdown(wait=False)
            log.info("audio thread stopped")

    def _stopping(self) -> bool:
        return self._stop_event.is_set() or self.bus.stop.is_set()

    def _service_requests(self) -> None:
        if self._reset_gate.is_set():
            self._reset_gate.clear()
            self.gate.reset()

    # ---- playback -----------------------------------------------------------------

    def _forward_playback(self) -> bool:
        did = False
        for _ in range(MAX_FORWARD_PER_LOOP):
            try:
                chunk = self.bus.playback.get_nowait()
            except queue.Empty:
                break
            did = True
            if chunk.generation < self.bus.generation:
                self.dropped_chunks += 1
                continue
            self._forward_chunk(chunk)
        return did

    def _forward_chunk(self, chunk: SpeechChunk) -> None:
        now = self._clock()
        start = max(now, self._playback_until)
        ends_at = start + _chunk_duration(chunk)
        self._timeline.append((ends_at, chunk.sentence_id))
        self._playback_until = ends_at
        self._last_sentence_id = chunk.sentence_id
        self.bus.publish(chunk)
        self.forwarded_chunks += 1
        self._set_playback_active(True, now)

    def _refresh_playback_state(self) -> None:
        now = self._clock()
        self._set_playback_active(now < self._playback_until, now)

    def _set_playback_active(self, active: bool, now: float) -> None:
        if active == self._playback_active:
            return
        self._playback_active = active
        self._playback_started_at = now if active else None
        if not active:
            self._timeline.clear()
        if self.on_playback_state is not None:
            try:
                self.on_playback_state(active)
            except Exception:  # noqa: BLE001
                log.exception("on_playback_state callback failed")

    def _current_sentence_id(self, now: float) -> int | None:
        while self._timeline and self._timeline[0][0] <= now:
            self._timeline.popleft()
        if self._timeline:
            return self._timeline[0][1]
        return self._last_sentence_id

    # ---- inbound frames -----------------------------------------------------------

    def _next_frame(self, timeout: float) -> bytes | AudioFrame | None:
        try:
            if timeout > 0:
                return self.bus.inbound_audio.get(timeout=timeout)
            return self.bus.inbound_audio.get_nowait()
        except queue.Empty:
            return None

    def _handle_frame(self, item: bytes | AudioFrame) -> None:
        if isinstance(item, AudioFrame):
            pcm, ts, arrived = item.pcm, item.ts, item.arrived
        else:
            pcm, ts, arrived = bytes(item), None, time.perf_counter()
        now = self._clock() if ts is None else ts
        self._set_playback_active(now < self._playback_until, now)
        event = self.gate.feed(
            pcm,
            playback_active=self._playback_active,
            now=now,
            playback_started_at=self._playback_started_at,
        )
        if event.kind is GateKind.ONSET:
            if self._playback_active:
                self._barge_in(now, arrived)
        elif event.kind is GateKind.END:
            self._submit_transcription(event.utterance_pcm or b"", event.duration_s)

    def _tick_gate(self) -> None:
        if not self.gate.speaking:
            return
        event = self.gate.tick(self._clock())
        if event.kind is GateKind.END:
            self._submit_transcription(event.utterance_pcm or b"", event.duration_s)

    # ---- barge-in -----------------------------------------------------------------

    def _barge_in(self, now: float, arrived: float) -> None:
        generation = self.bus.next_generation()
        self.drained_chunks += drain(self.bus.playback)
        sentence_id = self._current_sentence_id(now)
        self.bus.publish(Flush(generation=generation, interrupted_sentence_id=sentence_id))
        latency_ms = (time.perf_counter() - arrived) * 1000.0
        self._record_latency(latency_ms)
        self._playback_until = 0.0
        self._set_playback_active(False, now)
        if sentence_id is not None:
            try:
                self.store.mark_unspoken(sentence_id)
            except Exception:  # noqa: BLE001
                log.exception("mark_unspoken(%s) failed", sentence_id)
        log.info(
            "barge-in: generation %d, sentence %s, flush after %.1f ms",
            generation,
            sentence_id,
            latency_ms,
        )

    def _record_latency(self, latency_ms: float) -> None:
        self.bargein_count += 1
        self.last_bargein_latency_ms = latency_ms
        for upper in LATENCY_BUCKETS_MS:
            if latency_ms <= upper:
                self._latency_hist[upper] += 1
                break

    # ---- speech to text -----------------------------------------------------------

    def _submit_transcription(self, pcm: bytes, duration_s: float) -> None:
        if not pcm:
            return
        self.pending_transcriptions += 1
        self._executor.submit(
            self._transcribe_job, pcm16_to_float32(pcm), duration_s, self._client_id
        )

    def _transcribe_job(self, audio, duration_s: float, client_id: str) -> None:
        try:
            result = self.stt.transcribe(audio)
        except ProviderError as e:
            log.warning("STT failed: %s", e)
            self.bus.publish(Notice(text=f"Speech recognition failed: {e}", level="warning"))
            return
        except Exception:  # noqa: BLE001
            log.exception("STT provider raised")
            return
        finally:
            self.pending_transcriptions -= 1
        text = (result.text or "").strip()
        if not text:
            log.debug("empty transcription for %.2f s utterance", duration_s)
            return
        self.bus.utterances.put(
            Utterance(text=text, confidence=result.confidence, source="voice", client_id=client_id)
        )
        self.utterances_sent += 1
        self._record_user_row(text)
        log.info("utterance (%.2f s, conf=%s): %s", duration_s, _fmt(result.confidence), text)

    def _record_user_row(self, text: str) -> None:
        session_id = self._session_id()
        try:
            event_id = self.store.add_event(session_id, "user", text)
        except Exception:  # noqa: BLE001
            log.exception("transcript add_event failed")
            event_id = 0
        self.bus.publish(
            TranscriptRow(
                row_id=-int(event_id),
                session_id=session_id,
                kind="user",
                text=text,
                raw_lines=[],
                ts=time.time(),
            )
        )

    def _session_id(self) -> str:
        if self._session_id_fn is None:
            return ""
        try:
            return self._session_id_fn() or ""
        except Exception:  # noqa: BLE001
            log.exception("session_id_fn failed")
            return ""


def _as_gate(gate_or_vad: Any, config: Any) -> SpeechGate:
    """Accept a ready SpeechGate or a bare VAD (then build the gate from ``config.voice``)."""
    if isinstance(gate_or_vad, SpeechGate) or hasattr(gate_or_vad, "feed"):
        return gate_or_vad
    voice = getattr(config, "voice", None)
    if voice is None:
        return SpeechGate(gate_or_vad)
    return SpeechGate.from_config(gate_or_vad, voice)


def _chunk_duration(chunk: SpeechChunk) -> float:
    if chunk.sample_rate <= 0:
        return 0.0
    return len(chunk.pcm) / 2 / chunk.sample_rate


def _fmt(confidence: float | None) -> str:
    return "n/a" if confidence is None else f"{confidence:.2f}"
