# Prompt detection: versioning and fixtures

Zordon recognises Claude Code's permission prompts, plan approvals, questions, the
trust dialog, the spinner and the idle input box by matching rendered terminal text
against regexes in `zordon/session/prompts.py`. Claude Code's interface changes
between releases, so the patterns are tied to the version they were verified
against and tested against real captures. This page says how that works and how to
update it when a release changes the screen.

## `PROMPTS_VERSION`

```python
# zordon/session/prompts.py
PROMPTS_VERSION = "claude-code-2.1.x"
```

The value names the Claude Code release line the regexes were captured from. It is
shown by `zordon doctor` next to the installed `claude --version` and logged at
startup; a mismatch is a warning, not an error, because most releases do not change
the prompt text. When the patterns are re-verified against a new release the
constant is bumped, the fixtures are re-captured, and `eval/fixtures/pane/PROVENANCE.md`
records the version and date.

The same module holds, for the same reason, the exact strings the state machine
relies on: the completion line `✻ <Verb> for <N>s · done <h>:<mm> <AM|PM>`, the
interruption line `⎿  Interrupted · What should Claude do instead?`, the menu
footer `Esc to cancel · Tab to amend`, the plan question `Claude has written up a
plan and is ready to execute. Would you like to proceed?`, and the trust option
`Yes, I trust this folder`. Decision 0007 lists the prompt layouts; decision 0008
lists the spinner format and the screen details (alternate screen, no-break space in
the input box, blinking tool bullet).

Everything else that parses Claude Code's output is versioned the same way:
`zordon/session/jsonl.py` carries the store format version for the session jsonl
(decision 0002).

## Fixtures

`eval/fixtures/pane/` holds real `tmux capture-pane` output, never hand-written.
Each state has a plain `.txt` (from `capture-pane -p -J`) and an `.ansi.txt` (from
`-p -J -e`, raw escape bytes kept). Frame sequences (`spinner_frames.txt`,
`streaming_frames.txt`, `spinner_frames_plan.txt`) hold consecutive distinct 100 ms
polls separated by `=====FRAME N t=<ms>=====` headers. `PROVENANCE.md` lists every
file, the version and pane size it came from, and the masking applied. Do not run a
formatter or trailing-whitespace cleaner on this directory.

## Re-capturing after a Claude Code release

This is a manual procedure. It runs the real `claude` binary, so it is never done
from the test suite. It needs tmux, a logged-in Claude Code, and a throwaway project
directory (the probe creates sessions in Claude Code's store and may create files
if you approve a prompt).

1. **Use a private tmux server** so nothing touches your own sessions, with the
   same pane size as the existing fixtures:

   ```
   tmux -L zordon-probe new-session -d -s zordon-probe -x 160 -y 45 -c /path/to/throwaway/dir
   tmux -L zordon-probe resize-window -t zordon-probe -x 160 -y 45
   ```

   The second command matters: with `window-size latest` tmux may open the pane at
   another size (169x58 was observed) and the captures will not line up.

2. **Launch Claude Code in the pane**, with the nested-session guard variables
   unset if you are running this from inside another Claude Code session:

   ```
   tmux -L zordon-probe send-keys -t zordon-probe -l 'env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude'
   tmux -L zordon-probe send-keys -t zordon-probe Enter
   ```

   Never pass `--dangerously-skip-permissions` or `--permission-mode
   bypassPermissions`; the point is to capture the prompts.

3. **Trust dialog.** On the first launch in a directory the normal screen shows
   `Accessing workspace: ...` with `❯ No, exit` highlighted. **Do not press Enter
   blindly: Enter on the default exits Claude Code.** Capture it first
   (`trust_dialog`), then press Down and capture again (`trust_dialog_yes_selected`),
   then Enter. Only accept for a directory you are willing to let Claude Code read,
   edit and execute in.

