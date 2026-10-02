"""Mask things that look like secrets before they reach the normalizer, TTS,
the transcript store or a client. The raw pane is still visible in the
terminal; Zordon just does not repeat it.

Patterns are deliberately broad. A false positive costs a masked token in the
spoken transcript; a false negative reads an API key aloud over a tunnel.

Two entry points:

* :func:`redact` masks one piece of text.
* :func:`redact_pair` masks the current pane line knowing the previous one, so
  a key the TUI hard-wrapped at the pane width is masked on both lines.
"""

from __future__ import annotations

import re

MASK = "[redacted]"

# NAME=value, NAME: value, "name": "value" (JSON) for secret-looking names. An
# optional closing quote may sit between the name and the separator.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|AUTH)[A-Z0-9_]*)"
    r"['\"]?\s*[=:]\s*['\"]?([^\s'\"]{6,})"
)
_URL_CREDS = re.compile(r"(?i)\b[a-z][a-z0-9+\-.]*://[^/\s:@]+:[^/\s@]+@")
# Zordon's own config.toml: ``[providers.keys] anthropic = "..."`` etc. The names
# are not secret-looking on their own, so match the TOML line shape.
_CONFIG_KEY_LINE = re.compile(
    r"(?im)^(\s*(?:anthropic|openai|elevenlabs|groq|typesafe)\s*=\s*)\"([^\"]{8,})\""
)
# ElevenLabs legacy keys are bare 32-hex; only mask them next to a provider hint.
_CONTEXT_HEX = re.compile(r"(?i)\b(xi-api-key|elevenlabs(?:_api_key)?)\b(\W{0,6})([0-9a-f]{32})\b")
# "Your session token is: <urlsafe>" and the cookie value.
_ZORDON_TOKEN = re.compile(r"(?i)(session token is:?\s*|zordon_session=)([A-Za-z0-9_\-]{16,})")

_PATTERNS: list[re.Pattern[str]] = [
    # Provider key shapes
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk_[A-Za-z0-9]{24,}"),  # ElevenLabs, Stripe
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\btsk_[A-Za-z0-9_\-]{16,}"),  # TypeSafe-style
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\bpypi-[A-Za-z0-9_\-]{40,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    # Bearer / basic auth headers
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9_\-\.=+/]{16,}"),
    # KEY=value / key: value / "key": "value" for secret-looking names
    _ASSIGNMENT,
    _CONFIG_KEY_LINE,
    _CONTEXT_HEX,
    _ZORDON_TOKEN,
    # Private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # URLs with embedded credentials
    _URL_CREDS,
    # Long hex / base64 blobs that are probably tokens (40+ chars, no spaces)
    re.compile(r"\b[0-9a-f]{40,}\b"),
]


def _sub(pat: re.Pattern[str], text: str) -> str:
    if pat is _ASSIGNMENT:
        return pat.sub(lambda m: f"{m.group(1)}={MASK}", text)
    if pat is _URL_CREDS:
        return pat.sub(lambda m: m.group(0).split("://")[0] + f"://{MASK}@", text)
    if pat is _CONFIG_KEY_LINE:
        return pat.sub(lambda m: f'{m.group(1)}"{MASK}"', text)
    if pat is _CONTEXT_HEX:
        return pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    if pat is _ZORDON_TOKEN:
        return pat.sub(lambda m: f"{m.group(1)}{MASK}", text)
    return pat.sub(MASK, text)


def redact(text: str) -> tuple[str, bool]:
    """Return (masked_text, was_redacted)."""
    if not text:
        return text, False
    out = text
    hit = False
    for pat in _PATTERNS:
        new = _sub(pat, out)
        if new != out:
            hit = True
            out = new
    return out, hit


def redact_pair(prev: str | None, curr: str) -> tuple[str, bool]:
    """Redact ``curr`` knowing that ``prev`` was the pane line right before it.

    The TUI hard-wraps at the pane width, so a key may continue on the next line
    where its tail is just letters and digits. Any pattern that matches across
    the ``prev``/``curr`` boundary of the joined text masks the part of ``curr``
    it covers; the rest of ``curr`` is redacted on its own. Returns
    (masked_curr, was_redacted).
    """
    masked, hit = redact(curr)
    if not prev or not curr:
        return masked, hit
    joined = prev + curr
    split = len(prev)
    cut = 0
    for pat in _PATTERNS:
        for m in pat.finditer(joined):
            if m.start() < split < m.end():
                cut = max(cut, m.end() - split)
    if not cut:
        return masked, hit
    rest, _ = redact(curr[cut:])
    return MASK + rest, True


def looks_sensitive(text: str) -> bool:
    return redact(text)[1]
