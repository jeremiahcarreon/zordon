"""TTS implementations and the factory that picks one from config.

``make_tts`` raises ``ProviderNotConfigured`` when the configured provider
cannot run (missing model files, missing key) so the caller can tell the user
what to fix. It never swaps in silence on its own: ``SilenceTTS`` is only
returned when ``providers.tts`` asks for it, and then with a warning.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from zordon import assets, paths
from zordon.config import Config
from zordon.output.tts.base import TTSProvider
from zordon.output.tts.elevenlabs import DEFAULT_VOICE_ID as ELEVENLABS_DEFAULT_VOICE_ID
from zordon.output.tts.elevenlabs import ElevenLabsTTS
from zordon.output.tts.kokoro import DEFAULT_VOICE as KOKORO_DEFAULT_VOICE
from zordon.output.tts.kokoro import MISSING_MODEL_HINT, KokoroTTS
from zordon.output.tts.openai import DEFAULT_VOICE as OPENAI_DEFAULT_VOICE
from zordon.output.tts.openai import VOICES as OPENAI_VOICES
from zordon.output.tts.openai import OpenAITTS
from zordon.output.tts.silence import SilenceTTS
from zordon.providers import ProviderNotConfigured

__all__ = [
    "ElevenLabsTTS",
    "KokoroTTS",
    "OpenAITTS",
    "SilenceTTS",
    "TTSProvider",
    "kokoro_model_paths",
    "make_tts",
    "models_dir",
]

log = logging.getLogger("zordon.output.tts")

TTS_PROVIDERS = ("kokoro", "openai", "elevenlabs", "silence")
# Set by the test suite (and usable by hand) to point at a read-only model directory.
TEST_MODELS_ENV = "ZORDON_TEST_MODELS"


def models_dir() -> Path:
    override = os.environ.get(TEST_MODELS_ENV, "").strip()
    return Path(override).expanduser() if override else paths.models_dir()


def kokoro_model_paths() -> tuple[Path, Path]:
    """(model, voices) under ``models_dir()``; the asset registry supplies the file names."""
    base = models_dir()
    if os.environ.get(TEST_MODELS_ENV, "").strip():
        return base / assets.KOKORO_MODEL.filename, base / assets.KOKORO_VOICES.filename
    return assets.path_for(assets.KOKORO_MODEL), assets.path_for(assets.KOKORO_VOICES)


def make_tts(config: Config) -> TTSProvider:
    name = (config.providers.tts or "").strip().lower()
    voice = (config.providers.tts_voice or "").strip()
    if name == "kokoro":
        model, voices = kokoro_model_paths()
        missing = [str(p) for p in (model, voices) if not p.is_file()]
        if missing:
            raise ProviderNotConfigured(
                f"kokoro model files missing ({', '.join(missing)}); {MISSING_MODEL_HINT}"
            )
        return KokoroTTS(
            model,
            voices,
            voice=voice or KOKORO_DEFAULT_VOICE,
            speed=config.providers.tts_speed,
        )
    if name == "openai":
        key = config.providers.key("openai")
        if not key:
            raise ProviderNotConfigured(
                "tts=openai but no key; set providers.keys.openai or OPENAI_API_KEY"
            )
        # The shared tts_voice knob defaults to a Kokoro voice name; only honour it
        # here when it is one OpenAI knows.
        return OpenAITTS(key, voice=voice if voice in OPENAI_VOICES else OPENAI_DEFAULT_VOICE)
    if name == "elevenlabs":
        key = config.providers.key("elevenlabs")
        if not key:
            raise ProviderNotConfigured(
                "tts=elevenlabs but no key; set providers.keys.elevenlabs or ELEVENLABS_API_KEY"
            )
        # ElevenLabs voices are ids, not names; a Kokoro-style name means "use the default".
        voice_id = voice if _looks_like_elevenlabs_voice_id(voice) else ELEVENLABS_DEFAULT_VOICE_ID
        return ElevenLabsTTS(key, voice_id=voice_id)
    if name in ("silence", "none", "off"):
        log.warning("tts=%s: no speech will be produced; the pipeline runs with silent audio", name)
        return SilenceTTS()
    raise ProviderNotConfigured(
        f"unknown tts provider {name!r}; expected one of {', '.join(TTS_PROVIDERS)}"
    )


def _looks_like_elevenlabs_voice_id(value: str) -> bool:
    return bool(value) and len(value) >= 16 and value.isalnum() and "_" not in value
