"""SpeechGate logic with a FakeVAD: onset counting, end timing, pre-roll, the
30 s cap and the echo guard. No models, no threads."""

from __future__ import annotations

import numpy as np
import pytest

from zordon.config import VoiceConfig
from zordon.speech.vad import FakeVAD, GateKind, SpeechGate, onset_chunks_for

FRAME_SAMPLES = 320  # 20 ms at 16 kHz
FRAME_S = 0.02
SPEECH_LEVEL = 8000  # int16 amplitude of a "speech" frame
# Amplitude-driven fake: any chunk containing speech samples is speech.
AMPLITUDE_VAD = lambda chunk: 1.0 if float(np.abs(chunk).max()) > 0.05 else 0.0  # noqa: E731


def frame(level: int = 0, tag: int | None = None) -> bytes:
    """One 20 ms int16 frame. ``tag`` stamps the last sample so frames can be identified."""
    samples = np.full(FRAME_SAMPLES, level, dtype=np.int16)
    if tag is not None:
        samples[-1] = tag
    return samples.tobytes()


def tag_of(pcm_frame: bytes) -> int:
    return int(np.frombuffer(pcm_frame, dtype="<i2")[-1])


def frames_of(pcm: bytes) -> list[bytes]:
    n = FRAME_SAMPLES * 2
    assert len(pcm) % n == 0
    return [pcm[i : i + n] for i in range(0, len(pcm), n)]


def run(
    gate: SpeechGate, levels: list[int], t0: float = 0.0, **kw
) -> list[tuple[int, GateKind, object]]:
    """Feed frames (tagged by index), return (index, kind, event) for each frame."""
    out = []
    for i, level in enumerate(levels):
        ev = gate.feed(frame(level, tag=i), now=t0 + i * FRAME_S, **kw)
        out.append((i, ev.kind, ev))
    return out


def events(seq, kind: GateKind) -> list[int]:
    return [i for i, k, _ in seq if k is kind]


# ---- onset mapping ----------------------------------------------------------------


def test_onset_chunk_mapping_documents_three_frames_as_two_chunks():
    assert onset_chunks_for(3, 20) == 2  # 60 ms -> ceil(60/32) = 2 chunks = 64 ms
    assert onset_chunks_for(1, 20) == 1
    assert onset_chunks_for(5, 20) == 4  # 100 ms -> 4 chunks
    gate = SpeechGate(FakeVAD(), onset_frames=3, frame_ms=20)
    assert gate.onset_chunks == 2


def test_from_config_uses_voice_settings():
    voice = VoiceConfig(speech_onset_frames=5, speech_end_ms=500, echo_guard_ms=200)
    gate = SpeechGate.from_config(FakeVAD(), voice)
    assert gate.onset_chunks == 4
    assert gate.end_s == pytest.approx(0.5)
    assert gate.echo_guard_s == pytest.approx(0.2)


def test_onset_after_two_speech_chunks_not_one():
    gate = SpeechGate(FakeVAD(lambda c: 1.0))
    seq = run(gate, [SPEECH_LEVEL] * 6)
    # Frame 1 completes chunk 1 (640 >= 512), frame 3 completes chunk 2 (1280 >= 1024).
    assert events(seq, GateKind.ONSET) == [3]
    assert seq[1][2].prob == 1.0 and seq[1][2].speaking is False
    assert gate.speaking


def test_single_speech_chunk_then_silence_does_not_trigger_onset():
    # Chunk-level script: speech, silence, speech, silence... never two in a row.
    gate = SpeechGate(FakeVAD([1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]))
    seq = run(gate, [SPEECH_LEVEL] * 14)
    assert events(seq, GateKind.ONSET) == []
    assert not gate.speaking


def test_threshold_is_respected():
    gate = SpeechGate(FakeVAD(lambda c: 0.49), threshold=0.5)
    assert events(run(gate, [SPEECH_LEVEL] * 10), GateKind.ONSET) == []
    gate = SpeechGate(FakeVAD(lambda c: 0.51), threshold=0.5)
    assert events(run(gate, [SPEECH_LEVEL] * 10), GateKind.ONSET) == [3]


