#!/bin/sh
# Zordon installer.
#
#   curl -fsSL https://raw.githubusercontent.com/jeremiahcarreon/zordon/main/install.sh | sh
#
# What it does, in order, and nothing else:
#   1. checks the OS (Linux or macOS) and that curl or wget exists;
#   2. installs uv into ~/.local/bin if it is not already there (no sudo);
#   3. has uv install a managed Python 3.12 if the system has none, then
#      installs zordon as an isolated tool (uv tool install);
#   4. runs `zordon setup`, the guided setup, which detects what else is missing
#      (tmux, Node.js, the coding agent, Ollama) and installs each piece only
#      after you say yes.
#
# Read it before you run it: it is short. Set ZORDON_REF to install a branch or
# tag (default main), ZORDON_SOURCE to a local checkout or another git URL, or
# ZORDON_NO_SETUP=1 to skip the wizard.

set -eu

REPO="https://github.com/jeremiahcarreon/zordon"
REF="${ZORDON_REF:-main}"
SOURCE="${ZORDON_SOURCE:-git+${REPO}@${REF}}"
PYTHON_VERSION="${ZORDON_PYTHON:-3.12}"
BIN_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
die() { printf 'zordon install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---- 1. environment -----------------------------------------------------------------
case "$(uname -s)" in
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then
      say "WSL detected: good. Zordon runs here; open http://localhost:8765 from your Windows browser."
    fi
    ;;
  Darwin) ;;
  MINGW*|MSYS*|CYGWIN*)
    die "native Windows is not supported (Zordon drives the agent inside tmux). Install WSL2 (wsl --install), open an Ubuntu terminal, and run this command there." ;;
  *) die "Linux and macOS only (Zordon drives the agent inside tmux)." ;;
esac

if have curl; then
  fetch() { curl -fsSL "$1"; }
elif have wget; then
  fetch() { wget -qO- "$1"; }
else
  die "curl or wget is required to download uv. Install one and run this again."
fi

# ---- 2. uv ----------------------------------------------------------------------------
if ! have uv && [ ! -x "$BIN_DIR/uv" ]; then
  say "Installing uv (Python installer and tool runner, no sudo) into $BIN_DIR ..."
  # The official installer honours UV_INSTALL_DIR and never touches the system Python.
  fetch https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH="${UV_NO_MODIFY_PATH:-}" sh
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) PATH="$BIN_DIR:$PATH"; export PATH ;;
esac
have uv || die "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"
say "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ---- 3. python + zordon ----------------------------------------------------------------
say "Ensuring Python $PYTHON_VERSION (uv downloads a managed build if the system has none) ..."
uv python install "$PYTHON_VERSION" >/dev/null 2>&1 || uv python install "$PYTHON_VERSION"

say "Installing zordon from $SOURCE ..."
if uv tool list 2>/dev/null | grep -q '^zordon '; then
  uv tool upgrade zordon --python "$PYTHON_VERSION" >/dev/null 2>&1 || \
  uv tool install --force --python "$PYTHON_VERSION" "zordon @ $SOURCE"
else
  uv tool install --python "$PYTHON_VERSION" "zordon @ $SOURCE"
fi

TOOL_BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$BIN_DIR")"
case ":$PATH:" in
  *":$TOOL_BIN:"*) ;;
  *) PATH="$TOOL_BIN:$PATH"; export PATH ;;
esac
have zordon || die "zordon installed but is not on PATH; add $TOOL_BIN to PATH and run: zordon setup"
say "zordon $(zordon --version 2>/dev/null | awk '{print $2}') installed."

# ---- 4. guided setup -------------------------------------------------------------------
if [ -n "${ZORDON_NO_SETUP:-}" ]; then
  say "Skipping setup (ZORDON_NO_SETUP set). Run: zordon setup"
  exit 0
fi

# When this script arrives through a pipe, stdin is the pipe. The wizard needs the
# terminal, so reattach it; without a terminal, take the detected defaults.
if [ -t 1 ] && [ -r /dev/tty ]; then
  say ""
  say "Starting the guided setup. Enter takes the default at every question."
  exec zordon setup </dev/tty
else
  say "No terminal attached; writing a default configuration. Run `zordon setup` to change it."
  exec zordon setup --yes --no-download
fi
