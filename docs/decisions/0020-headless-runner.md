# 0020: A headless runner for Claude Code, chosen per project

**Status:** accepted, 2026-10-04

## Context

Decision 0019 moved prompts off the screen and onto Claude Code's hook, and
prose was already read from the session transcript. What the terminal still
did for Zordon was typing and state, and both kept biting in live use: an
Enter swallowed right after a burst of typed text, a dialog painted while the
hook was waiting, a trust screen on a fresh folder, a resumed TUI that was not
ready for keystrokes yet. Each got a workaround. A runner with no terminal at
all has none of them. Claude Code offers one: `claude -p` with stream-json in
and out keeps a conversation open over stdin and stdout, and
`--permission-prompt-tool` names an MCP tool that it calls for every
permission, question and plan instead of drawing anything.

## What was verified (Claude Code 2.1.288, live)

* Flags: `-p --input-format stream-json --output-format stream-json --verbose
  --permission-prompt-tool mcp__zordon__permission --mcp-config <file>
  --strict-mcp-config`, with `--session-id` or `--resume`, `--permission-mode`,
  `--append-system-prompt` and `--settings` as for the pane.
* Events: `system/init` with `session_id`, `cwd`, `mcp_servers[{name,status}]`
  and `permissionMode`; `assistant` records whose `content` blocks are `text`,
  `tool_use` or `thinking`; `user` records with `tool_result`; `result` with
  `subtype`, `is_error`, `session_id`, `permission_denials`. The shapes are the
  session jsonl's, so the existing parser reads them unchanged.
* The MCP tool round trip against the running server: Claude's Write reached
  `zordon mcp-permission`, which POSTed to `/hooks/permission`; the page got the
  prompt card, it was spoken as "Claude Code (headless) wants to create ...",
  `approve` answered it, the file was created.

## Decision

A project has a `runner`: `terminal` (the tmux pane, today's default) or
`headless`. A headless project runs a long-lived `claude -p` process per
session (`zordon/agents/headless.py`): user turns are JSON lines on its stdin;
its stdout feeds the output pipeline through the jsonl parser; `result` ends the
turn; "stop" sends SIGINT. Permissions go through `zordon mcp-permission`, a
newline-delimited JSON-RPC MCP server Zordon ships (no new dependency), which
forwards each request to `/hooks/permission` in the PermissionRequest shape, so
the manager and the user answer it exactly as in 0019. No answer, an unknown
session or an unreachable server is a deny: with no dialog to fall back to,
nothing may run by default. The session id from `system/init` is stored on the
project for `--resume`. The voice-mode system prompt and the edit-scope hook
apply as for the pane; the PermissionRequest hook is not installed (the MCP
tool is the channel).

Terminal stays the default: a person can look at the pane, and the pane path is
where a year of fixtures lives. Headless is the choice for someone who never
wants to see a terminal, and the path that removes the remaining terminal races.

## Consequences

* A headless session has no screen: no trust dialog (`-p` does not ask), no
  swallowed Enter, no painted dialog to reconcile. The transcript on the page is
  the whole view.
* The MCP tool answers only with the user's decision; a timeout is a deny, not a
  dialog. A headless project that expects quick work needs the user present or
  "never ask".
* Two runners share the adapter contract; everything above the adapter is
  unchanged. The generic and Codex adapters are unaffected.

## Open

* The plan tool through the MCP channel was not exercised live; its payload is
  the same shape as the hook's.
* Resumed headless sessions take the project's stored permission mode; a mode
  changed by voice in a previous run is not remembered.
* `--include-partial-messages` could stream sentences before the turn ends; the
  pipeline speaks at `result` today.
