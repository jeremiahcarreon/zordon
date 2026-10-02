"""Kokoro local TTS through ``kokoro-onnx`` (fp32 model, CPU, 24 kHz).

Measured facts this module is built on (research report, section 2):

* ``Kokoro()`` takes 640-660 ms to load the 325 MB graph and the voices, so the
  model is loaded lazily on the first ``synthesize`` (or explicitly via
  ``warm_up``), never at import or construction.
* ``create()`` returns the whole utterance at once: a typical sentence takes
  300-600 ms of compute for 2-4 s of audio (real-time factor 0.11-0.15). The
  package's ``create_stream`` only splits above 510 phonemes, so there is no
  intra-sentence streaming to be had. Streaming in Zordon therefore comes from
  the pipeline calling ``synthesize`` once per sentence: the first chunk of
  sentence N is yielded as soon as ``create()`` returns, while sentence N-1 is
  still playing. Do not pass paragraphs.
* espeak-ng has a fixed 160-byte buffer for its data path. When the bundled
  ``espeakng_loader`` data directory sits at an absolute path of 160 characters
  or more, ``espeak_Initialize`` silently falls back to a compiled-in path and
  the whole process exits with status 1. ``espeak_data_path`` copies the data to
  ``ZORDON_HOME/espeak-ng-data`` once and passes it through ``EspeakConfig``.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from zordon import paths
from zordon.output.tts.base import CHUNK_SAMPLES, chunk_pcm, duration_seconds, float_to_int16
from zordon.providers import ProviderError, ProviderNotConfigured

log = logging.getLogger("zordon.output.tts.kokoro")

SAMPLE_RATE = 24000
DEFAULT_VOICE = "af_heart"
ESPEAK_PATH_LIMIT = 160  # bytes; the buffer size inside espeak-ng
ESPEAK_DATA_DIRNAME = "espeak-ng-data"
SPEED_MIN, SPEED_MAX = 0.5, 2.0
MISSING_MODEL_HINT = "run zordon doctor --download"


def espeak_data_path(
    override: str | Path | None = None,
    home: Path | None = None,
    limit: int = ESPEAK_PATH_LIMIT,
) -> str | None:
    """Return the espeak-ng data directory to pass to Kokoro, or ``None`` to use
    the bundled default.

    ``override`` wins. Otherwise the bundled path is used when it is shorter than
    the espeak-ng buffer; when it is not, the data is copied once into
    ``ZORDON_HOME/espeak-ng-data`` and that (short) path is returned.
    """
    if override:
        return str(override)
    try:
        import espeakng_loader
    except ImportError:
        return None
    bundled = str(espeakng_loader.get_data_path())
    if len(bundled.encode()) < limit:
        return None
    dest = (home or paths.zordon_home()) / ESPEAK_DATA_DIRNAME
    if len(str(dest).encode()) >= limit:
        raise ProviderNotConfigured(
            f"kokoro: espeak-ng data path is {len(bundled)} characters (limit {limit}) and "
            f"ZORDON_HOME is too long to hold a copy; set ZORDON_HOME to a shorter path"
        )
    if not (dest / "phontab").exists():
        log.info(
            "kokoro: espeak-ng data path is %d characters (limit %d); copying to %s",
            len(bundled),
            limit,
            dest,
        )
        paths.ensure_private_dir(dest.parent)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(bundled, tmp)
        os.replace(tmp, dest)
    return str(dest)


class KokoroTTS:
    """``TTSProvider`` backed by kokoro-onnx. Single-owner: call it from one thread."""

    name = "kokoro"
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        model_path: str | Path,
        voices_path: str | Path,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        lang: str = "en-us",
        espeak_data_path: str | Path | None = None,
        chunk_samples: int = CHUNK_SAMPLES,
    ) -> None:
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.voice = voice
        self.speed = _clamp_speed(speed)
        self.lang = lang
        self.espeak_data_override = espeak_data_path
        self.chunk_samples = chunk_samples
        self._kokoro: Any | None = None
        self._lock = threading.Lock()
        self.load_ms: float | None = None
        self.last_first_chunk_ms: float | None = None

    # -- loading ----------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def warm_up(self) -> None:
        """Load the model now (660 ms) instead of on the first sentence."""
        self._ensure_loaded()

    def _ensure_loaded(self) -> Any:
        if self._kokoro is not None:
            return self._kokoro
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro
            for p, what in ((self.model_path, "model"), (self.voices_path, "voices")):
                if not p.is_file():
                    raise ProviderNotConfigured(
                        f"kokoro {what} file missing at {p}; {MISSING_MODEL_HINT}"
                    )
            try:
                from kokoro_onnx import EspeakConfig, Kokoro
            except ImportError as exc:
                raise ProviderNotConfigured(
                    "kokoro-onnx is not installed; pip install 'zordon[local]'"
                ) from exc
            data_path = espeak_data_path(self.espeak_data_override)
            espeak_config = EspeakConfig(data_path=data_path) if data_path else None
            started = time.monotonic()
            try:
                kokoro = Kokoro(
                    str(self.model_path), str(self.voices_path), espeak_config=espeak_config
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise ProviderError(f"kokoro: failed to load model: {exc}") from exc
            self.load_ms = (time.monotonic() - started) * 1000
            voices = list(kokoro.get_voices())
            if self.voice not in voices:
                raise ProviderNotConfigured(
                    f"kokoro: voice {self.voice!r} is not in the voices file; "
                    f"choose one of: {', '.join(voices)}"
                )
            log.info(
                "kokoro loaded in %.0f ms (voice=%s speed=%.2f lang=%s)",
                self.load_ms,
                self.voice,
                self.speed,
                self.lang,
            )
            self._kokoro = kokoro
            return kokoro

    def voices(self) -> list[str]:
        return list(self._ensure_loaded().get_voices())

    # -- synthesis -------------------------------------------------------------------

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield int16 LE mono 24 kHz chunks for one sentence.

        ``create()`` is synchronous and returns the whole sentence, so the first
        chunk is yielded immediately after it returns (that is the first-audio
        latency, ~300-600 ms for a typical sentence on CPU) and the rest follow
        without further compute. Call once per sentence: that is what gives the
        pipeline streaming across sentences.
        """
        text = (text or "").strip()
        if not text:
            return
        kokoro = self._ensure_loaded()
        started = time.monotonic()
        try:
            samples, rate = kokoro.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        except Exception as exc:  # noqa: BLE001 - phonemizer/onnx failures degrade to a skipped sentence
            raise ProviderError(f"kokoro: synthesis failed: {type(exc).__name__}: {exc}") from exc
        if rate != self.sample_rate:
            log.warning("kokoro returned %d Hz, expected %d", rate, self.sample_rate)
        pcm = float_to_int16(samples)
        first = True
        for chunk in chunk_pcm(pcm, self.chunk_samples):
            if first:
                self.last_first_chunk_ms = (time.monotonic() - started) * 1000
                log.debug(
                    "kokoro: first chunk after %.0f ms for %.2f s of audio (%d chars)",
                    self.last_first_chunk_ms,
                    duration_seconds(len(pcm), self.sample_rate),
                    len(text),
                )
                first = False
            yield chunk


def _clamp_speed(speed: float) -> float:
    try:
        s = float(speed)
    except (TypeError, ValueError):
        s = 1.0
    if not (SPEED_MIN <= s <= SPEED_MAX):
        log.warning("kokoro: speed %s outside [%.1f, %.1f]; clamping", speed, SPEED_MIN, SPEED_MAX)
        s = min(SPEED_MAX, max(SPEED_MIN, s))
    return s
