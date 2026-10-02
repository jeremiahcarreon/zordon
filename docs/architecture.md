# Architecture

This is the implementation map for the design in `Zordon Technical Design.md`.
It says which module owns what, which thread runs it, and the function
signatures modules call across boundaries. The design doc says *why*; this file
says *where*.

## Package layout

One package, `zordon`, with a subpackage per concern. (Decision record:
`decisions/0001-single-package.md`.)

```
zordon/
  cli.py                 zordon serve | doctor | token show|rotate | sessions
  app.py                 Agent: builds config, bus, threads; start/stop
  config.py              config.toml dataclasses, first-run token, validation
  paths.py               ZORDON_HOME / CLAUDE_CONFIG_DIR resolution
  bus.py                 events + queues shared between threads
  providers.py           STTProvider / TTSProvider / Normalizer / Router protocols
  doctor.py              dependency + provider checks, model/binary download

  session/               owner: SessionThread
    tmux.py              the ONLY module that shells out to tmux
    discovery.py         ~/.claude/projects + ~/.claude/sessions -> SessionInfo
    jsonl.py             tail a session's jsonl for assistant blocks
    screen.py            pure: diff two pane captures -> new stable lines
    prompts.py           pure: regexes for permission / plan / question / trust prompts
    state.py             pure: state machine transitions
    permissions.py       read Claude Code settings -> PermissionSummary; mode switching
    manager.py           SessionThread + Session objects; watchdog; keystroke injection

  output/                owner: PipelineThread
    prepass.py           pure: classify + collapse lines (most tests live here)
    acronyms.py          expansion table
    verbosity.py         pure: keep/drop by level + tool_chatter
    sentences.py         pure: sentence boundary buffer
    normalizer/          Normalizer implementations (anthropic, passthrough) + prompt.md
    tts/                 TTSProvider implementations (kokoro, openai, elevenlabs)
    pipeline.py          PipelineThread: pane_lines -> sentences -> playback

  speech/                owner: AudioThread
    vad.py               Silero VAD on onnxruntime + onset/offset gate + echo guard
    stt/                 STTProvider implementations (faster_whisper, openai, groq)
    audio_thread.py      inbound frames -> VAD -> utterance -> STT -> utterances; barge-in kill switch

  routing/               runs on the Dispatcher thread
    base.py              re-exports provider types; shared helpers
    keyword.py           deterministic router (no network): shim commands, yes/no
    typesafe.py          Jev router (TypeSafe System One)
    anthropic.py         Haiku fallback router (structured output)
    commands.py          closed set of shim commands + their handlers' signatures
    dispatcher.py        DispatcherThread: utterances -> pane / transcript answer / command

  transcript/
    store.py             SQLite: raw lines, spoken sentences, links, user column
    redaction.py         secret patterns -> masked text

  transport/             owner: Transport (uvicorn asyncio loop)
    protocol.py          pydantic message models (the wire schema)
    server.py            FastAPI app factory, token auth, cookie, rate limit, static
    ws.py                per-client WebSocket: bus events -> JSON, JSON -> bus
    tunnel.py            cloudflared / ngrok child process, URL extraction
    qr.py                terminal + SVG QR

  web/                   static client (no build step)
    index.html app.js audio.js worklet.js style.css
```

## Threads and ownership

| Thread | Owns | Reads from | Writes to |
| --- | --- | --- | --- |
| SessionThread (`session/manager.py`) | tmux panes, jsonl tails, session state | `bus.keystrokes` (internal), commands from dispatcher | `bus.pane_lines`, `bus.client_events` (StateChanged, PromptDetected, PromptCleared, Notice) |
| PipelineThread (`output/pipeline.py`) | normalizer + TTS providers, sentence buffer | `bus.pane_lines` | `bus.sentences` (for transcript), `bus.playback`, `bus.client_events` (TranscriptRow) |
| AudioThread (`speech/audio_thread.py`) | VAD, STT provider, playback generation | `bus.inbound_audio`, `bus.playback` | `bus.utterances`, `bus.client_events` (SpeechChunk, Flush, TranscriptRow for user speech) |
| DispatcherThread (`routing/dispatcher.py`) | router provider, shim command table | `bus.utterances` | SessionThread methods (thread-safe), `bus.client_events` |
| Transport (asyncio) | WebSocket clients, HTTP | `bus.client_events` (via a drain task) | `bus.inbound_audio`, `bus.utterances` (text input), dispatcher commands |

