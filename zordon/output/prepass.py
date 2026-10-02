"""Deterministic pre-pass: classify and collapse Claude Code output before any
model sees it. Pure functions over strings; no I/O, no threads.

Two inputs, one output type:

* ``prepass_pane_line`` takes one rendered terminal line (``tmux capture-pane``)
  and the per-session ``PrepassState``. It strips ANSI, drops TUI chrome
  (banner, rules, input box, status rows, spinners, prompt menus), recognises
  tool lifecycle lines, and groups consecutive code or diff lines into one
  placeholder. It may return zero items (a line absorbed into a block) or
  several (a block closed by this line, plus the line itself).
* ``prepass_markdown`` takes a complete markdown text block from the session
  jsonl. Fenced code, tables, headings, lists, quotes, inline markup, links,
  URLs and paths are reduced to what a listener needs to hear.

Every item is a ``Tagged``: ``kind`` drives the verbosity filter, ``text`` is
what the client shows, ``spoken`` is what the TTS says (``None`` = nothing),
``raw`` is the source line and ``meta`` carries hints for the pipeline
(``touches_file``, ``list_item``, ``marker``, ``literal`` ...).

Facts about the TUI rendering come from the probe of Claude Code 2.1.287 in
``eval/fixtures/pane``: rendered code blocks carry no fences and no colour, so
code detection from pane text is heuristic; the done line
``✻ Worked for 4s · done 8:33 PM`` is the turn boundary; ``  ⎿  `` prefixes tool
detail lines; the input box is ``❯`` followed by U+00A0 while a sent message is
``❯`` followed by U+0020.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from zordon.bus import LineKind
from zordon.output.acronyms import expand_acronyms, expand_symbols
from zordon.output.sentences import split_sentences

log = logging.getLogger("zordon.output.prepass")

__all__ = [
    "Tagged",
    "PrepassState",
    "prepass_pane_line",
    "prepass_line",
    "prepass_markdown",
    "flush_prepass",
    "strip_ansi",
    "speak_path",
    "speakable",
    "expand_acronyms",
    "looks_like_path",
]


# ---- data ---------------------------------------------------------------------


@dataclass(slots=True)
class Tagged:
    """One classified unit of output."""

    kind: LineKind
    text: str  # display form
    spoken: str | None  # what to say; None = say nothing
    raw: str  # source line(s), ANSI stripped
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrepassState:
    """Per-session memory between pane lines."""

    inside_code_block: bool = False
    code_lines: list[str] = field(default_factory=list)
    code_lang: str = ""
    inside_table: bool = False
    table_rows: int = 0
    table_separator_seen: bool = False
    inside_diff: bool = False
    diff_adds: int = 0
    diff_dels: int = 0
    diff_lines: list[str] = field(default_factory=list)
    inside_prompt: bool = False
    inside_box: bool = False
    last_kind: LineKind | None = None
    last_tool: str = ""  # name of the last tool header seen, for ⎿ dedupe
    turn_started: bool = False


# ---- ANSI ---------------------------------------------------------------------

try:  # the session package owns the canonical implementation when present
    from zordon.session.screen import strip_ansi  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001  pragma: no cover - fallback path
    _ANSI = re.compile(
        r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (hyperlinks, titles)
        r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (colours, cursor)
        r"|\x1b[@-Z\\-_]"  # two byte escapes
        r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # other C0 controls
    )

    def strip_ansi(s: str) -> str:
        """Remove escape sequences and control characters (keeps tab/newline)."""
        return _ANSI.sub("", s)


# ---- shared text helpers --------------------------------------------------------

_NBSP = " "
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"']+")
_PATH = re.compile(
    r"(?<![\w.\-/@])(?:~/|\.{1,2}/|/)?(?:[\w.\-@]+/)+[\w.\-@]*|(?<![\w.\-/@])~/[\w.\-@/]*"
)
_PATH_LIKE = re.compile(
    r"^(?:~/|\.{1,2}/|/)?(?:[\w.\-@]+/)+[\w.\-@]*/?$|^~/.*$|^[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,5}$"
)
_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,6}$")
_TRAIL_PUNCT = ".,;:!?)\"'"
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-⛿✀-➿⬀-⯿⌀-⏿️‍⃣]")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])(\*|_)(?=\S)([^*_\n]+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>\n]*>")
_EXTRA_SYMBOLS = {
    "→": " to ",
    "⇒": " to ",
    "←": " from ",
    "✓": "",
    "✔": "",
    "✗": "",
    "✘": "",
    "•": "",
}
_WS = re.compile(r"\s+")
_PUNCT_AFTER_WORD = re.compile(r"(?<=[A-Za-z0-9])([.,;:!?]+)(?=\s|$)")
_SPACE_BEFORE_PUNCT = re.compile(r" ([.,;:!?]+)(?=\s|$)")


def speak_path(path: str) -> str:
    """``/a/b/auth.py`` -> ``auth.py``. The normalizer turns that into words."""
    p = path.strip()
    core = p.rstrip(_TRAIL_PUNCT).rstrip("/")
    base = core.rsplit("/", 1)[-1]
    return base or p


def looks_like_path(text: str) -> bool:
    t = text.strip().rstrip(_TRAIL_PUNCT)
    if not t or " " in t:
        return False
    if t.startswith("~/"):
        return True
    if "/" in t:
        return bool(_PATH_LIKE.match(t))
    if not _PATH_LIKE.match(t) or t[0].isdigit():
        return False
    stem, _, ext = t.rpartition(".")
    return len(stem) >= 2 or len(ext) >= 2


def _speak_paths(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        core = tok.rstrip(_TRAIL_PUNCT)
        tail = tok[len(core) :]
        if not core or "/" not in core:
            return tok
        base = core.rstrip("/").rsplit("/", 1)[-1]
        if core.startswith("~/") or _EXT.search(base):
            return (base or core) + tail
        return tok

    return _PATH.sub(sub, text)


def _strip_inline_markup(text: str) -> str:
    """Display form: markdown markers gone, paths and URLs kept."""
    t = _IMAGE.sub(lambda m: m.group(1) or "image", text)
    t = _LINK.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _STRIKE.sub(r"\1", t)
    t = _BOLD.sub(r"\2", t)
    t = _ITALIC.sub(r"\2", t)
    t = _HTML_TAG.sub("", t)
    return _WS.sub(" ", t).strip()


def speakable(text: str) -> str:
    """Spoken form of a piece of prose: markup gone, links and URLs reduced,
    paths to basenames, emoji dropped, acronyms and symbols expanded."""
    t = _IMAGE.sub(lambda m: m.group(1) or "an image", text)
    t = _LINK.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _STRIKE.sub(r"\1", t)
    t = _BOLD.sub(r"\2", t)
    t = _ITALIC.sub(r"\2", t)
    t = _HTML_TAG.sub("", t)
    t = _URL.sub(lambda m: "a link" + m.group(0)[len(m.group(0).rstrip(_TRAIL_PUNCT)) :], t)
    t = _speak_paths(t)
    t = _EMOJI.sub("", t)
    for k, v in _EXTRA_SYMBOLS.items():
        t = t.replace(k, v)
    t = expand_symbols(t)
    t = _PUNCT_AFTER_WORD.sub(r" \1", t)  # "JSON." -> "JSON ." so the whole-word table sees it
    t = expand_acronyms(t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    return _WS.sub(" ", t).strip()


def _prose(text: str, raw: str, **meta: Any) -> Tagged:
    display = _strip_inline_markup(text)
    spoken = speakable(text)
    return Tagged(LineKind.PROSE, display, spoken or None, raw, dict(meta))


def _ui(raw: str, **meta: Any) -> Tagged:
    return Tagged(LineKind.UI, raw.strip(), None, raw, dict(meta))


def _blank(raw: str) -> Tagged:
    return Tagged(LineKind.BLANK, "", None, raw, {})


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---- block collapsing (shared by pane and markdown) ---------------------------------


def _code_block(lines: list[str], lang: str = "", raw: str | None = None) -> Tagged:
    n = len(lines)
    spoken = f"a code block, {_plural(n, 'line')}"
    return Tagged(
        LineKind.CODE,
        f"[code block, {_plural(n, 'line')}]",
        spoken,
        raw if raw is not None else "\n".join(lines),
        {"lines": n, "lang": lang, "literal": True},
    )


def _diff_block(
    adds: int, dels: int, lines: list[str], target: str = "", raw: str | None = None
) -> Tagged:
    total = adds + dels
    if target:
        spoken = f"edited {speak_path(target)}, {_plural(total, 'line')} changed"
    else:
        spoken = f"a diff, {_plural(adds, 'addition')} and {_plural(dels, 'removal')}"
    return Tagged(
        LineKind.DIFF,
        f"[diff +{adds} -{dels}]" + (f" {target}" if target else ""),
        spoken,
        raw if raw is not None else "\n".join(lines),
        {"adds": adds, "dels": dels, "target": target, "literal": True},
    )


def _table_block(rows: int, raw: str) -> Tagged:
    return Tagged(
        LineKind.CODE,
        f"[table, {_plural(rows, 'row')}]",
        f"a table with {_plural(rows, 'row')}",
        raw,
        {"table": True, "rows": rows, "literal": True},
    )


def _close_code(state: PrepassState) -> list[Tagged]:
    if not state.inside_code_block:
        return []
    out = [_code_block(state.code_lines, state.code_lang)]
    state.inside_code_block = False
    state.code_lines = []
    state.code_lang = ""
    return out


def _close_diff(state: PrepassState) -> list[Tagged]:
    if not state.inside_diff:
        return []
    out = [_diff_block(state.diff_adds, state.diff_dels, state.diff_lines)]
    state.inside_diff = False
    state.diff_adds = state.diff_dels = 0
    state.diff_lines = []
    return out


def _close_table(state: PrepassState, raw: str = "") -> list[Tagged]:
    if not state.inside_table:
        return []
    rows = state.table_rows - (1 if state.table_separator_seen and state.table_rows > 0 else 0)
    out = [_table_block(max(rows, 0), raw)]
    state.inside_table = False
    state.table_rows = 0
    state.table_separator_seen = False
    return out


def flush_prepass(state: PrepassState) -> list[Tagged]:
    """Close any open code/diff/table block (end of turn, lull)."""
    return _close_code(state) + _close_diff(state) + _close_table(state)


# ---- pane rules ---------------------------------------------------------------------

_RULE = re.compile(r"^\s*[─━═▔▁]{10,}\s*$")
_DOTTED = re.compile(r"^\s*╌{10,}\s*$")
_BOX_TOP = re.compile(r"^\s*╭[─]*╮?\s*$")
_BOX_BOTTOM = re.compile(r"^\s*╰[─]*╯?\s*$")
_BOX_SIDE = re.compile(r"^\s*│(?P<rest>.*)$")
_TABLE_TOP = re.compile(r"^\s*┌[─┬]*┐?\s*$")
_TABLE_MID = re.compile(r"^\s*├[─┼]*┤?\s*$")
_TABLE_BOTTOM = re.compile(r"^\s*└[─┴]*┘?\s*$")
_TABLE_ROW = re.compile(r"^\s*│.*│\s*$")
_BANNER_ART = re.compile(r"^\s*[▐▝▜▛█▀]+(\s|$)")
_BANNER_WORDS = (
    "Claude Code v",
    "Updated to latest.",
    "code.claude.com/docs",
    "Get to finished work sooner",
    "Switch anytime with /model",
    "Tips for getting started",
    "Welcome to Claude Code",
    "Welcome back",
)
_STATUS_MODE = re.compile(r"^\s*⏸ .*\bmode on\b")
_STATUS_TAG = re.compile(r"^\s*\[[A-Za-z0-9 _:./-]+\]\s*$")
_EFFORT_HINT = re.compile(r"^\s*◐ .*·\s*/effort\s*$")
_INPUT_BOX = re.compile(r"^❯(?: |$)")
_USER_ECHO = re.compile(r"^❯ (?P<text>\S.*)$")
_EXIT_LINES = re.compile(r"^\s*(?:Resume this session with:|claude --resume [0-9a-f-]{36})\s*$")
_SHELL_PROMPT = re.compile(r"^[\w.\-]+@[\w.\-]+:\S*[$#](?:\s|$)")
_SPINNER = re.compile(r"^\s*[^\s●❯⎿▎\-] [A-Z][^\s…(]+…(?: \(.*\))?\s*$")
_DONE = re.compile(
    r"^\s*✻ (?P<verb>[A-Z][a-zé]+) for (?P<dur>\d+(?:m \d+)?s|\d+m) · done (?P<clock>\d{1,2}:\d{2} [AP]M)\s*$"
)
_TIP = re.compile(r"^\s*(?:⎿\s+)?Tip: ")
_WARNING = re.compile(r"^\s*(?:Permission deny rule|Warning:|⚠)")

_PERM_HEADER = re.compile(
    r"^\s{0,3}(?:Bash command|Create file|Edit file|Write file|Read file|Fetch content|Run command|"
    r"Update file|Delete file|Web fetch|Web search|Tool use)\s*$"
)
_PERM_QUESTION = re.compile(r"^\s*Do you want to .*\?\s*$")
_PERM_FOOTER = re.compile(r"^\s*Esc to cancel · Tab to amend\s*$")
_ASK_HEADER = re.compile(r"^\s*☐ \S.*$")
_ASK_FOOTER = re.compile(r"^\s*Enter to select · ↑/↓ to navigate · Esc to cancel\s*$")
_PLAN_HEADER = re.compile(r"^\s*Ready to code\?\s*$")
_PLAN_QUESTION = re.compile(
    r"^\s*Claude has written up a plan and is ready to execute\. Would you like to proceed\?\s*$"
)
_PLAN_FOOTER = re.compile(r"^\s*ctrl\+g to edit in VS Code\b.*$")
_TRUST_HEADER = re.compile(r"^\s*Accessing workspace:\s*$")
_TRUST_FOOTER = re.compile(r"^\s*Enter to confirm · Esc to cancel\s*$")
_MENU_POINTER = re.compile(r"^\s*❯\s*\d+\. \S.*$")
_MENU_OPTION = re.compile(
    r"^\s*\d+\. (?:Yes|No|Yes, and .*|Tell Claude what to change|Chat about this|Type something\.?|"
    r"No, exit|Yes, I trust this folder)\s*$"
)
_TRUST_OPTION = re.compile(r"^\s*❯?\s*(?:No, exit|Yes, I trust this folder)\s*$")
_MENU_HINT = re.compile(
    r"^\s*(?:shift\+tab to approve with this feedback|Here is Claude's plan:)\s*$"
)

_TOOL_BULLET = re.compile(r"^●[  ]?(?P<text>\S.*)$")
_TOOL_CALL_PAREN = re.compile(r"^(?P<name>[A-Z][A-Za-z]+)\((?P<arg>.*)\)\s*$")
_TOOL_SUMMARY = re.compile(
    r"^(?:(?P<run>Running) (?P<run_n>\d+) shell commands?…?|(?P<listed>Listed) (?P<listed_n>\d+) director(?:y|ies)|"
    r"(?P<ran>Ran) (?P<ran_n>\d+) shell commands?|(?P<read>Read) (?P<read_n>\d+) files?|"
    r"(?P<searched>Searched) (?P<searched_n>\d+) .*|(?P<edited>Edited) (?P<edited_n>\d+) files?|"
    r"(?P<wrote>Wrote) (?P<wrote_n>\d+) files?|(?P<fetched>Fetched) (?P<fetched_n>\d+) .*)\s*$"
)
_GERUND = re.compile(r"^(?P<verb>[A-Z][a-z]+ing)\b(?P<rest>[^.!?:]*)$")
_RESULT_LINE = re.compile(r"^\s*⎿\s+(?P<text>\S.*)$")
_SHELL_CMD = re.compile(r"^\$ (?P<cmd>\S.*)$")
_INTERRUPTED = re.compile(r"^Interrupted(?: · What should Claude do instead\?)?\s*$")
_REJECTED = re.compile(r"^User rejected (?P<what>write|edit|update|change)s? to (?P<file>\S+)\s*$")
_ANSWERED = re.compile(r"^·\s*(?P<q>.+?)\s*→\s*(?P<a>.+?)\s*$")
_ERROR_LINE = re.compile(r"^(?:Error|ERROR|Exception|Traceback|FAILED|fatal):?\b")
_LS_LINE = re.compile(r"^\s*[-dlcbps][rwxsStT-]{9}[+@.]?\s+\d+\s+\S+")
_LS_TOTAL = re.compile(r"^\s*total \d+\s*$")

_DIFF_UPDATED = re.compile(
    r"Updated (?P<file>\S+) with (?P<adds>\d+) additions? and (?P<dels>\d+) removals?"
)
_DIFF_ADDED = re.compile(r"(?:Added|Appended) (?P<adds>\d+) lines? to (?P<file>\S+)")
_DIFF_REMOVED = re.compile(r"Removed (?P<dels>\d+) lines? from (?P<file>\S+)")
_DIFF_WROTE = re.compile(r"Wrote (?P<n>\d+) lines? to (?P<file>\S+)")
_DIFF_EDITED = re.compile(r"\b[Ee]dited (?P<file>\S+?),? (?P<n>\d+) lines?\b")
_DIFF_PLUSMINUS = re.compile(r"(?<![\w+])\+(?P<adds>\d+)\s+-(?P<dels>\d+)(?![\w-])")
_DIFF_NUMBERED = re.compile(r"^\s*\d+\s*(?P<sign>[+-])\s")
_DIFF_HEADER = re.compile(r"^\s*(?:diff --git |--- [ab/]|\+\+\+ [ab/]|@@ .* @@)")

_BULLET = re.compile(r"^\s*(?:[-*•◦▪]|\d+[.)])\s+(?P<text>\S.*)$")
_CODE_START = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |const |let |var |return\b|if \(|for \(|while \(|"
    r"elif |else:|try:|except\b|finally:|function\b|#include\b|package \S+;|fn |pub fn|async def |"
    r"export (?:default |const |function |class )|@\w+\(?$|\$ |>>> |\.\.\. )"
)
_CODE_END = re.compile(r"[;{}]\s*$|^\s*[\]\)\}]+[;,]?\s*$")
_SHELL_WORDS = {
    "tmux",
    "git",
    "npm",
    "npx",
    "pip",
    "pip3",
    "python",
    "python3",
    "pytest",
    "cd",
    "ls",
    "cat",
    "grep",
    "rg",
    "find",
    "mkdir",
    "rm",
    "cp",
    "mv",
    "curl",
    "wget",
    "docker",
    "make",
    "cargo",
    "go",
    "node",
    "yarn",
    "pnpm",
    "uv",
    "ruff",
    "echo",
    "export",
    "source",
    "sudo",
    "chmod",
    "chown",
    "ssh",
    "scp",
    "tar",
    "kill",
    "ps",
    "brew",
    "apt",
    "apt-get",
    "sed",
    "awk",
    "touch",
    "ln",
    "head",
    "tail",
    "less",
    "diff",
    "patch",
    "black",
    "mypy",
    "poetry",
    "pipx",
    "claude",
    "systemctl",
    "journalctl",
    "cloudflared",
    "ngrok",
    "tailscale",
    "ffmpeg",
    "aplay",
    "arecord",
}
_WORDISH = re.compile(r"[A-Za-z']+")
_HAS_LETTER = re.compile(r"[A-Za-z]")


def _looks_like_shell(text: str) -> bool:
    t = text.strip()
    if not t or t[0].isupper() or t[-1] in ".!?:":
        return False
    toks = t.split()
    if len(toks) < 2:
        return False
    return toks[0] in _SHELL_WORDS or t.startswith(("./", "../")) and len(toks) >= 1


def _looks_like_prose(text: str) -> bool:
    t = text.strip()
    words = _WORDISH.findall(t)
    if len(words) < 3:
        return False
    if any(ch in t for ch in ";{}=<>|\\"):
        return False
    letters = sum(len(w) for w in words)
    return letters >= 0.6 * len(t.replace(" ", ""))


def _looks_like_code(text: str, indent: int, state: PrepassState) -> bool:
    t = text.strip()
    if not t:
        return False
    if _CODE_START.search(text) or _CODE_END.search(text):
        return True
    if indent >= 4:
        if state.last_kind in (LineKind.PROSE, LineKind.PATH) and _looks_like_prose(t):
            return False  # wrapped continuation of a list item
        return True
    if state.inside_code_block and indent >= 2 and len(t) < 120 and not _looks_like_prose(t):
        sentence_like = t[-1] in ".!?" or (
            t[0].isupper() and not any(ch in t for ch in "(){}[]=;<>")
        )
        if not sentence_like:
            return True
    return _looks_like_shell(t)


def _enter_prompt(s: str) -> bool:
    return bool(
        _PERM_HEADER.match(s)
        or _ASK_HEADER.match(s)
        or _PLAN_HEADER.match(s)
        or _TRUST_HEADER.match(s)
        or _PERM_QUESTION.match(s)
        or _PLAN_QUESTION.match(s)
    )


def _exit_prompt(s: str) -> bool:
    return bool(
        _PERM_FOOTER.match(s)
        or _ASK_FOOTER.match(s)
        or _PLAN_FOOTER.match(s)
        or _TRUST_FOOTER.match(s)
    )


def _is_chrome(s: str) -> bool:
    if _RULE.match(s) or _DOTTED.match(s):
        return True
    if _BANNER_ART.match(s) or any(w in s for w in _BANNER_WORDS):
        return True
    if _STATUS_MODE.match(s) or _STATUS_TAG.match(s) or _EFFORT_HINT.match(s):
        return True
    if _EXIT_LINES.match(s) or _SHELL_PROMPT.match(s) or _TIP.match(s) or _WARNING.match(s):
        return True
    if (
        _MENU_POINTER.match(s)
        or _MENU_OPTION.match(s)
        or _TRUST_OPTION.match(s)
        or _MENU_HINT.match(s)
    ):
        return True
    return False


def _diff_summary(text: str) -> Tagged | None:
    m = _DIFF_UPDATED.search(text)
    if m:
        return _diff_block(int(m["adds"]), int(m["dels"]), [text], m["file"], raw=text)
    m = _DIFF_ADDED.search(text)
    if m:
        return _diff_block(int(m["adds"]), 0, [text], m["file"], raw=text)
    m = _DIFF_REMOVED.search(text)
    if m:
        return _diff_block(0, int(m["dels"]), [text], m["file"], raw=text)
    m = _DIFF_WROTE.search(text)
    if m:
        n = int(m["n"])
        t = Tagged(
            LineKind.DIFF,
            f"[wrote {m['file']}, {_plural(n, 'line')}]",
            f"wrote {speak_path(m['file'])}, {_plural(n, 'line')}",
            text,
            {"adds": n, "dels": 0, "target": m["file"], "literal": True},
        )
        return t
    m = _DIFF_EDITED.search(text)
    if m:
        return _diff_block(int(m["n"]), 0, [text], m["file"], raw=text)
    m = _DIFF_PLUSMINUS.search(text)
    if m and len(text) < 120:
        target = ""
        for tok in text.replace(",", " ").split():
            if looks_like_path(tok) and not tok.lstrip("+-").isdigit():
                target = tok
                break
        return _diff_block(int(m["adds"]), int(m["dels"]), [text], target, raw=text)
    return None


def _tool_header(text: str, raw: str, state: PrepassState) -> Tagged | None:
    """``Write(probe.txt)``, ``Listed 1 directory``, ``Listing all files…`` -> TOOL_CALL."""
    from zordon.output.tooldesc import describe_tool_use, tool_input_from_pane

    m = _TOOL_CALL_PAREN.match(text)
    if m:
        name, arg = m["name"], m["arg"].strip()
        t = describe_tool_use(name, tool_input_from_pane(name, arg))
        t.raw = raw
        t.meta["pane"] = True
        state.last_tool = name
        return t
    m = _TOOL_SUMMARY.match(text)
    if m:
        d = m.groupdict()
        if d["run"]:
            spoken = (
                "running a shell command"
                if d["run_n"] == "1"
                else f"running {d['run_n']} shell commands"
            )
            state.last_tool = "Bash"
        elif d["ran"]:
            spoken = (
                "ran a shell command" if d["ran_n"] == "1" else f"ran {d['ran_n']} shell commands"
            )
            state.last_tool = "Bash"
        elif d["listed"]:
            spoken = (
                "listed a directory"
                if d["listed_n"] == "1"
                else f"listed {d['listed_n']} directories"
            )
            state.last_tool = "Bash"
        elif d["read"]:
            spoken = "read a file" if d["read_n"] == "1" else f"read {d['read_n']} files"
            state.last_tool = "Read"
        elif d["searched"]:
            spoken = "searched the codebase"
            state.last_tool = "Grep"
        elif d["edited"]:
            spoken = "edited a file" if d["edited_n"] == "1" else f"edited {d['edited_n']} files"
            state.last_tool = "Edit"
        elif d["wrote"]:
            spoken = "wrote a file" if d["wrote_n"] == "1" else f"wrote {d['wrote_n']} files"
            state.last_tool = "Write"
        else:
            spoken = "looked something up on the web"
            state.last_tool = "WebFetch"
        return Tagged(
            LineKind.TOOL_CALL, text.strip(), spoken, raw, {"tool": state.last_tool, "pane": True}
        )
    m = _GERUND.match(text)
    if m and len(text.split()) <= 14:
        desc = text.strip()
        state.last_tool = "Bash"
        return Tagged(
            LineKind.TOOL_CALL,
            desc,
            "running: " + desc[0].lower() + desc[1:],
            raw,
            {"tool": "Bash", "description": desc, "pane": True},
        )
    return None


def _result_line(text: str, raw: str, state: PrepassState) -> Tagged:
    """``  ⎿  <text>`` detail lines."""
    from zordon.output.tooldesc import describe_tool_result

    if _INTERRUPTED.match(text):
        return Tagged(
            LineKind.ERROR,
            "Interrupted",
            "interrupted",
            raw,
            {"interrupted": True, "literal": True},
        )
    m = _REJECTED.match(text)
    if m:
        return Tagged(
            LineKind.TOOL_RESULT,
            text.strip(),
            f"the {m['what']} to {speak_path(m['file'])} was denied",
            raw,
            {"rejected": True, "file": m["file"]},
        )
    m = _SHELL_CMD.match(text)
    if m:
        dedupe = state.last_tool == "Bash" and state.last_kind is LineKind.TOOL_CALL
        return Tagged(
            LineKind.TOOL_CALL,
            "$ " + m["cmd"],
            None if dedupe else "running a shell command",
            raw,
            {"tool": "Bash", "command": m["cmd"], "pane": True},
        )
    m = _ANSWERED.match(text)
    if m:
        return Tagged(
            LineKind.TOOL_RESULT, text.strip(), f"answered: {m['a']}", raw, {"answered": m["a"]}
        )
    if _TIP.match("⎿ " + text) or text.startswith("Tip: "):
        return _ui(raw)
    if text.endswith("…") and len(text.split()) <= 4:
        return Tagged(LineKind.PROGRESS, text.strip(), None, raw, {})
    d = _diff_summary(text)
    if d is not None:
        d.raw = raw
        return d
    summary = _TOOL_SUMMARY.match(text)
    if summary:
        t = _tool_header(text, raw, state)
        if t is not None:
            return t
    is_error = bool(_ERROR_LINE.match(text))
    t = describe_tool_result(text, is_error=is_error)
    t.raw = raw
    return t


def _classify_pane(s: str, state: PrepassState) -> list[Tagged]:
    """Classify one ANSI-stripped, right-trimmed pane line."""
    raw = s
    stripped = s.strip()

    # Blank: a paragraph boundary. Blocks stay open across blanks.
    if not stripped:
        return [_blank(raw)]

    # Spinner, done line, input box, user echo: these end or reset regions.
    if _SPINNER.match(s):
        return [Tagged(LineKind.PROGRESS, stripped, None, raw, {"spinner": True})]
    m = _DONE.match(s)
    if m:
        out = flush_prepass(state)
        state.inside_prompt = False
        state.inside_box = False
        state.turn_started = False
        state.last_tool = ""
        out.append(_ui(raw, done=True, duration=m["dur"]))
        out.append(
            Tagged(
                LineKind.SUMMARY,
                "",
                None,
                raw,
                {"marker": True, "turn_end": True, "duration": m["dur"], "verb": m["verb"]},
            )
        )
        return out
    if _INPUT_BOX.match(s):
        state.inside_prompt = False
        return flush_prepass(state) + [_ui(raw, input_box=True)]
    # A lone bullet, dash or line number mid-render: nothing to say. Inside an
    # open code block a bare ``}`` or ``);`` belongs to the block.
    if not _HAS_LETTER.search(stripped) and not state.inside_table and not _TABLE_TOP.match(s):
        if (
            state.inside_code_block
            and not state.inside_prompt
            and not _RULE.match(s)
            and not _DOTTED.match(s)
        ):
            state.code_lines.append(s)
            return []
        if _DIFF_NUMBERED.match(s):
            pass
        elif not (
            _BOX_TOP.match(s)
            or _BOX_BOTTOM.match(s)
            or _BOX_SIDE.match(s)
            or _RULE.match(s)
            or _DOTTED.match(s)
        ):
            return [_ui(raw, partial=True)]
    if _MENU_POINTER.match(s):
        return flush_prepass(state) + [_ui(raw, prompt=True)]
    m = _USER_ECHO.match(s)
    if m:
        out = flush_prepass(state)
        state.inside_prompt = False
        state.inside_box = False
        state.turn_started = False
        state.last_tool = ""
        out.append(_ui(raw, user_echo=True, text=m["text"], turn_start=True))
        return out

    # Prompt regions are UI for the pre-pass: the session thread detects them and
    # the manager speaks them through the pipeline's priority path.
    if _enter_prompt(s):
        out = flush_prepass(state)
        state.inside_prompt = True
        out.append(_ui(raw, prompt=True))
        return out
    if state.inside_prompt:
        if _exit_prompt(s):
            state.inside_prompt = False
            return [_ui(raw, prompt=True)]
        if not _TOOL_BULLET.match(s):
            return [_ui(raw, prompt=True)]
        state.inside_prompt = False  # a new tool header means the prompt is gone

    # Tables (box drawing) and the rounded plan box.
    if state.inside_table:
        if _TABLE_MID.match(s):
            state.table_separator_seen = True
            return []
        if _TABLE_BOTTOM.match(s):
            return _close_table(state, raw)
        if _TABLE_ROW.match(s):
            state.table_rows += 1
            return []
        out = _close_table(state, raw)
        return out + _classify_pane(s, state)
    if _TABLE_TOP.match(s):
        out = _close_code(state) + _close_diff(state)
        state.inside_table = True
        return out
    if _TABLE_ROW.match(s) and s.count("│") >= 3 and not state.inside_box:
        out = _close_code(state) + _close_diff(state)
        state.inside_table = True
        state.table_rows = 1
        return out
    if _BOX_TOP.match(s):
        state.inside_box = True
        return _close_code(state) + _close_diff(state) + [_ui(raw, box=True)]
    if _BOX_BOTTOM.match(s):
        state.inside_box = False
        return _close_code(state) + _close_diff(state) + [_ui(raw, box=True)]
    m = _BOX_SIDE.match(s)
    if m and (state.inside_box or s.count("│") == 2):
        rest = m["rest"]
        idx = rest.rfind("│")
        inner = rest[:idx] if idx >= 0 else rest
        inner = inner.rstrip()
        if not inner.strip():
            return [_blank(raw)]
        # Box content is indented one level; drop that so prose rules apply.
        if inner.startswith(" "):
            inner = inner[1:]
        return _classify_pane(inner, state)

    if _is_chrome(s):
        return flush_prepass(state) + [_ui(raw)]

    # Tool bullet or first prose line.
    m = _TOOL_BULLET.match(s)
    if m:
        out = flush_prepass(state)
        text = m["text"].rstrip()
        if _LS_TOTAL.match(text):
            out.append(Tagged(LineKind.TOOL_RESULT, text, None, raw, {"output": True}))
            return out
        tool = _tool_header(text, raw, state)
        if tool is not None:
            out.append(tool)
            return out
        if text.rstrip(":").endswith("User answered Claude's questions"):
            out.append(Tagged(LineKind.TOOL_RESULT, text, None, raw, {"answered": True}))
            return out
        d = _diff_summary(text)
        if d is not None:
            d.raw = raw
            out.append(d)
            return out
        state.turn_started = True
        state.last_tool = ""
        out.append(_prose(text, raw, paragraph_start=True))
        return out

    # Tool detail lines.
    m = _RESULT_LINE.match(s)
    if m:
        return flush_prepass(state) + [_result_line(m["text"].rstrip(), raw, state)]

    # Command output that is obviously not prose.
    if _LS_LINE.match(s) or _LS_TOTAL.match(s):
        return flush_prepass(state) + [
            Tagged(LineKind.TOOL_RESULT, stripped, None, raw, {"output": True})
        ]

    # Indented tool lifecycle without the bullet ("  Running 1 shell command…",
    # or the description line while the ● blinks off).
    if _TOOL_SUMMARY.match(stripped) or (
        _GERUND.match(stripped)
        and state.last_kind is not LineKind.PROSE
        and len(stripped.split()) <= 14
    ):
        out = flush_prepass(state)
        t = _tool_header(stripped, raw, state)
        if t is not None:
            out.append(t)
            return out

    # Diffs.
    if _DIFF_NUMBERED.match(s) or _DIFF_HEADER.match(s):
        out = _close_code(state) + _close_table(state)
        m = _DIFF_NUMBERED.match(s)
        if m:
            if m["sign"] == "+":
                state.diff_adds += 1
            else:
                state.diff_dels += 1
        state.inside_diff = True
        state.diff_lines.append(s)
        return out
    d = _diff_summary(stripped)
    if d is not None and len(stripped) < 160:
        d.raw = raw
        return flush_prepass(state) + [d]

    indent = len(s) - len(s.lstrip())

    # Markdown-ish bullets and numbered items.
    m = _BULLET.match(s)
    if m and not _CODE_START.match(m["text"]):
        out = flush_prepass(state)
        item = m["text"].strip()
        if looks_like_path(item):
            core = item.rstrip(_TRAIL_PUNCT)
            out.append(
                Tagged(
                    LineKind.PATH, core, speak_path(core), raw, {"path": core, "list_item": True}
                )
            )
        else:
            out.append(_prose(item, raw, list_item=True))
        return out

    # Code heuristics (rendered code has no fences in the pane).
    if _looks_like_code(s, indent, state):
        out = _close_diff(state) + _close_table(state)
        state.inside_code_block = True
        state.code_lines.append(s)
        return out

    # Error-looking output lines.
    if _ERROR_LINE.match(stripped) and indent <= 2:
        from zordon.output.tooldesc import describe_tool_result

        t = describe_tool_result(stripped, is_error=True)
        t.raw = raw
        return flush_prepass(state) + [t]

    # Anything else is prose (continuation lines are indented two spaces).
    out = flush_prepass(state)
    state.turn_started = True
    out.append(_prose(stripped, raw))
    return out


def prepass_pane_line(line: str, state: PrepassState) -> list[Tagged]:
    """Classify one pane line. May return 0..n items (block collapse)."""
    s = strip_ansi(line).rstrip(" \t\r\n")
    try:
        out = _classify_pane(s, state)
    except Exception:  # noqa: BLE001  never let one odd line kill the pipeline
        log.exception("prepass failed on a pane line; passing it through as prose")
        out = [_prose(s.strip(), s)]
    for t in out:
        if t.kind not in (LineKind.BLANK, LineKind.UI, LineKind.PROGRESS):
            state.last_kind = t.kind
    if not out and (state.inside_code_block or state.inside_diff):
        state.last_kind = LineKind.CODE if state.inside_code_block else LineKind.DIFF
    return out


# Name used in docs/architecture.md.
prepass_line = prepass_pane_line


# ---- markdown ----------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(?P<fence>```+|~~~+)\s*(?P<lang>[\w+.#-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(?P<level>#{1,6})\s+(?P<text>\S.*?)\s*#*\s*$")
_MD_LIST = re.compile(r"^(?P<indent>\s*)(?:[-*+•]|\d+[.)])\s+(?P<text>\S.*)$")
_MD_TASK = re.compile(r"^\[[ xX]\]\s+")
_MD_QUOTE = re.compile(r"^\s*>\s?(?P<text>.*)$")
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_HR = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_DIFF_LANGS = {"diff", "patch", "udiff"}


def _md_paragraph(lines: list[str], out: list[Tagged]) -> None:
    if not lines:
        return
    text = " ".join(x.strip() for x in lines)
    raw = "\n".join(lines)
    display_all = _strip_inline_markup(text)
    spoken_all = speakable(text)
    if not spoken_all:
        lines.clear()
        return
    sentences = split_sentences(spoken_all)
    display_sentences = split_sentences(display_all)
    for i, s in enumerate(sentences):
        disp = display_sentences[i] if i < len(display_sentences) else s
        out.append(
            Tagged(LineKind.PROSE, disp, s, raw, {"paragraph_start": i == 0, "complete": True})
        )
    lines.clear()


def _md_code(lines: list[str], lang: str, raw: str) -> Tagged:
    adds = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    if lang.lower() in _DIFF_LANGS or (
        lines and (adds + dels) >= max(1, len(lines) // 2) and (adds or dels)
    ):
        return _diff_block(adds, dels, lines, "", raw=raw)
    body = [x for x in lines]
    # Trim leading/trailing blank lines from the count.
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return _code_block(body, lang, raw=raw)


def prepass_markdown(text: str, state: PrepassState | None = None) -> list[Tagged]:
    """Classify a complete markdown text block (one jsonl ``text`` content block)."""
    state = state or PrepassState()
    out: list[Tagged] = []
    lines = strip_ansi(text).replace("\r\n", "\n").split("\n")
    para: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        m = _FENCE.match(line)
        if m:
            _md_paragraph(para, out)
            fence, lang = m["fence"], m["lang"]
            body: list[str] = []
            i += 1
            closed = False
            while i < n:
                close = _FENCE.match(lines[i])
                if (
                    close
                    and close["fence"][0] == fence[0]
                    and len(close["fence"]) >= len(fence)
                    and not close["lang"]
                ):
                    closed = True
                    break
                body.append(lines[i])
                i += 1
            raw = "\n".join([line, *body] + ([lines[i]] if closed else []))
            out.append(_md_code(body, lang, raw))
            i += 1
            continue

        if not stripped:
            _md_paragraph(para, out)
            i += 1
            continue

        if _MD_TABLE_ROW.match(line) or (_MD_TABLE_SEP.match(line) and para and "|" in para[-1]):
            # A header row without leading pipe may already sit in ``para``.
            rows: list[str] = []
            if para and "|" in para[-1] and _MD_TABLE_SEP.match(line):
                rows.append(para.pop())
            _md_paragraph(para, out)
            while i < n and (_MD_TABLE_ROW.match(lines[i]) or _MD_TABLE_SEP.match(lines[i])):
                rows.append(lines[i])
                i += 1
            sep = sum(1 for r in rows if _MD_TABLE_SEP.match(r))
            data = len(rows) - sep - (1 if sep else 0)
            out.append(_table_block(max(data, 0), "\n".join(rows)))
            continue

        if _MD_HR.match(line):
            _md_paragraph(para, out)
            out.append(_ui(line, rule=True))
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            _md_paragraph(para, out)
            t = _prose(
                m["text"], line, heading=len(m["level"]), paragraph_start=True, complete=True
            )
            out.append(t)
            i += 1
            continue

        m = _MD_LIST.match(line)
        if m:
            _md_paragraph(para, out)
            item_lines = [m["text"]]
            base_indent = len(m["indent"])
            i += 1
            # Wrapped continuation lines: indented deeper than the marker, not a new item.
            while (
                i < n
                and lines[i].strip()
                and not _MD_LIST.match(lines[i])
                and not _FENCE.match(lines[i])
            ):
                cont_indent = len(lines[i]) - len(lines[i].lstrip())
                if cont_indent <= base_indent:
                    break
                item_lines.append(lines[i].strip())
                i += 1
            item = _MD_TASK.sub("", " ".join(item_lines))
            raw = "\n".join([line, *item_lines[1:]])
            core = _strip_inline_markup(item)
            if looks_like_path(core):
                p = core.rstrip(_TRAIL_PUNCT)
                out.append(
                    Tagged(
                        LineKind.PATH,
                        p,
                        speak_path(p),
                        raw,
                        {"path": p, "list_item": True, "complete": True},
                    )
                )
            else:
                out.append(_prose(item, raw, list_item=True, complete=True))
            continue

        m = _MD_QUOTE.match(line)
        if m:
            para.append(m["text"])
            i += 1
            continue

        para.append(stripped)
        i += 1

    _md_paragraph(para, out)
    for t in out:
        if t.kind not in (LineKind.BLANK, LineKind.UI):
            state.last_kind = t.kind
            if t.kind is LineKind.PROSE:
                state.turn_started = True
    return out