# ---- end of speech -----------------------------------------------------------------


def test_end_after_700ms_of_silence():
    gate = SpeechGate(FakeVAD(AMPLITUDE_VAD), end_ms=700)
    speech_frames = 50  # 1.0 s of speech
    levels = [SPEECH_LEVEL] * speech_frames + [0] * 60
    seq = run(gate, levels)
    onsets, ends = events(seq, GateKind.ONSET), events(seq, GateKind.END)
    assert onsets == [3]
    assert len(ends) == 1
    end_idx = ends[0]
    # The last chunk containing speech samples completes at most two frames after
    # the last speech frame; END fires on the first frame >= 700 ms after that.
    last_speech_frame = speech_frames - 1
    assert last_speech_frame + 35 <= end_idx <= last_speech_frame + 37, end_idx
    ev = seq[end_idx][2]
    assert ev.reason == "silence"
    assert ev.utterance_pcm is not None
    assert ev.duration_s == pytest.approx(len(ev.utterance_pcm) / 2 / 16000)
    assert not gate.speaking


def test_no_end_while_speech_continues_with_short_gaps():
    gate = SpeechGate(FakeVAD(AMPLITUDE_VAD), end_ms=700)
    # 400 ms gaps are shorter than end_ms: must stay in one utterance.
    levels = ([SPEECH_LEVEL] * 20 + [0] * 20) * 3 + [0] * 60
    seq = run(gate, levels)
    assert len(events(seq, GateKind.ONSET)) == 1
    assert len(events(seq, GateKind.END)) == 1
    assert events(seq, GateKind.END)[0] > 120


def test_tick_ends_utterance_when_frames_stop():
    gate = SpeechGate(FakeVAD(AMPLITUDE_VAD), end_ms=700)
    run(gate, [SPEECH_LEVEL] * 20)
    assert gate.speaking
    last_t = 19 * FRAME_S
    assert gate.tick(last_t + 0.5).kind is GateKind.NONE
    ev = gate.tick(last_t + 0.75)
    assert ev.kind is GateKind.END
    assert ev.utterance_pcm and not gate.speaking
    assert gate.tick(last_t + 2.0).kind is GateKind.NONE


# ---- pre-roll ---------------------------------------------------------------------


def test_utterance_starts_300ms_before_onset_and_ends_at_end_frame():
    gate = SpeechGate(FakeVAD(AMPLITUDE_VAD), preroll_ms=300)
    silence_before = 40
    levels = [0] * silence_before + [SPEECH_LEVEL] * 25 + [0] * 50
    seq = run(gate, levels)
    onset_idx = events(seq, GateKind.ONSET)[0]
    end_idx = events(seq, GateKind.END)[0]
    pcm = seq[end_idx][2].utterance_pcm
    tags = [tag_of(f) for f in frames_of(pcm)]
    # 300 ms = 15 frames up to and including the onset frame, then every frame through END.
    assert tags == list(range(onset_idx - 14, end_idx + 1))
    assert silence_before in tags  # the first speech frame is inside the utterance
    assert tags[0] < silence_before  # and so is some silence before it


def test_preroll_is_bounded_by_samples_not_frames():
    gate = SpeechGate(FakeVAD(AMPLITUDE_VAD), preroll_ms=300)
    # 40 ms frames: 300 ms of pre-roll is 8 such frames (7.5 rounded up to whole frames).
    big = np.zeros(640, dtype=np.int16)
    for i in range(30):
        big[-1] = i
        gate.feed(big.tobytes(), now=i * 0.04)
    assert gate._preroll_samples <= 300 * 16 + 640
    assert len(gate._preroll) == 8


# ---- max duration -----------------------------------------------------------------


