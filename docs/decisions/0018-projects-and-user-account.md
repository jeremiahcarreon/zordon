# 0018: Projects, admin mode, a per-project bypass choice, and no root

**Status:** accepted, 2026-10-03

## Context

The first client exposed sessions and tmux panes directly: a sidebar with
attach, detach, create, resume, focus and delete, a form that wanted a full
directory path, and a permission-mode selector. The audience Zordon is for
does not know what a pane is, cannot detach from one while Claude Code holds
the keyboard, and should never have to type `/home/me/Code/thing`. Two
things made this urgent in testing:

* A fresh container runs everything as root. Claude Code refuses
  `--permission-mode bypassPermissions` as root outright ("cannot be used
  with root/sudo privileges"), the installer's prerequisite steps assume
  `sudo`, and every project would otherwise run with root's power.
* The owner of the install wants Claude Code to run without permission
  prompts for their own projects and does not want to reach into tmux to set
  that up. Decision 0007 made bypass unreachable from Zordon on purpose: the
  voice channel is lossy, and a misheard phrase that widens permissions is
  the worst failure. Both are true; they apply to different moments.

## Decision

**Projects.** The unit the user sees is a project: a directory under their
home plus how its agent runs there. `~/.zordon/projects.json` records, per
project: id, name, directory, agent, permission mode, whether file edits are
scoped to the directory, the agent's last session id and the tmux pane it
last ran in. Two entry points: *Start a new project* (a walkthrough: pick or
create a folder inside home, pick the assistant when more than one is
installed, pick how much it should ask) and *Continue a previous project*
(a list). Opening a project reconnects to its pane when the pane is alive,
else resumes its last conversation, else starts a fresh one in the folder.
Nothing about tmux is shown unless the user opens *Advanced*.

**Admin mode.** With nothing focused, Zordon is in admin mode: speech goes
to Zordon only ("open the api project", "list projects", "new project"),
never to an agent. *Pause* (voice: "pause", "back to projects") leaves work
mode; every pane keeps running. This replaces detach as the thing a user does
when they want to stop talking to a project.

**Bypass is a per-project launch choice, nothing else.** When creating a
project the user may choose *Never ask*; the project then launches with
`--permission-mode bypassPermissions` and nothing more. That is the only
place the mode is emitted (`discovery.BYPASS_MODE`, `allow_bypass=True`).
Unchanged from 0007: voice cannot switch into it, the mode switcher refuses
it, the config default refuses it, no settings file ever carries it, and the
dangerous skip flags (`--dangerously-skip-permissions` and friends) are
refused everywhere. Claude Code shows its own one-time warning dialog before
running that way; Zordon recognises it as a TRUST-kind prompt and reads it
aloud, and *accept* is honoured only for a session that was launched for a
bypass project. The same dialog in any other session (a pane the user
started that way and attached) is left alone with a spoken explanation.
Codex projects cannot choose it yet: its equivalent flag is still refused.

**Scoped edits.** A project keeps *Keep file edits inside this folder* on by
default. Its sessions get a synchronous `PreToolUse` hook on the file
editing tools (Edit, Write, MultiEdit, NotebookEdit) that POSTs to
`/hooks/scope`; Zordon denies, with the reason, any path that resolves
outside the project directory or the agent's own home (`~/.claude`, where
its scratchpad lives). The hook fails closed: if Zordon cannot be reached
the edit is denied. It is the counterweight to *Never ask*: a bypass project
still cannot edit another project's files. Shell commands are not
sandboxed; the README says so plainly.

**No root.** `zordon serve`, `start`, `restart`, `setup` and
`service install` refuse to run as root (`ZORDON_ALLOW_ROOT=1` for CI).
The installer, when it finds itself root, creates a normal user (name asked,
default `zordon`), gives it the admin group, sets its password, and
re-executes itself as that user. The health strip warns when a server is
nevertheless running as root.

## Consequences

* Non-technical use needs exactly two decisions up front (folder, how much to
  ask); everything else has a default. Power users keep the old controls
  under *Advanced*.
* "Bypass" exists in the codebase as one named constant and one allow flag;
  the safety tests allow-list that spelling and nothing else.
* A project remembers its conversation. Deleting the pane no longer loses
  the thread; *Forget* drops only the record and touches no files.
* Running as a user means the wizard's prerequisite installs need `sudo` and
  a password once; the installer sets both up.

## Open

* The bypass warning dialog's wording is from Claude Code 2.1.x and has no
  fixture: the automated check could not be driven through it. The header
  regex is loose on purpose; a captured fixture should replace it.
* Scope covers the file tools only. A `Bash` heuristic (deny absolute paths
  outside the project) was considered and rejected for now as too noisy.
* "Start a new project" by voice only points at the app; a spoken walkthrough
  (name, folder, mode by voice) is future work.
* Projects are per machine. Nothing syncs them.
