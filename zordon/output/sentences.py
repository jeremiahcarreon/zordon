"""Sentence boundary buffering. Pure.

Prose arrives as lines or markdown blocks; TTS wants whole sentences. The
buffer accumulates text and releases complete sentences. Abbreviations, file
names with dots, version numbers and decimals do not end a sentence.
"""

from __future__ import annotations

import re

_ABBREVIATIONS = {
    "e.g",
    "i.e",
    "etc",
    "vs",
    "mr",
    "mrs",
    "ms",
    "dr",
    "st",
    "no",
    "approx",
    "fig",
    "cf",
    "inc",
    "ltd",
}

# A terminator followed by whitespace+capital/quote/digit, or end of text.
_BOUNDARY = re.compile(r"([.!?]+)([\"')\]]*)(\s+|$)")
_MAX_CHARS = 280  # force a break on very long run-on text so TTS never waits forever


class SentenceBuffer:
    def __init__(self, max_chars: int = _MAX_CHARS) -> None:
        self._buf = ""
        self.max_chars = max_chars

    def push(self, text: str) -> list[str]:
        """Add text; return any complete sentences."""
        if not text:
            return []
        if self._buf and not self._buf.endswith((" ", "\n")) and not text.startswith((" ", "\n")):
            self._buf += " "
        self._buf += text
        return self._extract()

    def flush(self) -> list[str]:
        """Release whatever is left (end of turn, prompt, barge-in)."""
        rest = _clean(self._buf)
        self._buf = ""
        return [rest] if rest else []

    def pending(self) -> str:
        return self._buf

    def _extract(self) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._find_boundary(self._buf)
            if cut is None:
                if len(self._buf) >= self.max_chars:
                    cut = self._soft_cut(self._buf)
                    if cut is None:
                        break
                else:
                    break
            sentence = _clean(self._buf[:cut])
            self._buf = self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def _find_boundary(self, text: str) -> int | None:
        for m in _BOUNDARY.finditer(text):
            end = m.end(2)
            if m.group(3) == "" and end == len(text):
                # Terminator at the very end: only a boundary if we are sure it is
                # not a decimal/filename awaiting more characters. Treat "?" and
                # "!" as final; "." at end waits for more text unless it is a run
                # of dots or the buffer already ends in a space after it.
                if m.group(1).startswith((".",)) and not m.group(1).startswith("..."):
                    return None
                return end
            before = text[: m.start()]
            if self._is_abbreviation(before):
                continue
            if self._is_mid_token(text, m):
                continue
            return end
        return None

    @staticmethod
    def _is_abbreviation(before: str) -> bool:
        word = before.rsplit(None, 1)[-1] if before.strip() else ""
        word = word.strip("(\"'").lower()
        if not word:
            return False
        if word in _ABBREVIATIONS:
            return True
        # Single letter initials like "J." or "v."
        return len(word) == 1 and word.isalpha()

    @staticmethod
    def _is_mid_token(text: str, m: re.Match[str]) -> bool:
        # "3.14 is", "auth.py is", "v1.2 was": the terminator is inside a token when
        # there is no whitespace after it.
        return m.group(3) == "" and m.end(2) < len(text)

    @staticmethod
    def _soft_cut(text: str) -> int | None:
        # Break at the last comma/semicolon/colon or space before max_chars.
        window = text[:_MAX_CHARS]
        for sep in (", ", "; ", ": ", " "):
            i = window.rfind(sep)
            if i > 40:
                return i + len(sep)
        return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def split_sentences(text: str) -> list[str]:
    """Convenience: split a complete text into sentences."""
    b = SentenceBuffer()
    out = b.push(text)
    out.extend(b.flush())
    return out
