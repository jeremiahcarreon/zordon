"""Pure functions over ``tmux capture-pane`` output. No I/O, no threads.

A Claude Code pane (2.1.287, alternate screen, 160x45) has this anatomy:

    banner (3 lines) + startup notices              UI, dropped
    ❯ <echo of a sent message>                      content
    ● <tool call header> / ● <first prose line>     content
      <continuation lines, 2-space indent>          content
    ✻ Worked for 4s · done 8:33 PM                  content (turn terminator)
    <glyph> Thinking… (3s · ↓ 77 tokens)            spinner, masked
                                 ◐ medium · /effort UI hint, dropped
    ────────────────────────────                    input-box rule
    ❯<NBSP><ghost or typed text>                    input box
    ────────────────────────────                    rule
      [status line] / ⏸ manual mode on …            status rows

When a permission, plan, question or trust prompt is up the input box is gone
and a prompt block (rule, header, question, numbered options, footer) sits at
the bottom instead. ``parse_screen`` splits a capture into those regions and
``diff_screens`` turns two consecutive captures into the content lines that
appeared between them.

Facts this module relies on (verified in ``eval/fixtures/pane``):

* the idle input line is ``❯`` + U+00A0, while a transcript echo is ``❯`` + U+0020;
  trailing spaces must be stripped with ``rstrip(" ")`` so the NBSP survives;
* the spinner glyph cycles ``· ✢ * ✶ ✻ ✽`` every poll and the active tool call's
  ``●`` bullet blinks (alternates with a blank), so both are masked before any
  comparison;
* content lands in whole paragraphs, but a line can appear partially rendered
  for one frame (``  -`` then ``  - ~/.config/tmux/tmux.conf``), so the last
  content line is held back while the session is still rendering;
* the alternate screen has no scrollback: once it fills, content scrolls off
  the top, which the differ handles by aligning the previous content inside the
  new one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

SCREEN_FORMAT_VERSION = "claude-code-2.1.287"

NBSP = " "

# ---- ANSI ------------------------------------------------------------------

_CSI = r"\x1b\[[0-?]*[ -/]*[@-~]"
# OSC (incl. OSC 8 hyperlinks) terminated by BEL or ST; tolerate a missing terminator.
_OSC = r"\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?"
_CHARSET = r"\x1b[()*+\-./][0-9A-Za-z@<>%]"
_ESC_SINGLE = r"\x1b[@-Z\\-_=>78]"
_ANSI = re.compile(f"{_CSI}|{_OSC}|{_CHARSET}|{_ESC_SINGLE}")
_C0 = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove CSI/SGR, OSC (hyperlinks, titles), charset selects and other escapes.

    Keeps ``\\n`` and ``\\t``; drops every other C0 control character.
    """
    if "\x1b" not in text and not _C0.search(text):
        return text
    return _C0.sub("", _ANSI.sub("", text))


def normalize_line(line: str) -> str:
    """One captured line, escapes removed, trailing U+0020 removed, NBSP kept."""
    return strip_ansi(line).rstrip(" ")


def display_line(line: str) -> str:
    """Display form: NBSP becomes a space and all trailing whitespace goes."""
    return normalize_line(line).replace(NBSP, " ").rstrip()


# ---- structural regexes ------------------------------------------------------

# Idle/typing input line: "❯" + NBSP + ghost or typed text (NBSP distinguishes it
# from the transcript echo "❯ text", which uses U+0020).
INPUT_BOX = re.compile(r"^❯(?: (?P<text>.*))?$")
USER_ECHO = re.compile(r"^❯ (?P<text>\S.*)$")
RULE = re.compile(r"^\s*─{20,}\s*$")
DOTTED_RULE = re.compile(r"^\s*╌{20,}\s*$")
TOP_RULE = re.compile(r"^\s*▔{20,}\s*$")
SPINNER = re.compile(
    r"^(?P<glyph>[·✢*✶✻✽]) (?P<verb>[A-Z][a-z]+)…"
    r"(?: \((?P<secs>\d+)s(?: · ↓ (?P<tokens>[\d.]+k?) tokens)?(?: · (?P<extra>[^)]*))?\))?\s*$"
)
SPINNER_TIP = re.compile(r"^\s*⎿\s+Tip: ")
DONE_LINE = re.compile(
    r"^✻ (?P<verb>[A-Z][a-zé]+) for (?P<dur>\d+(?:m \d+)?s|\d+m) · done "
    r"(?P<clock>\d{1,2}:\d{2} [AP]M)\s*$"
)
INTERRUPTED = re.compile(r"^\s*⎿\s+Interrupted · What should Claude do instead\?\s*$")
REJECTED_WRITE = re.compile(r"^\s*⎿\s+User rejected (?P<action>write|edit|update) to (?P<file>\S+)\s*$")
STATUS_MODE = re.compile(
    r"^\s*⏸ (?P<mode>manual|plan|accept edits|auto|bypass permissions|don't ask|dont ask) mode on\b"
)
EXIT_RESUME = re.compile(
    r"^claude --resume (?P<sid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$"
)
EFFORT_HINT = re.compile(r"^\s*◐ \S+ · /effort\s*$")
BANNER = re.compile(r"^\s*[▐▛█▝▜▀]")
NOTICE = re.compile(
    r"^\s{1,3}(?:Updated to latest\. Got \d+ features|code\.claude\.com/docs/en/changelog|"
    r"Get to finished work sooner with )"
)
LONE_BULLET = re.compile(r"^●\s*$")
ESC_TO_INTERRUPT = re.compile(r"esc to interrupt", re.IGNORECASE)
# A notification popup overlays the right edge with a close glyph; the "/plan to
# preview" bar is one such popup.
POPUP_CLOSE = re.compile(r"\s{4,}✕\s*$")
POPUP_BAR = re.compile(r"^\s*⎿\s+/plan to preview\s*$")
GHOST_TEXT = re.compile(r'^Try ".*"$')

