# Zordon

Zordon is a full-duplex voice interface for a running [Claude Code](https://code.claude.com) session.
You talk to it, it talks back in plain spoken English, and you can cut it off mid-sentence.
It runs on the same machine as Claude Code, attaches to the session's tmux pane, and serves a
phone-friendly web page you can open from your desk, your couch, or a cellular connection.

Claude Code stays in charge of everything that plans and executes work. Zordon is a channel:
speech in, keystrokes to the pane, pane output back out as speech. Its permission prompts are
always spoken, answered only with a clear yes or no, and never widened.

## What it does

- **Hands-free operation from a browser.** Resume any Claude Code session, give it a task by voice,
  type when speech is the wrong tool, drop a file or a photo in.
- **Barge-in.** Start talking and playback stops within ~150 ms on a LAN or Tailscale (no
  cloud call is involved); over a public tunnel the two network legs can add 60-160 ms.
  The interrupted sentence is marked as unspoken in the transcript.
- **Output that sounds like a colleague, not a terminal.** A deterministic pre-pass collapses
  code blocks, diffs and paths; a small language model rewrites each sentence into spoken
  English; verbosity levels (`minimal`, `normal`, `technical`) decide how much you hear.
- **Permission prompts are first-class.** Zordon detects when Claude Code is waiting on a
  permission, plan approval or question, speaks it at every verbosity level, and shows a card
  with buttons. Voice answers are gated to a strict yes or no. "Always allow" cannot be granted
  by voice.
- **Transcript questions stay out of Claude Code's context.** "What file did it just change?" is
  answered from the transcript, not sent to the pane.
- **Idle watchdog.** If Claude Code is silent for 20 seconds without a recognized prompt,
  Zordon tells you and shows the last lines of the pane, so an undetected prompt never sits
  unanswered for an hour.

## How it works

```
 phone / laptop browser                        your machine
 ┌──────────────────────┐   WebSocket   ┌─────────────────────────────────────────────┐
 │ talk button          │◀────────────▶│ zordon serve (one Python process)            │
 │ transcript + cards   │  audio, json  │                                             │
 │ text box, file drop  │               │  audio thread   VAD · STT · barge-in switch │
 └──────────────────────┘               │  pipeline       pre-pass · normalizer · TTS │
                                        │  dispatcher     router · shim commands      │
                                        │  session thread tmux capture · prompts      │
                                        └───────────────┬─────────────────────────────┘
                                                        │ send-keys / capture-pane
                                                ┌───────▼────────┐
                                                │ tmux pane:     │
                                                │ claude --resume│
                                                └────────────────┘
```

Each resource (microphone, speaker, tmux pane, WebSocket) has exactly one owning thread, and
threads talk only through queues. That is what makes barge-in and verbosity filtering possible
without either side knowing about the other. Prose comes from Claude Code's own session
transcript file, which it appends to in near real time; prompts and state come from the
rendered pane. See `docs/architecture.md` and `docs/decisions/` for the reasoning.

## Requirements

- Linux or macOS, Python 3.12 or newer (the local Kokoro TTS needs 3.12 or 3.13; `kokoro-onnx` has no 3.14 build yet)
- `tmux` 3.2 or newer
- `curl` (Claude Code's hook handlers use it to tell Zordon about prompts; without it only pane detection runs)
- Claude Code installed and logged in (`claude` on your `PATH`)
- A browser with microphone access: Safari on iOS, Chrome on Android, or any desktop browser

Local speech runs on the CPU and needs about 1 GB of disk for models. A GPU is optional and
makes local speech-to-text roughly 15 times faster.

## Install

```bash
pipx install "zordon[local]"
zordon doctor --download      # fetches the local VAD, TTS and STT models (~820 MB)
zordon serve
```

The first run writes `~/.zordon/config.toml` (mode `0600`) and prints a one-time token.
Open `http://127.0.0.1:8765`, paste the token, tap **Talk**.

`zordon doctor` checks every dependency and every configured provider and prints a one-line fix
for anything missing. `zordon token show` prints the token again.

### First run, step by step

1. `zordon doctor --download`, then `zordon serve`. Serving works without models or keys too,
   but degraded: without the VAD model voice input is off, without a TTS provider nothing is
   spoken; the startup warnings and `zordon doctor` say what to fix. No API key is needed:
   without one, output is normalized by Claude Code itself (headless, under your existing
   login) once each response is complete; with an Anthropic key it is normalized sentence
   by sentence as it streams.
2. Open the page, paste the token. Nothing is spoken until a session is focused: the picker
   opens by itself; **Resume** a session or **Start** one in a directory.
3. A brand-new directory shows Claude Code's trust dialog. Its highlighted default is
   "No, exit", which ends the session; answer the card (or say "yes") to trust the folder.
4. Tap **Talk** and speak. Permission prompts are read aloud and shown as a card.

### Without local models

Cloud providers work out of the box once a key is set in `[providers.keys]` or the matching
environment variable (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `GROQ_API_KEY`,
`ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`):

```toml
[providers]
stt = "openai"          # or groq
tts = "elevenlabs"      # or openai
normalizer = "auto"     # anthropic key -> per sentence; else ollama -> per sentence; else claude-cli -> per turn
router = "jev"          # falls back to anthropic, then to a keyword router
```

### Normalizing without an API key

The default normalizer is `auto`. With no Anthropic key it first looks for a local
[Ollama](https://ollama.com) server with the configured model (default `qwen2.5:3b-instruct`,
about 2 GB):

```bash
ollama pull qwen2.5:3b-instruct
```

Small instruct models rewrite a sentence in 150-300 ms on a desktop GPU, so this path streams
sentence by sentence like the API path, with no key and no quota. Small models like to pad, so
the provider asks for the same facts at the same length and speaks the pre-passed text when a
rewrite grows past 1.6x the input. `ollama_model = "qwen2.5:14b-instruct"` is the higher-quality
option when you have the memory (300-550 ms here).

Without Ollama it runs `claude -p` (Claude Code's headless print mode) under the login you
already have, so the subscription pays, not an API account. Each request is a fresh process, so nothing from one response is in the context of
the next; one process is kept warm so start-up is hidden. It is slow per request (about 4-7 s
with `claude-haiku-4-5`, about 1.5 s API time with `claude-sonnet-5` on Claude Code 2.1.287), so Zordon waits
until a response is finished and normalizes the whole thing in one call instead of sentence
by sentence. Permission prompts are never held back by this. Set
`claude_cli_model = "claude-sonnet-5"` to trade subscription quota for speed, or add an Anthropic key
for per-sentence normalization as text streams.

See `docs/providers.md` for every slot, model, and cost note.

## Reaching Zordon from a phone

Remote access is part of the product, because a voice interface that only works at the desk is
pointless. Three routes, in the order to try them. Browsers only grant microphone access over
HTTPS (or `localhost`), so the first two routes are the practical ones for a phone.

| Route | Command | Who it is for | Cost |
| --- | --- | --- | --- |
| Public tunnel | `zordon serve --tunnel` | Anyone; no network knowledge needed | Free, URL changes each run |
| Tailscale | `zordon serve --bind tailscale` | Users who already have it | Free, stable address |
| LAN | `zordon serve --bind 0.0.0.0` | Same Wi-Fi only; HTTPS needed for the mic | Free, local IP |

`--tunnel` starts a `cloudflared` quick tunnel (downloaded on first use into `~/.zordon/bin/`; `zordon doctor --download --tunnel` fetches it ahead of time),
waits for its `trycloudflare.com` URL, and prints it as text and as a QR code in the terminal and
on the session picker. A public URL changes the threat model, so the tunnel route turns three
recommendations into requirements: the token is mandatory, failed token attempts are limited to
five per minute per IP and logged, and the WebSocket drops after 30 minutes idle.
Details, including Tailscale HTTPS and a static URL on your own domain, are in
`docs/remote-access.md`.

## Talking to it

Everything you say goes to a router first. It decides between three destinations:

| You say | Where it goes |
| --- | --- |
| "add retry logic to the upload handler", "run the tests again", "yes" | Claude Code, as keystrokes |
| "what file did it just change?", "did the tests pass?" | Answered from the transcript; Claude Code is not touched |
| "mute", "stop", "switch to the API session", "set verbosity to technical" | Zordon itself |

When Claude Code is waiting on a permission, routing is replaced by a strict yes/no gate. Anything
else is read back to you. "Stop" sends Escape to the pane and interrupts Claude Code itself;
simply starting to talk only interrupts playback.

## Safety

Zordon turns speech into keystrokes in a shell with your permissions, so it treats the
transcript as untrusted input and keeps Claude Code's own guardrails fully intact.

- Claude Code's `settings.json` files are the only source of truth for permissions. Zordon reads
  the active mode and allow/deny lists and speaks a summary; it never writes them.
- It never passes a permission-bypass flag, never writes `bypassPermissions` anywhere, and refuses
  to start a pane in that mode. Permission prompts cannot be approved on your behalf.
- Keystrokes are sent literally (`tmux send-keys -l`) with control characters stripped; Enter is
  a separate call.
- Shim commands are a closed set. The router can select one; it cannot construct one.
- The browser holds a session cookie only. Provider keys live in `~/.zordon/config.toml`
  (`0600`) and are never sent to the client or logged.
- The server binds to `127.0.0.1` unless you say otherwise, and refuses a non-loopback bind
  without a token.
- Lines that look like secrets (API keys, bearer tokens, private key headers) are masked before
  they reach the normalizer, the speech engine, or the client.

More in `docs/security.md`.

## Configuration

`~/.zordon/config.toml`, created on first run:

```toml
[server]
bind = "127.0.0.1"
port = 8765
token = "generated-on-first-run"

[providers]
stt = "faster-whisper"      # or openai, groq
tts = "kokoro"              # or elevenlabs, openai
normalizer = "auto"         # or anthropic, ollama, claude-cli, passthrough
router = "jev"              # or anthropic, keyword

[providers.keys]
anthropic = ""
openai = ""
elevenlabs = ""
groq = ""
typesafe = ""

[voice]
verbosity = "minimal"
tool_chatter = false
idle_watchdog_seconds = 20
router_confidence = 0.85
```

Set `ZORDON_HOME` to relocate the whole state directory.

## Status

Alpha. The session core, output pipeline, web client, audio path, router and release tooling are
implemented and tested against real captures of Claude Code 2.1.x. Prompt detection is versioned
(`zordon/session/prompts.py`, `docs/prompts-version.md`) because the terminal format changes
between Claude Code releases; the watchdog exists for the formats it has not seen yet.

Known limits and the reasoning behind the main choices are in `docs/decisions/`.

## Development

```bash
git clone https://github.com/jeremiahcarreon/zordon
cd zordon
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,local,jev]"
.venv/bin/python -m pytest -q                 # everything, including the integration tests (they need tmux and start a private server)
.venv/bin/python -m pytest -q -m "not integration"  # unit tests only
ZORDON_TEST_MODELS=~/.zordon/models .venv/bin/python -m pytest -q -m provider
```

`eval/fixtures/pane/` holds real terminal captures used by the prompt and pre-pass tests.
`eval/router_set.jsonl` and `eval/normalizer_set.jsonl` are the evaluation sets the design
requires; `eval/run_router_eval.py` and `eval/run_normalizer_eval.py` run them against any
configured provider.

## License

MIT. See `LICENSE`.