def test_forced_end_at_30_seconds():
    gate = SpeechGate(FakeVAD(lambda c: 1.0), max_utterance_s=30.0)
    n = int(31.0 / FRAME_S)
    seq = run(gate, [SPEECH_LEVEL] * n)
    onsets, ends = events(seq, GateKind.ONSET), events(seq, GateKind.END)
    assert onsets[0] == 3
    assert len(ends) >= 1
    first_end = ends[0]
    assert (first_end - onsets[0]) * FRAME_S == pytest.approx(30.0, abs=FRAME_S)
    ev = seq[first_end][2]
    assert ev.reason == "max_duration"
    # Pre-roll here is only the 4 frames that existed before the onset (80 ms).
    assert ev.duration_s == pytest.approx(30.0 + (onsets[0] + 1) * FRAME_S, abs=0.05)
    # Speech continues: a new utterance starts right after.
    assert len(onsets) == 2 and onsets[1] > first_end


# ---- echo guard -------------------------------------------------------------------


def test_echo_guard_ignores_vad_for_120ms_after_playback_start():
    gate = SpeechGate(FakeVAD(lambda c: 1.0), echo_guard_ms=120)
    started = 10.0
    seq = run(
        gate, [SPEECH_LEVEL] * 20, t0=started, playback_active=True, playback_started_at=started
    )
    onsets = events(seq, GateKind.ONSET)
    assert len(onsets) == 1
    onset_t = onsets[0] * FRAME_S
    # No decision counts before 120 ms; the onset needs two more chunks after that.
    assert onset_t >= 0.12 + 0.04
    assert onset_t < 0.12 + 0.12


def test_echo_guard_tracks_playback_start_itself_when_not_given():
    gate = SpeechGate(FakeVAD(lambda c: 1.0), echo_guard_ms=120)
    # Playback becomes active at frame 10; the gate records that moment.
    levels = [SPEECH_LEVEL] * 30
    out = []
    for i, level in enumerate(levels):
        ev = gate.feed(frame(level), playback_active=i >= 10, now=i * FRAME_S)
        out.append(ev.kind)
    # Frames 0..9 (no playback) already produce an onset at frame 3.
    assert out.index(GateKind.ONSET) == 3


def test_no_echo_guard_without_playback():
    gate = SpeechGate(FakeVAD(lambda c: 1.0), echo_guard_ms=120)
    seq = run(gate, [SPEECH_LEVEL] * 10, playback_active=False, playback_started_at=0.0)
    assert events(seq, GateKind.ONSET) == [3]


def test_echo_guard_does_not_apply_once_window_passed():
    gate = SpeechGate(FakeVAD(lambda c: 1.0), echo_guard_ms=120)
    seq = run(gate, [SPEECH_LEVEL] * 10, t0=5.0, playback_active=True, playback_started_at=4.0)
    assert events(seq, GateKind.ONSET) == [3]


# ---- reset ------------------------------------------------------------------------


def test_reset_drops_partial_utterance_and_vad_state():
    vad = FakeVAD(AMPLITUDE_VAD)
    gate = SpeechGate(vad)
    run(gate, [SPEECH_LEVEL] * 10)
    assert gate.speaking
    gate.reset()
    assert not gate.speaking
    assert vad.resets >= 2  # once in __init__, once now
    assert gate.tick(100.0).kind is GateKind.NONE
    # Works again after reset.
    seq = run(gate, [SPEECH_LEVEL] * 6, t0=200.0)
    assert events(seq, GateKind.ONSET) == [3]


def test_odd_and_empty_frames_are_tolerated():
    gate = SpeechGate(FakeVAD(lambda c: 1.0))
    assert gate.feed(b"", now=0.0).kind is GateKind.NONE
    assert gate.feed(b"\x00", now=0.02).kind is GateKind.NONE
    seq = run(gate, [SPEECH_LEVEL] * 6, t0=1.0)
    assert events(seq, GateKind.ONSET) == [3]
