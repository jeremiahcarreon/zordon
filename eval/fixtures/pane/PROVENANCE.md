# Pane fixtures

Real `tmux capture-pane` output from the agent CLI's terminal UI, used by the
prompt-detection and pre-pass tests. Nothing here is hand-written.

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
