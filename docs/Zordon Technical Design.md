# Zordon: Technical Design

Oct 1, 2026 · @Jeremiah Carreon

## Problem and goals

Zordon is a full-duplex voice interface for Claude Code: you talk to a running Claude Code session, it talks back in plain spoken English, and you can cut it off mid-sentence. Claude Code today is text in, text out. Its native dictation handles input only, has no speech back, and raw terminal output read aloud is unusable (code blocks, file paths, diff markers, and shorthand from plugins like Caveman).

This is a fresh repository. Nothing from the earlier Claude Voice prototype is carried over as code or as a constraint; the lessons from it are recorded in this document, not inherited by implementation.

**Goals for the MVP**

- Hands-free, phone-friendly operation of an existing Claude Code session from a browser
- Barge-in that stops playback within 150 ms of the user starting to speak, with no network round trip
- Output that sounds like a colleague summarizing, not a terminal being read aloud
- Claude Code's own permission model stays fully in force; Zordon never widens it
- Self-hosted, single user, runs on the same machine as Claude Code

**Non-goals**

- Replacing or wrapping Claude Code's reasoning; Claude Code remains the only thing that plans and executes work
- Running anyone else's compute, storing anyone's code, or relaying across machines (the hosted relay is a later product, not part of this repo)
- A general-purpose voice assistant
- Multi-user access on one host (see Deferred)

## Architecture

Zordon is one Python process on the user's machine, split into three threads with a strict ownership rule: each resource (mic, speaker, tmux pane, WebSocket) has exactly one owner, and threads talk only through queues. Claude Code runs in a tmux pane that Zordon attaches to; it is never embedded, imported, or wrapped.

&#91;embedded content: Zordon architecture: browser, agent, Claude Code, speech providers\]

Audio never touches Claude Code and text never touches the speaker directly: everything crosses through the transport thread's queues, which is what makes barge-in and verbosity filtering possible without either side knowing about the other.

**Why a standalone process rather than a Claude Code plugin or MCP server.** Barge-in has to kill playback the instant speech is detected, and a plugin cannot interrupt its own host. A separate process also means Claude Code's permission prompts, which block its own event loop, are visible to Zordon as pane output rather than invisible to a tool running inside it.

**Why tmux rather than a subprocess pipe.** A tmux pane survives Zordon restarting, keeps the session usable from a normal terminal at the same time, and gives Zordon a reliable way to read the current screen state (`capture-pane`) instead of reassembling it from a byte stream. The cost is a dependency on tmux and on parsing rendered terminal text, covered in Session management.

**Why FastAPI.** The audio and session code will be Python regardless, FastAPI gives WebSockets and static file serving in one dependency, and it keeps the whole MVP in one language.

**Stack**

| Concern | Choice | Reason |
| --- | --- | --- |
| Server | Python 3.12, FastAPI, uvicorn | One language, WebSocket built in |
| Session control | tmux via libtmux or raw `tmux` CLI | Survives restarts, capture-pane for screen state |
| VAD | Silero VAD (local, CPU) | Sub-50 ms, no network, good enough for barge-in |
| STT | Pluggable: faster-whisper local, or OpenAI / Groq Whisper API | Local default for privacy, cloud for speed |
| Normalizer | Pluggable: Claude Haiku via Anthropic API by default | Cheap, fast enough at sentence granularity |
| TTS | Pluggable: Kokoro local, or ElevenLabs / OpenAI streaming API | Must support streaming synthesis |
| Router | Pluggable: Jev (TypeSafe) by default, Haiku fallback | Sub-100 ms, returns a probability |
| Web client | Vanilla HTML, CSS, JS served by FastAPI | No build step for an MVP |
| State | In-memory, SQLite only for transcript history | Nothing to administer |

Every provider sits behind an interface (`STTProvider`, `TTSProvider`, `Normalizer`, `Router`) with one method each, so swapping Jev for a different classifier or Kokoro for ElevenLabs is a config change, not a code change.