`Bus.generation` is the barge-in counter. AudioThread bumps it on speech onset
while playback is active, drains `bus.playback`, and publishes `Flush`. Every
`SpeechChunk` carries the generation it was produced under; transport and the
client drop chunks whose generation is older than the latest `Flush`.

## Output source

Prose comes from two places; `config.output.source` picks (`auto` default):

1. **jsonl** (`session/jsonl.py`): Claude Code appends one record per completed
   content block to `~/.claude/projects/<encoded cwd>/<session id>.jsonl`.
   `text` blocks are clean markdown; `tool_use` blocks give the tool name and
   input (so "edited `auth.py`" needs no diff parsing); `user` records with
   `toolUseResult` give tool outcomes. Latency is one content block.
2. **pane** (`session/screen.py`): `tmux capture-pane -p -J` every 100 ms,
   diffed for new stable lines. Always running, because this is the only place
   permission prompts, plan approvals and the input box are visible.

In `auto`, prose is taken from jsonl when the session's file is found and the
pane feed is used only for prompt detection and state; if the jsonl stops
advancing while the pane shows output, the pipeline switches to pane prose for
that turn and raises a Notice. (Decision record: `decisions/0003-output-source.md`.)

## Cross-module signatures

```python
# session/tmux.py
class Tmux:
    def __init__(self, binary: str = "tmux", socket: str | None = None): ...
    def run(self, *args: str, timeout: float = 2.0) -> str      # the one shell-out
    def list_panes(self) -> list[PaneInfo]                      # PaneInfo(target, session, window, pane_id, pid, cwd, command)
    def pane_exists(self, target: str) -> bool
    def new_session(self, name: str, cwd: str, command: list[str], width=160, height=45) -> str  # returns pane target
    def capture(self, target: str, with_ansi: bool = False, history_lines: int = 0) -> list[str]
    def send_literal(self, target: str, text: str) -> None      # send-keys -l, control chars stripped
    def send_enter(self, target: str) -> None                   # separate call, by design
    def send_key(self, target: str, key: str) -> None           # Escape, Up, Down, Enter, BTab, C-c ... from an allowlist
    def kill_session(self, name: str) -> None
    def pane_pid(self, target: str) -> int | None

# session/discovery.py
@dataclass class SessionInfo: session_id, directory, title, first_prompt, last_active, version, git_branch, permission_mode, running_pid, tmux_target, jsonl_path
def encode_project_dir(cwd: str) -> str
def list_sessions(claude_home: Path, tmux: Tmux | None) -> list[SessionInfo]
def find_session(session_id: str, ...) -> SessionInfo | None
def resume_command(session_id: str, settings_path: Path | None = None, permission_mode: str | None = None) -> list[str]   # never contains a bypass flag
def new_session_command(session_id: str, settings_path: Path | None = None, permission_mode: str | None = None) -> list[str]  # session_id is the UUID the new session gets; the pane's cwd is set by tmux
def hook_settings_json(port: int, secret: str) -> str          # Notification/UserPromptSubmit/Stop hooks for --settings
def hook_command(port: int, secret: str) -> str                # the curl line those hooks run
def write_hook_settings(path: Path, port: int, secret: str) -> Path   # 0600 settings file per session

# session/screen.py
@dataclass class ScreenDiff: new_lines: list[str]; live_region: list[str]; changed: bool
def diff_captures(prev: list[str], curr: list[str], live_region_hint: int) -> ScreenDiff
def strip_ansi(s: str) -> str

# session/prompts.py
PROMPTS_VERSION = "claude-code-2.1.287"
@dataclass class PromptMatch: kind: PromptKind; title: str; options: list[str]; raw_lines: list[str]; confidence: float
def detect_prompt(lines: list[str]) -> PromptMatch | None   # last ~25 pane lines
def is_idle_prompt(lines: list[str]) -> bool                  # input box visible, no spinner
def is_working(lines: list[str]) -> bool                      # spinner / "esc to interrupt"
def permission_mode_from_screen(lines: list[str]) -> str | None

# session/state.py
def next_state(current: SessionState, obs: Observation, cfg) -> tuple[SessionState, str]
#   Observation(prompt: PromptMatch|None, idle: bool, working: bool, output_advanced: bool, seconds_since_output: float, pane_alive: bool)

# output/prepass.py
@dataclass class Tagged: kind: LineKind; text: str; spoken: str | None; raw: str; meta: dict
def prepass_line(line: str, ctx: PrepassState) -> list[Tagged]   # may emit 0..n items (code block collapse)
def prepass_markdown(text: str) -> list[Tagged]                   # for jsonl text blocks
def describe_tool_use(name: str, input: dict) -> Tagged           # "edited auth.py" / "running tests"
def expand_acronyms(text: str) -> str
def speak_path(path: str) -> str                                   # "/a/b/auth.py" -> "auth dot p y" style handled by normalizer; here: basename

# output/verbosity.py
def keep(tagged: Tagged, level: str, tool_chatter: bool) -> bool

# output/sentences.py
class SentenceBuffer: push(text) -> list[str]; flush() -> list[str]

# output/pipeline.py
class PipelineThread(threading.Thread): __init__(bus, config, normalizer, tts, transcript_store)

# speech/vad.py
class SileroVAD: __init__(model_path); prob(frame_f32_512) -> float; reset()
class SpeechGate: feed(frame_i16_20ms, playback_active: bool) -> GateEvent   # ONSET / END / NONE, with the utterance pcm on END

# speech/audio_thread.py
class AudioThread(threading.Thread): __init__(bus, config, vad, stt, transcript_store)

# routing/dispatcher.py
class DispatcherThread(threading.Thread): __init__(bus, config, router, session_manager, transcript_store, normalizer)

# transcript/store.py
class TranscriptStore: __init__(db_path); add_raw(session_id, text, ts, source) -> int; add_spoken(...) -> int; link(spoken_id, raw_ids); mark_unspoken(sentence_id); tail(session_id, n) -> list[TranscriptRow]; raw_for(sentence_id) -> list[str]
# transcript/redaction.py
def redact(text: str) -> tuple[str, bool]
def redact_pair(prev: str | None, curr: str) -> tuple[str, bool]   # curr masked knowing the previous pane line (hard-wrapped keys)

# transport/server.py
def create_app(agent) -> FastAPI
# transport/sanitize.py
def sanitize_keystrokes(text: str) -> str    # strips C0/C1 controls and zero-width/bidi format characters before text reaches the manager
# transport/tunnel.py
class Tunnel: start() -> str (url); stop(); provider: cloudflared | ngrok
```