4. **Drive it to each state** and capture. The capture command, exactly:

   ```
   tmux -L zordon-probe capture-pane -t zordon-probe -p -J    > eval/fixtures/pane/<name>.txt
   tmux -L zordon-probe capture-pane -t zordon-probe -p -J -e > eval/fixtures/pane/<name>.ansi.txt
   ```

   `-p` prints to stdout, `-J` joins wrapped lines, `-e` keeps escape sequences.
   `-S -` adds nothing: the interface runs on the alternate screen, which has no
   history, so a capture is always exactly the visible lines. Capture a prompt as
   soon as it appears; long content scrolls off the top within seconds.

   Prompts to collect, and how they were produced last time: a shell permission
   (`touch <file> && echo done`), a file write (ask it to create a small file), an
   `AskUserQuestion` (ask it to ask you a two-option question), a plan approval
   (`--permission-mode plan`, ask for a plan), each denial rendering (`No` on the
   menu; Escape on a write prompt; Escape mid-generation), an idle screen, a
   working screen with and without the spinner, and the exit screen (`/exit`).
   Text goes in with `send-keys -l '<text>'` followed by a separate
   `send-keys Enter`; menu navigation with `send-keys Down` / `Enter` / `Escape`.

   For frame sequences, poll `capture-pane -p -J` every 100 ms, keep only frames
   that differ from the previous one, and write them with the
   `=====FRAME N t=<ms>=====` separator.

5. **Mask before committing.** Replace the model name in the banner and upsell
   lines with `Model 0.0` / `Model 0.1`; replace the session id inside the `/rc`
   hyperlink (`session_01` followed by 22 characters) with
   `session_01MASKEDMASKEDMASKEDMAS` in the `.ansi.txt` files; grep every file for
   `sk-`, `ghp_`, `AKIA`, `xox`, `Bearer`, `PRIVATE KEY`, `api_key=`, `token=`,
   `password=` and JWT shapes and expect zero hits. Your user name in paths is your
   call. Your own status line (the bottom rows) is machine-specific; leave it, the
   tests ignore those rows.

6. **Clean up.** `tmux -L zordon-probe kill-server`. Delete any file you approved.
   The probe's sessions remain in `~/.claude/projects/<encoded dir>/`; delete the
   jsonl files if you do not want them in your picker.

7. **Record it.** Add each new file to the table in `PROVENANCE.md` with the Claude
   Code version, then bump `PROMPTS_VERSION`.

## Adding a fixture and its test

A new prompt kind, or a changed layout, needs three things:

1. The fixture pair in `eval/fixtures/pane/` and a `PROVENANCE.md` row.
2. A regex change in `zordon/session/prompts.py`, kept to the exact strings seen.
   Match option labels, not positions: option counts vary. Mark any option that
   widens permissions (`always allow`, `switch to auto mode`, `switch to accept
   edits`) as `unsafe`; `Yes` and `No` are the only labels `approve()` and `deny()`
   may select.
3. A test in `tests/test_prompts.py` that loads the fixture through the
   `pane_fixture` helper from `tests/conftest.py` and asserts the classification:

   ```python
   def test_write_permission(pane_fixture):
       lines = pane_fixture("write_permission.txt").splitlines()
       m = detect_prompt(lines)
       assert m is not None and m.kind is PromptKind.PERMISSION
       assert m.options[0] == "Yes" and m.options[-1] == "No"
       assert m.title == "Do you want to create probe.txt?"
   ```

   Also assert the negatives: `detect_prompt` returns `None` on `idle.txt`,
   `prose_output.txt` and every frame of `spinner_frames.txt`; `is_working` is true
   on `working_no_spinner.txt`; `is_idle_prompt` is false on every prompt fixture.
   Run every fixture through every classifier: the cost of a false positive (a
   prompt card that is not a prompt) is a wrong keystroke.

Keep fixtures byte-exact. If a test needs a variation (a different file name, a
fifth option), make it a new capture, not an edited copy.