# Prompt-block headers: a block starts at the rule directly above one of these.
PROMPT_HEADERS = (
    re.compile(r"^ (Bash command|Create file|Edit file|Write file|Read file|Fetch content|Run command)\s*$"),
    re.compile(r"^\s*Ready to code\?\s*$"),
    re.compile(r"^\s*☐ \S"),
    re.compile(r"^\s*Accessing workspace:\s*$"),
)
# Prompt-block anchors: lines that only exist inside a prompt block.
PROMPT_ANCHORS = (
    re.compile(r"^\s*Do you want to .*\?\s*$"),
    re.compile(r"^\s*Claude has written up a plan and is ready to execute\."),
    re.compile(r"^\s*Enter to select · ↑/↓ to navigate · Esc to cancel\s*$"),
    re.compile(r"^\s*Enter to confirm · Esc to cancel\s*$"),
    re.compile(r"^\s*Esc to cancel · Tab to amend\s*$"),
)

_BLINK = re.compile(r"^●(?=[  ]|$)")


# ---- dataclasses -------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InputBox:
    text: str  # ghost suggestion or typed-but-unsent text; indistinguishable in plain capture

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def looks_like_ghost(self) -> bool:
        """The dim suggestion shape (``Try "..."``). Other ghosts exist and look typed."""
        return bool(GHOST_TEXT.match(self.text))


@dataclass(slots=True, frozen=True)
class Spinner:
    glyph: str
    verb: str
    secs: int | None
    tokens: str | None
    extra: str | None
    raw: str


@dataclass(slots=True, frozen=True)
class DoneLine:
    verb: str
    duration: str  # "4s", "1m 12s", "3m"
    clock: str  # "8:33 PM"
    raw: str

    @property
    def secs(self) -> int:
        m = re.fullmatch(r"(?:(\d+)m)?\s*(?:(\d+)s)?", self.duration)
        if not m:
            return 0
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


@dataclass(slots=True)
class Screen:
    """One parsed capture. ``lines`` are the normalized raw lines (NBSP kept)."""

    lines: list[str]
    content: list[str]  # conversation lines above the input box / prompt block, UI removed
    input_box: InputBox | None
    spinner: Spinner | None
    status_mode: str | None  # raw status-row word: manual | plan | accept edits | auto | ...
    status_rows: list[str]
    done_line: DoneLine | None  # the last completion line visible in content
    turn_ended: bool  # the last content line is a done/interrupted/rejected line (or content is empty)
    exited: bool  # "claude --resume <id>" visible and no input box: the TUI has left
    prompt_block: list[str]  # lines from the prompt's opening rule to the bottom, else []
    content_key: str  # stable hash of content with spinner and blink glyphs masked
    interrupt_hint: bool = False  # "esc to interrupt" somewhere on screen
    effort_hint: bool = False  # the right-aligned "◐ <effort> · /effort" idle hint
    user_echoes: list[str] = field(default_factory=list)

    @property
    def last_content_line(self) -> str | None:
        for line in reversed(self.content):
            if line.strip():
                return line
        return None


@dataclass(slots=True)
class ScreenDiff:
    new_lines: list[str]
    live_region: list[str]  # spinner line(s) and the held tail, for the client
    changed: bool


# ---- parsing -------------------------------------------------------------------


