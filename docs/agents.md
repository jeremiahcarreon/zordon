# Agent adapters

Zordon talks to a coding agent through its terminal UI in a tmux pane. Everything
that is specific to one agent lives in one adapter class under `zordon/agents/`;
the session manager, state machine, pipeline, router, transport and client are
agent-neutral and only see the `AgentAdapter` protocol in `zordon/agents/base.py`.
`providers.agent` in `config.toml` selects the adapter (`claude-code` default,
`codex`, `generic`); `zordon/agents/__init__.py` is the registry.

An adapter supplies six things:

1. **Launch and resume**: the argv for a new and a resumed session, with the
   permission mode mapped from Zordon's names and every bypass flag refused.
2. **Prompts**: how a permission request, plan approval, question and folder-trust
   dialog look on screen, which option is the plain "yes", which is "no", and which
   options widen permissions (`PromptOption.unsafe`, never selected by voice).
3. **Screen anatomy**: the input box, the spinner or status row, the completion
   row, the exit screen, whether the TUI runs on the alternate screen.
4. **Discovery**: where sessions live on disk, their cwd, title and last activity,
   and whether one is running in a pane.
5. **Transcript**: an optional clean source the agent writes itself (a session
   log), yielding `PaneLine`s with `source="jsonl"` and a `block` kind.
6. **Permissions and wording**: the active mode, a spoken summary of the settings,
   out-of-band signals (hooks, a registry) when the agent has them.

## Fidelity per adapter

| Adapter | Key | Built from | What works | What does not |
| --- | --- | --- | --- | --- |
| Claude Code | `claude-code` | Live captures of CLI 2.1.287 (`eval/fixtures/pane`) plus the session store, hooks and settings formats (decisions 0002, 0003, 0007, 0009) | Everything: permission, plan, AskUserQuestion and trust prompts; jsonl prose; Notification hook as a second prompt signal; registry status as a third; mode cycling with Shift+Tab; settings summary | - |
| Codex | `codex` | `codex-cli 0.160.0`: the binary's help, a live onboarding run (welcome, API-key screen, trust dialog, idle composer, working row, a failed turn, exit, resume picker) and the TUI/protocol source for everything a turn would show | Launch/resume argv with the approval policy; store discovery and the rollout transcript; trust dialog and sign-in screens from live captures; idle/working/exited from live captures; approval modals from the source strings | The approval modals are **unverified against a live run**: no OpenAI login was available, so their layout comes from the TUI's snapshot tests. No hook or registry: the pane is the only prompt signal. No plan-approval or question prompts (Codex's plan mode has no approval modal; `request_user_input` is not catalogued yet). The approval policy cannot be switched mid-session |
| generic | `generic` | Nothing specific | Attach to any pane; inline `(y/n)` / `[Y/n]` questions and numbered or lettered Yes/No menus; idle when the last line looks like a prompt glyph; exit on a shell prompt | Everything else is pane prose; no discovery, no resume, no modes |

## Codex notes

Verified live on this machine (binary in `.scratch/codex`, `CODEX_HOME` pointed at
a scratch directory, no login):

* `codex --version` prints `codex-cli 0.160.0`. `--ask-for-approval` accepts only
  `on-request` and `never`; `untrusted` and `on-failure` are rejected by the flag
  but remain valid `approval_policy` values, so the adapter passes them as
  `--config approval_policy="…"`. `--full-auto` no longer exists; the bypass flag
  is `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`) and
  `--approve-for-me` (alias `--not-so-yolo`) hands approvals to an automatic
  reviewer. All of these, `never` and `--sandbox danger-full-access` are refused.
* First run shows the welcome logo and a `> 1. Sign in with ChatGPT / 2. Sign in
  with Device Code / 3. Provide your own API key` menu (`Press enter to continue`),
  then the API-key box, then the folder-trust dialog (`Folder access`, `Trust this
  folder? …`, `› 1. Trust and continue / 2. Quit` or `Back to Agent Command
  Center`, footer `enter continue · esc quit|back`). Codex preselects "trust"; the
  manager still asks the user before accepting.
* The composer is `› Ask Codex to do anything`; the echo of a sent message uses the
  same `›` glyph in the history. While a turn runs the row above the composer reads
  `• Working (3s • esc to interrupt)` and the model row ends with a braille spinner
  (`· ⠙`). A failed turn leaves a `■ …` error line and the composer. `/quit` leaves
  the alternate screen and prints `To reconnect, run:` / `codex resume <uuid>`.
