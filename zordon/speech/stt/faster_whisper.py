"""Local speech to text with faster-whisper (CTranslate2). Default provider.

The model is loaded lazily on the first ``transcribe`` so the agent starts fast
and so constructing the provider never touches the network. Given a directory,
it is loaded with ``local_files_only=True``; given a model name, the folder
``<models_dir>/faster-whisper-<name>`` is downloaded on first use.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import site
import sysconfig
import threading
from pathlib import Path
from typing import Any

import numpy as np

from zordon import paths
from zordon.providers import ProviderError, ProviderNotConfigured, STTResult
from zordon.speech.stt.base import NO_SPEECH_MAX, confidence_from_logprobs, duration_of

log = logging.getLogger("zordon.speech.stt.faster_whisper")

DEFAULT_MODEL = "small.en"
CPU_COMPUTE_TYPE = "int8"
CUDA_COMPUTE_TYPE = "float16"

# pip-installed CUDA runtime libraries that ctranslate2 does not find on its own.
_NVIDIA_LIBS = (
    "nvidia/cublas/lib/libcublas.so.12",
    "nvidia/cublas/lib/libcublasLt.so.12",
    "nvidia/cudnn/lib/libcudnn.so.9",
)


def model_dirname(name: str) -> str:
    return f"faster-whisper-{name}"


def model_dir_for(name: str, models_dir: Path | None = None) -> Path:
    return (models_dir or paths.models_dir()) / model_dirname(name)


def ensure_model(models_dir: Path, name: str = DEFAULT_MODEL) -> Path:
    """Download ``name`` into ``models_dir/faster-whisper-<name>`` unless it is already there.

    Network access; not called from tests. ``zordon doctor`` calls this ahead of time.
    """
    dest = model_dir_for(name, Path(models_dir))
    if (dest / "model.bin").is_file():
        return dest
    try:
        from faster_whisper import download_model
    except ImportError as e:  # pragma: no cover - depends on the optional extra
        raise _not_installed() from e
    paths.ensure_private_dir(dest.parent)
    log.info("downloading faster-whisper %s to %s", name, dest)
    try:
        download_model(name, output_dir=str(dest))
    except Exception as e:  # noqa: BLE001 - hub errors are many and all mean "degrade"
        raise ProviderError(f"faster-whisper: download of {name} failed: {e}") from e
    return dest


def preload_nvidia_libs() -> int:
    """dlopen the pip-installed cuBLAS/cuDNN libraries (RTLD_GLOBAL) when present.

    Returns how many libraries were loaded. Zero means CUDA will almost certainly
    fail and the caller should expect to fall back to CPU.
    """
    roots: list[str] = []
    try:
        roots.extend(site.getsitepackages())
    except AttributeError:  # pragma: no cover - some embedded interpreters
        pass
    purelib = sysconfig.get_paths().get("purelib")
    if purelib and purelib not in roots:
        roots.append(purelib)
    loaded = 0
    for root in roots:
        for rel in _NVIDIA_LIBS:
            for lib in glob.glob(os.path.join(root, rel)):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    loaded += 1
                except OSError as e:
                    log.debug("could not preload %s: %s", lib, e)
    return loaded


def _not_installed() -> ProviderNotConfigured:
    return ProviderNotConfigured(
        "faster-whisper is not installed; install the local speech stack with `pip install 'zordon[local]'`"
    )


class FasterWhisperSTT:
    name = "faster-whisper"

    def __init__(
        self,
        model_dir_or_name: str | Path = DEFAULT_MODEL,
        device: str = "cpu",
        compute_type: str = CPU_COMPUTE_TYPE,
        cpu_threads: int = 8,
        beam_size: int = 5,
        *,
        language: str | None = "en",
        models_dir: Path | None = None,
    ) -> None:
        self.model_spec = str(model_dir_or_name)
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.language = language
        self.models_dir = Path(models_dir) if models_dir is not None else None
        self._model: Any | None = None
        self._lock = threading.Lock()

    # ---- loading ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def preload(self) -> None:
        """Load the model now (for example from ``zordon doctor`` or at agent start)."""
        self._model_or_load()

    def _model_or_load(self) -> Any:
        with self._lock:
            if self._model is None:
                self._model = self._load()
            return self._model

    def _resolve_model(self) -> str:
        spec = Path(self.model_spec).expanduser()
        if spec.is_dir():
            return str(spec)
        return str(ensure_model(self.models_dir or paths.models_dir(), self.model_spec))

    def _load(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise _not_installed() from e
        path = self._resolve_model()
        if self.device == "cuda":
            loaded = preload_nvidia_libs()
            try:
                model = WhisperModel(
                    path, device="cuda", compute_type=self.compute_type, local_files_only=True
                )
                log.info(
                    "faster-whisper loaded %s on cuda (%s, %d nvidia libs preloaded)",
                    path,
                    self.compute_type,
                    loaded,
                )
                return model
            except Exception as e:  # noqa: BLE001 - ctranslate2 raises RuntimeError for missing CUDA libs
                log.warning(
                    "faster-whisper: CUDA unavailable (%s); falling back to CPU %s",
                    e,
                    CPU_COMPUTE_TYPE,
                )
                self.device, self.compute_type = "cpu", CPU_COMPUTE_TYPE
        try:
            model = WhisperModel(
                path,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                local_files_only=True,
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderNotConfigured(
                f"faster-whisper: could not load model at {path}: {e}"
            ) from e
        log.info(
            "faster-whisper loaded %s on cpu (%s, %d threads)",
            path,
            self.compute_type,
            self.cpu_threads,
        )
        return model

    # ---- transcription ------------------------------------------------------------

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        audio = np.ascontiguousarray(np.asarray(pcm16k, dtype=np.float32).reshape(-1))
        duration = duration_of(audio)
        language = self.language or "en"
        if audio.size == 0:
            return STTResult(text="", confidence=None, language=language, duration_s=0.0)
        model = self._model_or_load()
        try:
            segments, info = model.transcribe(
                audio,
                beam_size=self.beam_size,
                language=self.language,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            segments = list(segments)  # the generator does the decoding
        except Exception as e:  # noqa: BLE001 - ctranslate2 failures are provider failures
            raise ProviderError(f"faster-whisper: transcription failed: {e}") from e
        kept = [s for s in segments if float(s.no_speech_prob) < NO_SPEECH_MAX]
        text = " ".join(s.text.strip() for s in kept).strip()
        confidence = confidence_from_logprobs(s.avg_logprob for s in kept)
        detected = getattr(info, "language", None) or language
        return STTResult(text=text, confidence=confidence, language=detected, duration_s=duration)
