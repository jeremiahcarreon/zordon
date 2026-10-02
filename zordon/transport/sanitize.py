"""Strip characters that must never be typed into a pane on the user's behalf.

``session/tmux.py`` strips C0 controls before ``send-keys -l``; this module goes
further and is applied on the transport side before text reaches the session
manager or the router:

* C0 controls and DEL (except tab, newline and carriage return, which typed
  text may legitimately contain and which tmux strips again anyway)
* C1 controls U+0080-U+009F (8-bit CSI, NEL, ...)
* zero-width and bidi format characters U+200B-U+200F, U+202A-U+202E,
  U+2066-U+2069, and the BOM U+FEFF, which can make what was typed look
  different from what the pane receives
"""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]"
)


def sanitize_keystrokes(text: str) -> str:
    """Return ``text`` without control, zero-width and bidi-override characters."""
    if not text:
        return text
    return _FORBIDDEN.sub("", text)


__all__ = ["sanitize_keystrokes"]
