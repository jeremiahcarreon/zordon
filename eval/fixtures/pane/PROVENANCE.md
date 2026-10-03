# Pane fixtures

Real `tmux capture-pane` output from the agent CLI's terminal UI, used by the
prompt-detection and pre-pass tests. Nothing here is hand-written: every file
is either a verbatim capture (masked as described below) or a mechanical
mutation of one, listed in the second table.

- Captured 2026-10-01 from CLI version 2.1.287 running inside tmux 3.4,
  pane size 160x45, with `capture-pane -p -J` (plain) and `-p -J -e` (ANSI).
- `*.txt` are plain; `*.ansi.txt` keep the raw escape bytes (SGR, OSC 8
  hyperlinks). Keep them byte-exact; do not run formatters on this directory.
- `spinner_frames.txt`, `spinner_frames_plan.txt`, `streaming_frames.txt`
  hold consecutive distinct 100 ms polls separated by
  `=====FRAME N t=<ms>=====` headers.
- Masking applied before commit: the model name in the two banner/upsell
  lines was replaced with `Model 0.0` / `Model 0.1`, and the remote-session
  URL inside the banner hyperlink was replaced with a placeholder
  `https://example.invalid/session/...` of the same length. Everything else
  is verbatim.
- The two bottom status rows (`[PLUGIN ]`, `⏸ ... mode on ...`) come from the
  capturing machine's own status-line configuration and vary per user.

| File | State shown |
| --- | --- |
| trust_dialog, trust_dialog_yes_selected | first-run folder trust dialog (normal screen) |
| idle, plan_idle | TUI waiting for input (alternate screen) |
| bash_permission, bash_permission_no_selected | shell-command permission prompt |
| write_permission | file-create permission prompt |
| ask_user_question, ask_user_question_answered | AskUserQuestion menu and its result |
| plan_approval | plan-mode approval prompt |
| denial, denial_write_escape, escape_interrupt | the three rejection renderings |
| prose_output | prose + code block + path list answer |
| working_no_spinner | mid-stream output with no spinner line visible |
| literal_sendkeys | `send-keys -l` text shown verbatim in the input box |
| exit | normal screen after `/exit` |

## Real-run captures (2026-10-01, CLI 2.1.287, 160x45, driven by Zordon itself)

Captured during the end-to-end review runs with the same masking as above
(model names in the banner/upsell lines replaced with `Model 0.0` / `Model 0.1`;
no session URL is present in a plain capture). The `[PLUGIN ]` row and the
`⏸ … / ⏵⏵ …` status row are the capturing machine's status line.

| File | State shown |
| --- | --- |
| bash_permission_bare_command | shell-command permission prompt for a short command: the command sits bare between the two dotted rules (no `│` gutter), with the one-line description above them |
| tip_line_tmux | idle after a finished turn; the slot above the input rule shows the right-aligned `tmux detected · scroll with PgUp/PgDn · …` tip instead of the effort hint; status row `⏵⏵ auto mode on (shift+tab to cycle)` |
| tip_line_tmux_focus_events | same, with the `tmux focus-events off · …` tip |
| status_auto_mode | idle with typed-but-unsent text in the input box; status row `⏵⏵ auto mode on (shift+tab to cycle) · ← 3 agents` |
| status_accept_edits | idle; status row `⏵⏵ accept edits on (shift+tab to cycle) · ← 3 agents` (no word "mode") |
| startup_notice_gutter | fresh session before the first prompt: `▎`-guttered "Auto mode is now …" notice box, `1 more notice hidden`, effort hint, ghost text |

## Synthetic variants (mechanical mutations of the captures above)

Produced by the prompt-detection review harness from the real fixtures: lines
inserted above the content, a different clock/duration on the completion row,
a word-wrap simulation at 80 columns, the top of the screen dropped, NBSP
replaced by U+0020, the spinner verb replaced. The wrapped layouts are
simulated (Ink-style word wrap), not captured; everything else is a verbatim
fixture with the one described change.

| File | Derived from | Change |
| --- | --- | --- |
| bash_permission_prose_question_above | bash_permission | Claude's prose `Do you want to proceed?` / `1. Yes` / `2. No` list and its completion row inserted above the real prompt |
| bash_permission_prose_do_you_want_to_above | bash_permission | a standalone prose `Do you want to proceed with …?` line inserted above the real prompt |
| idle_prose_question_list | idle | the same prose question + list on an idle screen (input box visible) |
| done_line_24h | prose_output | completion row clock in 24-hour form (`done 20:33`) |
| done_line_weekday | prose_output | completion row of a turn older than today (`done Tuesday 8:33 PM`) |
| done_line_waiting_for_agents | prose_output | `✻ Waiting for 1 background agent to finish` in place of the completion row |
| done_line_suffix_messages_hidden | prose_output | completion row with ` · 3 messages hidden (/focus to show)` appended |
| plan_approval_wrap_80 | plan_approval | word-wrapped at 80 columns: the plan question and the footer path wrap onto a second line |
| write_permission_question_wrapped | write_permission | a long path makes `Do you want to create …?` wrap, the `?` lands on the next line |
| trust_dialog_rule_offscreen | trust_dialog | top nine lines dropped: the dialog's opening rule has scrolled off, the header remains |
| ask_user_question_rule_offscreen | ask_user_question | everything above the `☐` header dropped, including the opening rule |
| idle_notice_reworded_no_hint | idle | the upsell notice reworded and the effort hint removed |
| idle_nbsp_to_space | idle | every U+00A0 replaced by U+0020 (input box rendered with a plain space) |
| exit_shell_only | exit | Claude Code never started: shell prompt, `No conversation found with session ID`, shell prompt |
| exit_crash_no_resume_line | exit | the `Resume this session with:` lines replaced by a Node error and stack line |
| spinner_verb_accent_frames | streaming_frames | the spinner verb replaced by `Sautéing` in every frame (frames file) |

| onboarding_theme, onboarding_login, onboarding_login_browser | first-run screens of a never-configured Claude Code (normal screen): text-style picker, login-method menu, browser sign-in URL (URL masked) |
