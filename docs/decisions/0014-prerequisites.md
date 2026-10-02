# 0014: Setup installs missing prerequisites, one explicit yes at a time

**Status:** accepted, 2026-10-02

## Question

`pipx install zordon` brings the Python side, but tmux, curl, Node.js, the
coding agent itself and Ollama are system installs. Telling users to go run
five commands elsewhere defeats the two-command install.

## Decision

- `zordon/prereqs.py` knows, for each prerequisite, why Zordon needs it, the
  binary to look for, the install command for every common package manager
  (brew, apt-get, dnf, yum, pacman, zypper, apk), what it depends on (npm
  globals need Node 18+), and what to do afterwards (log in, pull a model).
- The wizard's last step lists what is missing for the choices made, prints
  the exact command, and runs it only after a yes for that item. sudo prompts
  the user in their own terminal. After installing an agent it offers to open
  it right there so the user can log in, then continues. Node too old or
  absent with no package available falls back to a pointer to nodejs.org.
- `--yes` never installs system packages; it lists them. Non-interactive runs
  never install anything.
- `zordon doctor` uses the same table, so its fix column is the exact command
  for this machine, not a generic hint.
- Zordon still does not install pipx or Python; those are the two things the
  README tells the user to have.

## Open

- Windows is not covered (no package manager table, and tmux is not native).
- The npm global prefix may need sudo on some distributions; the command is
  shown as-is and npm reports the permission problem if it occurs.
