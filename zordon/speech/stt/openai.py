"""OpenAI speech to text: ``whisper-1`` (verbose_json, gives a confidence) or the
``gpt-4o-(mini-)transcribe`` models (json/text only, no confidence).
"""

from __future__ import annotations

from zordon.speech.stt.base import WhisperAPISTT

OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"


class OpenAISTT(WhisperAPISTT):
    name = "openai"
    base_url = OPENAI_BASE_URL
    verbose_json_models = frozenset({"whisper-1"})

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(api_key, model, **kwargs)
