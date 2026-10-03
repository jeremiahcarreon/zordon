#!/bin/sh
# Zordon installer.
#
#   curl -fsSL https://raw.githubusercontent.com/jeremiahcarreon/zordon/main/install.sh | sh
#
# What it does, in order, and nothing else:
#   1. checks the OS (Linux or macOS) and that curl or wget exists;
#   2. installs uv into ~/.local/bin if it is not already there (no sudo): a pinned
#      release of uv's installer, verified against its SHA-256 before it runs;
#   3. has uv install a managed Python 3.12 if the system has none, then
#      installs zordon as an isolated tool (uv tool install);
#   4. runs `zordon setup`, the guided setup, which detects what else is missing
#      (tmux, Node.js, the coding agent, Ollama) and installs each piece only
#      after you say yes.
#
# Read it before you run it: it is short. Set ZORDON_REF to install a branch or
# tag (default main), ZORDON_SOURCE to a local checkout or another archive URL,
# ZORDON_NO_SETUP=1 to skip the wizard, NO_COLOR=1 for plain output.

set -eu

REPO="https://github.com/jeremiahcarreon/zordon"
REF="${ZORDON_REF:-main}"
# A tarball, not a git URL, so git is not a prerequisite on a fresh machine.
SOURCE="${ZORDON_SOURCE:-${REPO}/archive/refs/heads/${REF}.tar.gz}"
PYTHON_VERSION="${ZORDON_PYTHON:-3.12}"
BIN_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"

# uv's installer, pinned to a release and verified before it runs. Bump both together:
#   curl -fsSL https://github.com/astral-sh/uv/releases/download/<ver>/uv-installer.sh | sha256sum
UV_VERSION="${ZORDON_UV_VERSION:-0.12.22}"
UV_INSTALLER_SHA256="${ZORDON_UV_INSTALLER_SHA256:-58488ae8dbd0773134c92c85e901430e33f99d975bd7f929d26aa9ab0c2f9390}"
UV_INSTALLER_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh"

# ---- presentation (pure ANSI, no dependencies) -----------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B="$(printf '\033[1m')"; D="$(printf '\033[2m')"; R="$(printf '\033[0m')"
  C1="$(printf '\033[38;5;141m')"; C2="$(printf '\033[38;5;75m')"; OK="$(printf '\033[38;5;114m')"; BAD="$(printf '\033[38;5;203m')"
  TTY=1
else
  B=""; D=""; R=""; C1=""; C2=""; OK=""; BAD=""; TTY=""
fi

say() { printf '%s\n' "$*"; }
step() { printf '  %s◆%s %s\n' "$C2" "$R" "$*"; }
done_() { printf '  %s✓%s %s\n' "$OK" "$R" "$*"; }
die() { printf '  %s✗ %s%s\n' "$BAD" "$*" "$R" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

banner() {
  say ""
  say "${C1}  ╭──────────────────────────────────────────────────────────╮${R}"
  say "${C1}  │${R}  ${B}Z O R D O N${R}                                              ${C1}│${R}"
  say "${C1}  │${R}  ${D}Talk to your coding agent. Hear it back. Interrupt it.${R}  ${C1}│${R}"
  say "${C1}  ╰──────────────────────────────────────────────────────────╯${R}"
  say ""
  say "  ${D}This script installs uv (no sudo), a managed Python, and zordon in its own${R}"
  say "  ${D}environment, then opens the guided setup. Nothing else happens without a yes.${R}"
  say ""
}

# Run a command behind a spinner when attached to a terminal; stream it otherwise.
spin() {  # spin "label" cmd args...
  label="$1"; shift
  if [ -n "$TTY" ]; then
    logf="$(mktemp 2>/dev/null || mktemp -t zordon-log)"
    "$@" >"$logf" 2>&1 &
    pid=$!
    i=0
    while kill -0 "$pid" 2>/dev/null; do
      case $((i % 8)) in 0) f='⠋';; 1) f='⠙';; 2) f='⠹';; 3) f='⠸';; 4) f='⠼';; 5) f='⠴';; 6) f='⠦';; *) f='⠧';; esac
      printf '\r  %s%s%s %s' "$C2" "$f" "$R" "$label"
      i=$((i + 1)); sleep 0.1
    done
    if wait "$pid"; then
      printf '\r  %s✓%s %s\n' "$OK" "$R" "$label"; rm -f "$logf"
    else
      printf '\r  %s✗%s %s\n' "$BAD" "$R" "$label"; sed 's/^/      /' "$logf" | tail -20; rm -f "$logf"; return 1
    fi
  else
    step "$label"; "$@"
  fi
}

banner

# ---- 1. environment -----------------------------------------------------------------
case "$(uname -s)" in
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then
      done_ "WSL detected: Zordon runs here; open http://localhost:8765 from your Windows browser."
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

