"""ElevenLabs streaming TTS.

``POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`` with
``output_format=pcm_24000`` returns raw int16 little-endian mono at 24 kHz,
streamed; chunks are yielded as they arrive (re-aligned to whole samples). The
key travels in the ``xi-api-key`` header and is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from zordon.output.tts.base import SampleAligner, status_error
from zordon.providers import ProviderError, ProviderNotConfigured

log = logging.getLogger("zordon.output.tts.elevenlabs")

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"
DEFAULT_MODEL_ID = "eleven_flash_v2_5"
# "Rachel", one of the premade voices every account has.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
OUTPUT_FORMAT = "pcm_24000"
SAMPLE_RATE = 24000
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)


class ElevenLabsTTS:
    name = "elevenlabs"
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        api_key: str | None,
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
        *,
        voice_settings: dict[str, Any] | None = None,
        base_url: str = ELEVENLABS_BASE_URL,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ProviderNotConfigured(
                "elevenlabs tts: no API key; set providers.keys.elevenlabs or ELEVENLABS_API_KEY"
            )
        if not voice_id or "/" in voice_id or "?" in voice_id:
            raise ProviderNotConfigured(f"elevenlabs tts: invalid voice id {voice_id!r}")
        self._key = key
        self.voice_id = voice_id
        self.model_id = model_id
        self.voice_settings = voice_settings
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.voice_id}/stream"

    def _body(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "model_id": self.model_id}
        if self.voice_settings:
            body["voice_settings"] = self.voice_settings
        return body

    def synthesize(self, text: str) -> Iterator[bytes]:
        text = (text or "").strip()
        if not text:
            return
        headers = {"xi-api-key": self._key, "Accept": "application/octet-stream"}
        aligner = SampleAligner()
        try:
            with self._client.stream(
                "POST",
                self.url,
                params={"output_format": OUTPUT_FORMAT},
                headers=headers,
                json=self._body(text),
            ) as resp:
                if resp.status_code != 200:
                    raise status_error("elevenlabs tts", resp)
                for chunk in resp.iter_bytes():
                    out = aligner.feed(chunk)
                    if out:
                        yield out
        except httpx.TimeoutException as exc:
            raise ProviderError(f"elevenlabs tts: timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"elevenlabs tts: request failed ({type(exc).__name__})") from exc
