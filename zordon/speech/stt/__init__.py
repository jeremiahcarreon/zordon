"""STT provider implementations and the factory that picks one from config.

``make_stt(config)`` returns an object satisfying ``zordon.providers.STTProvider``.
Model files are looked up under ``ZORDON_TEST_MODELS`` when that is set, else
``paths.models_dir()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from zordon.providers import ProviderNotConfigured, STTProvider
from zordon.speech.stt.fake import FakeSTT
from zordon.speech.stt.faster_whisper import (
    CPU_COMPUTE_TYPE,
    CUDA_COMPUTE_TYPE,
    FasterWhisperSTT,
    ensure_model,
    model_dir_for,
)
from zordon.speech.stt.groq import GroqSTT
from zordon.speech.stt.openai import OpenAISTT
from zordon.speech.vad import models_dir_override

if TYPE_CHECKING:
    from zordon.config import Config

log = logging.getLogger("zordon.speech.stt")

STT_PROVIDERS = ("faster-whisper", "openai", "groq", "fake")

__all__ = [
    "STT_PROVIDERS",
    "FakeSTT",
    "FasterWhisperSTT",
    "GroqSTT",
    "OpenAISTT",
    "ensure_model",
    "make_stt",
]


def make_stt(config: Config, name: str | None = None) -> STTProvider:
    """Build the configured STT provider. Never loads a model or opens a connection."""
    providers = config.providers
    name = (name or providers.stt or "").strip().lower()
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ProviderNotConfigured(
            f"unknown STT provider {name!r}; providers.stt must be one of {STT_PROVIDERS}"
        )
    stt = builder(config)
    log.info("STT provider: %s", stt.name)
    return stt


def _faster_whisper(config: Config) -> FasterWhisperSTT:
    p = config.providers
    models_dir = models_dir_override()
    local = model_dir_for(p.stt_model, models_dir)
    device = (p.stt_device or "cpu").lower()
    compute = CUDA_COMPUTE_TYPE if device == "cuda" else CPU_COMPUTE_TYPE
    return FasterWhisperSTT(
        local if local.is_dir() else p.stt_model,
        device=device,
        compute_type=compute,
        models_dir=models_dir,
    )


def _openai(config: Config) -> OpenAISTT:
    model = config.providers.stt_model
    if _is_local_model_name(model):
        model = "whisper-1"
    return OpenAISTT(config.providers.key("openai"), model=model)


def _groq(config: Config) -> GroqSTT:
    model = config.providers.stt_model
    if _is_local_model_name(model):
        model = "whisper-large-v3-turbo"
    return GroqSTT(config.providers.key("groq"), model=model)


def _fake(config: Config) -> FakeSTT:
    return FakeSTT("")


def _is_local_model_name(model: str) -> bool:
    """faster-whisper size names (``small.en``, ``base``...) make no sense for the cloud APIs."""
    m = (model or "").lower()
    return not m or m.split(".")[0] in {
        "tiny",
        "base",
        "small",
        "medium",
        "large",
        "large-v2",
        "large-v3",
        "turbo",
        "distil-large-v3",
    }


_BUILDERS: dict[str, Callable[[Config], STTProvider]] = {
    "faster-whisper": _faster_whisper,
    "faster_whisper": _faster_whisper,
    "openai": _openai,
    "groq": _groq,
    "fake": _fake,
}