sha256_of() {
  if have sha256sum; then sha256sum "$1" | awk '{print $1}'
  elif have shasum; then shasum -a 256 "$1" | awk '{print $1}'
  elif have openssl; then openssl dgst -sha256 "$1" | awk '{print $NF}'
  else die "need sha256sum, shasum or openssl to verify the uv installer"
  fi
}

# ---- 1b. never as root --------------------------------------------------------------------
# Zordon runs a coding agent with the power of the account it runs under, and Claude
# Code refuses to skip permissions as root. As root this script creates a normal user
# (asking for the name) and continues the install as that user. ZORDON_ALLOW_ROOT=1 skips.
if [ "$(id -u)" = 0 ]; then
  if [ "${ZORDON_ALLOW_ROOT:-}" = 1 ]; then
    printf '  %s!%s Running as root because ZORDON_ALLOW_ROOT=1 is set. Claude Code will refuse bypass mode.\n' "$BAD" "$R"
  else
    say "  Zordon must run as a normal user, not root: it drives a coding agent with the"
    say "  power of its account, and Claude Code refuses to skip permissions as root."
    say ""
    ZUSER=""
    if [ -r /dev/tty ]; then
      printf '  Name for the new user [%szordon%s]: ' "$B" "$R"
      read -r ZUSER </dev/tty || ZUSER=""
    else
      ZUSER="${ZORDON_USER:-}"
      say "  No terminal attached; using the user ${B}${ZUSER:-zordon}${R} (set ZORDON_USER to choose)."
    fi
    ZUSER="${ZUSER:-zordon}"
    case "$ZUSER" in
      *[!A-Za-z0-9._-]*|-*|"") die "user names use letters, digits, dots, dashes and underscores" ;;
    esac

    # sudo itself, so the setup wizard can install prerequisites later.
    if ! have sudo; then
      install_sudo() {
        if have apt-get; then apt-get update -qq && apt-get install -y -qq sudo
        elif have dnf; then dnf install -y -q sudo
        elif have yum; then yum install -y -q sudo
        elif have pacman; then pacman -Sy --noconfirm --needed sudo
        elif have zypper; then zypper --non-interactive install sudo
        elif have apk; then apk add --no-cache sudo shadow
        else return 1
        fi
      }
      spin "Installing sudo" install_sudo || say "  ${BAD}!${R} sudo could not be installed; prerequisite installs will have to be done by hand."
    fi

    if id "$ZUSER" >/dev/null 2>&1; then
      done_ "User $ZUSER exists"
    else
      if have useradd; then
        useradd -m -s /bin/bash "$ZUSER" || die "could not create user $ZUSER"
      elif have adduser; then
        adduser -D "$ZUSER" || die "could not create user $ZUSER"
      else
        die "neither useradd nor adduser is available; create a user by hand and run this again as that user"
      fi
      done_ "Created user $ZUSER"
    fi
    ADMIN_GROUP=""
    if getent group sudo >/dev/null 2>&1; then ADMIN_GROUP=sudo
    elif getent group wheel >/dev/null 2>&1; then ADMIN_GROUP=wheel
    fi
    if [ -n "$ADMIN_GROUP" ]; then
      if have usermod; then usermod -aG "$ADMIN_GROUP" "$ZUSER" 2>/dev/null || true
      elif have adduser; then adduser "$ZUSER" "$ADMIN_GROUP" 2>/dev/null || true
      fi
      done_ "$ZUSER can use sudo (group $ADMIN_GROUP)"
    fi
    if [ -r /dev/tty ]; then
      say "  Choose a password for $ZUSER; the wizard asks for it when it installs prerequisites."
      passwd "$ZUSER" </dev/tty || say "  ${BAD}!${R} No password set; set one later with: passwd $ZUSER (needed for installing prerequisites)."
    elif [ "${ZORDON_SUDO_NOPASSWD:-}" = 1 ]; then
      mkdir -p /etc/sudoers.d
      printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$ZUSER" > "/etc/sudoers.d/zordon-$ZUSER"
      chmod 0440 "/etc/sudoers.d/zordon-$ZUSER"
      done_ "$ZUSER may install packages without a password (ZORDON_SUDO_NOPASSWD=1)"
    else
      say "  ${BAD}!${R} No terminal to set a password: prerequisite installs will need one later (passwd $ZUSER)."
    fi

    # Fetch this script to a file the new user can read, then continue as that user.
    SELF="/tmp/zordon-install.sh"
    if [ -f "$0" ] && [ "$(basename "$0")" = "install.sh" ]; then
      cp "$0" "$SELF"  # run from a checkout: continue with this very file
    else
      fetch_self() { fetch "https://raw.githubusercontent.com/jeremiahcarreon/zordon/${REF}/install.sh" > "$SELF"; }
      spin "Fetching the installer for $ZUSER" fetch_self
    fi
    chmod 644 "$SELF"
    PASS="ZORDON_REF='$REF' ZORDON_SOURCE='$SOURCE' ZORDON_NO_SETUP='${ZORDON_NO_SETUP:-}' NO_COLOR='${NO_COLOR:-}'"
    PASS="$PASS ZORDON_PYTHON='${ZORDON_PYTHON:-}' ZORDON_UV_VERSION='${ZORDON_UV_VERSION:-}' ZORDON_UV_INSTALLER_SHA256='${ZORDON_UV_INSTALLER_SHA256:-}'"
    say ""
    say "  ${D}Continuing as ${R}${B}$ZUSER${R}${D}...${R}"
    say ""
    if [ -r /dev/tty ]; then
      exec su - "$ZUSER" -c "$PASS sh $SELF" </dev/tty
    else
      exec su - "$ZUSER" -c "$PASS sh $SELF"
    fi
  fi
