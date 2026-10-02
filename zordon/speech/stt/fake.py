"""Test double for ``STTProvider``: returns a fixed text or whatever a callable says."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

from zordon.providers import STTResult
from zordon.speech.stt.base import duration_of


class FakeSTT:
    name = "fake"

    def __init__(
        self,
        text: str | Callable[[np.ndarray], str | STTResult] = "",
        confidence: float | None = 0.9,
        delay_s: float = 0.0,
    ) -> None:
        self._text = text
        self.confidence = confidence
        self.delay_s = delay_s
        self.calls: list[np.ndarray] = []
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        audio = np.asarray(pcm16k, dtype=np.float32).reshape(-1)
        with self._lock:
            self.calls.append(audio)
        if self.delay_s:
            time.sleep(self.delay_s)
        out = self._text(audio) if callable(self._text) else self._text
        if isinstance(out, STTResult):
            return out
        return STTResult(text=str(out), confidence=self.confidence, duration_s=duration_of(audio))