def parse_screen(lines: Sequence[str], *, ansi: bool = False) -> Screen:
    """Split one capture into content, input box, spinner, status rows and prompt block."""
    raw = [normalize_line(line) if ansi else line.rstrip(" ") for line in lines]
    box_idx = _find_input_box(raw)
    input_box: InputBox | None = None
    status_rows: list[str] = []
    prompt_block: list[str] = []

    if box_idx is not None:
        m = INPUT_BOX.match(raw[box_idx])
        input_box = InputBox(text=(m.group("text") if m else "") or "")
        content_end = box_idx - 1 if box_idx > 0 and RULE.match(raw[box_idx - 1]) else box_idx
        status_rows = _status_rows(raw, box_idx)
    else:
        block_start = _find_prompt_block(raw)
        if block_start is not None:
            content_end = block_start
            prompt_block = raw[block_start:]
        else:
            content_end = len(raw)

    content, spinner = _extract_content(raw[:content_end])
    done = _last_done_line(content)
    last = _last_nonblank(content)
    turn_ended = last is None or _is_terminator(last)
    exited = input_box is None and any(EXIT_RESUME.match(line) for line in raw)
    status_mode = None
    for row in status_rows:
        sm = STATUS_MODE.match(row)
        if sm:
            status_mode = sm.group("mode")
            break
    key_src = "\n".join(blink_key(line) for line in content)
    return Screen(
        lines=raw,
        content=content,
        input_box=input_box,
        spinner=spinner,
        status_mode=status_mode,
        status_rows=status_rows,
        done_line=done,
        turn_ended=turn_ended,
        exited=exited,
        prompt_block=prompt_block,
        content_key=hashlib.sha1(key_src.encode("utf-8")).hexdigest(),
        interrupt_hint=any(ESC_TO_INTERRUPT.search(line) for line in raw),
        effort_hint=any(EFFORT_HINT.match(line) for line in raw[:content_end]),
        user_echoes=[m.group("text") for m in map(USER_ECHO.match, content) if m],
    )


