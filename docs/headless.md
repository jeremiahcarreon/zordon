# Headless Claude Code

A project can run its agent **headless**: no terminal, no tmux pane. Claude Code
runs as a long-lived `claude -p` process that Zordon talks to over JSON, and
everything, prose, tool calls, permissions and questions, goes through Zordon.
Pick it with the "Run without a terminal" switch when starting a project
(Claude Code only). The default stays the terminal pane, which you can look at.

## How it runs

```
claude -p --input-format stream-json --output-format stream-json --verbose \
  --permission-prompt-tool mcp__zordon__permission --mcp-config ~/.zordon/hooks/<id>.mcp.json --strict-mcp-config \
  --session-id <id>            # or --resume <id> when the project is continued
  [--settings ~/.zordon/hooks/<id>.json]   # Stop/UserPromptSubmit signals and the edit-scope hook
  [--permission-mode <mode>] [--append-system-prompt <voice-mode text>]
```

* Each thing you say is one JSON line on the process's stdin:
  `{"type":"user","message":{"role":"user","content":"..."}}`. Deferred submit
  works the same way as in a pane: phrases are collected and sent after the
  quiet, on "go ahead", or at once from the page.
* Every event comes back as one JSON line on stdout. `assistant` records carry
  `text`, `tool_use` and `thinking` blocks; `user` records carry `tool_result`
  blocks; `result` ends the turn. They have the same shapes as the session jsonl,
  so the same parser feeds the speech pipeline. `rate_limit_event` and the other
  `system` subtypes are ignored.
* "Stop" sends the process SIGINT, which aborts the current turn.
* Pause keeps the process running. Closing the project ends it; continuing the
  project starts a new process with `--resume <id>`, so the conversation goes on.
* The process's stderr goes to `~/.zordon/headless/<id>.log`.

## Permissions

`--print` mode has no dialog to draw. `--permission-prompt-tool
mcp__zordon__permission` makes Claude Code ask an MCP tool instead, for every
tool use that needs approval and for the question tool. The tool is Zordon
itself: `zordon mcp-permission`, launched by Claude Code per the `--mcp-config`
file, speaking MCP over stdio (newline-delimited JSON-RPC 2.0, protocol
2024-11-05, one tool `permission`).

Each call is forwarded to the running server as a `PermissionRequest`-shaped
payload on `POST /hooks/permission`, with the session's hook secret, so the same
code path as the terminal hook (decision 0019) speaks it and waits for your
answer. The hook decision is translated into the SDK's `PermissionResult`:
`{"behavior":"allow"}` (with `updatedInput` for the question tool's answers) or
`{"behavior":"deny","message":"..."}`. No answer (timeout, unknown session,
Zordon unreachable) is a deny with a message, never an allow.

Verified against Claude Code 2.1.288: the MCP server shows as `connected` in the
`init` record, the tool is called with `{tool_name, input, tool_use_id}`, and a
deny with a message is honoured and reported by Claude. In the live round trip a
headless project's Write prompt was spoken by Zordon, approved by the usual API,
and the file was created.

## What differs from the terminal

* Nothing to glance at. The page's transcript is the only view.
* No trust dialog, no bypass warning dialog, no theme/login screens: those are
  interactive-only. Sign in once in a terminal first.
* The plan tool: in `--print` mode plan approval goes through the permission
  tool like any other tool use, so "go ahead" answers it there (not yet
  exercised live).
* Claude Code's own `--permission-mode` still applies; `auto` and the
  per-project "Never ask" choice work the same way.
