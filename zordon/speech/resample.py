"""Sample-format and sample-rate helpers. Pure numpy, no I/O.

The WebSocket carries 16 kHz int16 mono; the VAD and STT want float32 in
[-1, 1] at 16 kHz; Kokoro emits 24 kHz float32. Linear interpolation is enough
for test audio and for feeding speech models (it is not a mastering-grade
resampler, and it does not low-pass before decimating).
"""

from __future__ import annotations

import numpy as np

INT16_SCALE = 32768.0


def pcm16_to_float32(pcm: bytes | np.ndarray) -> np.ndarray:
    """int16 little-endian mono PCM -> float32 in [-1, 1)."""
    if isinstance(pcm, np.ndarray):
        samples = pcm.astype(np.int16, copy=False)
    else:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        samples = np.frombuffer(pcm, dtype="<i2")
    return samples.astype(np.float32) / INT16_SCALE


def float32_to_pcm16(samples: np.ndarray) -> bytes:
    """float32 in [-1, 1] -> int16 little-endian bytes, clipped."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0 - 1.0 / INT16_SCALE)
    return (x * INT16_SCALE).astype("<i2").tobytes()


def resample_float32(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample of a float32 mono signal."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    if src_rate == dst_rate or x.size == 0:
        return x.copy()
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    # Output sample k sits at source position k * src/dst.
    positions = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    source_index = np.arange(x.size, dtype=np.float64)
    return np.interp(positions, source_index, x.astype(np.float64)).astype(np.float32)


def resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample int16 little-endian mono PCM bytes between two rates."""
    if src_rate == dst_rate:
        return bytes(pcm)
    return float32_to_pcm16(resample_float32(pcm16_to_float32(pcm), src_rate, dst_rate))


def duration_s(pcm: bytes, sample_rate: int, sample_width: int = 2) -> float:
    """Length in seconds of a mono PCM buffer."""
    if sample_rate <= 0:
        return 0.0
    return len(pcm) / sample_width / sample_rate
