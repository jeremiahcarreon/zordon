# WebSocket protocol

One WebSocket per client at `/ws`. Every message is a JSON object with a
`type` field. The agent validates every inbound message against
`zordon/transport/protocol.py` and closes the socket with code 1008 after
repeated invalid messages. Protocol version: **1** (sent in `hello`).

Authentication: the browser POSTs the token to `/auth` once and receives an
HttpOnly session cookie; the WebSocket upgrade is refused (HTTP 403) without it.

## Client to agent

| type | fields | meaning |
| --- | --- | --- |
| `audio` | `pcm` (base64 int16 LE mono 16 kHz, one 20 ms frame = 640 bytes), `seq` | Microphone frames. Only sent while a call is active. |
| `text` | `text` | Typed input. Routed exactly like speech. Control, zero-width and bidi format characters are stripped (also from `send_text`, `plan_revise` and string `answer` options). |
| `command` | `name`, `args` | One of the closed command set below. |
| `call` | `action`: `start` / `end` / `pause` / `resume` | Talk button and backgrounding. |
| `flush_ack` | `generation` | Client confirms it dropped audio older than `generation`. |
| `ping` | `ts` | Keepalive; agent answers `pong`. Does not count as activity for the idle disconnect. |

### Commands

`list_sessions`, `focus {session_id}`, `start {directory, permission_mode?, agent?}`,
`resume {session_id, permission_mode?, agent?}`, `attach {target, agent?}` (follow an
existing tmux pane, `target` like `session:window.pane`; `agent` is an adapter key from
`hello.agents`, default `generic`), `detach {session_id}`, `delete {session_id, confirm}`,
`send_text {text}` (bypasses the router; used by the permission card buttons and
the raw send button), `approve`, `deny`, `plan_approve`, `plan_revise {text}`,
`plan_deny`, `answer {option}`, `stop` (sends Escape to the pane), `mute`,
`unmute`, `set_verbosity {level}`, `set_tool_chatter {enabled}`,
`set_permission_mode {mode}` (never `bypassPermissions`), `set_provider {kind, name}`
(`kind` is `stt`, `tts`, `normalizer`, `router`, or `voice` for the TTS voice),
`repeat`, `status`, `upload {name, size}` (the bytes go to `POST /upload`).

