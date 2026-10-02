"""Full-screen Textual front ends for ``zordon setup`` and ``zordon uninstall``.

The plain question-and-answer wizards in :mod:`zordon.setup` and the
``uninstall`` command stay the source of truth for what is asked and done; the
screens here call the same ``detect``/``recommend``/``apply``/``run_actions`` and
``build_plan``/``execute`` functions, adding mouse support, live command output,
and a summary card. ``--plain`` or a non-terminal falls back to the text wizards.
"""

from __future__ import annotations

from textual.theme import Theme

from zordon.tui.widgets import BLUE, GREEN, PURPLE, RED


class TuiUnavailable(RuntimeError):
    """The full-screen UI cannot start here (no terminal, TERM=dumb); use the plain wizard."""


ZORDON_THEME = Theme(
    name="zordon",
    primary=PURPLE,
    secondary=BLUE,
    accent=BLUE,
    success=GREEN,
    error=RED,
    warning="#ffd75f",
    background="#121218",
    surface="#1c1c26",
    panel="#2a2a38",
    dark=True,
)

__all__ = ["ZORDON_THEME", "TuiUnavailable"]