def parse_capture(text: str, *, ansi: bool = False) -> Screen:
    """Convenience for a whole capture as one string."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return parse_screen(lines, ansi=ansi)


def _find_input_box(raw: list[str]) -> int | None:
    for i in range(len(raw) - 1, -1, -1):
        if INPUT_BOX.match(raw[i]) and i > 0 and RULE.match(raw[i - 1]):
            return i
    return None


def _status_rows(raw: list[str], box_idx: int) -> list[str]:
    for j in range(box_idx + 1, min(len(raw), box_idx + 6)):
        if RULE.match(raw[j]):
            return [line for line in raw[j + 1 :] if line.strip()]
    return []


def _find_prompt_block(raw: list[str]) -> int | None:
    """Index of the rule that opens a prompt block, or None."""
    anchor = next((i for i, line in enumerate(raw) if any(p.match(line) for p in PROMPT_ANCHORS)), None)
    if anchor is None:
        return None
    last_rule: int | None = None
    for i in range(anchor):
        if RULE.match(raw[i]):
            last_rule = i
            nxt = _next_nonblank(raw, i + 1)
            if nxt is not None and any(h.match(raw[nxt]) for h in PROMPT_HEADERS):
                return i
    if last_rule is not None:
        return last_rule
    return anchor


def _next_nonblank(raw: list[str], start: int) -> int | None:
    for j in range(start, len(raw)):
        if raw[j].strip():
            return j
    return None


def _extract_content(region: list[str]) -> tuple[list[str], Spinner | None]:
    spinner: Spinner | None = None
    out: list[str] = []
    after_spinner = False
    for line in region:
        line = POPUP_CLOSE.sub("", line)
        sm = SPINNER.match(line)
        if sm:
            spinner = Spinner(
                glyph=sm.group("glyph"),
                verb=sm.group("verb"),
                secs=int(sm.group("secs")) if sm.group("secs") else None,
                tokens=sm.group("tokens"),
                extra=sm.group("extra"),
                raw=line,
            )
            after_spinner = True
            out.append("")
            continue
        if after_spinner and SPINNER_TIP.match(line):
            out.append("")
            continue
        if line.strip():
            after_spinner = False
        if is_ui_line(line):
            out.append("")
            continue
        out.append(line)
    return _collapse_blanks(out), spinner


def is_ui_line(line: str) -> bool:
    """Banner, startup notices, rules, hints and empty bullets: never content."""
    return bool(
        BANNER.match(line)
        or NOTICE.match(line)
        or EFFORT_HINT.match(line)
        or RULE.match(line)
        or TOP_RULE.match(line)
        or LONE_BULLET.match(line)
        or POPUP_BAR.match(line)
    )


def _collapse_blanks(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and out[-1] == "":
                continue
            if not out:
                continue
            out.append("")
        else:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def _last_nonblank(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if line.strip():
            return line
    return None


def _last_done_line(content: list[str]) -> DoneLine | None:
    for line in reversed(content):
        m = DONE_LINE.match(line)
        if m:
            return DoneLine(verb=m.group("verb"), duration=m.group("dur"), clock=m.group("clock"), raw=line)
    return None


def _is_terminator(line: str) -> bool:
    return bool(DONE_LINE.match(line) or INTERRUPTED.match(line) or REJECTED_WRITE.match(line))


def is_terminator(line: str) -> bool:
    """A done / interrupted / rejected line: the turn is over at this line."""
    return _is_terminator(line)


# ---- diffing -------------------------------------------------------------------


def blink_key(line: str) -> str:
    """Comparison form of a content line: the blinking leading ``●`` counts as blank."""
    return _BLINK.sub(" ", line)


def held_tail(screen: Screen) -> list[str]:
    """Content lines at the bottom that may still be mid-render and are not emitted yet.

    While the session is rendering (spinner visible, or the input box is back
    but the last line is not a turn terminator) the final content line is held
    for one more poll so a partially drawn line is never spoken. The manager
    flushes it through this function when the watchdog fires.
    """
    if not screen.content or screen.turn_ended:
        return []
    if screen.spinner is None and screen.input_box is None:
        return []  # prompt up or normal screen: nothing is being rendered
    return screen.content[-1:]


def emitted_content(screen: Screen) -> list[str]:
    """The content lines that ``diff_screens`` treats as already emitted for this screen."""
    held = len(held_tail(screen))
    return screen.content[:-held] if held else list(screen.content)


def diff_screens(prev: Screen | None, curr: Screen) -> list[str]:
    """New content lines in ``curr`` that were not in ``prev``.

    Never returns input box, rules, status rows, banner or spinner lines; a line
    whose only change is the blink glyph is not new; the possibly partial last
    line is held back (see ``held_tail``). Lines that scrolled off the top are
    not re-emitted; lines replaced in place (a tool call's lifecycle text) are
    emitted in each form they were seen in, because each was really on screen.

    The caller should ignore diffs while the pane is on the normal screen
    (``Tmux.alternate_on`` is False): the shell, the trust dialog and startup
    warnings are not Claude Code output.
    """
    if curr.exited:
        return []  # the normal screen after /exit is shell output, not Claude Code's
    curr_lines = list(curr.content)
    if prev is None:
        new = curr_lines
    else:
        base = [blink_key(line) for line in emitted_content(prev)]
        keys = [blink_key(line) for line in curr_lines]
        matched = align(base, keys)
        new = curr_lines[matched:]
    held = len(held_tail(curr))
    if held and new:
        new = new[:-held]
    if not any(line.strip() for line in new):
        return []
    return new


def align(prev: list[str], curr: list[str]) -> int:
    """How many leading lines of ``curr`` were already present in ``prev``.

    Tries every scroll offset ``s`` (the top ``s`` lines of ``prev`` scrolled off)
    and keeps the one whose remainder shares the longest prefix with ``curr``.
    Trailing lines of ``prev`` that were replaced in place (tool-call lifecycle,
    partial renders) simply fail to match and are re-emitted in their new form.
    """
    if not prev or not curr:
        return 0
    best = 0
    for s in range(len(prev)):
        m = _common_prefix(prev, s, curr)
        if m > best:
            best = m
            if best == len(curr):
                break
    return best


def _common_prefix(prev: list[str], start: int, curr: list[str]) -> int:
    n = min(len(prev) - start, len(curr))
    i = 0
    while i < n and prev[start + i] == curr[i]:
        i += 1
    return i


def diff_captures(
    prev: Sequence[str] | None, curr: Sequence[str], live_region_hint: int = 0
) -> ScreenDiff:
    """Line-list convenience around ``parse_screen`` + ``diff_screens``.

    ``live_region_hint`` is accepted for signature compatibility and ignored: the
    live region (spinner and held tail) is found by content, not by position.
    """
    prev_screen = parse_screen(prev) if prev is not None else None
    curr_screen = parse_screen(curr)
    new = diff_screens(prev_screen, curr_screen)
    live = ([curr_screen.spinner.raw] if curr_screen.spinner else []) + held_tail(curr_screen)
    changed = prev_screen is None or prev_screen.content_key != curr_screen.content_key
    return ScreenDiff(new_lines=new, live_region=live, changed=changed)


def split_frames(text: str) -> list[tuple[int, int, list[str]]]:
    """Parse a ``=====FRAME N t=<ms>=====`` fixture file into (n, t_ms, lines)."""
    parts = re.split(r"^=====FRAME (\d+) t=(\d+)=====\n", text, flags=re.M)
    frames: list[tuple[int, int, list[str]]] = []
    for i in range(1, len(parts), 3):
        body = parts[i + 2]
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        frames.append((int(parts[i]), int(parts[i + 1]), lines))
    return frames
