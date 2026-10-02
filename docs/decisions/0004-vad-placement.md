# 0004: Voice activity detection runs in the agent, not the browser

**Status:** accepted, 2026-10-02

## Question

From the design: "Is Silero VAD's latency on the target machines inside the 150 ms
budget once browser capture and WebSocket framing are added, or does VAD need to run
in the browser with the ONNX runtime?"

## What was measured

Silero VAD v6.2 (`silero_vad.onnx`, 2,327,524 bytes) on onnxruntime 1.30 CPU, one
thread, no torch:

| item | measured |
| --- | --- |
| session load | 30 ms |
| per 32 ms frame (512 samples at 16 kHz) | mean 0.079 ms, p99 0.098 ms, max 0.41 ms (first call) |
| reaction to speech energy | P(speech) > 0.5 within one 32 ms frame of onset (`0.02, 0.09, 0.79, 0.97, ...`) |
| leading silence | max P 0.127 |
| trailing hangover after speech | max P 0.618, decays within a few frames |

The model's chunk size is fixed at 512 samples (32 ms); the 20 ms (320 sample)
wire frames must be rebuffered. The tensor fed is `[1, 576]`: 64 samples of
previous-chunk context plus the 512 new ones. Feeding raw 512 runs but gives
probabilities the model was not trained on.

The `silero-vad` PyPI package depends unconditionally on torch (`import torch` is
line 1 of its utils), so the lightweight path is the raw ONNX file. The
`silero_vad_16k_op15.onnx` variant (1,289,603 bytes) gave identical probabilities
and is 45% smaller; the canonical file is shipped because it also handles 8 kHz.

Browser side (verified in Node against a shimmed `AudioWorkletGlobalScope`): the
worklet downsamples from the device rate (48 kHz on most phones, 44.1 kHz on some
Macs) to 16 kHz Int16 and posts one 320-sample frame every 20 ms. AudioWorklet
needs Safari 14.1 / iOS 14.5 or later.

## Budget

Target: playback silent within 150 ms of the user starting to speak.

| stage | cost |
| --- | --- |
| worklet fills one 20 ms frame | up to 20 ms |
| frame crosses the WebSocket (browser to agent) | one-way latency: a few ms on LAN, tens of ms on Tailscale, 30-80 ms through a public tunnel (estimates; not measured here) |
| rebuffer to a 32 ms VAD chunk | up to 12 ms |
| inference | 0.08 ms |
| onset rule: first positive chunk plus two more consecutive positives | about 64 ms after the first positive chunk |
| flush message back to the browser | one-way latency again |
| `AudioBufferSourceNode.stop(0)` on every scheduled chunk | one render quantum, about 3 ms |

On LAN or Tailscale the sum is roughly 100-130 ms. Through a public tunnel the two
network legs alone can consume 60-160 ms, so the budget may not hold there.

## Decision

* VAD runs in the agent's AudioThread on onnxruntime, using the raw ONNX model and
  numpy only. Compute cost is negligible; one implementation serves the browser
  path and the host-microphone path; the model file is verified by `zordon doctor`.
* The design's "3 consecutive speech frames" rule is applied to 32 ms VAD chunks,
  not 20 ms wire frames, because Silero's chunk size is fixed. `voice.speech_onset_frames`
  counts VAD chunks. End of speech stays at `voice.speech_end_ms` (700 ms) of
  silence; the trailing hangover seen above is well inside that.
* The echo guard ignores VAD for `voice.echo_guard_ms` (120 ms) after playback
  starts and the browser requests `echoCancellation: true` (a hint, not a guarantee).
* The VAD session is pinned to one intra-op and one inter-op thread so it never
  contends with Kokoro.
* The barge-in path consults nothing but the VAD: on onset while playback is active
  the thread bumps `Bus.generation`, drains `bus.playback`, and publishes `Flush`.
  The normalizer and TTS are not cancelled; their late output is dropped by
  generation.

## When to revisit

Step 4 of the build order measures interrupt latency on the reference laptop over
LAN, Tailscale and the tunnel. If the tunnel route exceeds 150 ms, the fix is to run
the same ONNX model in the browser with onnxruntime-web and let the client stop
playback locally on onset, sending the agent a `flush_ack` after the fact. The wire
protocol already carries `generation` on every chunk, so that change does not touch
the server pipeline. Until the measurement exists, in-browser VAD is not built.
