"""Shared, pure helpers for Normalizer implementations.

The ``Normalizer`` protocol itself lives in ``zordon.providers``; this module
holds the string work every implementation needs: the deterministic cleanup of
markdown remnants, the first-line extraction applied to model output, and the
``<context>…</context><sentence>…</sentence>`` user message with its 600
character budget from the design.
"""

from __future__ import annotations

import re

from zordon.output.acronyms import expand_symbols
from zordon.providers import Normalizer

__all__ = [
    "MAX_INPUT_CHARS",
    "Normalizer",
    "build_user_content",
    "clean_spoken",
    "first_line",
    "strip_markdown",
]

# Design: "input under 600 characters including context".
MAX_INPUT_CHARS = 600
# How many earlier sentences travel with the current one.
CONTEXT_SENTENCES = 2

_FENCE = re.compile(r"```[^\n]*")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_BOLD = re.compile(
    r"\*\*(.+?)\*\*"
)  # ``__bold__`` is left alone: it is indistinguishable from ``__init__``
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_TABLE_PIPES = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_CHECKBOX = re.compile(r"\[(?: |x|X)\]\s*")
_STRAY_MARKS = re.compile(r"(?<!\w)[*_#]{1,3}(?!\w)")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])(?=\s|$)")
_ISSUE_NUMBER = re.compile(r"(?<![\w&])#(\d+)\b")
# Lone comparison operators between spaces; the two-character forms are in acronyms.SYMBOLS.
_LONE_SYMBOLS = {"<": " less than ", ">": " greater than ", "+": " plus "}
_LONE_SYMBOL = re.compile(r"(?<=\s)([<>+])(?=\s)")
_WS = re.compile(r"\s+")
_LEADING_TAGS = re.compile(
    r"^(?:spoken|output|rewrite|rewritten|answer|sentence|result)\s*:\s*", re.IGNORECASE
)


def strip_markdown(text: str) -> str:
    """Remove markdown syntax while keeping the words. Pure and conservative:
    identifiers like ``snake_case`` and ``__init__`` survive because the
    emphasis patterns require the marker to sit at a word boundary."""
    if not text:
        return ""
    out = _FENCE.sub(" ", text)
    out = _IMAGE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _BOLD.sub(r"\1", out)
    out = _ITALIC.sub(r"\1", out)
    out = _HEADING.sub("", out)
    out = _RULE.sub(" ", out)
    out = _TABLE_PIPES.sub(lambda m: m.group(0).replace("|", " "), out)
    out = _BLOCKQUOTE.sub("", out)
    out = _BULLET.sub("", out)
    out = _CHECKBOX.sub("", out)
    out = _STRAY_MARKS.sub(" ", out)
    return out


def clean_spoken(text: str) -> str:
    """The deterministic cleanup the passthrough normalizer applies and the
    anthropic normalizer uses as a last pass: markdown gone, inline symbols
    spoken (``->`` to "to"), whitespace collapsed, trailing colon dropped."""
    out = _ISSUE_NUMBER.sub(r"number \1", text or "")
    out = strip_markdown(out)
    out = expand_symbols(out)
    out = _LONE_SYMBOL.sub(lambda m: _LONE_SYMBOLS[m.group(1)], out)
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _WS.sub(" ", out).strip()
    out = out.rstrip(":").strip()
    return out


def first_line(text: str) -> str:
    """Model output discipline: keep only the first non-empty line, drop a
    leading label such as ``Spoken:`` and surrounding quotes."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _LEADING_TAGS.sub("", line)
        if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'“”":
            line = line[1:-1].strip()
        elif line.startswith(("“", '"')) and line.endswith(("”", '"')):
            line = line[1:-1].strip()
        return line
    return ""


def build_user_content(
    sentence: str,
    context: list[str] | tuple[str, ...] | None,
    limit: int = MAX_INPUT_CHARS,
) -> str:
    """``<context>…</context>`` then ``<sentence>…</sentence>``.

    Keeps the whole message under ``limit`` characters by dropping the oldest
    context line first and, only as a last resort, truncating the sentence.
    """
    sentence = (sentence or "").strip()
    ctx = [c.strip() for c in (context or []) if c and c.strip()][-CONTEXT_SENTENCES:]

    def render(ctx_lines: list[str], sent: str) -> str:
        body = "\n".join(ctx_lines)
        return f"<context>\n{body}\n</context>\n<sentence>{sent}</sentence>"

    msg = render(ctx, sentence)
    while len(msg) > limit and ctx:
        ctx = ctx[1:]
        msg = render(ctx, sentence)
    if len(msg) > limit:
        overhead = len(render([], ""))
        sentence = sentence[: max(0, limit - overhead)].rstrip()
        msg = render([], sentence)
    return msg
