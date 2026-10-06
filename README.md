# Zordon

Zordon is a full-duplex voice interface for a running [Claude Code](https://code.claude.com) session.
You talk to it, it talks back in plain spoken English, and you can cut it off mid-sentence.
It runs on the same machine as Claude Code, attaches to the session's tmux pane, and serves a
phone-friendly web page you can open from your desk, your couch, or a cellular connection.

Claude Code stays in charge of everything that plans and executes work. Zordon is a channel:
speech in, keystrokes to the pane, pane output back out as speech. Its permission prompts are
always spoken, answered only with a clear yes or no, and never widened.

## What it does

- **Projects, not panes.** Two buttons: *Start a new project* walks you through picking or
  creating a folder in your home directory and how much the agent should ask; *Continue a
  previous project* picks up where you left off, reconnecting to a running agent or resuming the
  conversation. Nothing about tmux unless you open *Advanced*.
- **Hands-free operation from a browser.** Give a project a task by voice, type when speech is
  the wrong tool, drop a file or a photo in. Say "pause" to step back to the project list.
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

**Linux, macOS, or Windows through WSL2.** Zordon drives the agent inside tmux, which has no
native Windows build. On Windows: `wsl --install`, open the Ubuntu terminal, run the install
line there, then open `http://localhost:8765` from your Windows browser (WSL2 forwards it; the
microphone works because the browser is on Windows). Native PowerShell support would need a
different pane backend and is not planned for the MVP.

**A normal user account with `sudo`.** Zordon will not run as root (see Install). The account
needs `sudo` only during setup, to install the system pieces below; serving needs none.

Zordon assumes nothing else about your machine. The setup wizard checks each item below, shows
the exact install command for your package manager (apt, dnf, pacman, zypper, apk or brew),
runs it only when you say yes (sudo prompts as usual), and offers to open the agent for its
first login. `zordon doctor` prints the same commands as fixes.

- Linux or macOS, Python 3.12 or newer (the local Kokoro TTS needs 3.12 or 3.13; `kokoro-onnx` has no 3.14 build yet)
- `tmux` 3.2 or newer
- `curl` (Claude Code's hook handlers use it to tell Zordon about prompts; without it only pane detection runs)
- A coding agent in the terminal. Claude Code (`claude` on your `PATH`) has full support;
  OpenAI's Codex CLI has an adapter built from its source and onboarding screens; any other
  terminal agent (aider, Gemini CLI, Amazon Q, ...) works through the generic adapter by attaching
  Zordon to the tmux pane it already runs in. See `docs/agents.md`.
- A browser with microphone access: Safari on iOS, Chrome on Android, or any desktop browser

Local speech runs on the CPU and needs about 1 GB of disk for models. A GPU is optional and
makes local speech-to-text roughly 15 times faster.

## Install

One line, no prerequisites beyond curl:

```bash
curl -fsSL https://raw.githubusercontent.com/jeremiahcarreon/zordon/main/install.sh | sh
```

The script is short and worth reading first. It installs [uv](https://docs.astral.sh/uv/) into
`~/.local/bin` (no sudo; a pinned uv release whose installer is checksum-verified before it runs), lets uv fetch a managed Python 3.12 if the system has none, installs
zordon as an isolated tool, and starts the guided setup. Nothing else happens without a yes.

Run it as a normal user **that can use `sudo`**: the setup wizard installs tmux, Node.js, the
agent and Ollama through it and asks for your password once. If you run the installer as root
(a fresh container, say), it stops, creates such a user for you (it asks for the name, default
`zordon`), adds it to the `sudo` group, asks for a password, and continues the install as
them. Zordon refuses to serve as root: it drives a coding agent with the power of the account
it runs under, and Claude Code itself will not skip permission checks for root.

Making the user yourself instead? As root:

```bash
useradd -m -s /bin/bash jeremiahc      # or: adduser jeremiahc
usermod -aG sudo jeremiahc             # "wheel" instead of "sudo" on Fedora/Arch
passwd jeremiahc                       # sudo asks for this during setup
su - jeremiahc                         # a fresh login, so the group applies
```

If `sudo` answers "jeremiahc is not in the sudoers file", the `usermod` step was skipped or the
shell predates it: run it as root and log in again. For a throwaway container you can skip the
password prompt entirely with
`echo 'jeremiahc ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/jeremiahc && chmod 0440 /etc/sudoers.d/jeremiahc`.
Already have Python 3.12+ and pipx? This works too:

```bash
pipx install zordon
zordon serve
```

That is the whole install. The first `zordon serve` finds no config and runs a guided setup in
the terminal: it shows what it found on the machine (tmux, Claude Code, Ollama, GPU), asks four
questions with the trade-offs written out, and then does the work: downloads the local speech
models (~820 MB), pulls the Ollama model, fetches cloudflared if you chose the tunnel, writes
`~/.zordon/config.toml` (mode `0600`) and prints your one-time token. Enter takes the detected
default at every question, so the no-key path is a few presses of Enter. If no coding agent is
installed it stops there, prints the install commands, and lets you finish later.

The questions:

0. **Agent**: Claude Code, Codex, or attach to any tmux pane.
   After the questions, a **prerequisites** step offers to install anything missing for your
   choices: tmux, curl, Node.js and npm, the agent itself, Ollama.
1. **Speech**: local (Kokoro + faster-whisper, private, one download) or cloud (OpenAI,
   ElevenLabs, Groq; lowest latency, pay per use, needs keys).
2. **Spoken English**: who rewrites Claude Code's terse output for speech. A local Ollama model
   (free, ~200 ms per sentence, speaks as Claude types), an Anthropic API key (best quality,
   ~$0.001 per sentence), your own Claude login through headless Claude Code (no key, no extra
   install, but 5-15 s per response so Zordon waits for a whole response), or none.
3. **Routing**: built-in rules (free, instant, safe default), TypeSafe Jev, or an Anthropic key.
4. **Reach**: this machine only, a public tunnel for your phone, Tailscale, or same Wi-Fi.

`zordon setup` re-runs it any time; `zordon setup --yes` takes the defaults without asking;
`zordon serve --no-setup` skips it. `zordon doctor` checks every dependency and provider and
prints a one-line fix for anything missing. `zordon token show` prints the token again.

**Setup TUI.** On a terminal, `zordon setup` (and the first `zordon serve`) opens a full-screen
version of the same wizard: a detection table, one screen per question with every option's
trade-offs on a card you can click or pick with the arrow keys, a prerequisites screen with an
Install button per missing piece and its output streamed live (sudo prompts take over the
terminal and hand it back), a progress bar for the downloads, and a summary card with your token
and the exact `zordon serve` command. Esc goes back, `q` asks before quitting, and nothing is
written until the downloads step. `zordon setup --plain` is the question-and-answer form; it is
also what runs when there is no terminal. `zordon uninstall` uses the same UI: what Zordon
installed outside its environment is a checkbox each.

### After setup

Setup ends with a **Start Zordon** button: it starts Zordon in the background (with the phone
tunnel when you chose phone access) and prints the address, the QR code and the token in the
terminal. The sign-in step for Claude Code opens it in the terminal and closes it by itself
once you are signed in.

1. Open `http://127.0.0.1:8765` (or scan the code), paste the token. You land on **Projects**. Tap **Start a new
   project**: pick a folder inside your home directory (or make one), choose how much the
   agent should ask, tap Start. Next time, **Continue a previous project** brings it back.
2. A brand-new folder shows Claude Code's trust dialog. Its highlighted default is "No, exit",
   which ends the session; answer the card (or say "yes") to trust the folder.
3. Tap **Talk** and speak. Permission prompts are read aloud and shown as a card. Say "pause"
   (or tap it) to go back to the project list; the agent keeps running.

Serving works with pieces missing, but degraded: without the VAD model voice input is off,
without a TTS provider nothing is spoken; the startup warnings and `zordon doctor` say what to
fix.

### Cloud providers

Cloud providers work once a key is set in `[providers.keys]` or the matching
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

## Running in the background

```bash
zordon start            # detached; log in ~/.zordon/serve.log
zordon status           # pid, URL, health, service state
zordon logs -f
zordon restart          # picks up an installed update; reuses the flags it was started with
zordon stop
zordon start --tunnel   # detached with the public tunnel; `zordon status --qr` shows the URL and code
```

To start at login and restart on failure: `zordon service install` writes a systemd user unit
(Linux) or a launchd agent (macOS), enables and starts it; `--tunnel` and `--bind` are passed
through. `zordon service status|uninstall` manage it. On Linux, `loginctl enable-linger $USER`
keeps it running after you log out. Containers without systemd use `zordon start`.

## Updates and health

Installs track the GitHub repository until there is a PyPI release. While `zordon serve` runs,
a background check compares the running version with the one on the tracked channel: at start
and then every 6 hours (cached, 3 s timeout, never blocks startup). When a newer version exists
and `[update] auto = true` (the default), it is installed through the same tool that installed
Zordon (uv or pipx); the terminal and every connected browser get a banner and you hear it once
("Zordon 0.4.0 is installed. Restart Zordon when convenient to use it."). The running process
keeps the old code until you do: `zordon restart` for a detached server, or stop and start
`zordon serve`. Set `auto = false` to be told instead, run `zordon update` yourself,
or `zordon serve --no-update` / `ZORDON_NO_UPDATE_CHECK=1` to skip the check entirely.

The web page shows a health strip: one dot per component (tmux, the agent, the focused
session, rewriter, voice, transcription, voice activity detection, routing, worker threads,
hooks, updates, tunnel). Amber is degraded, red means something will not work; tap a dot for
the detail and the exact fix. A red item also raises a banner, and saying "status" reads the
degraded items aloud. `GET /health` returns the same report as JSON; `zordon doctor` is the
offline equivalent before serving.

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

**You can pause to think.** What you say is typed into Claude Code's input box as each phrase is
transcribed, but not sent. It goes when you have been quiet for 2.5 seconds (`voice.submit_quiet_ms`),
or at once when you say "go ahead" or "send it". "Scratch that" clears the box. Text typed on the
page is sent immediately.

**Cut it off, or speed it up.** "Got it", "say no more" or the Hush button stop reading the
current answer and drop the rest; the agent keeps working and nothing is sent. Settings has a
speech speed slider (0.6x to 2x, saved), and picking a voice plays a short sample of it at once
(**Hear this voice** repeats it). Each bubble shows Claude's text as written; tap it to
see the sentences as spoken.

**You can tell when it is working.** The project header shows a pulsing "working" indicator,
"waiting for you" when it needs an answer, and the page plays a short two-note tone when the
agent starts and when it is done (Settings > Sounds to turn it off). Failed tool calls are not
read out any more; Claude explains them in its own words.

**Use the GPU for speech recognition.** With an NVIDIA GPU, setup offers the CUDA libraries
(`zordon[gpu]`, about 900 MB). A sentence is then recognised in about 20 ms instead of most of a
second, and the Heard line updates while you are still talking. The health strip warns when a
GPU is present but unused.

**Claude is told it is on a call.** Every session Zordon launches carries a short system
prompt: short turns, keep the listener in the loop like a colleague on a phone call, one
question at a time through the question tool, no overview or detail unless asked, say the plan
and wait for "go ahead". Read and rewrite it under Settings > What Claude is told (it lives in
`~/.zordon/voice_prompt.md`). Zordon speaks every sentence Claude writes (`voice.verbosity =
"normal"`; `minimal` keeps a turn's first and last sentences and is for agents that still write
reports). The rewriter is off (`voice.normalize = "never"`); the deterministic pre-pass still
strips markdown. A project with **Talk it through first**
(the default) also starts in plan mode when it asks before acting: Claude cannot change anything
until you approve its plan, and "go ahead" is the approval.

**Terminal or headless, per project.** By default a project runs Claude Code in a tmux pane you
can look at. Choose **Run without a terminal (headless)** when starting a project and it runs
`claude -p` over a JSON stream instead: no screen, no trust dialog, nothing to scrape, and every
permission or question comes to Zordon through its own MCP permission tool. The page's
transcript is the whole view. Details in `docs/headless.md`.

**Prompts come through Claude Code's own hook, not the screen.** When Claude Code wants permission,
asks a question, or has a plan ready, it tells Zordon directly (its `PermissionRequest` hook) with
the exact command description, every option, or the plan text, and waits. You hear it in full and
your yes, no, choice or feedback is the answer; nothing is drawn in the terminal unless you do not
answer, in which case Claude Code shows its usual dialog and Zordon reads that instead.

### Projects and how much the agent asks

A project is a folder in your home directory plus how its agent runs there. Zordon remembers
each one in `~/.zordon/projects.json`: the folder, the assistant, the permission mode, the
agent's last conversation and the pane it ran in. *Continue* reconnects to the pane when it is
still alive, else resumes the conversation, else starts fresh in the folder. *Forget* drops the
record and touches no files.

When you start a project you choose how much it should ask:

| Choice | Claude Code mode | What happens |
| --- | --- | --- |
| Ask me before acting | `default` | Every command and file change is read to you; you say yes or no. |
| Handle routine things itself | `auto` | Claude Code approves routine commands on its own and asks for the rest. Recommended for voice. |
| Never ask | `bypassPermissions` | Nothing is asked. Only for a folder you can afford to lose, ideally in a container or VM. |

*Never ask* is a per-project choice made when the project is created, and that is the only place
Zordon will ever set it: not by voice, not from the mode switcher, not as a config default, and
never with `--dangerously-skip-permissions`. Claude Code shows its own warning once before
running that way; Zordon reads it to you and you confirm. Every project also keeps **file edits
inside its folder** by default: a hook denies edits outside the project directory (the agent's
own `~/.claude` is allowed), with the reason, and fails closed if Zordon is unreachable. Shell
commands are not sandboxed by this; a *Never ask* project can still run `rm -rf ..`.

Running sessions can switch between default, accept edits and plan by voice ("switch to plan
mode"), and to auto from **Advanced**. The default for sessions started from *Advanced*:

```toml
[sessions]
permission_mode = "auto"    # default | acceptEdits | plan | auto | dontAsk
```

With nothing open Zordon is in *admin mode*: speech goes to Zordon only ("open the api
project", "list projects", "new project", "status"), never to an agent.

## Safety

Zordon turns speech into keystrokes in a shell with your permissions, so it treats the
transcript as untrusted input and keeps Claude Code's own guardrails fully intact.

- Claude Code's `settings.json` files are the only source of truth for permissions. Zordon reads
  the active mode and allow/deny lists and speaks a summary; it never writes them.
- It never passes a permission-bypass flag and never writes `bypassPermissions` to any settings
  file. The one exception is a project you created with *Never ask*, which launches with
  `--permission-mode bypassPermissions`; voice cannot switch into that mode and "always allow"
  options are never selected on your behalf.
- Zordon refuses to run as root, and projects live inside your home directory.
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

[update]
check = true                # every 6 h while serve runs
auto = true
channel = "main"

[sessions]
permission_mode = "default" # what new sessions start in; "auto" for fewer prompts

[voice]
verbosity = "normal"        # every sentence; "minimal" keeps a turn's first and last only
tool_chatter = false
idle_watchdog_seconds = 20
router_confidence = 0.85
submit_quiet_ms = 2500      # quiet before what you said is sent; 0 sends each phrase at once
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
