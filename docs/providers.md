# Providers

Zordon has four provider slots. Each is a small interface with one method
(`zordon/providers.py`); the implementation is chosen by name in
`~/.zordon/config.toml` and can be changed from the client's settings drawer
while running. Local providers need model files; cloud providers need an API key.

## The slots

| Slot | Config key | Default | Alternatives | Interface | What it does |
| --- | --- | --- | --- | --- | --- |
| Speech to text | `providers.stt` | `faster-whisper` | `openai`, `groq` | `STTProvider.transcribe(pcm16k) -> STTResult` | Turns one utterance (16 kHz float32) into text and a confidence where available |
| Text to speech | `providers.tts` | `kokoro` | `openai`, `elevenlabs` | `TTSProvider.synthesize(text) -> Iterator[bytes]` | Turns one sentence into int16 PCM chunks at `sample_rate` |
| Normalizer | `providers.normalizer` | `auto` (Anthropic key -> `anthropic`, else `ollama`, else `claude-cli`, else `passthrough`) | `anthropic`, `ollama`, `claude-cli`, `passthrough` | `Normalizer.normalize(sentence, context) -> str` | Rewrites one pre-passed sentence as fluent spoken English; `passthrough` returns it unchanged |
| Router | `providers.router` | `jev` | `anthropic`, `keyword` | `Router.route()`, `.yes_no()`, `.prompt_score()` | Decides whether an utterance goes to Claude Code, the transcript, or a shim command; the strict yes/no gate; the prompt second opinion |

### Speech to text

| Name | Where it runs | Needs | Measured (3.9 s utterance) | Notes |
| --- | --- | --- | --- | --- |
| `faster-whisper` | Local, CPU or CUDA | `pip install zordon[local]`; model folder `faster-whisper-small.en/` | CPU int8, beam 5: **880-1045 ms**; CUDA fp16: **59-60 ms** (257 ms first call) | Default. `stt_model` (default `small.en`), `stt_device` (`cpu` or `cuda`). Hallucinates `' You'` on pure silence with `no_speech_prob` 0.86; Zordon drops segments with `no_speech_prob > 0.6` and runs its own VAD before sending audio |
| `openai` | OpenAI API | `providers.keys.openai` or `OPENAI_API_KEY` | not measured here | Whisper over HTTPS; the utterance leaves the machine |
| `groq` | Groq API | `providers.keys.groq` or `GROQ_API_KEY` | not measured here | Whisper over HTTPS; the utterance leaves the machine |

faster-whisper reports no single confidence; Zordon derives one from the segments'
`avg_logprob` (about -0.25 is clean speech, below -1.0 is poor) and `no_speech_prob`.

### Text to speech

| Name | Where it runs | Needs | Measured | Notes |
| --- | --- | --- | --- | --- |
| `kokoro` | Local, CPU | `pip install zordon[local]`; `kokoro-v1.0.onnx` + `voices-v1.0.bin` | load 640-660 ms; 11-word sentence **440-590 ms** for 3.9 s of audio; "Done, tests pass." 266 ms | Default. 24 kHz output. One synthesis call per sentence; no streaming inside a sentence (decision 0005). `tts_voice` default `af_heart`; `tts_speed` 0.5-2.0 |
| `openai` | OpenAI API | `providers.keys.openai` or `OPENAI_API_KEY` | not measured here | Streams audio within a sentence; lower first-audio latency than Kokoro |
| `elevenlabs` | ElevenLabs API | `providers.keys.elevenlabs` or `ELEVENLABS_API_KEY` | not measured here | Streams audio within a sentence |

Kokoro voices (54 in `voices-v1.0.bin`). English, American (`lang="en-us"`):
`af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river
af_sarah af_sky am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck
am_santa`. English, British (`lang="en-gb"`): `bf_alice bf_emma bf_isabella bf_lily
bm_daniel bm_george bm_lewis` and one more male voice (the full list is in
`voices-v1.0.bin`). The remaining 26 are Spanish, French, Hindi, Italian, Japanese,
Portuguese and Chinese.

Do not use `kokoro-v1.0.int8.onnx` on CPU: it measured about 7x slower than fp32
(4.0-4.2 s for the sentence fp32 does in 0.5 s). The fp16 model and GPU execution
are untested.

### Normalizer

