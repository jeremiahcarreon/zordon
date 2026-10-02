# 0003: Prose from the session jsonl, prompts and state from the pane

**Status:** accepted, 2026-10-02

The design asked which Claude Code output is cleanest to parse: the rendered
TUI, `--output-format stream-json`, or print mode.

Observed on Claude Code 2.1.287: the session file
`~/.claude/projects/<encoded cwd>/<session id>.jsonl` receives one `assistant`
record per completed content block (`thinking`, `text`, `tool_use`), with an
ISO timestamp and `apiBlockIndex`, within about half a second of the block
finishing. `user` records carry tool results (`toolUseResult`). `text` blocks
are plain markdown, not terminal rendering.

So:

* **Prose, tool calls and tool outcomes** come from tailing the jsonl. No
  box-drawing, no wrapping, no spinner frames, and tool calls arrive as
  `{name, input}` instead of as rendered lines.
* **Permission prompts, plan approvals, AskUserQuestion, the trust dialog and
  idle/working state** come from `tmux capture-pane`, because they are UI and
  never appear in the jsonl.
* `--output-format stream-json` is print-mode only and is not available while a
  user also types into the same interactive session, so it is not used.

`config.output.source = "pane"` forces the pure pane path (the design's original
plan) and stays fully implemented; the pre-pass handles both inputs.
