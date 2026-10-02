"""Shared pieces for the STT providers: WAV encoding, confidence mapping and the
OpenAI-compatible ``/audio/transcriptions`` client that OpenAI and Groq share.
"""

from __future__ import annotations

import io
import logging
import math
import wave
from collections.abc import Iterable
from typing import Any

import httpx
import numpy as np

from zordon.providers import ProviderError, ProviderNotConfigured, STTResult
from zordon.speech.resample import float32_to_pcm16

log = logging.getLogger("zordon.speech.stt")

SAMPLE_RATE = 16000
# Whisper segments at or above this no_speech probability are hallucinations on silence.
NO_SPEECH_MAX = 0.6
DEFAULT_TIMEOUT_S = 10.0


def pcm_to_wav_bytes(pcm16k: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """float32 mono samples -> a complete 16-bit PCM WAV file in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(float32_to_pcm16(pcm16k))
    return buf.getvalue()


def confidence_from_logprobs(logprobs: Iterable[float]) -> float | None:
    """exp(mean avg_logprob) clamped to [0, 1]; None when there are no segments."""
    values = [float(v) for v in logprobs if v is not None and math.isfinite(float(v))]
    if not values:
        return None
    return max(0.0, min(1.0, math.exp(sum(values) / len(values))))


def duration_of(pcm16k: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    return float(np.asarray(pcm16k).reshape(-1).shape[0]) / sample_rate


class WhisperAPISTT:
    """Client for an OpenAI-compatible ``POST {base_url}/audio/transcriptions``.

    Encodes the utterance as a 16-bit WAV and sends it as multipart form data.
    ``verbose_json`` responses carry per-segment ``avg_logprob`` / ``no_speech_prob``
    which give a confidence; models that only speak ``json`` return ``confidence=None``.
    """

    name = "whisper-api"
    base_url = ""
    verbose_json_models: frozenset[str] = frozenset()

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        language: str | None = "en",
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ProviderNotConfigured(
                f"{self.name}: no API key configured (set providers.keys.{self.name} or the environment variable)"
            )
        self.model = model
        self.language = language
        self.timeout = timeout
        if base_url:
            self.base_url = base_url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    # ---- request ------------------------------------------------------------------

    @property
    def response_format(self) -> str:
        return "verbose_json" if self.model in self.verbose_json_models else "json"

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        audio = np.asarray(pcm16k, dtype=np.float32).reshape(-1)
        duration = duration_of(audio)
        if audio.size == 0:
            return STTResult(
                text="", confidence=None, language=self.language or "en", duration_s=0.0
            )
        payload = self._post(pcm_to_wav_bytes(audio))
        return self._parse(payload, duration)

    def _form(self) -> dict[str, str]:
        data = {"model": self.model, "response_format": self.response_format}
        if self.language:
            data["language"] = self.language
        return data

    def _post(self, wav: bytes) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        files = {"file": ("audio.wav", wav, "audio/wav")}
        try:
            resp = self._client.post(url, data=self._form(), files=files, headers=self._headers)
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.name}: request timed out after {self.timeout:g} s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name}: HTTP {resp.status_code} {_error_message(resp)}".rstrip()
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: response was not JSON") from e
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.name}: unexpected response shape")
        return payload

    # ---- response -----------------------------------------------------------------

    def _parse(self, payload: dict[str, Any], duration: float) -> STTResult:
        segments = [s for s in (payload.get("segments") or []) if isinstance(s, dict)]
        kept = [s for s in segments if float(s.get("no_speech_prob", 0.0) or 0.0) < NO_SPEECH_MAX]
        if segments:
            text = " ".join(str(s.get("text", "")).strip() for s in kept).strip()
            confidence = confidence_from_logprobs(
                s["avg_logprob"] for s in kept if s.get("avg_logprob") is not None
            )
        else:
            text = str(payload.get("text") or "").strip()
            confidence = None
        language = str(payload.get("language") or self.language or "en")
        reported = payload.get("duration")
        try:
            duration = float(reported) if reported is not None else duration
        except (TypeError, ValueError):
            pass
        return STTResult(text=text, confidence=confidence, language=language, duration_s=duration)


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))[:200]
        if isinstance(err, str):
            return err[:200]
    return ""