* The store is `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<uuid>.jsonl`.
  Each line is `{"timestamp", "type", "payload"}`: `session_meta` (cwd, id,
  cli_version), `turn_context` (approval_policy, sandbox_policy.type, model),
  `response_item` (`message` with role and `input_text`/`output_text` content,
  `function_call` with a JSON-string `arguments`, `function_call_output`,
  `local_shell_call`, `custom_tool_call`, `reasoning`), `event_msg`
  (`task_started`, `task_complete` with `last_agent_message`/`error`,
  `turn_aborted`, `item_completed`, and in legacy history mode `agent_message`).
  Assistant prose and tool calls are taken from `response_item` only; the
  `event_msg` copies are skipped so nothing is spoken twice. `history.jsonl`
  holds `{"session_id", "ts", "text"}` per typed prompt. The TUI also connects to a
  shared app-server daemon that survives `/quit` ("Any running work continues").
* `config.toml` keys read: `approval_policy`, `sandbox_mode`, `approvals_reviewer`
  and `[projects."<path>"] trust_level = "trusted"`.

From the source only (not seen live): the approval overlay titles `Would you like
to run the following command?` / `make the following edits?` / `grant these
permissions?` / `Do you want to approve network access to "host"?`; the header
fields `Reason:`, `Description:`, `Destination:`, `Permission rule:` and the `$ cmd`
block; the menu `› 1. Yes, proceed (y)` with the per-option shortcut in parentheses
(`y` approve, `a` approve for session, `p` approve for prefix, `d` deny, `esc`/`n`
decline, digits select); the footer `Press enter to confirm or esc to cancel`; the
labels `Yes, and don't ask again for commands that start with …`, `Yes, and don't
ask again for this command in this session`, `Yes, and don't ask again for these
files`, `Yes, and allow this host for this conversation`, `Yes, and allow this host
in the future`, `Yes, grant these permissions for this session`, `Yes, grant for
this turn with strict auto review` (all `unsafe`), `No, continue without running
it`, `No, and tell Codex what to do differently`, `No, continue without
permissions`, `No, and block this host in the future` (never the voice "no": it
writes a rule). Shift+Tab cycles the collaboration mode (Default/Plan), not the
approval policy, so `mode_cycle_key()` is None.

## Capturing fixtures for a new agent

Fixtures are verbatim `tmux capture-pane` output, masked, with a provenance file
next to them (`eval/fixtures/pane/PROVENANCE.md`, `eval/fixtures/codex/PROVENANCE.md`).
To add an agent:

1. Install the binary locally (a scratch directory, never globally) and record
   `--version` and the help text of every subcommand you will drive.
2. Start a private tmux server so nothing touches your own sessions:
   `tmux -L probe new-session -d -x 160 -y 45 -c <dir>`, with the agent's home
   directory variable pointed into the scratch directory. Drive it with
   `tmux -L probe send-keys` and capture after each state with
   `tmux -L probe capture-pane -p -J` (plain) and `-p -J -e` (ANSI). Capture at
   least: first run / login, trust or onboarding, idle, typed-but-unsent text,
   working (two or three 100 ms polls so the spinner frames differ), every
   prompt kind with the pointer on the default option and on another option, the
   screen right after answering, a finished turn, an interrupted turn, exit.
   Kill the server afterwards (`tmux -L probe kill-server`) and any daemon the
   agent left behind.
3. Mask before committing: API keys and tokens (even partially shown ones),
   request ids, hostnames in shell prompts, model names if they are not part of
   what you match, anything resembling a secret. Keep paths unless they are
   private. Keep `.ansi.txt` files byte-exact.
4. Read the agent's source for the strings you match (approval labels, footers,
   spinner glyphs, the exit line) and record file paths and the version in the
   adapter's comments. Mark every string you could not see live as unverified and
   build a `synthetic_*` fixture for it, listed as synthetic in the provenance file.
5. Write the adapter as a `BaseAdapter` subclass: argv builders with a refusal
   list for bypass flags, `detect_prompt` with `unsafe` flags on every widening
   option, `is_idle` / `is_working` / `exited`, discovery over the store with
   bounded head and tail reads, and a `TranscriptSource` if the agent keeps a log.
   Register it in `zordon/agents/__init__.py`.
6. Tests: argv per mode and every refused spelling, discovery over a synthetic
   store in `tmp_path`, prompt detection on every fixture (the ordinary screens
   must return None), the transcript tail on a synthetic log, idle/working/exited.