## Session management

A Zordon session is a tmux pane running `claude` in a project directory, identified by Claude Code's own session id. Zordon discovers sessions by reading Claude Code's session store under `~/.claude/projects/`, lists them with directory, last-active time, and whether a pane is currently attached, and starts or resumes one on request.

**Lifecycle**

| State | Meaning | Leaves on |
| --- | --- | --- |
| Idle | Pane exists, Claude Code is at its prompt | User input sent |
| Working | Claude Code is producing output | Prompt detected, permission prompt detected, or idle timeout |
| AwaitingPermission | Claude Code is blocked on a yes/no | Approve or deny sent |
| AwaitingPlanApproval | Claude Code is in plan mode waiting on the plan | Approve, revise, or deny sent |
| Stalled | No output for 20 s while Working and no prompt detected | Any output, or user speaks |
| Detached | Pane gone (tmux killed, machine rebooted) | Resume creates a new pane with `claude --resume <id>` |

The session thread derives state from pane output, never from its own assumptions about what it sent. If it sends a message and nothing changes on screen, the state stays Idle and the client is told so. That rule is what prevents the earlier prototype's failure, where the voice layer reported work in progress while Claude Code sat on a permission prompt for an hour.

**Output capture.** The session thread polls `tmux capture-pane -p -J` at 100 ms and diffs against the previous capture, emitting only new lines. Polling rather than `pipe-pane` because capture-pane returns the rendered screen, which is what Claude Code's TUI actually shows, and because it works unchanged after a Zordon restart. Each new line is tagged with a timestamp and the session id before it enters the output queue.

**Prompt detection.** Claude Code's permission and plan prompts are recognized by a small set of regex patterns kept in one file (`prompts.py`) with a version marker, since the TUI format changes between Claude Code releases. A pattern miss is expected; the Stalled state and the idle watchdog exist because detection will never be complete. The router's Score primitive is used as a second opinion when the regex is unsure: it scores the last 10 lines of pane output on how likely they are to be a prompt waiting for input.

**Permission modes.** Claude Code's `settings.json` and `.claude/settings.local.json` are the only source of truth for permissions. On session start Zordon reads the active mode (default, `acceptEdits`, `plan`, `bypassPermissions`) and the allow and deny lists, speaks a one-line summary, and asks whether to keep them. Voice can switch mode; granular allow and deny edits are not voice-editable and the client points at the file instead. Zordon never writes `bypassPermissions`.

**Multiple sessions.** Zordon can attach to several panes at once but only one is "focused" for voice. Unfocused sessions keep capturing output so the transcript stays complete and so the shim can announce when a background session finishes or hits a prompt.

## Output pipeline

Every line Claude Code emits passes through four stages before it is spoken, and most lines never reach the model: the deterministic pre-pass handles the bulk, the normalizer only sees prose, and the verbosity filter drops what the user does not want to hear before anything is synthesized.

1. **Classify and pre-pass (deterministic, no model).** Each new line is tagged by type: prose, code block, diff, file path, tool call, progress spinner, permission prompt, plan. Code blocks are collapsed to a placeholder ("a code block, 12 lines"), diffs to a count ("edited `auth.py`, 8 lines changed"), full paths to the filename, box-drawing and ANSI stripped, known acronyms expanded from a table. Progress spinners and repeated status lines are dropped. This stage is pure functions over strings and is where most of the test coverage lives.
2. **Verbosity filter.** Lines are kept or dropped by the session's verbosity level (below). Permission prompts, plan-approval prompts, and the final summary line are never filtered.
3. **Sentence buffer and normalizer.** Surviving prose is accumulated until a sentence boundary, then sent to the normalizer with the previous two sentences as context and an instruction to rewrite only the current one. The normalizer's job is to turn shorthand (Caveman plugin output, terse commit-message English, inline symbols) into fluent spoken English: numbers as words where natural, no markdown, no abbreviations, filenames spoken as words. The first three sentences are buffered before playback starts so a slow normalizer call does not produce a gap mid-speech.
4. **TTS, streaming.** Each normalized sentence goes to the TTS provider as soon as it returns, using streaming synthesis so first audio arrives before the sentence finishes rendering. Audio chunks go to the audio thread's playback queue, which the barge-in kill switch can flush at any moment.

