# 0013: Agent-specific knowledge sits behind an adapter with six slots

**Status:** accepted, 2026-10-02

## Question

Zordon was written against Claude Code 2.1.x: the command line, the alternate
screen TUI, the `❯` input box, the permission menus, the `~/.claude/projects`
jsonl, the Notification hook and the Shift+Tab mode cycle were spread across
`session/manager.py` and spoken as "Claude Code" everywhere. Other terminal
coding agents (Codex, and tools that have no session store at all) share the
tmux control, state machine, pipeline, router, transport and client, but none of
those details. Where does the agent-specific knowledge go, and how much of the
product survives for an agent Zordon knows nothing about?

## What was verified

- Every `discovery.`, `prompts.`, `jsonl.`, `hooks.` and `permissions.` call in
  the manager was enumerated. They fall into six groups: launching (argv, modes,
  hook settings), prompt detection and the option a yes/no/plan answer maps to,
  screen anatomy (idle, working, input quiet, exited, mode row, alternate
  screen), session discovery (store plus registry status), a clean transcript
  source, and wording (display name, permission summary, mode labels, hook
  hints). Nothing else in the manager depended on the agent.
- The existing manager suite (64 tests on scripted screens) and the tmux
  integration suite (the fake Claude TUI) pass unchanged with every one of those
  calls routed through `s.adapter`, which shows the manager's state logic is
  really agent-neutral.
- A generic detector that accepts only `(y/n)`-style inline cues or a
  question followed by a numbered/lettered menu with both a yes-ish and a
  no-ish entry, at the bottom of the screen, stays quiet on the false-positive
  shapes that matter (ordinary numbered lists, prose questions, a menu above a
  shell prompt, a yes without a no) while catching the twelve prompt shapes in
  `tests/test_agents_base.py`.

## Decision

- `zordon/agents/base.py` defines `AgentAdapter` (a runtime-checkable Protocol)
  with the six slots above, the data it exchanges with the manager
  (`AgentInfo`, `LaunchSpec`, `SessionInfo`, `HookRequest`, `TranscriptSource`)
  and `BaseAdapter`, the generic implementation. `zordon/agents/__init__.py` is
  the registry: `claude-code`, `codex` (imported lazily; a missing module does
  not break the registry), `generic`. `config.providers.agent` picks the
  default and is validated against the registry.
- `ClaudeCodeAdapter` moves nothing: it delegates to the existing modules, so
  the pane fixtures, the prompt regexes and the argv safety checks keep their
  tests and their provenance. Launch specs carry the files to remove when the
  pane goes (hook settings and its curl config) and the environment names to
  scrub.
- Each `Session` carries its adapter. The manager speaks
  `adapter.info.display_name`, gates keystrokes with the alternate screen only
  for adapters that use one (otherwise with `adapter.exited` on a fresh
  capture), confirms an exit over `EXIT_CONFIRM_POLLS` polls for both kinds,
  writes hook settings only when the adapter asked for them, and refuses mode
  switches with a spoken Notice when the adapter has no `mode_cycle_key`.
- `attach(target, agent="generic")` is the new path for an agent that is
  already running in tmux: a synthetic session id, `owned=False`, the pane's
  current path as `cwd`, no transcript unless the adapter finds one, and
  nothing that was on the pane before the attach is spoken. The web client gets
  an agent select on the New session form (uninstalled agents disabled) and an
  "Attach to a tmux pane" row; `hello.agents` says what is installed.
- How a generic prompt is answered rides on `PromptMatch.extra`: `inline_yn`
  types `y` or `n` then Enter; `key<n>` types the letter of a lettered menu with
  no Enter; otherwise the pointer is moved with Up/Down and Enter as for Claude
  Code. `approve` never selects a label that widens permissions, for any
  adapter.

### Generic adapter fidelity limits

- Prose comes from the pane only; there is no transcript, so verbosity and
  tool-chatter filtering work on screen lines and partial renders may be held
  one poll.
- Prompt detection needs an explicit yes/no shape. A tool that asks "Press
  Enter to continue" or uses a free-text confirmation is not detected; the
  stall watchdog and `last_pane_lines` are the fallback.
- There are no permission modes, no plan or question prompts and no trust
  dialog; `set_permission_mode` returns False with a Notice;
  `permission_summary` says the settings cannot be read.
- Idle is "a bare `>`/`❯`/`›`/`»` prompt on the last line and no spinner";
  working is a spinner glyph or "esc to interrupt". Tools with other input
  boxes are detected as idle only through the quiet-screen rule.
- The adapter cannot start, resume or list sessions: attach only.

### Why Claude Code stays the default

The Claude Code adapter is the only one with a clean transcript (the jsonl), a
second and third prompt signal (Notification hook, registry status), parsed
plan/question/trust prompts, and permission settings Zordon can read and
describe. Every safety invariant in `docs/architecture.md` was written and
measured against it (fixtures in `eval/fixtures/pane`, `PROMPTS_VERSION`).
`providers.agent = "claude-code"` therefore remains the default; other adapters
are opt-in per config or per session.

## Open

- A Codex adapter (`zordon/agents/codex.py`) fills the same six slots from its
  own fixtures; its fidelity depends on whether Codex writes a session log
  Zordon can tail.
- The generic detector could learn more inline cues (`[Enter]`, `Press y`) once
  captures from real tools exist; each addition needs a false-positive fixture
  beside it.
- `zordon sessions` (the CLI listing) still reads the Claude Code store
  directly; it should union the adapters like `SessionManager.list_sessions`.
