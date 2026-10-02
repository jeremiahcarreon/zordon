"""Shared router types and pure helpers.

The provider-facing types (``Router``, ``RouteContext``, ``RouteResult``,
``YesNoResult``) live in ``zordon.providers``; they are re-exported here so the
routing package has one import point. Everything else in this module is a pure
string function used by more than one router: utterance normalization, the
forbidden permission-phrase check, session-name fuzzy matching, the argument
heuristics for shim commands and the dispatcher-side decision policy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from zordon.providers import (
    DESTINATIONS,
    ProviderError,
    ProviderNotConfigured,
    RouteContext,
    Router,
    RouteResult,
    YesNoResult,
)
from zordon.routing import commands

__all__ = [
    "DESTINATIONS",
    "ProviderError",
    "ProviderNotConfigured",
    "RouteContext",
    "RouteResult",
    "Router",
    "YesNoResult",
    "effective_destination",
    "extract_argument",
    "forbidden_permission_phrase",
    "fuzzy_match_sessions",
    "normalize_utterance",
    "words",
]

# Filler tokens dropped from every utterance before matching.
FILLERS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "hmm", "hm", "mm", "please"})

_CONTRACTIONS = {
    "dont": "don't",
    "cant": "can't",
    "wont": "won't",
    "didnt": "didn't",
    "doesnt": "doesn't",
    "isnt": "isn't",
    "whats": "what's",
}

# Everything that is not a word character, whitespace or an apostrophe is punctuation.
_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_utterance(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, drop fillers.

    Apostrophes inside words are kept (``don't`` stays ``don't``) so the yes/no
    vocabulary can distinguish ``don't`` from ``do``.
    """
    if not text:
        return ""
    t = text.lower().replace("’", "'").replace("`", "'").replace("‘", "'")
    t = _PUNCT.sub(" ", t)
    out: list[str] = []
    for tok in _WS.split(t):
        tok = tok.strip("'")
        if not tok:
            continue
        tok = _CONTRACTIONS.get(tok, tok)
        if tok in FILLERS:
            continue
        out.append(tok)
    return " ".join(out)


def words(text: str) -> list[str]:
    return normalize_utterance(text).split()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w']){re.escape(phrase)}(?![\w'])")


_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _phrase_pattern(normalize_utterance(p)) for p in commands.FORBIDDEN_PERMISSION_PHRASES
)


def forbidden_permission_phrase(text: str) -> bool:
    """True when the utterance asks to widen a permission ("always allow" and friends).

    Matched as whole words on the normalized utterance, so "I always wanted that"
    is also refused: in a permission state that is the conservative answer.
    """
    n = normalize_utterance(text)
    if not n:
        return False
    return any(p.search(n) for p in _FORBIDDEN_PATTERNS)


# ---- session names ---------------------------------------------------------------

_SESSION_STOPWORDS = frozenset(
    {"the", "a", "an", "session", "sessions", "project", "one", "other", "to", "my", "that", "this"}
)


def _session_tokens(name: str) -> set[str]:
    return {w for w in words(name) if w not in _SESSION_STOPWORDS and len(w) > 1}


def fuzzy_match_sessions(query: str, candidates: Iterable[str]) -> list[str]:
    """Return the candidates that match ``query``, best tier only.

    Tiers: exact normalized match; one normalized string contains the other;
    shared content tokens. Candidates may be titles, directories or ids; a
    directory such as ``/home/me/code/api`` matches "api" through its path
    tokens. Returns [] when nothing matches, more than one entry when ambiguous.
    """
    q = normalize_utterance(query)
    if not q:
        return []
    qt = _session_tokens(q)
    exact: list[str] = []
    contains: list[str] = []
    overlap: list[str] = []
    for cand in candidates:
        c = str(cand)
        cn = normalize_utterance(c)
        if not cn:
            continue
        if cn == q:
            exact.append(c)
        elif q in cn or cn in q:
            contains.append(c)
        elif qt and (qt & _session_tokens(cn)):
            overlap.append(c)
    for tier in (exact, contains, overlap):
        if tier:
            return tier
    return []


# ---- argument heuristics ---------------------------------------------------------