| Name | Model | Needs | Notes |
| --- | --- | --- | --- |
| `anthropic` | `normalizer_model`, default `claude-haiku-4-5` | `providers.keys.anthropic` or `ANTHROPIC_API_KEY` | One call per sentence, `max_tokens` 200, temperature 0 (sent as `extra_body`), `normalizer_timeout_seconds` (1.5 s) and no retries; on any failure the sentence is spoken as pre-passed. `claude-sonnet-5` also works; it rejects a temperature and runs adaptive thinking unless disabled, which the provider handles |
| `ollama` | `ollama_model`, default `qwen2.5:3b-instruct`; `ollama_url`, default `http://127.0.0.1:11434` | A running Ollama server with the model pulled; no key | Per sentence over `/api/chat`, temperature 0, `keep_alive` 30 min, `normalizer_timeout_seconds` (1.5 s). Length guard: a rewrite over 1.6x the input words is retried once, then the pre-passed sentence is spoken. Measured 2026-10-02 on an RTX 4090 over the 50-sample set: 1.5b 208 ms mean, lenient 24% (ignores formatting rules); 3b 219 ms, lenient 40%; 14b 565 ms, lenient 44%. The dominant failure for every size is a dropped fact, not padding (the guard fired once in 50). The same object answers transcript questions |
| `claude-cli` | `claude_cli_model`, default `claude-haiku-4-5` (`claude-sonnet-5` measured faster) | Claude Code installed and logged in; no key | Runs `claude -p --input-format stream-json --output-format stream-json --tools "" --setting-sources "" --no-session-persistence --max-turns 1` under the user's login. One fresh process per request (nothing carries over), one kept warm. Measured on 2.1.287: 4-7 s per request with `claude-haiku-4-5`, about 1.5 s API time with `claude-sonnet-5`; the process still carries Claude Code's own ~67k-token baseline prefix (only `--bare` removes it, and `--bare` is API-key only). Too slow per sentence, so the provider declares turn granularity: the pipeline collects a whole turn and normalizes it once it ends (or after a 6 s lull without a completion). Prompts and notices bypass this. `claude_cli_timeout_seconds` (30 s) bounds a request; on failure the turn is spoken pre-passed. The same process answers transcript questions |
| `passthrough` | none | nothing | Speaks the pre-passed text. Readable but terse. Automatic when neither an Anthropic key nor the `claude` binary is available |

Prompt caching: the normalizer prompt carries a `cache_control` marker, but Haiku
4.5 only caches prefixes of 4,096 tokens or more and the prompt is a few hundred,
so no cache hits occur on Haiku. The marker is harmless and does engage on
`claude-sonnet-5` (1,024-token minimum).

### Router

| Name | Service | Needs | Notes |
| --- | --- | --- | --- |
| `jev` | TypeSafe System One (`jev-latest`) | `pip install zordon[jev]`; `providers.keys.typesafe` or `TYPESAFE_API_KEY` | Classifier, not a language model: returns a probability per destination. Timeout 1.5 s, one retry. Falls back to `anthropic` for the utterance on any error, and for the whole process if the key is rejected at startup |
| `anthropic` | `router_model`, default `claude-haiku-4-5` | `providers.keys.anthropic` or `ANTHROPIC_API_KEY` | Structured JSON output, `max_tokens` 100, 1.5 s, no retries. Falls back to `keyword` |
| `ollama` | `ollama_model` / `ollama_url` (shared with the normalizer) | A running Ollama server with the model pulled; no key | Explicit opt-in only, never part of the automatic chain. Same prompts and JSON schema as `anthropic`, served locally in 300-600 ms. Measured 2026-10-02 on `eval/router_set.jsonl`: `qwen2.5:3b-instruct` 50% with 12 unsafe misroutes (work requests answered from the transcript); `qwen2.5:14b-instruct` 87% with 4. Both miss the design's bar (95%, zero unsafe), so use it knowingly: the keyword fast path still handles shim commands and yes/no |
| `keyword` | none | nothing | Deterministic: shim commands by phrase table, yes/no by word list, everything else to Claude Code. No network; the last resort and the test default |

Whatever the router answers, only command names in `zordon/routing/commands.py`
can execute. See decision 0006 for thresholds.

## Configuration

```toml
[providers]
stt = "faster-whisper"      # faster-whisper | openai | groq
tts = "kokoro"              # kokoro | openai | elevenlabs
normalizer = "auto"         # auto | anthropic | ollama | claude-cli | passthrough
router = "jev"              # jev | anthropic | keyword

stt_model = "small.en"
stt_device = "cpu"          # cpu | cuda
tts_voice = "af_heart"
tts_speed = 1.0
normalizer_model = "claude-haiku-4-5"
router_model = "claude-haiku-4-5"
normalizer_timeout_seconds = 1.5
claude_cli_model = "claude-haiku-4-5"   # claude-cli normalizer model id
ollama_url = "http://127.0.0.1:11434"
ollama_model = "qwen2.5:3b-instruct"    # ollama normalizer (and opt-in router) model
claude_cli_timeout_seconds = 30.0

[providers.keys]
anthropic = ""
openai = ""
elevenlabs = ""
groq = ""
typesafe = ""
```

A key is read from `[providers.keys]` first and from the environment second:

