"""Prompt detection for Claude Code's terminal UI. Pure: regexes and small functions.

Everything here matches rendered text captured from Claude Code ``PROMPTS_VERSION``
(``eval/fixtures/pane``). The format changes between releases, so a miss is
expected; the state machine's watchdog and the Notification hook are the backstop.

Classification order (the one that passed on every fixture):
TRUST -> PLAN -> QUESTION (AskUserQuestion) -> PERMISSION -> none.

Safety: ``PromptOption.unsafe`` marks every option that widens permissions
("always allow", "switch to auto mode", "switch to accept edits", "don't ask
again", "use auto mode"). ``yes_option`` only ever returns the option whose label
is exactly ``Yes``; ``no_option`` only the one labelled ``No``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from zordon.bus import PromptKind
from zordon.session import screen as _screen
from zordon.session.screen import (
    BANNER,
    DONE_LINE,
    DOTTED_RULE,
    EFFORT_HINT,
    EXIT_RESUME,
    INPUT_BOX,
    INTERRUPTED,
    REJECTED_WRITE,
    RULE,
    SPINNER,
    STATUS_MODE,
    USER_ECHO,
    Screen,
    parse_screen,
    strip_ansi,
)

PROMPTS_VERSION = "claude-code-2.1.287"

__all__ = [
    "PROMPTS_VERSION",
    "PromptMatch",
    "PromptOption",
    "detect_prompt",
    "is_idle_prompt",
    "input_quiet",
    "is_working",
    "has_spinner",
    "has_interrupt_hint",
    "turn_ended",
    "permission_mode_from_screen",
    "yes_option",
    "no_option",
    "plan_manual_option",
    "plan_revise_option",
    "plan_auto_option",
    "question_option",
    "is_unsafe_label",
    # regexes (structural ones re-exported from screen.py)
    "INPUT_BOX",
    "INPUT_GHOST",
    "USER_ECHO",
    "PERM_QUESTION",
    "PERM_QUESTION_START",
    "PERM_FILE_Q",
    "PERM_HEADER",
    "PERM_FOOTER",
    "MENU_OPTION",
    "MENU_YES",
    "MENU_NO",
    "PLAN_HEADER",
    "PLAN_INTRO",
    "PLAN_QUESTION",
    "PLAN_OPTS",
    "PLAN_FOOTER",
    "ASK_HEADER",
    "ASK_FOOTER",
    "ASK_CHAT",
    "TRUST_HEADER",
    "TRUST_OPTION",
    "TRUST_DECLINE",
    "TRUST_FOOTER",
    "DONE_LINE",
    "INTERRUPTED",
    "REJECTED_WRITE",
    "SPINNER",
    "TOOL_BULLET",
    "TOOL_RESULT",
    "TOOL_SUMMARY",
    "COMMAND_LINE",
    "RULE",
    "DOTTED_RULE",
    "STATUS_MODE",
    "EXIT_RESUME",
    "BANNER",
    "EFFORT_HINT",
    "UNSAFE_PHRASES",
    "MODE_WORDS",
]

# ---- regexes -------------------------------------------------------------------

INPUT_GHOST = re.compile(r"^❯ (?P<ghost>\S.*)$")

# permission prompts. The question may wrap in a narrow pane ("Do you want to
# create" / "<long path>?"), so detection joins a question's first line with its
# continuation lines before matching PERM_QUESTION / PERM_FILE_Q.
PERM_QUESTION = re.compile(r"^\s*Do you want to .*\?\s*$")
PERM_QUESTION_START = re.compile(r"^\s*Do you want to \S")
PERM_FILE_Q = re.compile(
    r"^\s*Do you want to (?P<action>create|make this edit to|overwrite|delete|write to) (?P<target>\S.*?)\?\s*$"
)
PERM_HEADER = re.compile(
    r"^ (?P<header>Bash command|Create file|Edit file|Write file|Read file|Fetch content|Run command)\s*$"
)
PERM_FOOTER = re.compile(r"^\s*Esc to cancel · Tab to amend\s*$")
PERM_TIP = re.compile(r"^\s*Tip: auto mode handles these prompts for you")
MENU_OPTION = re.compile(r"^\s*(?P<ptr>❯)?\s*(?P<n>\d{1,2})\. (?P<label>\S.*)$")
MENU_YES = re.compile(r"^\s*❯?\s*1\. Yes\b")
MENU_NO = re.compile(r"^\s*❯?\s*\d{1,2}\. No\s*$")
# Bash prompts show the command between two dotted rules, bare for short commands
# and with a leading "│ " gutter when it wraps.
COMMAND_LINE = re.compile(r"^\s*│ (?P<cmd>.*)$")

# plan approval
PLAN_HEADER = re.compile(r"^\s*Ready to code\?\s*$")
PLAN_INTRO = re.compile(r"^\s*Here is Claude's plan:\s*$")
# First sentence only: the second ("Would you like to proceed?") wraps onto the
# next line in panes narrower than 84 columns.
PLAN_QUESTION = re.compile(r"^\s*Claude has written up a plan and is ready to execute\.")
PLAN_OPTS = re.compile(
    r"^\s*❯?\s*(1\. Yes, and use auto mode|2\. Yes, manually approve edits|3\. Tell Claude what to change)\s*$"
)
PLAN_FOOTER = re.compile(r"^\s*ctrl\+g to edit in VS Code(?: ·(?: (?P<path>\S+))?)?\s*$")

# AskUserQuestion
ASK_HEADER = re.compile(r"^\s*☐ (?P<header>\S.*?)\s*$")
ASK_FOOTER = re.compile(r"^\s*Enter to select · ↑/↓ to navigate · Esc to cancel\s*$")
ASK_CHAT = re.compile(r"^\s*\d\. Chat about this\s*$")

# trust dialog (normal screen)
TRUST_HEADER = re.compile(r"^\s*Accessing workspace:\s*$")
TRUST_OPTION = re.compile(r"^\s*(?P<ptr>❯)?\s*Yes, I trust this folder\s*$")
TRUST_DECLINE = re.compile(r"^\s*(?P<ptr>❯)?\s*No, exit\s*$")
TRUST_FOOTER = re.compile(r"^\s*Enter to confirm · Esc to cancel\s*$")
TRUST_QUESTION = re.compile(r"^\s*Quick safety check: .*$")

# tool lines
TOOL_BULLET = re.compile(r"^●[  ](?P<text>\S.*)$")  # tool header OR first prose line
TOOL_RESULT = re.compile(r"^\s*⎿\s+(?P<text>\S.*)$")  # "  ⎿  text" (\s matches U+00A0)
TOOL_SUMMARY = re.compile(
    r"^\s*●?\s*(Running \d+ shell commands?…|Listed \d+ director(y|ies)|Ran \d+ shell commands?|Read \d+ files?)\s*$"
)

UNSAFE_PHRASES = (
    "always allow",
    "switch to auto mode",
    "switch to accept edits",
    "don't ask again",
    "use auto mode",
)

# status-row wording -> Claude Code permission mode name
MODE_WORDS: dict[str, str] = {
    "manual": "default",
    "accept edits": "acceptEdits",
    "plan": "plan",
    "auto": "auto",
    "bypass permissions": "bypassPermissions",
    "don't ask": "dontAsk",
    "dont ask": "dontAsk",
}


# ---- results -------------------------------------------------------------------


@dataclass(slots=True)
class PromptOption:
    index: int  # 1-based, as shown
    label: str
    selected: bool = False  # the ❯ pointer is on it
    unsafe: bool = False  # widens permissions; voice may never pick it
    description: str = ""  # AskUserQuestion sub-text / plan hint lines


@dataclass(slots=True)
class PromptMatch:
    kind: PromptKind
    title: str
    question: str
    options: list[PromptOption]
    raw_lines: list[str]
    confidence: float
    command: str | None = None  # Bash prompts: the │-prefixed command text
    target_file: str | None = None  # file prompts: the file named in the question
    header: str = ""  # "Bash command", "Create file", the ☐ header, "Ready to code?"
    description: str = ""  # Bash prompts: Claude's one-line description of the command
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        return [o.label for o in self.options]

    @property
    def selected(self) -> PromptOption | None:
        return next((o for o in self.options if o.selected), None)

    def option(self, index: int) -> PromptOption | None:
        return next((o for o in self.options if o.index == index), None)


# ---- detection -----------------------------------------------------------------


def detect_prompt(lines: Sequence[str] | Screen) -> PromptMatch | None:
    """Classify the bottom of the pane. ``lines`` are plain capture lines or a Screen.

    The TUI hides the input box while any menu is up, so a visible input box
    means there is no prompt, whatever Claude's prose above it looks like.
    """
    scr = _as_screen(lines)
    if scr.input_box is not None:
        return None
    block = scr.prompt_block if scr.prompt_block else scr.lines
    return _detect_trust(block) or _detect_plan(block) or _detect_ask(block) or _detect_permission(block)


def _as_screen(lines: Sequence[str] | Screen) -> Screen:
    if isinstance(lines, Screen):
        return lines
    return parse_screen(list(lines))


def _detect_trust(block: list[str]) -> PromptMatch | None:
    opt_i = _find(block, TRUST_OPTION)
    footer_i = _find(block, TRUST_FOOTER)
    if opt_i is None or footer_i is None:
        return None
    decline_i = _find(block, TRUST_DECLINE)
    header_i = _find(block, TRUST_HEADER)
    path = ""
    if header_i is not None:
        nxt = _next_nonblank(block, header_i + 1)
        if nxt is not None:
            path = block[nxt].strip()
    options: list[PromptOption] = []
    if decline_i is not None:
        m = TRUST_DECLINE.match(block[decline_i])
        options.append(PromptOption(1, "No, exit", selected=bool(m and m.group("ptr"))))
    m = TRUST_OPTION.match(block[opt_i])
    options.append(
        PromptOption(len(options) + 1, "Yes, I trust this folder", selected=bool(m and m.group("ptr")))
    )
    q_i = _find(block, TRUST_QUESTION)
    question = block[q_i].strip() if q_i is not None else ""
    confidence = 1.0 if (header_i is not None and decline_i is not None) else 0.8
    start = _block_start(block, header_i if header_i is not None else opt_i)
    if not _at_bottom(block, footer_i):
        return None
    return PromptMatch(
        kind=PromptKind.TRUST,
        title=f"Trust this folder: {path}" if path else "Trust this folder",
        question=question,
        options=options,
        raw_lines=block[start : footer_i + 1],
        confidence=confidence,
        header="Accessing workspace:",
        extra={"path": path} if path else {},
    )


def _detect_plan(block: list[str]) -> PromptMatch | None:
    q_i = _find(block, PLAN_QUESTION)
    if q_i is None or _find(block, PLAN_OPTS) is None:
        return None
    q_end = _question_end(block, q_i)
    options = _menu_options(block, q_end + 1, stop=lambda s: bool(RULE.match(s) or PLAN_FOOTER.match(s)))
    if not options:
        return None
    header_i = _find(block, PLAN_HEADER)
    first_plan_line = _first_plan_line(block)
    title = f"Plan ready: {first_plan_line}" if first_plan_line else "Plan ready"
    confidence = 1.0 if header_i is not None and len(options) >= 3 else 0.8
    start = _block_start(block, header_i if header_i is not None else q_i)
    end = _last_option_end(block, options, q_i)
    extra: dict[str, str] = {}
    if first_plan_line:
        extra["first_plan_line"] = first_plan_line
    footer_i = _find(block, PLAN_FOOTER, start=q_i)
    if footer_i is not None:
        end = _wrapped_end(block, footer_i)
        fm = PLAN_FOOTER.match(block[footer_i])
        if fm and fm.group("path"):
            extra["plan_file"] = fm.group("path")
        elif end > footer_i and block[footer_i].rstrip().endswith("·"):
            extra["plan_file"] = _joined(block, footer_i + 1, end)  # the path wrapped onto the next line
    if not _at_bottom(block, end):
        return None
    return PromptMatch(
        kind=PromptKind.PLAN,
        title=title,
        question=_joined(block, q_i, q_end),
        options=options,
        raw_lines=block[start : end + 1],
        confidence=confidence,
        header="Ready to code?" if header_i is not None else "",
        extra=extra,
    )


def _first_plan_line(block: list[str]) -> str:
    intro_i = _find(block, PLAN_INTRO)
    if intro_i is None:
        return ""
    for j in range(intro_i + 1, len(block)):
        line = block[j]
        if not line.strip() or DOTTED_RULE.match(line) or RULE.match(line):
            continue
        return line.strip()
    return ""


def _detect_ask(block: list[str]) -> PromptMatch | None:
    footer_i = _find(block, ASK_FOOTER)
    chat_i = _find(block, ASK_CHAT)
    if footer_i is None or chat_i is None:
        return None
    header_i = _find(block, ASK_HEADER)
    header = ""
    if header_i is not None:
        hm = ASK_HEADER.match(block[header_i])
        header = hm.group("header").strip() if hm else ""
    # The question is the first non-blank, non-option, non-rule line after the header
    # (or, without a header, the last such line before the first option).
    first_opt = _find(block, MENU_OPTION, start=(header_i or 0) + 1)
    question = ""
    scan_from = header_i + 1 if header_i is not None else 0
    for j in range(scan_from, first_opt if first_opt is not None else footer_i):
        line = block[j]
        if line.strip() and not RULE.match(line) and not MENU_OPTION.match(line):
            question = line.strip()
            if header_i is not None:
                break
    options = _menu_options(block, (first_opt if first_opt is not None else scan_from), stop=lambda s: bool(ASK_FOOTER.match(s)))
    if not options:
        return None
    confidence = 1.0 if header_i is not None and question else 0.8
    start = _block_start(block, header_i if header_i is not None else (first_opt or 0))
    if not _at_bottom(block, footer_i):
        return None
    return PromptMatch(
        kind=PromptKind.QUESTION,
        title=question or header or "Claude has a question",
        question=question,
        options=options,
        raw_lines=block[start : footer_i + 1],
        confidence=confidence,
        header=header,
    )


def _detect_permission(block: list[str]) -> PromptMatch | None:
    """The permission menu at the bottom: the LAST ``1. Yes`` and the question above it.

    Anchoring on the last menu keeps a prose "Do you want to …?" (with or without
    a numbered list) above the real prompt out of the card. A menu with neither
    a known header nor the footer is not a prompt: that shape is Claude's prose.
    """
    yes_i = _rfind(block, MENU_YES)
    if yes_i is None:
        return None
    q_i = _rfind(block, PERM_QUESTION_START, end=yes_i)
    if q_i is None:
        return None
    q_end = _question_end(block, q_i, limit=yes_i)
    if _next_nonblank(block, q_end + 1) != yes_i:
        return None  # the question does not lead straight into the menu
    footer_i = _find(block, PERM_FOOTER, start=yes_i)
    options = _menu_options(block, yes_i, stop=lambda s: bool(PERM_FOOTER.match(s)))
    if not options:
        return None
    header_i = _rfind(block, PERM_HEADER, end=q_i)
    if header_i is not None and _find(block, MENU_YES, start=header_i, end=q_i) is not None:
        header_i = None  # that header belongs to an older menu above this one
    if header_i is None and footer_i is None:
        return None
    header = ""
    if header_i is not None:
        hm = PERM_HEADER.match(block[header_i])
        header = hm.group("header") if hm else ""
    question = _joined(block, q_i, q_end)
    command = _command_text(block, header_i if header_i is not None else 0, q_i, bash=header == "Bash command")
    target_file: str | None = None
    fm = PERM_FILE_Q.match(question)
    if fm:
        target_file = fm.group("target").strip()
    elif header and header != "Bash command" and header_i is not None:
        nxt = _next_nonblank(block, header_i + 1)
        if nxt is not None and nxt < q_i and not DOTTED_RULE.match(block[nxt]):
            target_file = block[nxt].strip()
    description = ""
    if header == "Bash command" and header_i is not None:
        first_dotted = _find(block, DOTTED_RULE, start=header_i + 1, end=q_i)
        for j in range(header_i + 1, first_dotted if first_dotted is not None else q_i):
            line = block[j]
            if line.strip() and not PERM_TIP.match(line) and not COMMAND_LINE.match(line):
                description = line.strip()
                break
    if header == "Bash command" and command:
        title = f"Bash command: {command}"
    elif header == "Bash command" and description:
        title = f"Bash command: {description}"
    elif header and target_file:
        title = f"{header} {target_file}"
    elif header:
        title = f"{header}: {question}"
    else:
        title = question
    confidence = 1.0 if footer_i is not None and header_i is not None else 0.7
    start = _block_start(block, header_i if header_i is not None else q_i)
    end = footer_i if footer_i is not None else _last_option_end(block, options, q_i)
    if not _at_bottom(block, end):
        return None
    return PromptMatch(
        kind=PromptKind.PERMISSION,
        title=title,
        question=question,
        options=options,
        raw_lines=block[start : end + 1],
        confidence=confidence,
        command=command or None,
        target_file=target_file,
        header=header,
        description=description,
    )


def _command_text(block: list[str], start: int, end: int, *, bash: bool = False) -> str:
    """The command shown in a Bash prompt: every non-blank line between the two
    dotted rules (an optional leading "│ " gutter stripped); without dotted
    rules, the "│ "-prefixed lines in the range."""
    if bash:
        first = _find(block, DOTTED_RULE, start=start, end=end)
        if first is not None:
            second = _find(block, DOTTED_RULE, start=first + 1, end=end)
            stop = second if second is not None else end
            parts = []
            for j in range(first + 1, stop):
                line = block[j]
                m = COMMAND_LINE.match(line)
                parts.append((m.group("cmd") if m else line).strip())
            return " ".join(p for p in parts if p).strip()
    parts = [m.group("cmd").rstrip() for m in (COMMAND_LINE.match(block[j]) for j in range(start, end)) if m]
    return " ".join(p for p in parts if p).strip()


def _question_end(block: list[str], q_i: int, limit: int | None = None) -> int:
    """Last line of a question that may have wrapped: continuation lines are the
    non-blank, non-option, non-rule lines directly below it (at most three)."""
    end = q_i
    stop = len(block) if limit is None else min(limit, len(block))
    for j in range(q_i + 1, min(stop, q_i + 4)):
        line = block[j]
        if not line.strip() or MENU_OPTION.match(line) or RULE.match(line) or DOTTED_RULE.match(line):
            break
        if "?" in block[end]:
            break
        end = j
    return end


def _wrapped_end(block: list[str], i: int) -> int:
    """Last line of a footer that wrapped: continuation lines are the non-blank,
    non-option, non-rule lines directly below it indented at least as far (Ink
    keeps the padding on wrapped lines)."""
    indent = len(block[i]) - len(block[i].lstrip())
    end = i
    for j in range(i + 1, min(len(block), i + 4)):
        line = block[j]
        if not line.strip() or len(line) - len(line.lstrip()) < indent or MENU_OPTION.match(line) or RULE.match(line):
            break
        end = j
    return end


def _joined(block: list[str], start: int, end: int) -> str:
    return " ".join(block[j].strip() for j in range(start, end + 1) if block[j].strip())


def _menu_options(block: list[str], start: int, stop) -> list[PromptOption]:
    """Numbered options from ``start`` until ``stop(line)`` is true.

    The menu ends at the first blank line after an option or at a non-option line
    that is not indented under the option's label (a rule between options is
    skipped: AskUserQuestion separates "Chat about this" that way). Indexes must
    run 1, 2, 3 … without gaps or repeats; anything else is not one menu and
    yields no options.
    """
    options: list[PromptOption] = []
    label_col = 0
    for j in range(start, len(block)):
        line = block[j]
        if stop(line) and options:
            break
        m = MENU_OPTION.match(line)
        if m:
            label = m.group("label").strip()
            index = int(m.group("n"))
            if index != len(options) + 1:
                return []  # duplicate or non-consecutive index: two lists merged
            label_col = m.start("label")
            options.append(
                PromptOption(index=index, label=label, selected=bool(m.group("ptr")), unsafe=is_unsafe_label(label))
            )
            continue
        if not options:
            continue
        if not line.strip():
            break
        if RULE.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        if indent < label_col:
            break
        opt = options[-1]
        opt.description = (opt.description + " " + line.strip()).strip()
    return options


def _last_option_end(block: list[str], options: list[PromptOption], q_i: int) -> int:
    last = q_i
    for j in range(q_i + 1, len(block)):
        if MENU_OPTION.match(block[j]):
            last = j
    return last


def _at_bottom(block: list[str], end: int) -> bool:
    """A live prompt is the last thing on screen: only blank lines may follow ``end``.

    A stale block (one already answered, with Claude's output below it) is not a
    prompt even when every regex matches.
    """
    return all(not line.strip() for line in block[end + 1 :])


def _block_start(block: list[str], anchor: int) -> int:
    """The rule line directly above ``anchor`` when there is one, else ``anchor``."""
    j = anchor - 1
    while j >= 0 and not block[j].strip():
        j -= 1
    if j >= 0 and RULE.match(block[j]):
        return j
    return anchor


def _find(block: list[str], pat: re.Pattern[str], start: int = 0, end: int | None = None) -> int | None:
    stop = len(block) if end is None else min(end, len(block))
    for i in range(start, stop):
        if pat.match(block[i]):
            return i
    return None


def _rfind(block: list[str], pat: re.Pattern[str], start: int = 0, end: int | None = None) -> int | None:
    stop = len(block) if end is None else min(end, len(block))
    for i in range(stop - 1, start - 1, -1):
        if pat.match(block[i]):
            return i
    return None


def _next_nonblank(block: list[str], start: int) -> int | None:
    for j in range(start, len(block)):
        if block[j].strip():
            return j
    return None


def is_unsafe_label(label: str) -> bool:
    low = label.lower()
    return any(p in low for p in UNSAFE_PHRASES)


# ---- option helpers (what the manager keys on) -----------------------------------


def yes_option(match: PromptMatch) -> int | None:
    """Index of the option labelled exactly ``Yes``; never a widening variant."""
    return next((o.index for o in match.options if o.label == "Yes"), None)


def no_option(match: PromptMatch) -> int | None:
    """Index of the option labelled exactly ``No`` (the last numbered option)."""
    return next((o.index for o in match.options if o.label == "No"), None)


def plan_manual_option(match: PromptMatch) -> int | None:
    return next((o.index for o in match.options if o.label == "Yes, manually approve edits"), None)


def plan_revise_option(match: PromptMatch) -> int | None:
    return next((o.index for o in match.options if o.label == "Tell Claude what to change"), None)


def plan_auto_option(match: PromptMatch) -> int | None:
    """The unsafe plan option; exposed so callers can assert they never pick it."""
    return next((o.index for o in match.options if o.label == "Yes, and use auto mode"), None)


def question_option(match: PromptMatch, choice: int | str) -> int | None:
    """AskUserQuestion: resolve a 1-based number or a label (case-insensitive)."""
    if isinstance(choice, int):
        return choice if match.option(choice) is not None else None
    want = choice.strip().lower().rstrip(".")
    for o in match.options:
        if o.label.lower().rstrip(".") == want:
            return o.index
    return None


# ---- state primitives ---------------------------------------------------------------


def has_spinner(lines: Sequence[str] | Screen) -> bool:
    return _as_screen(lines).spinner is not None


def has_interrupt_hint(lines: Sequence[str] | Screen) -> bool:
    return _as_screen(lines).interrupt_hint


def turn_ended(lines: Sequence[str] | Screen) -> bool:
    """The last content line is a completion, interruption or rejection line."""
    return _as_screen(lines).turn_ended


def is_idle_prompt(lines: Sequence[str] | Screen) -> bool:
    """Input box visible, no spinner, and the last turn is finished (or the idle
    hint slot above the input rule is occupied, or nothing has been said yet)."""
    scr = _as_screen(lines)
    if not input_quiet(scr):
        return False
    return scr.turn_ended or scr.idle_hint or scr.effort_hint or not scr.content


def input_quiet(lines: Sequence[str] | Screen) -> bool:
    """Input box visible with no spinner and no "esc to interrupt".

    Weaker than ``is_idle_prompt``: it does not need a completion row, which is
    absent with ``showTurnDuration: false``. A caller that sees this hold while
    ``Screen.content_key`` stays unchanged over a few polls may treat the session
    as idle.
    """
    scr = _as_screen(lines)
    return scr.input_box is not None and scr.spinner is None and not scr.interrupt_hint


def is_working(lines: Sequence[str] | Screen) -> bool:
    """Spinner or 'esc to interrupt' visible. Not sufficient on its own: a streaming
    answer can show neither (``working_no_spinner.txt``); the manager also counts
    content advancing (``Screen.content_key`` changing)."""
    scr = _as_screen(lines)
    return scr.spinner is not None or scr.interrupt_hint


def permission_mode_from_screen(lines: Sequence[str] | Screen) -> str | None:
    """Claude Code mode name from the ``⏸ <mode> mode on`` / ``⏵⏵ <mode> on`` status row, or None."""
    scr = _as_screen(lines)
    if scr.status_mode is None:
        return None
    return MODE_WORDS.get(scr.status_mode)


def exited(lines: Sequence[str] | Screen) -> bool:
    return _as_screen(lines).exited


def tail(lines: Sequence[str], n: int = 25) -> list[str]:
    """The last ``n`` lines, the window prompt detection is meant to look at."""
    return list(lines)[-n:]


_ = _screen  # keep the module reference for callers that want screen.* via prompts


# ---- first-run onboarding (normal screen, before the TUI) ------------------------------------

ONBOARDING_THEME = re.compile(r"^\s*Choose the text style that looks best with your terminal")
ONBOARDING_LOGIN = re.compile(r"^\s*Select login method:")
ONBOARDING_BROWSER = re.compile(r"Browser didn't open\? Use the url below|^\s*Paste code here if prompted")
ONBOARDING_WELCOME = re.compile(r"^\s*Welcome to Claude Code v\d")


def detect_onboarding(lines: Sequence[str] | Screen) -> str | None:
    """Claude Code's first-run screens: ``"theme"`` (text style picker; Enter accepts the
    default), ``"login"`` (login method menu) or ``"login_browser"`` (sign-in URL / code
    paste). None otherwise. These are drawn on the normal screen, so without this a
    not-yet-logged-in Claude Code looks like a crashed one."""
    scr = _as_screen(lines)
    text = [strip_ansi(ln) for ln in scr.lines]
    if any(ONBOARDING_BROWSER.search(ln) for ln in text):
        return "login_browser"
    if any(ONBOARDING_LOGIN.match(ln) for ln in text):
        return "login"
    if any(ONBOARDING_THEME.match(ln) for ln in text):
        return "theme"
    return None
