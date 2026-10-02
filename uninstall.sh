#!/bin/sh
# Zordon uninstaller.
#
#   curl -fsSL https://raw.githubusercontent.com/jeremiahcarreon/zordon/main/uninstall.sh | sh
#
# Prefers `zordon uninstall` (full-screen, shows everything Zordon installed and asks
# about each outside item). When the zordon command is already gone, falls back to
# removing the isolated environment and ~/.zordon by hand, asking first.

set -eu

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B="$(printf '\033[1m')"; D="$(printf '\033[2m')"; R="$(printf '\033[0m')"
  C1="$(printf '\033[38;5;141m')"; OK="$(printf '\033[38;5;114m')"; BAD="$(printf '\033[38;5;203m')"
else
  B=""; D=""; R=""; C1=""; OK=""; BAD=""
fi
say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
ask() {  # ask "question" -> 0 for yes
  printf '  %s [y/N]: ' "$1"
  if [ -r /dev/tty ]; then read -r a </dev/tty; else a=""; fi
  case "$a" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

say ""
say "${C1}  ╭──────────────────────────────────────────────────────────╮${R}"
say "${C1}  │${R}  ${B}Z O R D O N${R}  ${D}uninstall${R}                                   ${C1}│${R}"
say "${C1}  ╰──────────────────────────────────────────────────────────╯${R}"
say ""

BIN_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) PATH="$BIN_DIR:$PATH"; export PATH ;; esac
if have uv; then
  TOOL_BIN="$(uv tool dir --bin 2>/dev/null || true)"
  [ -n "$TOOL_BIN" ] && case ":$PATH:" in *":$TOOL_BIN:"*) ;; *) PATH="$TOOL_BIN:$PATH"; export PATH ;; esac
fi

if have zordon; then
  say "  ${D}Handing over to zordon uninstall...${R}"
  if [ -r /dev/tty ]; then exec zordon uninstall </dev/tty; else exec zordon uninstall --yes; fi
fi

say "  The zordon command is not on PATH; cleaning up by hand."
ZHOME="${ZORDON_HOME:-$HOME/.zordon}"

if have uv && uv tool list 2>/dev/null | grep -q '^zordon '; then
  if ask "Remove the isolated zordon environment (uv tool uninstall zordon)?"; then
    uv tool uninstall zordon && say "  ${OK}✓${R} environment removed"
  fi
elif have pipx && pipx list 2>/dev/null | grep -q 'package zordon'; then
  if ask "Remove the isolated zordon environment (pipx uninstall zordon)?"; then
    pipx uninstall zordon && say "  ${OK}✓${R} environment removed"
  fi
fi

if [ -d "$ZHOME" ]; then
  if ask "Remove $ZHOME (config, token, transcripts, models)?"; then
    rm -rf "$ZHOME" && say "  ${OK}✓${R} removed $ZHOME"
  fi
fi

if have tmux && tmux has-session -t =zordon 2>/dev/null; then
  if ask "Close the zordon tmux session (agents inside keep running detached)?"; then
    tmux kill-session -t =zordon && say "  ${OK}✓${R} tmux session closed"
  fi
fi

say ""
say "  ${D}Anything Zordon installed on request outside its environment (tmux, Node, the agent, Ollama)${R}"
say "  ${D}stays unless you remove it with your package manager; \`zordon uninstall\` offers each one when${R}"
say "  ${D}the zordon command is still installed.${R}"
say "  ${OK}Done.${R}"
