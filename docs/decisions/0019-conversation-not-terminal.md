# 0019: Talk to Claude Code through its hooks, not its screen

**Status:** accepted, 2026-10-04

## Context

In use, four things made the voice loop feel broken. Speech was cut off
mid-thought: the voice activity detector ended an utterance after 700 ms of
silence and Zordon submitted it at once, while a person pauses for one to
three seconds while thinking. Prompts Zordon could not read: permission
dialogs, the question tool's menu and the plan menu were regexes over
`tmux capture-pane`, and a menu with wrapped option descriptions produced
"Claude needs your permission but I can't read the prompt". Answers were slow
and long: Claude writes for a terminal (lists, paths, code) and Zordon then
rewrote that with a small model and read it at 1.0x. And there was no back
and forth: nothing told Claude it was in a conversation, so it executed.

The premise "Claude Code is a turn-by-turn terminal program and a thin layer
over it cannot work" is half right. The terminal is a poor API. But Claude
Code exposes three real ones: hooks, the session transcript, and a headless
JSON mode. Zordon used the first lightly and scraped the screen for the rest.

## What was verified (Claude Code 2.1.288, live in the test container)

* A `PermissionRequest` hook runs *before* any dialog is drawn, receives
  `tool_name`, `tool_input` (for Bash: the command and Claude's one-line
  description), `permission_mode`, `session_id` and `cwd`, and Claude Code
  waits for its stdout. `{"hookSpecificOutput": {"hookEventName":
  "PermissionRequest", "decision": {"behavior": "deny", "message": ...}}}` is
  honoured: the pane shows "Denied by PermissionRequest hook" and Claude reads
  the message. With no decision (`{}`), Claude Code draws its dialog as usual.
* The question tool (`AskUserQuestion`) goes through the same hook. A decision
  of `allow` with `updatedInput: {questions, answers: {<question>: <label>}}`
  answers it: the pane shows "User answered Claude's questions: ... → SQLite"
  and no menu is drawn.
* `--append-system-prompt` is accepted by the interactive CLI.
* `Ctrl-U` clears the input box; typed text stays in the box until Enter.
* The question tool's full `tool_input` also appears in the session jsonl.

## Decision

**1. The hook owns prompts.** Every Zordon-launched session registers a
synchronous `PermissionRequest` hook (timeout 900 s) that POSTs to
`/hooks/permission` and prints Zordon's answer. The manager turns the payload
into an ordinary `PromptMatch` (`extra.source == "hook"`): permission, question
(one question at a time when the tool sends several) or plan. The user's
approve, deny, option or plan feedback becomes the decision: `allow`,
`deny` with "The user said no." or the feedback, or `allow` with the answers.
Nothing in Zordon ever decides by itself; the hook times out to `{}` and
Claude Code draws its own dialog, which the screen reader of 0007 still
handles. The hook handler also prints `{}` when Zordon is unreachable.
Decision 0009's refusal of this hook is superseded by that rule.

**2. Deferred submit.** Speech is typed into Claude's input box as each
phrase is transcribed, without Enter. Enter follows a configurable quiet
(`voice.submit_quiet_ms`, 2500) with no further speech, or "go ahead" /
"send it", or a tap. "Scratch that" sends Ctrl-U. Transcription latency is
unchanged; only submission waits, so a pause to think no longer ends the turn.

**3. Claude is shaped for voice at the source.** Launched sessions get
`--append-system-prompt` with the voice-mode instruction: two or three plain
spoken sentences, no markdown or paths unless asked, one clarifying question at
a time through the question tool, say the plan in a sentence and wait for "go
ahead" before changing anything (the talk-first paragraph, on by default per
project). Conversational text skips the normalizer; the default TTS speed is
1.15.

**4. Headless is the next step, not a prerequisite.** With the hook owning
prompts and the jsonl owning prose, the terminal is left with typing and
state. A headless adapter (`claude -p` with stream-json and Zordon as the
permission tool) removes the terminal entirely and is tracked separately.

## Consequences

* Prompts arrive complete (every option, every description, the command's
  description) and are spoken before anything paints. The regex path is a
  fallback, not the product.
* `permission_mode` is known from the hook payload for every prompt.
* The user can keep talking across pauses. The cost is a longer wait after the
  last word; "go ahead" removes it.
* The hook holds one of the server's executor threads per pending prompt.
  One prompt per session at a time; a second request while one waits gets no
  opinion and falls back to the dialog.

## Open

* The plan tool through the hook was not exercised live; its payload shape is
  taken from the SDK (`tool_input.plan`). Allowing it is expected to leave plan
  mode; which mode follows is to be observed.
* Multi-select questions are answered with one label.
* The screen-reader fixtures for wrapped menus are still wanted for the
  fallback path.
