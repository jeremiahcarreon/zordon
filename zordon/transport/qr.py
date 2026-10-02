"""QR codes for the tunnel URL: one for the terminal, one for the session picker.

segno has no dependencies and a tunnel URL fits a version-4 symbol at error
level M. Both functions are pure and return strings.
"""

from __future__ import annotations

import io

import segno


def terminal_qr(url: str, border: int = 1) -> str:
    """Half-block rendering for a terminal (about half the rows of the full form)."""
    qr = segno.make(url, error="m")
    buf = io.StringIO()
    qr.terminal(out=buf, compact=True, border=border)
    return buf.getvalue()


def svg_qr(url: str, scale: int = 4, border: int = 1) -> str:
    """An ``<svg>`` element with no XML declaration, ready to drop into innerHTML."""
    qr = segno.make(url, error="m")
    return qr.svg_inline(scale=scale, border=border, dark="#000", light=None)
