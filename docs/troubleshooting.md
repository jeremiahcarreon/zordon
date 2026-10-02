# Troubleshooting

Start with `zordon doctor`. It checks Python, tmux, the `claude` binary, each
configured provider and its key or model files, the espeak path, the CUDA libraries
when `stt_device = "cuda"`, and cloudflared when `--tunnel` is configured, and says
what is missing. `zordon doctor --download` fetches missing models and binaries.

## Session start

### The session opens on a trust dialog

The first time Claude Code runs in a directory it shows:

```
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
```

This is Claude Code's workspace trust check, shown before its interface starts. The
highlighted default is **No, exit**: pressing Enter in the terminal ends the
process. Zordon detects the dialog, shows it as a card, and speaks it. Accepting
(tap, or say yes and confirm) sends Down then Enter. Decline and the pane exits;
Zordon marks the session detached. The dialog appears once per directory.

### Warnings scroll past before the interface appears

Lines such as
`Permission deny rule (../../.claude/settings.json): Write(.env) is not matched by file permission checks — only Edit(path) rules are. Use Edit(.env) instead`
come from your own Claude Code settings and appear on the normal screen before the
interface takes over. They are not Zordon's and they are harmless for Zordon. Fix
the rule in your settings if you want them gone; Zordon does not edit that file.

### "Nothing changed on screen"

You sent a message and Zordon said nothing changed. Zordon derives state from the
pane, never from what it sent, so this means the keystrokes arrived but Claude Code
did not react within a poll or two. Usual causes:

* The pane is on a prompt (permission, plan, question, trust) that was not
  recognised. Look at the card list and the last pane lines in the transcript row.
* Claude Code was still starting, or a slash-command popup was open. Send again.
* The pane is gone (tmux killed, machine rebooted). The session shows as detached;
  resume it.

### "Looks like Claude Code is waiting on something" (stalled)

The idle watchdog: a working session produced no output for
`voice.idle_watchdog_seconds` (20 s) and no prompt was detected. Zordon shows the
last 10 pane lines. Most often this is a prompt in a format the regex does not know;
answer it in the terminal (or with the client's raw send button) and then see
"Prompt not recognised" below so it gets a fixture. It can also be a genuinely long
model turn; the state returns to working on the next output.

### Prompt not recognised, or recognised wrongly

Prompt detection is a set of regexes against real captures of one Claude Code
version (`PROMPTS_VERSION` in `zordon/session/prompts.py`). Claude Code changes its
interface between releases. If a prompt is missed, misread, or the wrong option is
highlighted:

1. Check `claude --version` against `PROMPTS_VERSION`.
2. Capture the pane as a fixture and open an issue with it, or fix the regex and add
   the fixture and a test yourself. The procedure, including the exact capture
   command and the trust-dialog warning, is in `prompts-version.md`.

Until it is fixed, the watchdog still announces an unanswered prompt within 20 s,
and the registry status and the hook (decision 0009) give two more chances.

### "Session is running in another terminal"

The session has a live entry in `~/.claude/sessions/` that does not point at a tmux
pane. Zordon will not `--resume` a session that is already running (two copies
interleave into one transcript). Either exit it there and resume from Zordon, or
start it inside tmux so Zordon can attach to the pane.

## Audio

### No sound on iPhone or iPad until Talk is tapped

Expected. iOS allows a page to play audio only after a user gesture. The Talk
button creates the audio context and unlocks output inside that tap. If sound stops
after a phone call, Siri, or the page being in the background, tap Talk again.

### "Microphone blocked" or no microphone prompt

Browsers only expose the microphone to a secure context: HTTPS, or `localhost`.
`http://192.168.x.y:8765` and `http://100.x.y.z:8765` on a phone will not get the
microphone in Safari or Chrome. Use `zordon serve --tunnel` (HTTPS) or put Tailscale
Serve in front of the Tailscale address. See `remote-access.md`. On the machine
itself, `http://127.0.0.1:8765` works.

### It interrupts itself

The speaker is triggering the VAD. The browser asks for `echoCancellation`, and
Zordon ignores the VAD for `voice.echo_guard_ms` (120 ms) after playback starts, but
a loud speaker next to the microphone can still leak through. Lower the volume,
use headphones, or raise `voice.speech_onset_frames`.

### Interruption is slow over the tunnel

Barge-in has two network legs (audio up, flush down). On LAN or Tailscale the whole
path is about 100-130 ms; through a public tunnel the round trip can push it past
150 ms. Decision 0004 describes the in-browser VAD that would remove the network
legs; it is not built yet.

### Speech is recognised slowly

`faster-whisper` on CPU takes about 0.9-1.0 s for a 4 s utterance. Options: a cloud
STT (`providers.stt = "groq"` or `"openai"`), or CUDA (next item). A short
`stt_model` such as `base.en` is faster and less accurate.

### `Library libcublas.so.12 is not found or cannot be loaded`

`stt_device = "cuda"` without the CUDA 12 runtime. Run
`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` in Zordon's environment (about
2.2 GB) and restart; Zordon preloads the libraries from `site-packages/nvidia/`. If
it still fails, set
`LD_LIBRARY_PATH=<site-packages>/nvidia/cublas/lib:<site-packages>/nvidia/cudnn/lib:<site-packages>/nvidia/cuda_nvrtc/lib`.
Check `nvidia-smi` shows the GPU; the driver must support CUDA 12. Falling back to
`stt_device = "cpu"` always works. Details in `providers.md`.

