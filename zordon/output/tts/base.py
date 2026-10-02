"""Shared helpers for TTSProvider implementations.

Every provider yields int16 little-endian mono PCM at its ``sample_rate``. The
helpers here do the two conversions each one needs: float32 samples to int16
bytes, and a byte stream to whole-sample chunks (an HTTP chunk boundary can fall
between the two bytes of one sample; ``SampleAligner`` carries the odd byte).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import httpx
import numpy as np

from zordon.providers import ProviderError, ProviderNotConfigured, TTSProvider

__all__ = [
    "BYTES_PER_SAMPLE",
    "CHUNK_SAMPLES",
    "SampleAligner",
    "TTSProvider",
    "chunk_pcm",
    "duration_seconds",
    "float_to_int16",
    "silence",
    "status_error",
]

BYTES_PER_SAMPLE = 2  # int16
# 4096 samples = 170 ms at 24 kHz: small enough that barge-in drops little, big
# enough to keep the WebSocket message rate low.
CHUNK_SAMPLES = 4096
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE


def float_to_int16(samples: np.ndarray) -> bytes:
    """float32/float64 in [-1, 1] -> int16 LE bytes. Clipped, never wrapped."""
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return b""
    arr = np.clip(arr, -1.0, 1.0)
    return (arr * 32767.0).astype("<i2").tobytes()


def chunk_pcm(pcm: bytes, samples_per_chunk: int = CHUNK_SAMPLES) -> Iterator[bytes]:
    """Split one PCM buffer into whole-sample chunks of at most ``samples_per_chunk``."""
    step = max(1, samples_per_chunk) * BYTES_PER_SAMPLE
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    for start in range(0, usable, step):
        yield pcm[start : min(start + step, usable)]


def silence(seconds: float, sample_rate: int) -> bytes:
    n = max(0, int(round(seconds * sample_rate)))
    return bytes(n * BYTES_PER_SAMPLE)


def duration_seconds(pcm_bytes: int, sample_rate: int) -> float:
    return pcm_bytes / BYTES_PER_SAMPLE / sample_rate


class SampleAligner:
    """Re-chunk an arbitrary byte stream so every emitted piece holds whole
    int16 samples. Chunks are passed through as they arrive (no buffering to a
    fixed size) so first audio is not delayed."""

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, data: bytes) -> bytes:
        if self._carry:
            data = self._carry + data
            self._carry = b""
        if len(data) % BYTES_PER_SAMPLE:
            self._carry = data[-1:]
            data = data[:-1]
        return data

    def flush(self) -> bytes:
        """Anything left is a half sample; drop it."""
        self._carry = b""
        return b""

    def iter(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        for chunk in chunks:
            out = self.feed(chunk)
            if out:
                yield out
        self.flush()


def status_error(who: str, resp: httpx.Response) -> ProviderError:
    """Turn a non-200 streaming response into the exception the pipeline degrades
    from. 401/403 become ``ProviderNotConfigured`` (the key is wrong); the body is
    clipped and the key never appears in it."""
    try:
        resp.read()
        detail = resp.text[:200].replace("\n", " ")
    except httpx.HTTPError:
        detail = ""
    msg = f"{who}: HTTP {resp.status_code} {detail}".rstrip()
    if resp.status_code in (401, 403):
        return ProviderNotConfigured(msg + " (the API key was rejected)")
    return ProviderError(msg)
