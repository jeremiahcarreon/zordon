# 0015: A one-line installer built on uv

**Status:** accepted, 2026-10-02

## Question

Even `pipx install zordon` assumes Python 3.12 and pipx. People who have
neither should still get from zero to the wizard with one command.

## Decision

- `install.sh` at the repo root, run as `curl -fsSL .../install.sh | sh`.
  POSIX sh (dash-safe), `set -eu`, no sudo anywhere: it installs uv into
  `~/.local/bin`, has uv install a managed Python 3.12 when the system lacks
  one, installs zordon as an isolated uv tool from the GitHub repo (PyPI when
  published), then runs `zordon setup`. Because stdin is the pipe, the script
  reattaches `/dev/tty` before the wizard; with no terminal it writes defaults.
- System packages (tmux, curl, Node, the agent, Ollama) stay in the wizard's
  prerequisites step, one explicit yes each, so the script itself never
  escalates. Knobs: `ZORDON_REF`, `ZORDON_SOURCE`, `ZORDON_PYTHON`,
  `ZORDON_NO_SETUP`, and uv's own `UV_*` directories (used by the tests to run
  the installer in an isolated prefix).
- Windows: the script detects WSL and says it is the right place; it refuses
  MSYS/Cygwin/Git-Bash with the WSL2 instructions. Native Windows would need a
  non-tmux pane backend and is out of scope.

## Pinned uv

The script fetches `uv-installer.sh` from a fixed uv release (0.12.22 at the
time of writing) and compares its SHA-256 against a constant before running
it; a mismatch refuses and points at the repository for an updated script.
Bumping uv means updating the version and the checksum together (the comment
in the script has the one-liner). `ZORDON_UV_VERSION` and
`ZORDON_UV_INSTALLER_SHA256` override both for testing.

## Verified

Ran in an isolated prefix (`UV_INSTALL_DIR`, `UV_TOOL_DIR`, `UV_PYTHON_INSTALL_DIR`,
`UV_CACHE_DIR` under the repo's scratch dir) from the public repository: uv
installed, Python managed, zordon installed from git, `zordon setup --yes`
produced a config and token. The piped, no-terminal branch also completed.

## Open

- zordon itself still tracks `main` of its repository; pin a tag once there
  are releases.
- Publish to PyPI so the source becomes `zordon` instead of a git URL.
