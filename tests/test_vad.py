"""Real Silero VAD on the test models. Skipped unless ZORDON_TEST_MODELS points at a
directory holding silero_vad.onnx and kokoro_test_16k.wav."""

from __future__ import annotations

import logging
import os
import statistics
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from zordon.providers import ProviderNotConfigured
from zordon.speech.resample import pcm16_to_float32
from zordon.speech.vad import VAD_CHUNK, FakeVAD, GateKind, SileroVAD, SpeechGate, make_vad

log = logging.getLogger("zordon.tests.vad")

pytestmark = pytest.mark.provider


def _models_dir() -> Path:
    env = os.environ.get("ZORDON_TEST_MODELS")
    if not env:
        pytest.skip("ZORDON_TEST_MODELS not set")
    d = Path(env)
    if not (d / "silero_vad.onnx").is_file() or not (d / "kokoro_test_16k.wav").is_file():
        pytest.skip(f"test models missing under {d}")
    return d


@pytest.fixture(scope="module")
def models_dir() -> Path:
    return _models_dir()


@pytest.fixture(scope="module")
def vad(models_dir: Path) -> SileroVAD:
    return SileroVAD(models_dir / "silero_vad.onnx")


def read_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000
        return pcm16_to_float32(w.readframes(w.getnframes()))


def chunks(audio: np.ndarray) -> list[np.ndarray]:
    n = audio.shape[0] // VAD_CHUNK
    return [audio[i * VAD_CHUNK : (i + 1) * VAD_CHUNK] for i in range(n)]


def noise(seconds: float, dbfs: float = -50.0, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rms = 10 ** (dbfs / 20)
    return (rng.standard_normal(int(seconds * 16000)) * rms).astype(np.float32)


def test_speech_clip_scores_high(vad: SileroVAD):
    vad.reset()
    probs = [vad(c) for c in chunks(read_wav_16k(_models_dir() / "kokoro_test_16k.wav"))]
    mean = statistics.fmean(probs)
    log.info("speech clip: %d chunks, mean P=%.3f, max=%.3f", len(probs), mean, max(probs))
    assert mean > 0.6
    assert sum(p > 0.5 for p in probs) / len(probs) > 0.6


def test_low_noise_scores_low(vad: SileroVAD):
    vad.reset()
    probs = [vad(c) for c in chunks(noise(2.0))]
    log.info("noise: %d chunks, max P=%.3f", len(probs), max(probs))
    assert max(probs) < 0.3


def test_per_chunk_latency_under_2ms(vad: SileroVAD):
    vad.reset()
    audio = read_wav_16k(_models_dir() / "kokoro_test_16k.wav")
    timings = []
    for c in chunks(audio):
        t0 = time.perf_counter()
        vad(c)
        timings.append((time.perf_counter() - t0) * 1000.0)
    timings_sorted = sorted(timings)
    p95 = timings_sorted[int(len(timings_sorted) * 0.95) - 1]
    log.info(
        "VAD per-chunk latency: mean %.3f ms, p95 %.3f ms, max %.3f ms",
        statistics.fmean(timings),
        p95,
        max(timings),
    )
    print(
        f"VAD per-chunk latency: mean {statistics.fmean(timings):.3f} ms, p95 {p95:.3f} ms, max {max(timings):.3f} ms"
    )
    assert statistics.median(timings) < 2.0
    assert p95 < 2.0


def test_rejects_wrong_chunk_size(vad: SileroVAD):
    with pytest.raises(ValueError):
        vad(np.zeros(320, dtype=np.float32))


def test_feed_pcm16_matches_float_call(vad: SileroVAD):
    audio = read_wav_16k(_models_dir() / "kokoro_test_16k.wav")
    c = chunks(audio)[40]
    vad.reset()
    a = vad(c)
    vad.reset()
    b = vad.feed_pcm16((c * 32768.0).astype("<i2").tobytes())
    assert a == pytest.approx(b, abs=1e-3)


def test_gate_with_real_vad_detects_the_utterance(models_dir: Path):
    gate = SpeechGate(SileroVAD(models_dir / "silero_vad.onnx"))
    audio = read_wav_16k(models_dir / "kokoro_test_16k.wav")
    padded = np.concatenate([noise(0.5, seed=1), audio, noise(1.5, seed=2)])
    pcm = (padded * 32768.0).astype("<i2").tobytes()
    kinds = []
    for i in range(len(pcm) // 640):
        ev = gate.feed(pcm[i * 640 : (i + 1) * 640], now=i * 0.02)
        kinds.append((i, ev))
    onsets = [i for i, ev in kinds if ev.kind is GateKind.ONSET]
    ends = [ev for _, ev in kinds if ev.kind is GateKind.END]
    assert len(onsets) == 1, onsets
    # Speech energy begins 71 ms into the clip, after 500 ms of noise.
    assert 0.5 <= onsets[0] * 0.02 <= 0.5 + 0.071 + 0.2
    assert len(ends) == 1
    assert ends[0].duration_s > 3.5


def test_make_vad_reads_test_models_dir(models_dir: Path):
    vad = make_vad()
    assert isinstance(vad, SileroVAD)
    assert Path(vad.model_path) == models_dir / "silero_vad.onnx"


def test_make_vad_missing_model_has_doctor_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ZORDON_TEST_MODELS", raising=False)
    with pytest.raises(ProviderNotConfigured) as e:
        make_vad(tmp_path)
    assert "zordon doctor" in str(e.value)
    assert isinstance(FakeVAD(), FakeVAD)
