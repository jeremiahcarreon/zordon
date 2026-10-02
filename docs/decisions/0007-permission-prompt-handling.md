# 0007: Which prompt options voice may select, and the strict yes/no gate

**Status:** accepted, 2026-10-02

## Question

The design says permission prompts are the one place voice input narrows to a strict
yes or no, that "always allow" is never accepted by voice, and that Zordon never
widens Claude Code's permissions. The prompts were captured to see what the options
actually are and which keystrokes select which.

## What was verified

Captured from Claude Code 2.1.287 in a 160x45 tmux pane; fixtures under
`eval/fixtures/pane/`. Five prompt kinds were seen; the Edit-file variant was not
triggered and is unverified.

**Bash permission** (`bash_permission.txt`), question `Do you want to proceed?`:

```
 ❯ 1. Yes
   2. Yes, and always allow access to /tmp/.../research from this project
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No
 Esc to cancel · Tab to amend
```

**File write permission** (`write_permission.txt`), question
`Do you want to create probe.txt?`:

```
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No
 Esc to cancel · Tab to amend
```

Option 1 is `Yes` in both; the middle options widen permissions and their count and
wording vary with the command; `No` is always the **last** numbered option. Pressing
Down three times then Enter selected `4. No` (`bash_permission_no_selected.txt`).
Selecting `No` rendered `⎿  Interrupted · What should Claude do instead?`; Escape on
a write prompt rendered `⎿  User rejected write to probe.txt`.

**Plan approval** (`plan_approval.txt`), question `Claude has written up a plan and
is ready to execute. Would you like to proceed?`:

```
   ❯ 1. Yes, and use auto mode
     2. Yes, manually approve edits
     3. Tell Claude what to change
```

There is no plain `No`; Escape rejects, and the default-highlighted option 1 turns
on auto mode.

**AskUserQuestion** (`ask_user_question.txt`): a `☐ <header>` line, a bold
question, numbered options with descriptions, `Type something.` and `Chat about
this`, footer `Enter to select · ↑/↓ to navigate · Esc to cancel`.

**Trust dialog** (`trust_dialog.txt`), shown on the normal screen before the TUI
starts, only on the first launch in a directory:

```
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
```

The default is `No, exit`; pressing Enter blindly ends the process. Accepting needs
Down then Enter.

**Permission modes.** `--permission-mode` accepts `acceptEdits`, `auto`,
`bypassPermissions`, `manual`, `dontAsk`, `plan`; `default` is also accepted and
is what hooks and the session store write (never `manual`). On this version
`defaultMode` values `auto` and `bypassPermissions` only take effect from user or
managed settings, not from a project's `.claude/settings.local.json`.

**Auto-approval exists already.** `ls -la` ran without a prompt in default mode with
no allow rules; a write-ish command (`touch ... && echo done`) prompted. A built-in
read-only heuristic is the likely reason.

## Decision

* `prompts.py` parses every numbered option and marks as `unsafe` any label that
  is not exactly `Yes` or `No` and matches "always allow", "switch to auto mode",
  "switch to accept edits", "don't ask again" or similar wording.
  `SessionControl.approve()` selects only an option whose label is exactly `Yes`
  (Enter on option 1); `deny()` selects the option labelled `No` (Down to the last
  numbered option, then Enter) or sends Escape. Options are matched by label, not
  position, because the count varies.
* Voice can never pick an `unsafe` option. The dispatcher refuses the phrases in
  `commands.FORBIDDEN_PERMISSION_PHRASES` ("always allow", "yes to all", "skip
  permissions", ...) and says so. The widening options remain available to the user
  in the terminal; Zordon just does not operate them.
* Plan approval: `plan_approve()` selects `Yes, manually approve edits` (option 2),
  never `Yes, and use auto mode`. `plan_revise(feedback)` selects `Tell Claude what
  to change` and sends the feedback as literal text. `plan_deny()` sends Escape.
* Trust dialog: `accept_trust()` sends Down then Enter and is only called after the
  user tapped the card button or spoke a confirmation that passed the gate.
  `decline_trust()` sends Enter on the default. The card says plainly that
  accepting lets Claude Code read, edit and execute files in that directory.
* `AWAITING_PERMISSION` and the trust dialog use the strict gate: an utterance moves
  the session only when `Router.yes_no()` returns `yes` or `no` at
  `voice.yes_no_confidence` (0.95) or higher. Anything else is read back ("I heard:
  ... Yes or no?"). Plan approval accepts approve, revise and deny through the same
  gate shape. AskUserQuestion accepts a 1-based option number or an exact label
  match.
* `set_permission_mode()` by voice is limited to `default`, `acceptEdits` and
  `plan`; `auto` and `dontAsk` are tap-only; `bypassPermissions` is refused
  everywhere and never written to any settings file. The launcher never passes
  `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
  `--permission-mode bypassPermissions`, `--bare` or `--safe-mode`
  (the last two disable the hooks of decision 0009).
* Permission prompts bypass the verbosity filter and are always spoken.

## Open

* The Edit-file prompt (`Edit file` header, `Do you want to make this edit to
  <file>?`) is a guess from the Write variant; a fixture is needed when it is first
  seen. Until then the generic `Do you want to ...?` + `1. Yes` + footer match
  catches it.
* Status-row wording was only observed for `manual` and `plan` modes; the other
  mode names in the regex are guesses.
* The read-only auto-approval heuristic means some actions never prompt; Zordon
  reports what it sees and does not try to second-guess Claude Code.
