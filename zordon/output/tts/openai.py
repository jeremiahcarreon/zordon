"""OpenAI speech API as a streaming TTS provider.

``POST https://api.openai.com/v1/audio/speech`` with ``response_format: pcm``
returns raw int16 little-endian mono at 24 kHz, streamed; chunks are yielded as
they arrive (re-aligned to whole samples). The key is sent as a bearer header
and never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from zordon.output.tts.base import SampleAligner, status_error
from zordon.providers import ProviderError, ProviderNotConfigured

log = logging.getLogger("zordon.output.tts.openai")

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "alloy"
VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
SAMPLE_RATE = 24000  # what response_format=pcm delivers
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)


class OpenAITTS:
    name = "openai"
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        *,
        instructions: str | None = None,
        url: str = OPENAI_TTS_URL,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ProviderNotConfigured(
                "openai tts: no API key; set providers.keys.openai or OPENAI_API_KEY"
            )
        self._key = key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.url = url
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _body(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
        }
        if self.instructions:
            body["instructions"] = self.instructions
        return body

    def synthesize(self, text: str) -> Iterator[bytes]:
        text = (text or "").strip()
        if not text:
            return
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/octet-stream"}
        aligner = SampleAligner()
        try:
            with self._client.stream(
                "POST", self.url, headers=headers, json=self._body(text)
            ) as resp:
                if resp.status_code != 200:
                    raise status_error("openai tts", resp)
                for chunk in resp.iter_bytes():
                    out = aligner.feed(chunk)
                    if out:
                        yield out
        except httpx.TimeoutException as exc:
            raise ProviderError(f"openai tts: timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai tts: request failed ({type(exc).__name__})") from exc
