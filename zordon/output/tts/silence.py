"""A TTS provider that produces silence: 100 ms of zeros per 10 characters.

For tests, and for running the agent with no speech output at all while the
rest of the pipeline (transcript, prompts, routing) still works. The factory
only picks it when the config asks for it.
"""

from __future__ import annotations

from collections.abc import Iterator

from zordon.output.tts.base import BYTES_PER_SAMPLE, CHUNK_SAMPLES, chunk_pcm

SECONDS_PER_10_CHARS = 0.1


class SilenceTTS:
    name = "silence"
    sample_rate = 24000

    def __init__(self, sample_rate: int = 24000, chunk_samples: int = CHUNK_SAMPLES) -> None:
        self.sample_rate = sample_rate
        self.chunk_samples = chunk_samples

    def duration_for(self, text: str) -> float:
        return len(text or "") / 10 * SECONDS_PER_10_CHARS

    def synthesize(self, text: str) -> Iterator[bytes]:
        n_samples = int(round(self.duration_for(text) * self.sample_rate))
        if n_samples <= 0:
            return
        yield from chunk_pcm(bytes(n_samples * BYTES_PER_SAMPLE), self.chunk_samples)
