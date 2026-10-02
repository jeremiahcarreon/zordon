"""Silero VAD on onnxruntime (no torch) plus the speech gate that turns per-chunk
probabilities into ONSET / END events for the audio thread.

Timing model
------------
The browser sends 20 ms frames (320 samples at 16 kHz). Silero's ONNX graph
wants exactly 512-sample chunks (32 ms) with the previous chunk's last 64
samples prepended, so the gate rebuffers frames into chunks internally and the
VAD decision is made per chunk. The design's "3 consecutive speech frames
(60 ms)" is therefore mapped to chunks: ``ceil(onset_frames * frame_ms / 32)``,
which is 2 chunks (64 ms) for the defaults. Silence timing (``end_ms``), the
echo guard and the 30 s cap are measured on the ``now`` timestamps the caller
passes, so a test can drive the gate with explicit frame times and the audio
thread can use a monotonic clock.

The utterance handed back on END is every frame from ``preroll_ms`` before the
onset decision through the frame that triggered END, as int16 16 kHz bytes, so
the first ~64-80 ms of speech that produced the onset are always inside it.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from zordon import assets, paths
from zordon.providers import ProviderNotConfigured

log = logging.getLogger("zordon.speech.vad")

SAMPLE_RATE = 16000
VAD_CHUNK = 512  # samples per VAD call at 16 kHz (32 ms)
VAD_CONTEXT = 64  # samples of the previous chunk prepended (upstream OnnxWrapper semantics)


# ---- VAD models -------------------------------------------------------------------


class SileroVAD:
    """Torch-free Silero VAD (v5/v6 ONNX) on onnxruntime CPU, one thread.

    Feed 16 kHz float32 frames of exactly 512 samples. The LSTM state
    ``[2, 1, 128]`` and the 64-sample audio context are carried between calls.
    """

    SR = SAMPLE_RATE
    CHUNK = VAD_CHUNK
    CONTEXT = VAD_CONTEXT

    def __init__(self, model_path: str | Path) -> None:
        import onnxruntime as ort  # local import: ~90 ms, only needed by the real VAD

        so = ort.SessionOptions()
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        so.log_severity_level = 3
        self.model_path = str(model_path)
        self.sess = ort.InferenceSession(self.model_path, so, providers=["CPUExecutionProvider"])
        names = {i.name for i in self.sess.get_inputs()}
        self._has_sr = "sr" in names  # silero_vad_half.onnx has no 'sr' input
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """``frame``: float32, shape (512,) or (1, 512), range [-1, 1]. Returns P(speech)."""
        x = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != self.CHUNK:
            raise ValueError(f"need {self.CHUNK} samples, got {x.shape[1]}")
        x = np.concatenate([self._context, x], axis=1)  # -> (1, 576)
        feeds = {"input": x, "state": self._state}
        if self._has_sr:
            feeds["sr"] = np.array(self.SR, dtype=np.int64)  # 0-d int64
        out, state = self.sess.run(["output", "stateN"], feeds)
        self._state = state  # (2, 1, 128) float32
        self._context = x[:, -self.CONTEXT :]
        return float(out[0, 0])

    prob = __call__

    def feed_pcm16(self, pcm: bytes) -> float:
        """1024 bytes of int16 LE mono at 16 kHz -> P(speech)."""
        return self(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)


class FakeVAD:
    """Test double with the SileroVAD call surface.

    ``prob_fn`` is either a callable ``(chunk_f32) -> float`` or an iterable of
    probabilities consumed one per chunk (the last value repeats when exhausted).
    """

    SR = SAMPLE_RATE
    CHUNK = VAD_CHUNK
    CONTEXT = VAD_CONTEXT

    def __init__(self, prob_fn: Callable[[np.ndarray], float] | Iterable[float] = ()) -> None:
        if callable(prob_fn):
            self._fn: Callable[[np.ndarray], float] | None = prob_fn
            self._script: list[float] = []
        else:
            self._fn = None
            self._script = [float(p) for p in prob_fn]
        self._pos = 0
        self.calls = 0
        self.resets = 0
        self.last_chunk: np.ndarray | None = None

    def reset(self) -> None:
        self.resets += 1

    def __call__(self, frame: np.ndarray) -> float:
        x = np.asarray(frame, dtype=np.float32).reshape(-1)
        if x.shape[0] != self.CHUNK:
            raise ValueError(f"need {self.CHUNK} samples, got {x.shape[0]}")
        self.calls += 1
        self.last_chunk = x
        if self._fn is not None:
            return float(self._fn(x))
        if not self._script:
            return 0.0
        p = self._script[min(self._pos, len(self._script) - 1)]
        self._pos += 1
        return p

    prob = __call__

    def feed_pcm16(self, pcm: bytes) -> float:
        return self(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)


def models_dir_override(models_dir: Path | None = None) -> Path:
    """``ZORDON_TEST_MODELS`` wins, then the explicit argument, then ``paths.models_dir()``."""
    env = os.environ.get("ZORDON_TEST_MODELS")
    if env:
        return Path(env)
    return Path(models_dir) if models_dir is not None else paths.models_dir()


def make_vad(models_dir: Path | None = None) -> SileroVAD:
    """Load ``silero_vad.onnx`` from the models directory or explain how to get it."""
    model = models_dir_override(models_dir) / assets.SILERO_VAD.filename
    if not model.is_file():
        raise ProviderNotConfigured(
            f"Silero VAD model not found at {model}; run `zordon doctor --download` to fetch it"
        )
    vad = SileroVAD(model)
    log.info("loaded Silero VAD from %s", model)
    return vad


# ---- speech gate ------------------------------------------------------------------


class GateKind(StrEnum):
    NONE = "none"
    ONSET = "onset"
    END = "end"


@dataclass(slots=True)
class GateEvent:
    kind: GateKind
    utterance_pcm: bytes | None = None  # int16 LE 16 kHz, only on END
    duration_s: float = 0.0  # length of utterance_pcm, only on END
    prob: float | None = None  # VAD probability of the chunk completed by this frame
    speaking: bool = False  # gate state after this frame
    reason: str = ""  # END: "silence" | "max_duration" | "reset"

    @property
    def is_onset(self) -> bool:
        return self.kind is GateKind.ONSET

    @property
    def is_end(self) -> bool:
        return self.kind is GateKind.END


_NONE = GateEvent(GateKind.NONE)


def onset_chunks_for(onset_frames: int, frame_ms: float, chunk_samples: int = VAD_CHUNK) -> int:
    """Map the design's N consecutive 20 ms frames onto whole VAD chunks.

    3 x 20 ms = 60 ms -> ceil(60 / 32) = 2 chunks = 64 ms.
    """
    chunk_ms = chunk_samples * 1000.0 / SAMPLE_RATE
    return max(1, math.ceil(onset_frames * frame_ms / chunk_ms))


class SpeechGate:
    """Turns a stream of int16 16 kHz frames into ONSET / END events.

    ``feed`` is called once per frame with the frame's timestamp (``now``), whether
    playback is active and, when it is, when it started. The gate itself never
    looks at a wall clock unless ``now`` is omitted.
    """

    def __init__(
        self,
        vad: SileroVAD | FakeVAD,
        onset_frames: int = 3,
        end_ms: int = 700,
        echo_guard_ms: int = 120,
        threshold: float = 0.5,
        frame_ms: int = 20,
        *,
        preroll_ms: int = 300,
        max_utterance_s: float = 30.0,
        sample_rate: int = SAMPLE_RATE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vad = vad
        self.chunk_samples = int(getattr(vad, "CHUNK", VAD_CHUNK))
        self.onset_frames = onset_frames
        self.frame_ms = frame_ms
        self.onset_chunks = onset_chunks_for(onset_frames, frame_ms, self.chunk_samples)
        self.end_s = end_ms / 1000.0
        self.echo_guard_s = echo_guard_ms / 1000.0
        self.threshold = threshold
        self.preroll_samples = int(sample_rate * preroll_ms / 1000)
        self.max_utterance_s = max_utterance_s
        self.sample_rate = sample_rate
        self._clock = clock
        # Mutable state; see reset().
        self._pending = np.zeros(0, dtype=np.int16)
        self._preroll: deque[bytes] = deque()
        self._preroll_samples = 0
        self._utterance: list[bytes] = []
        self._speaking = False
        self._run = 0
        self._onset_at = 0.0
        self._last_speech_at = 0.0
        self._own_playback_started: float | None = None
        self.reset()

    @classmethod
    def from_config(cls, vad: SileroVAD | FakeVAD, voice, **kwargs) -> SpeechGate:
        """Build from ``config.voice`` (a ``VoiceConfig``)."""
        return cls(
            vad,
            onset_frames=int(voice.speech_onset_frames),
            end_ms=int(voice.speech_end_ms),
            echo_guard_ms=int(voice.echo_guard_ms),
            **kwargs,
        )

    # ---- state ------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def onset_at(self) -> float | None:
        return self._onset_at if self._speaking else None

    def reset(self) -> None:
        """Forget everything, including a half-collected utterance. Call on call start/end."""
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.int16)
        self._preroll.clear()
        self._preroll_samples = 0
        self._utterance = []
        self._speaking = False
        self._run = 0
        self._onset_at = 0.0
        self._last_speech_at = 0.0
        self._own_playback_started = None

    # ---- input --------------------------------------------------------------------

    def feed(
        self,
        frame: bytes,
        playback_active: bool = False,
        now: float | None = None,
        playback_started_at: float | None = None,
    ) -> GateEvent:
        """Consume one int16 frame (any whole number of samples; 320 is typical)."""
        now = self._clock() if now is None else now
        if len(frame) % 2:
            frame = frame[:-1]
        if not frame:
            return self._check_end(now, None)
        self._push_preroll(frame)
        if self._speaking:
            self._utterance.append(frame)
        guarded = self._echo_guarded(playback_active, now, playback_started_at)
        prob = None
        for prob in self._chunks(frame):
            self._update_run(prob >= self.threshold and not guarded, now)
            if not self._speaking and self._run >= self.onset_chunks:
                return self._begin(now, prob)
        return self._check_end(now, prob)

    def tick(self, now: float | None = None) -> GateEvent:
        """Time-only check, for when frames stop arriving mid-utterance (mute, pause)."""
        now = self._clock() if now is None else now
        return self._check_end(now, None)

    # ---- internals ----------------------------------------------------------------

    def _chunks(self, frame: bytes) -> Iterable[float]:
        samples = np.frombuffer(frame, dtype="<i2")
        self._pending = (
            samples if self._pending.size == 0 else np.concatenate([self._pending, samples])
        )
        while self._pending.size >= self.chunk_samples:
            chunk = self._pending[: self.chunk_samples]
            self._pending = self._pending[self.chunk_samples :]
            yield float(self.vad(chunk.astype(np.float32) / 32768.0))

    def _push_preroll(self, frame: bytes) -> None:
        self._preroll.append(frame)
        self._preroll_samples += len(frame) // 2
        while (
            self._preroll
            and self._preroll_samples - len(self._preroll[0]) // 2 >= self.preroll_samples
        ):
            self._preroll_samples -= len(self._preroll.popleft()) // 2

    def _echo_guarded(self, playback_active: bool, now: float, started_at: float | None) -> bool:
        if not playback_active:
            self._own_playback_started = None
            return False
        if started_at is None:
            if self._own_playback_started is None:
                self._own_playback_started = now
            started_at = self._own_playback_started
        return (now - started_at) < self.echo_guard_s

    def _update_run(self, is_speech: bool, now: float) -> None:
        if is_speech:
            self._run += 1
            self._last_speech_at = now
        else:
            self._run = 0

    def _begin(self, now: float, prob: float) -> GateEvent:
        self._speaking = True
        self._onset_at = now
        self._last_speech_at = now
        self._utterance = list(self._preroll)
        log.debug("speech onset (p=%.2f, preroll=%d frames)", prob, len(self._utterance))
        return GateEvent(GateKind.ONSET, prob=prob, speaking=True)

    def _check_end(self, now: float, prob: float | None) -> GateEvent:
        if not self._speaking:
            return (
                GateEvent(GateKind.NONE, prob=prob, speaking=False) if prob is not None else _NONE
            )
        if now - self._last_speech_at >= self.end_s:
            return self._end(prob, "silence")
        if now - self._onset_at >= self.max_utterance_s:
            return self._end(prob, "max_duration")
        return GateEvent(GateKind.NONE, prob=prob, speaking=True)

    def _end(self, prob: float | None, reason: str) -> GateEvent:
        pcm = b"".join(self._utterance)
        duration = len(pcm) / 2 / self.sample_rate
        self._utterance = []
        self._speaking = False
        self._run = 0
        log.debug("speech end (%s, %.2f s)", reason, duration)
        return GateEvent(
            GateKind.END,
            utterance_pcm=pcm,
            duration_s=duration,
            prob=prob,
            speaking=False,
            reason=reason,
        )