### Zordon exits with status 1 and `Error processing file '/home/runner/work/espeakng-loader/...'`

The espeak-ng data path is too long. espeak-ng has a 160-byte buffer for it; if the
absolute path to `site-packages/espeakng_loader/espeak-ng-data` is 160 characters
or longer, it falls back to a path that only existed on the build machine and the
process exits with no Python traceback. `zordon doctor` reports the length. Fixes:
install Zordon somewhere shorter (pipx's default is about 105 characters), or
install the system `espeak-ng` package and point Kokoro at it
(`EspeakConfig(lib_path=..., data_path=...)`; see `providers.md`).

### Kokoro is slow

Make sure the fp32 model `kokoro-v1.0.onnx` is in use. The int8 model is about 7x
slower on CPU. A sentence should take 0.3-0.6 s; the first sentence of a turn is
buffered with the next two, so a 1-2 s wait before the first words is normal.

## Network

### `zordon serve --tunnel` fails to start

* `cloudflared not found`: run `zordon doctor --download`, or install cloudflared
  yourself and put it on `PATH`. The download comes from GitHub's release page
  (`cloudflared-linux-amd64`, `cloudflared-linux-arm64`,
  `cloudflared-darwin-*.tgz`), about 40 MB.
* `did not print a trycloudflare.com URL in 30s`: cloudflared could not reach
  Cloudflare. Its last log lines are shown. QUIC on UDP 7844 is sometimes blocked;
  cloudflared's `--protocol http2` fallback usually works. Corporate networks may
  block it entirely; use Tailscale instead.
* `a server.token is required`: the tunnel refuses to run without a token. Check
  `~/.zordon/config.toml`.

### `server.bind=... is not loopback; a server.token is required`

Any bind address other than `127.0.0.1` needs `server.token` in `config.toml`.
A fresh config has one; if it was removed, generate a new one (see `security.md`).

### 429 on login

Five failed token attempts from one address within a minute. Wait for the
`Retry-After` period; the correct token is also refused until then. `zordon token
show` prints the token so it can be copied rather than typed.

### Disconnected after 30 minutes

The idle disconnect (`server.idle_disconnect_minutes`, forced on under `--tunnel`).
Reconnect; the cookie is still valid. The limit counts application messages, not
protocol pings, so an open tab with no activity will be dropped.

### Logged out after restarting Zordon

Session cookies live in memory. Enter the token again.

## Providers

### Everything is spoken terse, like commit messages

The normalizer is in `passthrough` mode, either by configuration or because no
Anthropic key is configured (`providers.keys.anthropic` or `ANTHROPIC_API_KEY`).
With a key, the Haiku normalizer rewrites each sentence; a call that fails or takes
longer than 1.5 s falls back to the terse text for that sentence only, so brief
terseness during a provider outage is expected.

### Questions about the transcript go to Claude Code

The router is unsure (probability below `voice.router_confidence`, 0.85) and sends
the utterance to Claude Code, which is the designed safe failure. Rephrase as a
question about the past ("what file did it change?"), or type it. If the `jev`
router is configured but its key is missing or rejected, Zordon uses the Haiku
router for the whole process and logs why; with no Anthropic key either, the
`keyword` router handles only shim commands and yes/no.

### `TypeSafe key rejected` or `Jev unavailable` in the log

The TypeSafe API returned 401 for the configured key, or could not be reached at
startup. The router falls back as above. Check `providers.keys.typesafe` /
`TYPESAFE_API_KEY`.

### Model download fails or `sha256 mismatch`

`zordon doctor --download` verifies every single-file model against the hashes in
`zordon/assets.py` and deletes a mismatching download. A persistent mismatch means
the upstream file changed; open an issue with the hash printed. A partial download
is retried from scratch.