**Verbosity levels**

| Level | Spoken | Default |
| --- | --- | --- |
| Minimal | Intent at start ("adding the retry logic"), outcome at end ("done, tests pass"), prompts, errors | Yes |
| Normal | Minimal plus file names touched, without line counts |  |
| Technical | Everything after pre-pass, including diff counts and tool calls |  |

Tool-call and progress chatter ("running tests", "searching the codebase") is a separate toggle, off by default, independent of verbosity.

**Normalizer contract.** One call per sentence, input under 600 characters including context, output a single sentence, temperature 0. A call that fails or exceeds 1.5 s falls back to the pre-passed text unnormalized, so a provider outage degrades to "readable but terse" rather than silence. The prompt is versioned in `normalizer/prompt.md` and has its own test set of 50 real Claude Code output samples with expected spoken forms.

**Transcript.** Both the raw pane output and the spoken form are stored per session, line by line, with timestamps and a link between them, so the client can show the exact terminal text under any spoken sentence.

## Input pipeline

The audio thread decides when the user is speaking; the router decides where the words go. Barge-in is handled entirely inside the audio thread with no network round trip, which is the single change that fixes the earlier prototype's poor interruption behavior.

**Capture and VAD.** The browser sends 16 kHz mono PCM frames over the WebSocket in 20 ms chunks (or, when the agent and browser are on the same machine, the agent captures the mic directly). Silero VAD runs on every frame. Speech onset is declared after 3 consecutive speech frames (60 ms); speech end after 700 ms of silence.

**Barge-in.** On speech onset while playback is active, the audio thread flushes the playback queue, stops the TTS stream, and marks the interrupted sentence in the transcript as unspoken. Target latency from first speech frame to silence is 150 ms. The normalizer and TTS are not consulted and do not need to be cancelled synchronously; their late results are discarded by sequence number. A short acoustic echo guard (ignore VAD for 120 ms after playback starts, and use the browser's `echoCancellation` constraint) prevents the speaker triggering its own interruption.

**STT.** The utterance is sent to the STT provider on speech end. Default local provider is faster-whisper `small.en` on CPU; cloud providers are selectable in config. STT output is plain text plus a confidence score where the provider supplies one.

**Routing.** Every transcribed utterance goes to the router before anything else. The router is a classifier, not a language model; it returns one of three destinations and a probability.

| Destination | Examples | Handling |
| --- | --- | --- |
| Claude Code | "add retry logic to the upload handler", "run the tests again", "yes" | Sent to the focused pane as keystrokes, followed by Enter |
| Transcript query | "what file did it just change?", "what was the commit message?" | Answered by the normalizer model over the last N transcript lines; Claude Code is not touched |
| Shim command | "mute", "switch to the API session", "set verbosity to technical", "stop" | Executed locally |

The default router is Jev (TypeSafe) using its Choice primitive with those three options plus "unclear", and its Noul primitive for a confidence score. Anything below the confidence threshold (0.85 to start) or classified as unclear is sent to Claude Code. The safe failure is a question that reaches Claude Code unnecessarily; the unsafe failure is a work request silently answered from a stale transcript.

**Permission responses.** When the session is in AwaitingPermission, routing is replaced by a strict match: the utterance must resolve to yes or no with the router's Noul at 0.95 or higher. Anything else is read back ("I heard: yeah I guess, actually no. Yes or no?"). "Always allow" is not accepted by voice. In AwaitingPlanApproval the accepted set is approve, revise (which forwards the user's next utterance as the revision), and deny.

