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
| `text` | `text` | Typed input. Routed exactly like speech. |
| `command` | `name`, `args` | One of the closed command set below. |
| `call` | `action`: `start` / `end` / `pause` / `resume` | Talk button and backgrounding. |
| `flush_ack` | `generation` | Client confirms it dropped audio older than `generation`. |
| `ping` | `ts` | Keepalive; agent answers `pong`. |

### Commands

`list_sessions`, `focus {session_id}`, `start {directory, permission_mode?}`,
`resume {session_id}`, `detach {session_id}`, `delete {session_id, confirm}`,
`send_text {text}` (bypasses the router; used by the permission card buttons and
the raw send button), `approve`, `deny`, `plan_approve`, `plan_revise {text}`,
`plan_deny`, `answer {option}`, `stop` (sends Escape to the pane), `mute`,
`unmute`, `set_verbosity {level}`, `set_tool_chatter {enabled}`,
`set_permission_mode {mode}` (never `bypassPermissions`), `set_provider {kind, name}`,
`repeat`, `status`, `upload {name, size}` (the bytes go to `POST /upload`).

## Agent to client

| type | fields | meaning |
| --- | --- | --- |
| `hello` | `protocol`, `version`, `focused_session`, `verbosity`, `tool_chatter`, `providers`, `tts_sample_rate`, `tunnel_url` | First message after connect. |
| `sessions` | `sessions[]` of `{session_id, directory, title, last_active, attached, running, state, permission_mode, focused}` | Session picker contents. |
| `speech` | `sentence_id`, `seq`, `generation`, `sample_rate`, `pcm` (base64 int16 LE mono), `final` | Audio to play. Discard if `generation` is older than the last `flush`. |
| `flush` | `generation` | Barge-in: stop playback now, drop queued audio with an older generation. |
| `transcript` | `row_id`, `session_id`, `kind` (`spoken`/`user`/`notice`/`raw`), `text`, `raw_lines[]`, `ts`, `sentence_id`, `spoken` | One transcript row; tapping shows `raw_lines`. |
| `state` | `session_id`, `state`, `detail`, `ts` | Session state change (`idle`, `working`, `awaiting_permission`, `awaiting_plan_approval`, `awaiting_question`, `stalled`, `detached`). |
| `prompt` | `prompt_id`, `session_id`, `kind` (`permission`/`plan`/`question`/`trust`), `title`, `options[]`, `raw_lines[]`, `cleared` | Render as a card with buttons. `cleared: true` removes it. |
| `settings` | `verbosity`, `tool_chatter`, `muted`, `providers`, `permission_mode` | Current settings after any change. |
| `tunnel` | `url`, `qr_svg` | Public URL when `--tunnel` is active. |
| `error` | `message`, `code` | Something the user should see. |
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
