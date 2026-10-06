"""Deterministic router: no network, no model.

It is the first router in every chain and the last resort when every provider
fails. It is also the only router that is allowed to be *final* on its own in
the fast path (``FallbackRouter``): an exact shim command such as "mute" or a
strict yes/no must never cost a network round trip.

Everything here is pure string matching over ``base.normalize_utterance``
output. Anything it does not recognise goes to Claude Code at confidence 0.6,
below the design threshold, so a chained smarter router gets to decide while
the default stays the safe one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from zordon.routing import commands
from zordon.routing.base import (
    RouteContext,
    RouteResult,
    YesNoResult,
    extract_argument,
    forbidden_permission_phrase,
    fuzzy_match_sessions,
    normalize_utterance,
)

log = logging.getLogger("zordon.routing.keyword")

EXACT_CONFIDENCE = 0.98
PATTERN_CONFIDENCE = 0.96
# An explicit "... session" cue with a name we cannot resolve: above the design
# threshold so the dispatcher says it cannot find the session, below the fast path.
FOCUS_CUE_CONFIDENCE = 0.9
# A pattern matched but no usable argument: below the design threshold so a
# keyword-only setup sends it to Claude Code and a chained router gets to decide.
WEAK_CONFIDENCE = 0.8
DEFAULT_CONFIDENCE = 0.6
TRANSCRIPT_CONFIDENCE = 0.9
YES_NO_CONFIDENCE = 0.98

# Words that may wrap a command without changing it: "can you mute", "mute now".
_COURTESY = (
    "can you",
    "could you",
    "would you",
    "will you",
    "hey",
    "just",
    "now",
    "for now",
    "for me",
    "thanks",
    "thank you",
    "ok",
    "okay",
)
# Wake words: only ever stripped from the front ("go to zordon" names a session).
_WAKE = ("hey zordon", "zordon", "hey")

# Extra spoken forms beyond the examples in commands.py. Keys are command names.
_EXTRA_PHRASES: dict[str, tuple[str, ...]] = {
    "mute": ("quiet", "be quiet", "hush", "silence", "mute yourself", "stop talking", "shush"),
    "unmute": ("talk again", "speak again", "resume speaking", "un mute", "you can speak again", "unmute yourself"),
    "stop": ("halt", "interrupt", "cancel", "escape", "stop stop", "stop it", "stop that", "stop right there"),
    "repeat": (
        "repeat",
        "say again",
        "come again",
        "pardon",
        "one more time",
        "what was that",
        "repeat the last thing",
        "say the last thing again",
    ),
    "status": (
        "what's the status",
        "what is the status",
        "what's happening",
        "what is happening",
        "are you done",
        "is it done",
        "did it finish",
        "is it finished",
        "are you finished",
        "is it still running",
        "is it still working",
        "what's going on",
        "what is going on",
        "how's it going",
        "how is it going",
        "status report",
    ),
    "set_verbosity": ("more detail", "be less verbose", "be more verbose", "verbosity technical", "verbosity normal", "verbosity minimal"),
    "set_tool_chatter": (
        "turn off tool chatter",
        "tool chatter on",
        "tool chatter off",
        "tell me about tool calls",
        "narrate tool calls",
        "stop narrating tool calls",
        "don't tell me about tool calls",
    ),
    "list_sessions": (
        "list the sessions",
        "which sessions are there",
        "what sessions do i have",
        "show sessions",
        "show me the sessions",
        "sessions",
        "what sessions are running",
        "what sessions are available",
        "read out the sessions",
    ),
    "set_permission_mode": (
        "plan mode",
        "default mode",
        "default permissions",
        "enter plan mode",
        "exit plan mode",
        "leave plan mode",
        "switch to accept edits",
        "switch to default mode",
        "go back to default mode",
        "back to default mode",
        "normal permissions",
        "switch to auto mode",
        "auto mode",
    ),
    "delete": ("delete this session", "kill the session", "delete session", "kill session", "kill it", "delete it"),
    "detach": ("detach from this session", "stop following", "unfollow", "stop following this session", "detach from the session"),
    "hush": (
        "got it",
        "i got it",
        "okay got it",
        "ok got it",
        "say no more",
        "that's enough",
        "that is enough",
        "enough",
        "okay okay",
        "ok ok",
        "shut up",
        "hush",
        "quiet",
        "stop talking",
        "stop reading",
        "skip",
        "skip that",
        "i get it",
        "understood",
        "yeah yeah",
    ),
    "send": (
        "go ahead",
        "go ahead and send it",
        "send it",
        "send that",
        "send",
        "submit",
        "submit that",
        "that's all",
        "that is all",
        "that's it",
        "over",
        "done",
        "i'm done",
        "end of message",
        "send the message",
    ),
    "scratch": (
        "scratch that",
        "never mind",
        "nevermind",
        "clear that",
        "clear it",
        "start over",
        "forget that",
        "forget it",
        "delete that",
        "erase that",
        "cancel that message",
    ),
    "open_project": (
        "open project",
        "open a project",
        "continue a project",
        "continue a previous project",
        "continue the previous project",
        "switch project",
        "switch projects",
        "change project",
        "change projects",
    ),
    "list_projects": (
        "list projects",
        "list the projects",
        "list my projects",
        "what projects do i have",
        "what projects are there",
        "which projects do i have",
        "show projects",
        "show me the projects",
        "show my projects",
        "projects",
        "read out the projects",
    ),
    "new_project": (
        "new project",
        "start a new project",
        "create a new project",
        "make a new project",
        "start new project",
        "begin a new project",
    ),
    "admin": (
        "pause",
        "pause the project",
        "pause this project",
        "admin mode",
        "go to admin mode",
        "admin",
        "back to projects",
        "go back to projects",
        "leave the project",
        "leave this project",
        "close the project",
        "close this project",
        "stop working on this",
        "put this project away",
    ),
}

# Pattern-based commands: things with an argument the user says inline.
_VERBOSITY_PATTERN = re.compile(
    r"^(?:set |change |make )?(?:the )?verbosity(?: to| level to|$| )|"
    r"^(?:be |get |make it )?(?:more|less) (?:verbose|detailed|talkative|chatty)$|"
    r"^(?:switch|go|set) to (?:minimal|normal|technical)(?: verbosity| mode| detail)?$|"
    r"^(?:minimal|normal|technical)(?: verbosity| detail| mode)$|"
    r"^(?:more|less) detail(?:s)?$|"
    r"^(?:give me |i want )?(?:more|less|fewer) details?$"
)
_TOOL_CHATTER_PATTERN = re.compile(
    r"\btool (?:chatter|calls?|narration)\b|\bnarrat(?:e|ing) tools?\b|\bprogress (?:chatter|narration)\b"
)
_PERMISSION_MODE_PATTERN = re.compile(
    r"^(?:switch|go|change|set|put (?:it|this|claude)|move|flip)(?: back)? (?:to |into )(?:the )?"
    r"(?:plan|planning|default|normal|regular|standard|accept edits?|accept-edits|auto|automatic|bypass|yolo)"
    r"(?: permissions?| mode)?$|"
    r"^(?:plan|default|accept edits?|auto) (?:mode|permissions?)$|"
    r"^(?:enter|exit|leave|start|end) plan(?:ning)? mode$|"
    r"^(?:back|return) to (?:default|normal|regular)(?: permissions?| mode)?$|"
    r"^(?:turn on|enable|turn off|disable) (?:plan|accept edits?|auto) mode$|"
    r"^(?:what|which) permission mode(?: is this| are we in| am i in)?$"
)
_FOCUS_PATTERN = re.compile(
    r"^(?:switch|go|move|change|jump|hop|flip)(?: over| back)? to(?: the)? (?P<name>.+?)(?P<cue> session| project)?$|"
    r"^(?:focus|select)(?: on)?(?: the)? (?P<name2>.+?)(?P<cue2> session| project)?$"
)
_FOCUS_NOISE = frozenset({"next", "previous", "other", "last", "first"})
# "open the api project", "continue zordon", "work on the website project": a saved
# project by name. Checked before the focus pattern so a project that is not running
# is started rather than "not found".
_PROJECT_PATTERN = re.compile(
    r"^(?:open|continue|resume|work on|reopen|load|start)(?: up)?(?: the| my)?(?: project)? (?P<name>.+?)(?: project)?$"
)

# Transcript-query cues: anchored at the start after optional lead-ins. All of
# these ask ABOUT something that already happened; none asks for new work.
_LEAD = r"^(?:(?:so|and|hey|ok|okay|zordon|wait|right|also|quick question|remind me)\s+)*"
_TRANSCRIPT_CUES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(_LEAD + body)
    for body in (
        r"what did (?:it|you|claude|claude code|he|she|they) (?:just |last |actually )?(?:change|do|say|edit|modify|run|write|fix|touch|delete|remove|add|create|update|install|find|report|conclude|decide)\b",
        r"what (?:was|is|were) the (?:last |latest |previous |most recent )?(?:commit message|commit|error|output|result|results|summary|command|reason|message|exception|stack trace|failure|warning|diff|change)\b",
        r"what (?:was|is) that (?:error|message|output|file|command|warning)\b",
        r"what (?:error|exception|warning|message) (?:was that|did (?:it|you) (?:get|hit|see|report|mention))\b",
        r"(?:what|which) files? did (?:it|you) (?:just |last )?(?:change|edit|touch|modify|update|create|delete|write)\b",
        r"how many (?:lines|files|tests|errors|changes|commits|warnings) (?:did (?:it|you)|were|was|got)\b",
        r"did (?:the |all (?:the )?|those |my )?tests? (?:pass|fail|run|go)\b",
        r"did (?:it|you|everything|that|they|the build|the deploy|the lint|the linter|the migration|the commit) (?:pass|fail|succeed|work|finish|complete|go through|run)\b",
        r"(?:what|which) (?:test|tests|check|checks) (?:failed|passed|broke)\b",
        r"what did (?:it|you) say about\b",
        r"what did (?:it|you) say\b",
        r"what (?:just )?happened\b",
        r"what was the (?:last thing|first thing) (?:it|you) (?:said|did|changed)\b",
        r"what (?:does|did) (?:it|that|the (?:error|output|log|message)) say\b",
        r"(?:where|what) did (?:it|you) (?:put|save|write|leave) (?:that|the|it)\b",
        r"(?:what|which) (?:branch|directory|folder|file|function|module|class) (?:was that|did (?:it|you) (?:mention|say|use|touch|edit))\b",
        r"(?:is|was) (?:it|that) (?:already )?(?:committed|pushed|merged|done|finished)\b",
        r"did (?:it|you) (?:already )?(?:commit|push|merge|run the tests|run tests|finish|write|create|delete|change|edit|say)\b",
        r"what(?:'s| is) the (?:current )?(?:test|build) (?:status|result)\b",
    )
)

# Strict permission vocabulary. Multi-word phrases first so "do it" is one token.
YES_PHRASES: tuple[str, ...] = (
    "go ahead",
    "do it",
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "approve",
    "approved",
    "allow",
    "confirmed",
    "confirm",
    "affirmative",
    "proceed",
)
NO_PHRASES: tuple[str, ...] = (
    "don't do it",
    "do not do it",
    "don't allow",
    "do not allow",
    "don't do that",
    "do not do that",
    "no",
    "nope",
    "nah",
    "don't",
    "do not",
    "deny",
    "denied",
    "reject",
    "rejected",
    "cancel",
    "stop",
    "negative",
    "decline",
)
# Words allowed to appear next to a yes/no without making it ambiguous.
_YES_NO_FILLER = frozenset({"thanks", "thank", "you", "that's", "fine", "it's", "good", "and", "that"})

_PROMPT_STRONG = (
    re.compile(r"Do you want to", re.IGNORECASE),
    re.compile(r"Esc to cancel", re.IGNORECASE),
    re.compile(r"❯\s*1\.\s*Yes"),
    re.compile(r"Enter to select", re.IGNORECASE),
    re.compile(r"Would you like to proceed", re.IGNORECASE),
    re.compile(r"Do you trust the files in this folder", re.IGNORECASE),
)
_PROMPT_WEAK = (
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"^\s*(?:❯\s*)?\d+\.\s+\S"),
    re.compile(r"\?\s*$"),
    re.compile(r"\b(?:Yes|No)\b\s*$"),
)


@dataclass(slots=True)
class KeywordYesNo(YesNoResult):
    """YesNoResult with the reason the keyword gate decided as it did."""

    reason: str = ""


def _squeeze(normalized: str) -> str:
    """Remove courtesy wrappers so "can you mute now" compares equal to "mute"."""
    toks = normalized.split()
    for wake in _WAKE:
        w = wake.split()
        if toks[: len(w)] == w and len(toks) > len(w):
            toks = toks[len(w) :]
            break
    changed = True
    while changed and toks:
        changed = False
        for phrase in sorted(_COURTESY, key=len, reverse=True):
            p = phrase.split()
            if toks[: len(p)] == p and len(toks) > len(p):
                toks = toks[len(p) :]
                changed = True
            if toks[-len(p) :] == p and len(toks) > len(p):
                toks = toks[: -len(p)]
                changed = True
    return " ".join(toks)


def _build_exact_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for cmd in commands.SHIM_COMMANDS:
        phrases = list(cmd.examples) + list(cmd.aliases) + list(_EXTRA_PHRASES.get(cmd.name, ()))
        phrases.append(cmd.name.replace("_", " "))
        for p in phrases:
            n = normalize_utterance(p)
            if n and n not in table:
                table[n] = cmd.name
    return table


_EXACT: dict[str, str] = _build_exact_table()


class KeywordRouter:
    name = "keyword"

    # ---- route ---------------------------------------------------------------------

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        n = normalize_utterance(utterance)
        if not n:
            return RouteResult("unclear", 1.0, probabilities={"unclear": 1.0})
        sq = _squeeze(n)

        shim = self._match_shim(sq, ctx, raw=n)
        if shim is not None:
            return shim

        if self._is_transcript_query(sq):
            return RouteResult(
                "transcript_query",
                TRANSCRIPT_CONFIDENCE,
                probabilities={"transcript_query": TRANSCRIPT_CONFIDENCE, "claude_code": 1 - TRANSCRIPT_CONFIDENCE},
            )

        return RouteResult(
            "claude_code",
            DEFAULT_CONFIDENCE,
            probabilities={"claude_code": DEFAULT_CONFIDENCE, "unclear": 1 - DEFAULT_CONFIDENCE},
        )

    def _match_shim(self, sq: str, ctx: RouteContext, raw: str = "") -> RouteResult | None:
        cmd = _EXACT.get(raw) or _EXACT.get(sq)
        if cmd is not None:
            arg = extract_argument(cmd, sq, ctx)
            return self._shim(cmd, EXACT_CONFIDENCE, arg)

        if _VERBOSITY_PATTERN.search(sq):
            arg = extract_argument("set_verbosity", sq, ctx)
            return self._shim("set_verbosity", PATTERN_CONFIDENCE if arg else WEAK_CONFIDENCE, arg)

        if _TOOL_CHATTER_PATTERN.search(sq):
            arg = extract_argument("set_tool_chatter", sq, ctx)
            return self._shim("set_tool_chatter", PATTERN_CONFIDENCE if arg else WEAK_CONFIDENCE, arg)

        if _PERMISSION_MODE_PATTERN.search(sq):
            arg = extract_argument("set_permission_mode", sq, ctx)
            if arg is None and sq.startswith(("what", "which")):
                # "what permission mode is this": the dispatcher speaks the summary.
                return self._shim("set_permission_mode", PATTERN_CONFIDENCE, None)
            return self._shim("set_permission_mode", PATTERN_CONFIDENCE if arg else WEAK_CONFIDENCE, arg)

        project = self._match_project(sq, ctx)
        if project is not None:
            return project
        focus = self._match_focus(sq, ctx)
        if focus is not None:
            return focus
        return None

    def _match_project(self, sq: str, ctx: RouteContext) -> RouteResult | None:
        names = list(getattr(ctx, "project_names", []) or [])
        m = _PROJECT_PATTERN.match(sq)
        if not m:
            return None
        name = (m.group("name") or "").strip()
        if not name or name in ("a new project", "new project"):
            return None
        hits = fuzzy_match_sessions(name, names) if names else []
        if len(hits) == 1:
            return self._shim("open_project", PATTERN_CONFIDENCE, hits[0])
        if hits:
            return self._shim("open_project", PATTERN_CONFIDENCE, name)
        if sq.startswith(("open", "continue", "reopen", "load")) and (" project" in sq or names):
            # Named like a project but no match: the dispatcher reads the list back.
            return self._shim("open_project", FOCUS_CUE_CONFIDENCE, name)
        return None

    def _match_focus(self, sq: str, ctx: RouteContext) -> RouteResult | None:
        m = _FOCUS_PATTERN.match(sq)
        if not m:
            return None
        name = (m.group("name") or m.group("name2") or "").strip()
        cue = bool(m.group("cue") or m.group("cue2"))
        for lead in ("the ", "my "):
            if name.startswith(lead):
                name = name[len(lead) :]
        if not name:
            return None
        hits = fuzzy_match_sessions(name, ctx.session_names)
        if len(hits) == 1:
            return self._shim("focus", PATTERN_CONFIDENCE, hits[0])
        if hits:
            # Ambiguous: the dispatcher asks which one.
            return self._shim("focus", PATTERN_CONFIDENCE, name)
        if name in _FOCUS_NOISE:
            return self._shim("focus", PATTERN_CONFIDENCE, name)
        if cue:
            return self._shim("focus", FOCUS_CUE_CONFIDENCE, name)
        return None

    @staticmethod
    def _shim(command: str, confidence: float, argument: str | None) -> RouteResult:
        return RouteResult(
            "shim_command",
            confidence,
            command=command,
            argument=argument,
            probabilities={"shim_command": confidence, "claude_code": round(1 - confidence, 4)},
        )

    @staticmethod
    def _is_transcript_query(sq: str) -> bool:
        return any(p.search(sq) for p in _TRANSCRIPT_CUES)

    # ---- yes / no --------------------------------------------------------------------

    def yes_no(self, utterance: str) -> YesNoResult:
        if forbidden_permission_phrase(utterance):
            return KeywordYesNo("unclear", 0.0, reason="always")
        n = _squeeze(normalize_utterance(utterance))
        if not n:
            return KeywordYesNo("unclear", 0.0, reason="empty")
        rest, yes_hits, no_hits = _consume(n)
        leftover = [t for t in rest.split() if t not in _YES_NO_FILLER]
        if leftover:
            return KeywordYesNo("unclear", 0.0, reason="extra words")
        if yes_hits and no_hits:
            return KeywordYesNo("unclear", 0.0, reason="mixed")
        if yes_hits:
            return KeywordYesNo("yes", YES_NO_CONFIDENCE, reason="vocabulary")
        if no_hits:
            return KeywordYesNo("no", YES_NO_CONFIDENCE, reason="vocabulary")
        return KeywordYesNo("unclear", 0.0, reason="no match")

    # ---- prompt score ------------------------------------------------------------------

    def prompt_score(self, lines: list[str]) -> float:
        tail = [ln for ln in lines[-25:] if ln is not None]
        if not tail:
            return 0.0
        detected = _detect_with_prompts_module(tail)
        if detected is True:
            return 0.95
        text = "\n".join(tail)
        if any(p.search(text) for p in _PROMPT_STRONG):
            return 0.95
        last_nonblank = next((ln for ln in reversed(tail) if ln.strip()), "")
        if any(p.search(last_nonblank) for p in _PROMPT_WEAK) or any(
            _PROMPT_WEAK[1].search(ln) for ln in tail[-6:]
        ):
            return 0.5
        return 0.0


def _consume(n: str) -> tuple[str, int, int]:
    """Strip yes/no phrases from ``n``; return (remainder, yes_count, no_count)."""
    rest = f" {n} "
    yes_hits = no_hits = 0
    # Longest phrases first; NO phrases before YES so "don't do it" is one negative.
    for phrase in sorted(NO_PHRASES, key=len, reverse=True):
        pat = f" {phrase} "
        while pat in rest:
            rest = rest.replace(pat, " ", 1)
            no_hits += 1
    for phrase in sorted(YES_PHRASES, key=len, reverse=True):
        pat = f" {phrase} "
        while pat in rest:
            rest = rest.replace(pat, " ", 1)
            yes_hits += 1
    return rest.strip(), yes_hits, no_hits


def _detect_with_prompts_module(lines: list[str]) -> bool | None:
    """Second opinion from ``zordon.session.prompts`` when it is importable.

    Returns True when it detects a prompt, False when it is sure there is none,
    None when the module is absent or its API differs from what we expect.
    """
    try:
        from zordon.session import prompts  # noqa: PLC0415
    except Exception:  # noqa: BLE001  (module may not exist yet)
        return None
    detect = getattr(prompts, "detect_prompt", None)
    if not callable(detect):
        return None
    try:
        match = detect(lines)
    except Exception:  # noqa: BLE001
        log.debug("prompts.detect_prompt raised; using local patterns", exc_info=True)
        return None
    return match is not None