**Router evaluation.** Before Jev is trusted for anything that executes, the repo carries `eval/router_set.jsonl` of at least 60 real utterances with expected destinations, and a test that fails if accuracy drops below 95% or if any transcript-query example is misrouted to Claude Code. The same harness runs against the Haiku fallback router so the two can be compared.

**Text input.** The client's text box bypasses STT but still goes through the router, so typed questions about the transcript also stay out of Claude Code's context.

## Safety

Zordon is a channel that turns speech into keystrokes in a shell with the user's full permissions, so the design assumes the transcript is untrusted input and that Claude Code's own guardrails must stay fully intact.

- **Permission prompts are always spoken**, at every verbosity level, and are the one place voice input is narrowed to a strict yes or no. Nothing in Zordon can approve a prompt on the user's behalf.
- **No auto-approve.** Zordon never passes `--dangerously-skip-permissions`, never writes `bypassPermissions` to a settings file, and refuses to start a pane in that mode. A user who wants it can set it in their own Claude Code config; Zordon reports it on session start so they know.
- **Idle watchdog.** If a Working session produces no output for 20 seconds and no prompt was detected, Zordon tells the user it looks like Claude Code is waiting on something and shows the last 10 pane lines in the transcript. This is the backstop for every prompt format the regex has not seen yet.
- **Keystroke injection is literal.** Utterances are sent to the pane with `tmux send-keys -l` so no character is interpreted as a tmux key name, and a trailing Enter is sent as a separate call. The agent strips control characters before sending.
- **Shim commands are a closed set.** The router can only select commands from a fixed list; it cannot construct one. "Delete the session" by voice asks for spoken confirmation.
- **No long-lived secrets in the client.** The browser holds a session token only; provider API keys live in the agent's config file on disk with `0600` permissions and are never sent to the client or logged.
- **Bound to localhost by default.** The server listens on `127.0.0.1` unless the config sets a bind address explicitly, and refuses to bind to `0.0.0.0` without a token configured. Remote access goes through \`--tunnel\`, Tailscale, or an explicit LAN bind, each described under Configuration; the tunnel route turns token auth, rate limiting, and idle disconnect from recommendations into requirements.
- **Transcript redaction.** Lines matching common secret patterns (API keys, bearer tokens, `AWS_SECRET`, private key headers) are masked in the transcript and never sent to the normalizer or TTS. The raw pane is still visible in the terminal; Zordon just does not repeat it.

The earlier prototype's practice of starting every session with all permissions bypassed is explicitly rejected. It was a workaround for undetected prompts, and the state machine, prompt detection, and watchdog are the proper fix.

## Web client

The browser is the primary interface. The terminal is where Claude Code happens to run; a user should not need to open it. One page, no framework, no build step.

**Layout**

- Session picker: list of discovered sessions with directory, last active, and attached state; new session in a chosen directory; resume.
- Call controls: one large talk button (tap to start a continuous call, tap to end), mute, and a stop button that sends Escape to the pane to interrupt Claude Code itself, distinct from barge-in which only stops playback.
- Transcript pane: a single scrolling feed where each spoken sentence is a row, and tapping a row expands the raw terminal lines it was derived from. Permission prompts and plan approvals render as cards with explicit buttons as well as accepting voice. Background sessions' events appear as collapsed notices.
- Text input: a box that goes through the same router as speech, with Enter to send. Used for paths, pasted errors, and anything speech mangles.
- File drop: drag-and-drop or a mobile camera button. The file is written to `.zordon/uploads/` in the project directory and its path is injected into the next message to Claude Code.
- Settings drawer: verbosity level, tool-chatter toggle, STT and TTS provider selection, voice.

**Protocol.** One WebSocket per client. Messages are JSON with a `type` field: `audio` (base64 PCM, client to agent), `speech` (audio chunk, agent to client), `transcript` (one row), `state` (session state change), `prompt` (a permission or plan card), `command` (client to agent), `error`. Sequence numbers on `speech` chunks let the client discard anything from an interrupted sentence. The schema lives in `protocol.md` and the agent validates every inbound message against it.

**Mobile.** The page must work in Safari on iOS and Chrome on Android over a Tailscale or LAN connection. iOS requires a user gesture before audio playback, so the talk button is also what unlocks the audio context. The page handles backgrounding by pausing capture and resuming on return, without dropping the WebSocket.

## Configuration, auth and deployment

Zordon installs with `pipx install zordon` and runs with `zordon serve`. First run writes `~/.zordon/config.toml` with defaults and prints a one-time token.

```toml
[server]
bind = "127.0.0.1"
port = 8765
token = "generated-on-first-run"

