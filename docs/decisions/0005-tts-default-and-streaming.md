# 0005: Kokoro fp32 is the default TTS; synthesis is per sentence

**Status:** accepted, 2026-10-02

## Question

From the design: "Kokoro's streaming granularity: does it emit audio per sentence or
per clause, and is first-audio latency acceptable, or should the MVP default to a
cloud TTS and make Kokoro the privacy option?"

## What was measured

kokoro-onnx 0.6.1 on CPU (onnxruntime 1.30, default threading), model files from
the `model-files-v1.1` release:

| file | size | sha256 |
| --- | --- | --- |
| `kokoro-v1.0.onnx` (fp32) | 325,505,369 | `beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a` |
| `voices-v1.0.bin` | 28,214,398 | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |
| `kokoro-v1.0.int8.onnx` | 114,119,327 | `ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c` |

| measurement | fp32 | int8 |
| --- | --- | --- |
| model load | 640-660 ms | |
| 11-word sentence (68 phonemes, 3.88 s of audio) | **440-590 ms** (real-time factor 0.11-0.15) | 4.0-4.2 s (RTF about 1.05) |
| "Done, tests pass." (1.47 s of audio) | 266 ms | 2.0 s |
| phonemization alone | 0.4 ms | |

int8 is about 7x slower than fp32 on this CPU, so it is not the default. The fp16
model and GPU execution were not tested.

Output is float32 mono at 24,000 Hz, peak about 0.63 for `af_heart`. Kokoro cannot
emit 16 kHz; the browser's `AudioContext` resamples 24 kHz buffers on playback.

**Streaming granularity.** `create_stream` yields one chunk per phoneme batch, and
batching only happens above `MAX_PHONEME_LENGTH = 510` phonemes (roughly 80-100
English words). Below that the whole text is one batch, so time-to-first-audio
equals total synthesis time:

| input | chunks | first audio | total |
| --- | --- | --- | --- |
| 1 sentence, 11 words | 1 | 435 ms | 435 ms |
| 3 sentences, 17 words | 1 | 863 ms | 863 ms |
| 152 words, 903 phonemes | 2 | 3,068 ms | 6,450 ms |

When it does split, batches are balanced to equal size, so the first chunk of a
long text is about half of it. There is no per-clause streaming inside a sentence.

**Voices.** `voices-v1.0.bin` holds 54 styles. English: `af_alloy af_aoede af_bella
af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky` and `am_adam
am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa` (American,
`lang="en-us"`); `bf_alice bf_emma bf_isabella bf_lily` and `bm_daniel bm_george
bm_lewis` plus one more male voice (British, `lang="en-gb"`). The rest are Spanish (`ef_`/`em_`),
French (`ff_`), Hindi (`hf_`/`hm_`), Italian (`if_`/`im_`), Japanese (`jf_`/`jm_`),
Portuguese (`pf_`/`pm_`) and Chinese (`zf_`/`zm_`). Per-sentence cost is the same
across voices (af_sarah 595 ms, am_adam 554 ms for the test sentence).

**Dependencies.** `espeakng-loader` bundles `libespeak-ng.so.1.52.0` and its data
(21 MB); no system package is needed. espeak-ng has a fixed 160-byte path buffer:
if the absolute path to `espeak-ng-data` is 160 characters or longer, the process
exits with code 1 and no Python exception (bisected: 159 works, 160 fails). A normal
pipx install path is about 105 characters.

## Decision

* Default `providers.tts = "kokoro"` with the fp32 model, `tts_voice = "af_heart"`,
  `tts_speed = 1.0` (allowed range 0.5-2.0). `zordon doctor --download` fetches the
  two files above and verifies their hashes.
* The pipeline splits text into sentences itself (`output/sentences.py`) and calls
  Kokoro once per sentence. A typical sentence's audio is ready 300-600 ms after the
  normalizer returns and plays for 2-4 s, so the next sentence synthesizes while the
  current one plays. The first `voice.prebuffer_sentences` (3) are synthesized
  before playback starts so a slow normalizer call cannot open a gap.
* Multi-sentence text is never passed to Kokoro in one call.
* The 160-character espeak path is a `zordon doctor` check; the workaround is
  `EspeakConfig(data_path=<short copy>)` or the system library.
* Cloud TTS (`openai`, `elevenlabs`) is the low-latency option: both stream audio
  within a sentence, which Kokoro does not. A user who wants first audio under 300 ms
  sets `providers.tts` to one of them and accepts that sentences leave the machine.
  Kokoro stays the default because it needs no key and sends nothing anywhere.

## Open

* Sub-sentence first audio with Kokoro by splitting at clause marks and passing
  `sentence_pause=0, clause_pause=0` was not tested.
* The fp16 model and `onnxruntime-gpu` were not measured. GPU would replace the CPU
  onnxruntime wheel in the venv, so it is not a default candidate.
* Memory was not measured precisely (estimate: about 0.5 GB resident for fp32).
