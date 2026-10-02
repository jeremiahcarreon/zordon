"""Speech input: VAD, the onset/end gate, STT providers and the AudioThread."""

from __future__ import annotations

from zordon.speech.audio_thread import AudioFrame, AudioThread
from zordon.speech.stt import FakeSTT, FasterWhisperSTT, GroqSTT, OpenAISTT, make_stt
from zordon.speech.vad import FakeVAD, GateEvent, GateKind, SileroVAD, SpeechGate, make_vad

__all__ = [
    "AudioFrame",
    "AudioThread",
    "FakeSTT",
    "FakeVAD",
    "FasterWhisperSTT",
    "GateEvent",
    "GateKind",
    "GroqSTT",
    "OpenAISTT",
    "SileroVAD",
    "SpeechGate",
    "make_stt",
    "make_vad",
]