_VERBOSITY_WORDS: tuple[tuple[str, str], ...] = (
    ("technical", "technical"),
    ("detailed", "technical"),
    ("everything", "technical"),
    ("full detail", "technical"),
    ("maximum", "technical"),
    ("minimal", "minimal"),
    ("minimum", "minimal"),
    ("brief", "minimal"),
    ("terse", "minimal"),
    ("normal", "normal"),
    ("medium", "normal"),
    ("regular", "normal"),
)
_MORE_VERBOSE = re.compile(r"\b(more verbose|more detail|more details|more talkative|verbose)\b")
_LESS_VERBOSE = re.compile(r"\b(less verbose|less detail|less details|fewer details|quieter|shorter)\b")

_ON = re.compile(r"\b(on|enable|enabled|start|narrate|tell me about|turn up)\b")
_OFF = re.compile(r"\b(off|disable|disabled|stop|don't|do not|quit|no more|turn down)\b")

_MODE_WORDS: tuple[tuple[str, str], ...] = (
    ("accept edits", "acceptEdits"),
    ("accept edit", "acceptEdits"),
    ("acceptedits", "acceptEdits"),
    ("auto accept", "acceptEdits"),
    ("plan", "plan"),
    ("planning", "plan"),
    ("default", "default"),
    ("normal", "default"),
    ("regular", "default"),
    ("standard", "default"),
    ("ask me", "default"),
    ("bypass", "bypass"),
    ("skip permissions", "bypass"),
    ("dangerously", "bypass"),
    ("yolo", "bypass"),
    ("don't ask", "dontAsk"),
    ("do not ask", "dontAsk"),
    ("auto", "auto"),
    ("automatic", "auto"),
)

_FOCUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:switch|go|move|change|jump|hop|flip)(?: over| back)? to(?: the)? (.+?)"
        r"(?: session| project| one)?$"
    ),
    re.compile(r"^(?:focus|select|open|use)(?: on)?(?: the)? (.+?)(?: session| project| one)?$"),
    re.compile(r"^(?:the )?(.+?) session$"),
)


def extract_argument(command: str, utterance: str, ctx: RouteContext | None = None) -> str | None:
    """Best-effort argument for a shim command from the utterance text.

    * set_verbosity: a level name, or ``more`` / ``less`` for relative requests
    * set_tool_chatter: ``on`` / ``off``
    * focus: the session name, resolved against ``ctx.session_names`` when that
      gives exactly one match, otherwise the raw spoken name
    * set_permission_mode: ``default`` / ``acceptEdits`` / ``plan``, or a word the
      dispatcher refuses (``auto``, ``dontAsk``, ``bypass``)
    Returns None when the command takes no argument or nothing was found.
    """
    n = normalize_utterance(utterance)
    if command == "set_verbosity":
        for word, level in _VERBOSITY_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", n):
                return level
        if _LESS_VERBOSE.search(n):
            return "less"
        if _MORE_VERBOSE.search(n):
            return "more"
        return None
    if command == "set_tool_chatter":
        if _OFF.search(n):
            return "off"
        if _ON.search(n):
            return "on"
        return None
    if command == "set_permission_mode":
        for word, mode in _MODE_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", n):
                return mode
        return None
    if command == "focus":
        return extract_session_name(n, ctx.session_names if ctx else ())
    return None


def extract_session_name(normalized: str, session_names: Sequence[str]) -> str | None:
    """Pull the spoken session name out of a focus request and resolve it."""
    raw: str | None = None
    for pat in _FOCUS_PATTERNS:
        m = pat.match(normalized)
        if m:
            raw = m.group(1).strip()
            break
    if raw is None:
        # No verb; try the whole utterance against the known names.
        raw = normalized
    for lead in ("the ", "my ", "that "):
        if raw.startswith(lead):
            raw = raw[len(lead) :]
    if not raw:
        return None
    hits = fuzzy_match_sessions(raw, session_names)
    if len(hits) == 1:
        return hits[0]
    return raw


# ---- decision policy -------------------------------------------------------------


def effective_destination(result: RouteResult, threshold: float) -> str:
    """The design's policy: ``unclear`` or confidence below threshold -> claude_code.

    Used by the dispatcher and by the eval harness so both agree on what a
    RouteResult means. A destination outside DESTINATIONS is treated as unclear.
    """
    dest = result.destination
    if dest not in DESTINATIONS or dest == "unclear":
        return "claude_code"
    if result.confidence < threshold:
        return "claude_code"
    return dest