fi

# ---- 2. uv ----------------------------------------------------------------------------
INSTALLED_UV=""
if ! have uv && [ ! -x "$BIN_DIR/uv" ]; then
  TMP="$(mktemp -d 2>/dev/null || mktemp -d -t zordon)"
  trap 'rm -rf "$TMP"' EXIT
  fetch_uv() { fetch "$UV_INSTALLER_URL" > "$TMP/uv-installer.sh"; }
  spin "Downloading uv $UV_VERSION installer" fetch_uv
  GOT="$(sha256_of "$TMP/uv-installer.sh")"
  if [ "$GOT" != "$UV_INSTALLER_SHA256" ]; then
    die "uv installer checksum mismatch (expected $UV_INSTALLER_SHA256, got $GOT). Refusing to run it. Check ${REPO} for an updated install.sh."
  fi
  done_ "Checksum verified"
  run_uv_installer() { UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH="${UV_NO_MODIFY_PATH:-}" sh "$TMP/uv-installer.sh"; }
  spin "Installing uv into $BIN_DIR (no sudo)" run_uv_installer
  INSTALLED_UV=1
else
  done_ "uv already present"
fi
PATH_HINT=""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) PATH="$BIN_DIR:$PATH"; export PATH; PATH_HINT="$BIN_DIR" ;;
esac
have uv || die "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"

# ---- 3. python + zordon ----------------------------------------------------------------
py_install() { uv python install "$PYTHON_VERSION"; }
spin "Python $PYTHON_VERSION (managed by uv; the system Python is untouched)" py_install

zordon_install() {
  if uv tool list 2>/dev/null | grep -q '^zordon '; then
    uv tool upgrade zordon --python "$PYTHON_VERSION" || uv tool install --force --python "$PYTHON_VERSION" "zordon @ $SOURCE"
  else
    uv tool install --python "$PYTHON_VERSION" "zordon @ $SOURCE"
  fi
}
spin "Installing zordon into its own environment" zordon_install

TOOL_BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$BIN_DIR")"
case ":$PATH:" in
  *":$TOOL_BIN:"*) ;;
  *) PATH="$TOOL_BIN:$PATH"; export PATH; PATH_HINT="${PATH_HINT:-$TOOL_BIN}" ;;
esac
have zordon || die "zordon installed but is not on PATH; add $TOOL_BIN to PATH and run: zordon setup"
done_ "zordon $(zordon --version 2>/dev/null | awk '{print $2}') ready"

# ---- 4. guided setup -------------------------------------------------------------------
export ZORDON_TOOL_MANAGER=uv
[ -n "$PATH_HINT" ] && export ZORDON_PATH_HINT="$PATH_HINT"
if [ -n "$INSTALLED_UV" ]; then
  export ZORDON_INSTALLED_UV=1 UV_INSTALL_DIR="$BIN_DIR"
fi

if [ -n "${ZORDON_NO_SETUP:-}" ]; then
  say ""
  say "  Skipping setup (ZORDON_NO_SETUP set). Run: ${B}zordon setup${R}"
  [ -n "$PATH_HINT" ] && say "  This shell cannot see zordon yet: ${B}source $PATH_HINT/env${R} (or open a new terminal)."
  exit 0
fi

# When this script arrives through a pipe, stdin is the pipe. The wizard needs the
# terminal, so reattach it; without a terminal, take the detected defaults.
if [ -t 1 ] && [ -r /dev/tty ]; then
  say ""
  say "  ${D}Opening the guided setup...${R}"
  exec zordon setup </dev/tty
else
  say "  No terminal attached; writing a default configuration. Run ${B}zordon setup${R} to change it."
  exec zordon setup --yes --no-download
fi