## Safety invariants (tested)

* No code path constructs a `claude` command line containing a permission-bypass flag; `discovery.resume_command` is the only constructor and `tests/test_safety.py` greps the package for the flag names.
* `tmux.send_literal` strips C0 control characters and always uses `-l`; Enter is a separate call.
* `commands.COMMANDS` is the only set the router may select from; `dispatcher` rejects anything else.
* Permission prompts are never filtered by verbosity and are always spoken.
* In `AWAITING_PERMISSION`, only `yes_no()` at >= `voice.yes_no_confidence` moves the session; "always allow" wording is refused by voice.
* Server refuses to start when `bind` is not loopback and no token is set; under `--tunnel` the idle disconnect and rate limit are forced on (the idle timer counts only non-`ping` client messages).
* The tunnel child never sees a provider key: `transport/tunnel.py` starts it with `*_API_KEY`, `*_TOKEN`, `*_SECRET` and provider-prefixed variables removed from the environment.
* `POST /hooks/claude` accepts only direct loopback peers (no proxy headers) and at most 64 KB; `POST /upload` and `/logout` apply the same-origin check the WebSocket uses; uploads never follow a symlinked `.zordon` or `.zordon/uploads`.
* WebSocket command handlers run on a worker thread (`run_in_executor`) with a timeout, so a blocking `SessionControl` call never stalls audio or flushes for any client.
* API keys never appear in logs, in `hello`, in `settings` or in any transcript row.
* Lines matching `transcript/redaction.py` patterns are masked before the normalizer, TTS or the client see them.