| `[providers.keys]` entry | Environment fallback |
| --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `elevenlabs` | `ELEVENLABS_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `typesafe` | `TYPESAFE_API_KEY` |

An empty entry means "not configured", not "an empty key". `config.toml` is written
with mode `0600`. Keys are passed to the SDKs explicitly and Zordon never exports
them into the environment itself; the tunnel child (cloudflared/ngrok) is started
with every `*_API_KEY`, `*_TOKEN`, `*_SECRET` and provider-prefixed variable
removed. Keys you supply through environment variables are, like any variable,
visible to the tmux server and the Claude Code panes Zordon starts, so prefer
`config.toml` when that matters. Keys never appear in logs, in the `hello` or
`settings` messages, or in a transcript row.

## Model and binary downloads

`zordon doctor --download` fetches what the configured local providers need into
`~/.zordon/models/` (or `$ZORDON_HOME/models/`) and `~/.zordon/bin/`, verifies size
and sha256 where a hash exists, and re-downloads on mismatch. Plain `zordon doctor`
only reports.

| Asset | File | Size | sha256 | Used by |
| --- | --- | --- | --- | --- |
| `silero_vad` | `silero_vad.onnx` | 2,327,524 | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` | VAD, always |
| `kokoro_model` | `kokoro-v1.0.onnx` | 325,505,369 | `beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a` | `tts = "kokoro"` |
| `kokoro_voices` | `voices-v1.0.bin` | 28,214,398 | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` | `tts = "kokoro"` |
| faster-whisper | `faster-whisper-small.en/` (folder: `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt`) | about 464 MB | none published; `doctor` checks that `model.bin` exists | `stt = "faster-whisper"` |
| `cloudflared` | `cloudflared` (binary, `0755`) | about 40 MB | none; "latest" release | `zordon serve --tunnel` |

Sources: Silero from `raw.githubusercontent.com/snakers4/silero-vad/master/...`
(v6.2; the hash pins the bytes); Kokoro from the `thewh1teagle/kokoro-onnx`
release `model-files-v1.1`; faster-whisper from the Hugging Face repo
`Systran/faster-whisper-small.en` through `faster_whisper.download_model(...,
output_dir=...)`, loaded by path with `local_files_only=True` so no hub call
happens at runtime; cloudflared from
`github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-<os>-<arch>`.
The whole local stack is about 820 MB on disk. Nothing in it needs torch, a CUDA
toolkit, or system packages for espeak-ng or libsndfile.

## faster-whisper on CUDA

`stt_device = "cuda"` needs the CUDA 12 runtime libraries, which the `faster-whisper`
wheel does not bundle. Without them: `RuntimeError: Library libcublas.so.12 is not
found or cannot be loaded`. Install them into the same environment, no system CUDA
required (about 2.2 GB):

```
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

CTranslate2 does not find those wheels by itself. Zordon preloads them with
`ctypes.CDLL(..., mode=RTLD_GLOBAL)` from `site-packages/nvidia/cublas/lib/libcublas.so.12`,
`.../libcublasLt.so.12` and `site-packages/nvidia/cudnn/lib/libcudnn.so.9` before
importing `faster_whisper`. The equivalent manual fix is
`LD_LIBRARY_PATH=<site-packages>/nvidia/cublas/lib:<site-packages>/nvidia/cudnn/lib:<site-packages>/nvidia/cuda_nvrtc/lib`.
Measured: `float16` 59-60 ms per 3.9 s utterance against 880-1045 ms on CPU int8;
`int8_float16` 67-78 ms. `zordon doctor` reports whether the libraries load.

## The espeak path gotcha

Kokoro phonemizes through the bundled espeak-ng. espeak-ng has a fixed 160-byte
buffer for its data path: if the absolute path to
`site-packages/espeakng_loader/espeak-ng-data` is **160 characters or longer**, the
process exits with status 1 and no Python exception, printing
`Error processing file '/home/runner/work/espeakng-loader/.../phontab': No such file or directory`.
Bisected: 159 works, 160 fails. A typical pipx path is about 105 characters; deep
virtualenvs under long project paths hit it. `zordon doctor` checks the length. The
fix is a short copy of `espeak-ng-data` passed as
`EspeakConfig(data_path=...)`, or the system library
(`apt install espeak-ng`; `EspeakConfig(lib_path="/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1", data_path="/usr/lib/x86_64-linux-gnu/espeak-ng-data")`).
See `troubleshooting.md`.

## Cost

With the defaults, only two cloud calls happen per spoken sentence or utterance:

| Provider | Price (as published 2026-10-01) | Per call | Per hour of active use (rough) |
| --- | --- | --- | --- |
| `anthropic` normalizer, Haiku 4.5 | $1.00 per million input tokens, $5.00 per million output | about 600 input + 30 output tokens per sentence: under $0.001 | a few cents |
| `jev` router | $0.042 per million input tokens; output free | about 700 tokens per utterance: $0.00003 | well under a cent |
| `anthropic` router (fallback) | as above | about 400 input + 30 output tokens | a few cents |
| `claude-sonnet-5` normalizer | $2.00 / $10.00 per million; about 30% more tokens than `claude-sonnet-4-6` for the same text | | roughly 2-3x Haiku |
| `openai`, `groq`, `elevenlabs` | not measured here; see the provider's pricing page | | |

The local providers cost nothing to run. `passthrough` and `keyword` make no calls
at all, so a fully offline configuration is `tts = "kokoro"`, `stt = "faster-whisper"`,
`normalizer = "passthrough"`, `router = "keyword"`.