[providers]
stt = "faster-whisper"      # or openai, groq
tts = "kokoro"              # or elevenlabs, openai
normalizer = "anthropic"    # claude-haiku
router = "jev"              # or anthropic

[providers.keys]
anthropic = ""
openai = ""
elevenlabs = ""
typesafe = ""

[voice]
verbosity = "minimal"
tool_chatter = false
idle_watchdog_seconds = 20
router_confidence = 0.85
```

Auth is the single token: the browser presents it once, gets a session cookie, and every WebSocket upgrade checks it. There is no user table. The session store keeps a `user` column from day one, set to `"local"`, so multi-user can be added without a migration.

Requirements: Python 3.12, tmux, Claude Code already installed and logged in. Local STT and TTS pull their models on first use into `~/.zordon/models/`. A `zordon doctor` command checks each dependency and each configured provider and reports what is missing.

**Remote access.** Reaching Zordon from a phone outside the LAN is part of the MVP, because the product is pointless if it only works at the desk. Three routes, in the order the README presents them:

| Route | Command | Who it is for | Cost |
| --- | --- | --- | --- |
| Public tunnel | `zordon serve --tunnel` | Anyone; no network knowledge needed | Free, URL changes each run |
| Tailscale | `zordon serve --bind tailscale` | Users who already have it | Free, stable address |
| LAN | `zordon serve --bind 0.0.0.0` | Same Wi-Fi only | Free, local IP |

`--tunnel` spawns a cloudflared quick tunnel as a child process, waits for it to print its `trycloudflare.com` URL, and shows that URL as text and as a QR code in the terminal and on the client's session picker. cloudflared is the default because it needs no account, no domain, and no port forwarding; `--tunnel ngrok` is a config option for users who already have it. Zordon downloads the cloudflared binary on first use into `~/.zordon/bin/` and `zordon doctor` verifies it. The tunnel connects to `127.0.0.1`, so the server's bind address does not change.

A public URL changes the threat model, so `--tunnel` enforces three things the local modes only recommend: the token is required, failed token attempts are rate-limited to 5 per minute per IP and logged, and the WebSocket drops after 30 minutes idle. Quick-tunnel URLs are random and ephemeral, which is acceptable for the MVP; a static URL needs a Cloudflare named tunnel on the user's own domain and is a README guide, not code.

The session token is shown once at first run and again with `zordon token show`, so a user can type it on the phone after scanning the QR code.

## MVP scope and build order

The MVP is done when a user can resume a Claude Code session from a phone browser, give it a task by voice, be told when it is waiting on a permission, answer yes or no, interrupt it mid-sentence, and ask what it just changed without touching Claude Code's context. Everything else waits.

Build in this order, each step shippable and tested on its own before the next starts:

1. **Session core.** tmux discovery, resume, capture-pane diffing, state machine, prompt detection with the regex file and the idle watchdog. Text-only CLI client for testing. No audio yet.
2. **Output pipeline.** Pre-pass with its test suite, verbosity filter, sentence buffer, normalizer with the 50-sample eval set, TTS provider interface with Kokoro local. Still text in, but speech out.
3. **Web client, text mode.** FastAPI, WebSocket protocol, session picker, transcript pane with raw-line expansion, text input, permission cards. Token auth.
4. **Audio in.** Browser capture, VAD, STT provider interface with faster-whisper, barge-in with the kill switch and echo guard. Measure interrupt latency.
5. **Router.** Jev integration, the 60-utterance eval set, transcript queries, shim commands, strict yes/no in permission states. Haiku fallback router.
6. **Polish for release.** `zordon doctor`, \`--tunnel\` with QR code, README with the three phone-access routes, provider docs, license, a two-minute demo video.

Order matters: the session core is the part most likely to have surprises (tmux behavior, prompt formats), so it goes first with no audio complexity in the way, and the router goes last because every destination it routes to has to exist before it can be tested.

**Done means**

- Interrupt latency under 150 ms measured from VAD onset to silence, on the reference laptop
- Pre-pass test suite covers code blocks, diffs, paths, ANSI, spinners, and Caveman-style shorthand
- Normalizer eval passes on all 50 samples
- Router eval at 95% or better with zero transcript-query examples sent to Claude Code
- A permission prompt left unanswered is announced within 20 seconds in every test scenario
- Fresh install on a second machine works from the README alone, and \`zordon serve --tunnel\` reaches the client from a phone on cellular within two minutes of install

## Deferred and open questions

These are intentionally out of the MVP. Each has a reason and a note on what the MVP does to avoid closing the door.

| Deferred | Why not now | What the MVP keeps open |
| --- | --- | --- |
| Hosted relay (paid tier) | A separate product with its own security model; the local build must prove the pipeline first | Transport thread is the only component that knows about the WebSocket, so a relay client is a second transport |
| Multi-user on one host | Not real isolation without per-user OS accounts or containers; a user column without enforcement is theater | `user` column in the session store, token auth that can become per-user |
| Cross-session announcements | Needs stable multi-session attachment first | Unfocused sessions already capture output |
| Ambient awareness (loop and retry detection) | A heuristic layer that needs real usage data to tune | Transcript stores raw lines with timestamps, which is the input it needs |
| Plan-mode spoken summaries | Depends on the normalizer being trusted with longer input | AwaitingPlanApproval state exists; MVP reads the plan's first line and the step count |
| Container-per-session isolation | Heavy for a self-hosted tool | Session thread shells out to tmux through one function, so a container backend is a swap |

**Open questions for Claude Code to resolve against the actual codebase**

These are decisions this document deliberately does not make. Each should be answered with a short note in `docs/decisions/` once the relevant code exists.

- [ ] Does `capture-pane` diffing at 100 ms hold up when Claude Code redraws its TUI (spinners, progress bars), or does it need a settle delay before diffing? Measure with a real session before building the state machine on it.
- [ ] What does Claude Code's session store under `~/.claude/projects/` actually contain in the installed version, and is the session id stable enough to key on? Check before writing the picker.
- [ ] Which Claude Code output format is cleaner to parse: the default TUI, `--output-format stream-json` in a non-interactive run, or print mode? The design assumes the TUI because that is what resume gives, but if stream-json can be used for the output side while keystrokes still go to the pane, the pre-pass gets much simpler.
- [ ] Is Silero VAD's latency on the target machines inside the 150 ms budget once browser capture and WebSocket framing are added, or does VAD need to run in the browser with the ONNX runtime?
- [ ] Does Jev's Choice primitive accept enough instruction context to distinguish "what did you change" (transcript) from "change what you did" (Claude Code)? If not, the fallback router becomes the default and Jev handles only the yes/no gate.
- [ ] Kokoro's streaming granularity: does it emit audio per sentence or per clause, and is first-audio latency acceptable, or should the MVP default to a cloud TTS and make Kokoro the privacy option?
- [ ] Package layout: a single `zordon` package with subpackages per thread, or separate `zordon-core` and `zordon-web`? Decide after step 2 of the build order when the shape of the code is visible.