## SessionControl (what the dispatcher and transport call on the session manager)

`zordon.session.manager.SessionManager` implements this; `zordon.routing.dispatcher`
and `zordon.transport.ws` only ever see this surface. Every method is thread-safe
and returns quickly; long work happens on the SessionThread.

```python
class SessionControl(Protocol):
    def list_sessions(self) -> list[SessionSummaryLike]           # discovery + live state, for the picker
    def focused(self) -> str | None                                 # focused session id
    def focus(self, session_id: str) -> None
    def state_of(self, session_id: str) -> SessionState
    def current_prompt(self, session_id: str) -> PromptDetected | None
    def start(self, directory: str, permission_mode: str | None = None) -> str   # returns new session id
    def resume(self, session_id: str, permission_mode: str | None = None) -> None
    def detach(self, session_id: str) -> None                       # stop following; pane keeps running
    def delete(self, session_id: str) -> None                       # kill the pane (caller already confirmed)
    def send_text(self, session_id: str, text: str) -> None         # literal keystrokes + separate Enter
    def send_escape(self, session_id: str) -> None                  # "stop": interrupt Claude Code itself
    def approve(self, session_id: str) -> bool                      # permission: plain "Yes" only
    def deny(self, session_id: str) -> bool                         # permission: the "No" option / Escape
    def plan_approve(self, session_id: str) -> bool                 # "Yes, manually approve edits" (never the auto-mode option)
    def plan_revise(self, session_id: str, feedback: str) -> bool   # "Tell Claude what to change" + feedback
    def plan_deny(self, session_id: str) -> bool                    # Escape
    def answer_question(self, session_id: str, option: int | str) -> bool   # AskUserQuestion option (1-based or label)
    def accept_trust(self, session_id: str) -> bool                 # trust dialog: Down + Enter (user confirmed first)
    def decline_trust(self, session_id: str) -> bool
    def set_permission_mode(self, session_id: str, mode: str) -> bool  # default|acceptEdits|plan (voice); auto|dontAsk (tap only); never bypassPermissions
    def permission_summary(self, session_id: str) -> str            # one spoken sentence about the active mode and rules
    def last_pane_lines(self, session_id: str, n: int = 10) -> list[str]
    def hook_event(self, payload: dict) -> None                     # Notification hook from Claude Code (second signal)
```

## AgentAPI (what the transport needs from the agent)

```python
class AgentAPI(Protocol):
    config: Config
    bus: Bus
    sessions: SessionControl
    version: str
    hook_secret: str                                   # per-process secret checked on POST /hooks/claude
    tunnel_url: str | None
    def settings(self) -> dict                         # verbosity, tool_chatter, muted, providers, permission_mode
    def set_verbosity(self, level: str) -> None
    def set_tool_chatter(self, enabled: bool) -> None
    def set_muted(self, muted: bool) -> None
    def set_provider(self, kind: str, name: str) -> None    # stt|tts|normalizer|router
    def set_voice(self, name: str) -> None                   # optional; the transport maps set_provider {kind: voice} to it
    def submit_text(self, text: str, client_id: str) -> None       # typed input -> bus.utterances (router)
    def call_state(self, client_id: str, action: str) -> None      # start/end/pause/resume
    def repeat_last(self) -> None
    def upload_path(self, filename: str) -> Path                   # <focused cwd>/.zordon/uploads/<safe name>
```

## Permission-option safety

Permission menus contain options that widen permissions ("Yes, and always allow…",
"Yes, and switch to auto mode", "Yes, and switch to accept edits"). `prompts.py`
marks those options `unsafe=True`; `manager.approve()` only ever selects an option
whose label is exactly `Yes` (option 1), and `deny()` selects the option labelled
`No` (always the last numbered option) or sends Escape. Plan approval: option 1 is
"Yes, and use auto mode" (unsafe); `plan_approve()` selects "Yes, manually approve
edits". The trust dialog defaults to "No, exit"; `accept_trust()` sends Down then
Enter and is only called after the user tapped or spoke a confirmation.
