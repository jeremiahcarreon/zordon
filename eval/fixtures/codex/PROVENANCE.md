# Codex fixtures

Screens of the Codex CLI terminal UI (`@openai/codex`, `codex-cli 0.160.0`) used by
`tests/test_agents_codex.py`. Two kinds of file live here and the table says which
is which: **live** captures are verbatim `tmux capture-pane` output, masked as
described below; **synthetic** files were assembled by hand from strings in the
Codex source and its snapshot tests, because no agent turn could run on the
capturing machine (no OpenAI login).

## Live captures (2026-10-02, codex-cli 0.160.0, tmux 3.4, pane 160x45)

Captured with the binary installed into `.scratch/codex/node_modules` by
`npm install @openai/codex`, run inside a private tmux server
(`tmux -L zordon-codex-probe`) with `CODEX_HOME` pointed at a scratch directory
and `TERM=xterm-256color`; `capture-pane -p -J` (plain) and `-p -J -e` (ANSI, kept
byte-exact). Login was passed with a placeholder API key so the trust dialog and
the composer could be reached; the one turn that was sent failed with HTTP 401, so
`working*` and `turn_error` show the retry and error renderings rather than output.
Upstream source checked against: `github.com/openai/codex` commit `44dd77b7`
(the clone date's main; the TUI strings match the 0.160.0 binary where observed).

Masking applied before commit: the placeholder key fragment Codex echoed back
(`sk-proj-***…0000`) became `sk-proj-[masked]`; Cloudflare ray ids and OpenAI
request ids became `cf-ray: [masked]` / `req_[masked]`; the model name in the
status row became `Model 0.0` (it was a hyphenated, dotted id); the shell prompt's
`user@host` is a placeholder. Paths are real (`/home/operator/Code/zordon/.scratch/codex`).

| File | State shown |
| --- | --- |
| welcome_login | first run: logo, `Welcome to Codex, OpenAI's command-line coding agent`, sign-in menu with `>` on option 1, `Press enter to continue` |
| welcome_login_apikey_selected | same, pointer moved to `3. Provide your own API key` |
| apikey_entry | the `Use your own OpenAI API key for usage-based billing` box (`Press enter to save` / `Press esc to go back`) |
| trust_dialog | `Folder access` dialog for a subdirectory of a git repo (`Note: You’re in a subdirectory…`), `› 1. Trust and continue` / `2. Back to Agent Command Center`, footer `enter continue · esc back` |
| idle | alternate screen after trust: header `>_ OpenAI Codex (v0.160.0)`, logo, composer `› Ask Codex to do anything`, status row `Model 0.0 default · ~/…`, footer `← for agents · ? for shortcuts … ⚠ 1 warning · f2 to view` |
| warning_overlay | the `f2` warnings pane (`Codex's Linux sandbox uses bubblewrap…`) |
| idle_typed | typed-but-unsent text in the composer |
| working | 0.4 s after Enter: the echo `› List the files…`, `• Working (0s • esc to interrupt)`, composer, status row ending `· ⠙` |
| working_reconnecting | `• Reconnecting... 2/5 (1s • esc to interrupt)` with the `└ Unexpected status 401…` detail |
| working_reconnecting_long | the same at 8 s (`3/5`) |
| turn_error | the turn given up: `■ unexpected status 401 Unauthorized…`, composer back, spinner gone |
| quit_typed | `/quit` typed, the slash-command popup above the composer |
| exited | normal screen after `/quit`: `Disconnected from this task. Any running work continues.` / `To reconnect, run:` / `  codex resume <uuid>` / shell prompt |
| resume_picker | `codex resume` with one session: filter row, `› 2m ago  List the files…`, key hints |

The live `CODEX_HOME` also showed the store layout the adapter reads:
`sessions/2026/10/02/rollout-2026-10-02T14-04-20-<uuid>.jsonl` (11 records:
`session_meta`, `event_msg task_started`, developer/user `response_item message`s,
`world_state`, `turn_context` with `approval_policy: "on-request"` and
`sandbox_policy.type: "workspace-write"`, the typed prompt, `event_msg
item_completed` (UserMessage) and `event_msg task_complete` carrying the 401
error), `history.jsonl`, `config.toml` with `[projects."…"] trust_level = "trusted"`,
and an `app-server-daemon` that kept running after `/quit` (killed by hand).

## Synthetic fixtures (not captured; built from the source)

Assembled from `codex-rs/tui/src/bottom_pane/approval_overlay.rs` (titles and
option labels), its default keymap (`codex-rs/tui/src/keymap.rs`: `y`, `a`, `p`,
`d`, `esc`/`n`, `c`) and the rendered insta snapshots under
`codex-rs/tui/src/chatwidget/snapshots/` and `bottom_pane/snapshots/`
(`approval_modal_exec`, `approval_modal_patch`, `approval_overlay_permissions_prompt`,
`network_exec_prompt`, the 40-column clipping snapshot). The header rows above the
modal (`>_ OpenAI Codex`, the `›` echo, `• …` history cells) follow the live idle
capture. Whether the status row stays visible under the overlay, and whether the
composer is hidden, is **unverified**: the snapshots render the overlay alone.

| File | Shows |
| --- | --- |
| synthetic_approval_exec | shell command approval: `Reason:`, `$ python -m pytest -q`, options proceed / don't-ask-again-for-prefix (`p`) / tell Codex what to do differently (`esc`) |
| synthetic_approval_exec_session | four options with the pointer on `2. Yes, and don't ask again for this command in this session (a)`; `3. No, continue without running it (d)` |
| synthetic_approval_patch | file edit approval: `Description:`, `Destination:`, `Yes, and don't ask again for these files (a)` |
| synthetic_approval_permissions | `Would you like to grant these permissions?` with `Permission rule:` and the four grant/deny options (`y`, `r`, `a`, `d`) |
| synthetic_approval_network | `Do you want to approve network access to "example.com"?` with `Yes, just this once`, per-conversation and in-the-future variants |
| synthetic_approval_exec_narrow_40 | the 40-column rendering: wrapped title, `[… 43 lines]` clipping marker, a label wrapped onto an indented row, wrapped footer |
| synthetic_turn_done | a finished turn: `✔ You approved codex to run …`, `• Ran …` / `└ output`, `• Explored`, prose with a numbered `1. Yes… 2. No…` list, `Worked for 48s • 2:32 PM`, composer, footer with `100% context left` |
| synthetic_rollout.jsonl | a two-turn rollout in the shapes of `codex-rs/protocol/src/models.rs` (ResponseItem) and `protocol.rs` (EventMsg, TurnContextItem): both the legacy `agent_message` and the paginated `item_completed` duplicates are present so the de-duplication is exercised; turn 2 ends with `turn_aborted` |

Re-capture plan: someone with a Codex login runs the probe in `docs/agents.md`
("Capturing fixtures for a new agent"), replaces every `synthetic_approval_*` file
with a live capture of the same state, and updates this table and
`CODEX_VERSION` in `zordon/agents/codex.py`.
