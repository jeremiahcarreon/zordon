"""Groq speech to text through its OpenAI-compatible endpoint. The Whisper models
support ``verbose_json``, so a confidence is available.
"""

from __future__ import annotations

from zordon.speech.stt.base import WhisperAPISTT

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "whisper-large-v3-turbo"


class GroqSTT(WhisperAPISTT):
    name = "groq"
    base_url = GROQ_BASE_URL
    verbose_json_models = frozenset(
        {"whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en"}
    )

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(api_key, model, **kwargs)
