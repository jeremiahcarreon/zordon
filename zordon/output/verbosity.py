"""Verbosity filter. Pure.

| Level     | Spoken                                                                  |
| minimal   | intent at start, outcome at end, prompts, errors                        |
| normal    | minimal + file names touched (no line counts)                           |
| technical | everything after pre-pass, including diff counts and tool calls         |

Tool-call and progress chatter is a separate toggle, independent of verbosity.
Prompts, plan approvals, errors and the final summary are never filtered.
"""

from __future__ import annotations

from zordon.bus import LineKind

NEVER_FILTERED = {
    LineKind.PERMISSION_PROMPT,
    LineKind.PLAN,
    LineKind.QUESTION,
    LineKind.ERROR,
    LineKind.SUMMARY,
}

ALWAYS_DROPPED = {LineKind.BLANK, LineKind.UI, LineKind.PROGRESS}


def keep(kind: LineKind, level: str, tool_chatter: bool, *, touches_file: bool = False) -> bool:
    """Decide whether a pre-passed item is spoken.

    ``touches_file`` marks a tool call that edits/creates a file (Edit, Write,
    MultiEdit, NotebookEdit): at ``normal`` those are spoken as file names.
    """
    if kind in NEVER_FILTERED:
        return True
    if kind in ALWAYS_DROPPED:
        return False
    if kind is LineKind.INTENT:
        return True
    if kind in (LineKind.TOOL_CALL, LineKind.TOOL_RESULT):
        if tool_chatter:
            return True
        if level == "technical":
            return True
        if level == "normal" and touches_file and kind is LineKind.TOOL_CALL:
            return True
        return False
    if kind is LineKind.DIFF:
        return level == "technical"
    if kind is LineKind.CODE:
        return level == "technical"
    if kind is LineKind.PATH:
        return level in ("normal", "technical")
    if kind is LineKind.PROSE:
        # At minimal, mid-turn prose is dropped; INTENT/SUMMARY carry the start and end.
        return level in ("normal", "technical")
    return level == "technical"
