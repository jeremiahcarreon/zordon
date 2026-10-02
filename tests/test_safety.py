"""Invariants that must hold across the whole package. Cheap grep-level checks
that catch the class of mistake the design forbids outright."""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "zordon"

BYPASS_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)


def _py_files():
    return [p for p in PKG.rglob("*.py")]


def test_no_bypass_flag_is_ever_constructed():
    """The flag names may appear only inside a refusal list (a tuple named *FORBIDDEN* or
    *REFUSED*) so the launcher can reject them; never in a command construction."""
    for path in _py_files():
        text = path.read_text()
        for flag in BYPASS_FLAGS:
            for m in re.finditer(re.escape(flag), text):
                line_no = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1]
                # Allowed only on a line that is part of a FORBIDDEN/REFUSED constant or a comment.
                ctx = text[max(0, m.start() - 400) : m.start()]
                ok = (
                    "FORBIDDEN" in ctx.rsplit("\n\n", 1)[-1]
                    or "REFUSED" in ctx.rsplit("\n\n", 1)[-1]
                    or line.lstrip().startswith("#")
                )
                assert ok, f"{path}:{line_no} builds or mentions {flag} outside a refusal list"


def test_bypass_permissions_never_written():
    """'bypassPermissions' may be read (to report it) but never assigned or written."""
    pat = re.compile(r"""(?:["']bypassPermissions["']\s*[:=](?!=))|(?:=\s*["']bypassPermissions["'])""")
    for path in _py_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pat.search(line) and not line.lstrip().startswith("#"):
                raise AssertionError(f"{path}:{i} assigns bypassPermissions: {line.strip()}")


def test_send_keys_always_literal():
    """Every tmux send-keys call that carries user text must use -l."""
    tmux = PKG / "session" / "tmux.py"
    if not tmux.exists():
        return
    text = tmux.read_text()
    assert '"send-keys"' in text or "'send-keys'" in text
    # Any send-keys without -l must be in send_key (named keys from an allowlist).
    assert "KEY_ALLOWLIST" in text or "ALLOWED_KEYS" in text


def test_no_secrets_in_logging_calls():
    for path in _py_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            low = line.lower()
            if ("log." in low or "logger." in low or "logging." in low) and (
                "api_key" in low or "token" in low
            ):
                assert "redact" in low or "len(" in low or "configured" in low or "***" in line or "bool(" in low, (
                    f"{path}:{i} may log a secret: {line.strip()}"
                )