Projects (decision 0018), the non-technical surface over sessions: `list_projects`;
`browse {path?, hidden?}` (one level of folders, never outside the user's home);
`create_project {parent, name, agent?, permission_mode?, scope_edits?, talk_first?, runner?, existing?}`
(makes `parent/name`, or with `existing: true` takes `parent` itself as the folder;
`permission_mode` may be `bypassPermissions` here and only here; `scope_edits` and
`talk_first` default to true; `runner` is `terminal` (a tmux pane, the default) or
`headless` (Claude Code as a `claude -p` process with no terminal, decision 0020)); `open_project {project_id}` (reconnect to its pane, else resume its
last conversation, else start fresh in its folder; focuses it); `admin` (focus nothing,
every pane keeps running); `forget_project {project_id, confirm}` (drops the record,
touches no files). Each answers with `sessions` then `projects`.

The draft box: `set_draft {text}` replaces what is waiting to be sent (an empty text clears),
`send_draft {text}` replaces it and sends. `set_speed {speed}` sets the speech rate (0.5 to 2.0, saved to config); `preview_voice {name}` says a short sample in that voice without changing the setting; `hush` stops reading
the current answer and drops the rest without touching the agent. `set_system_prompt {text}` stores the user's own
voice-mode instruction for new sessions (empty resets to Zordon's default); `settings` carries
`system_prompt` and `system_prompt_custom`.

## Agent to client

| type | fields | meaning |
| --- | --- | --- |
| `hello` | `protocol`, `version`, `focused_session`, `verbosity`, `tool_chatter`, `muted`, `providers`, `tts_sample_rate`, `tunnel_url`, `agents`, `default_agent`, `home` | First message after connect. `muted` is the agent's current mute state (voice `mute` or another client may have set it), so a fresh client renders its toggle right before any `settings` arrives. `agents` maps each adapter key (`claude-code`, `codex`, `generic`) to whether it is installed; `default_agent` is `providers.agent` from config. |
| `sessions` | `sessions[]` of `{session_id, directory, title, last_active, attached, running, state, permission_mode, focused, agent, model, effort}` | Session picker contents. `agent` is the adapter key the session runs under; `model` and `effort` are what the agent reports (transcript records and hook payloads). |
| `projects` | `projects[]` of `{id, name, directory, agent, permission_mode, scope_edits, talk_first, runner, running, session_id, focused, state, last_used, exists}`, `focused_project` | Saved projects, most recently used first; sent on connect and after every project command. `running` is true when a session of the project is attached or its last pane is still alive; `exists` false when its folder is gone. |
| `browse` | `path`, `parent` (null at home), `home`, `entries[]` of `{name, path, has_git, project_id}`, `can_create` | One level of the folder picker. |
| `speech` | `sentence_id`, `seq`, `generation`, `sample_rate`, `pcm` (base64 int16 LE mono), `final` | Audio to play. Discard if `generation` is older than the last `flush`. |
| `flush` | `generation`, `sentence_id` (optional) | Barge-in: stop playback now, drop queued audio with an older generation. `sentence_id` is the sentence that was playing, when known; the client marks it and every later unfinished sentence as cut off. The `stop` command also produces one. |
| `transcript` | `row_id`, `session_id`, `kind` (`spoken`/`user`/`notice`/`raw`), `text`, `raw_lines[]`, `ts`, `sentence_id`, `spoken` | One transcript row; tapping shows `raw_lines`. |
| `state` | `session_id`, `state`, `detail`, `ts` | Session state change (`idle`, `working`, `awaiting_permission`, `awaiting_plan_approval`, `awaiting_question`, `stalled`, `detached`). |
| `prompt` | `prompt_id`, `session_id`, `kind` (`permission`/`plan`/`question`/`trust`), `title`, `options[]`, `raw_lines[]`, `cleared` | Render as a card with buttons. `cleared: true` removes it. |
| `settings` | `verbosity`, `tool_chatter`, `muted`, `providers`, `permission_mode`, `launch_mode`, `tts_speed`, `system_prompt`, `system_prompt_custom` | Current settings after any change. `launch_mode` is the configured default for new sessions; `system_prompt` is what new Claude sessions are told. |
| `tunnel` | `url`, `qr_svg` | Public URL when `--tunnel` is active. |
| `update` | `current`, `latest`, `command`, `auto`, `notes_url` | A newer Zordon exists; `auto: true` means it was installed and a restart picks it up. |
| `health` | `status` (`ok`/`warn`/`fail`), `items[]` of `{key, label, status, detail, fix}`, `ts` | Runtime health of every component; sent on connect, on change, and at least every 60 s. Also at `GET /health`. |
| `heard` | `text`, `confidence`, `ts` | What speech recognition made of the last utterance, before routing: the page's live "Heard" line. |
| `draft` | `session_id`, `text`, `state` (`composing`/`sent`/`cleared`), `ts` | What has been said for a session and not sent yet (decision 0019); `composing` carries the whole draft so far. |
| `error` | `message`, `code` | Something the user should see. `code` is `timeout` when a command did not finish within 12 s (it may still complete). |
| `pong` | `ts` | Reply to `ping`. |

## Audio framing

* Inbound: 16 kHz, mono, int16, 20 ms frames (320 samples, 640 bytes). The
  browser worklet downsamples from the device rate.
* Outbound: the TTS provider's native rate (Kokoro: 24 kHz), int16, mono, in
  chunks of at most 4096 samples. `final: true` marks the last chunk of a
  sentence so the client can mark it as spoken.

## Sequence numbers and barge-in

`seq` increases per chunk within a connection. `generation` increases each time
the agent detects barge-in. The client keeps the highest `generation` it has
seen in a `flush` and silently drops `speech` chunks with a lower value, so
chunks already in flight over the network never play.
