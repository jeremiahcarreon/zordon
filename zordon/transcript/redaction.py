"""Mask things that look like secrets before they reach the normalizer, TTS,
the transcript store or a client. The raw pane is still visible in the
terminal; Zordon just does not repeat it.

Patterns are deliberately broad. A false positive costs a masked token in the
spoken transcript; a false negative reads an API key aloud over a tunnel.
"""

from __future__ import annotations

import re

MASK = "[redacted]"

_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|AUTH)[A-Z0-9_]*)"
    r"\s*[=:]\s*['\"]?([^\s'\"]{6,})"
)
_URL_CREDS = re.compile(r"(?i)\b[a-z][a-z0-9+\-.]*://[^/\s:@]+:[^/\s@]+@")

_PATTERNS: list[re.Pattern[str]] = [
    # Provider key shapes
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    # Bearer / basic auth headers
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9_\-\.=+/]{16,}"),
    # KEY=value / key: value assignments for secret-looking names
    _ASSIGNMENT,
    # Private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # URLs with embedded credentials
    _URL_CREDS,
    # Long hex / base64 blobs that are probably tokens (40+ chars, no spaces)
    re.compile(r"\b[0-9a-f]{40,}\b"),
]



def redact(text: str) -> tuple[str, bool]:
    """Return (masked_text, was_redacted)."""
    if not text:
        return text, False
    out = text
    hit = False
    for pat in _PATTERNS:
        if pat is _ASSIGNMENT:
            new = pat.sub(lambda m: f"{m.group(1)}={MASK}", out)
        elif pat is _URL_CREDS:
            new = pat.sub(lambda m: m.group(0).split("://")[0] + f"://{MASK}@", out)
        else:
            new = pat.sub(MASK, out)
        if new != out:
            hit = True
            out = new
    return out, hit


def looks_sensitive(text: str) -> bool:
    return redact(text)[1]
