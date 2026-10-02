"""Provider interfaces. One small Protocol per concern; implementations live in
``zordon.speech.stt``, ``zordon.output.tts``, ``zordon.output.normalizer`` and
``zordon.routing``. Swapping a provider is a config change.

All methods are synchronous and may block; callers run them on their own worker
thread. Implementations raise ``ProviderError`` for anything the caller should
degrade from (timeout, auth, network); any other exception is a bug.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


class ProviderError(RuntimeError):
    """Expected provider failure: the caller falls back or degrades."""


class ProviderNotConfigured(ProviderError):
    """Missing API key, missing optional dependency or missing model file."""


# ---- speech to text -----------------------------------------------------------


@dataclass(slots=True)
class STTResult:
    text: str
    confidence: float | None = None  # 0..1 when the provider supplies one
    language: str = "en"
    duration_s: float = 0.0


@runtime_checkable
class STTProvider(Protocol):
    name: str

    def transcribe(self, pcm16k: np.ndarray) -> STTResult:
        """``pcm16k``: float32 mono samples in [-1, 1] at 16 kHz."""
        ...


# ---- text to speech -----------------------------------------------------------


@runtime_checkable
class TTSProvider(Protocol):
    name: str
    sample_rate: int

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield int16 little-endian mono PCM chunks at ``sample_rate`` as soon as they exist."""
        ...


# ---- normalizer ---------------------------------------------------------------


@runtime_checkable
class Normalizer(Protocol):
    name: str

    def normalize(self, sentence: str, context: list[str]) -> str:
        """Rewrite ``sentence`` as fluent spoken English. ``context`` is the previous
        two spoken sentences (may be empty). Must return within the configured
        timeout or raise ProviderError; the caller then speaks ``sentence`` as is."""
        ...


# ---- router -------------------------------------------------------------------

DESTINATIONS = ("claude_code", "transcript_query", "shim_command", "unclear")


@dataclass(slots=True)
class RouteContext:
    session_state: str  # SessionState value
    transcript_tail: list[str] = field(default_factory=list)  # last N spoken rows
    focused_session: str = ""
    session_names: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)  # closed set of shim command names


@dataclass(slots=True)
class RouteResult:
    destination: str  # one of DESTINATIONS
    confidence: float  # 0..1
    command: str | None = None  # shim command name when destination == shim_command
    argument: str | None = None  # e.g. session name or verbosity level
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class YesNoResult:
    answer: str  # yes | no | unclear
    confidence: float


@runtime_checkable
class Router(Protocol):
    name: str

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult: ...

    def yes_no(self, utterance: str) -> YesNoResult:
        """Strict gate used in AwaitingPermission. Must be conservative."""
        ...

    def prompt_score(self, lines: list[str]) -> float:
        """0..1: how likely the last pane lines are a prompt waiting for input."""
        ...
