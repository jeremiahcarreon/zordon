"""Events and queues shared between threads.

Ownership rule (from the design): every resource has exactly one owning thread
and threads talk only through these queues.

    SessionThread  --pane_lines-->   OutputPipelineThread --sentences--> (TTS) --playback--> AudioThread/Transport
    SessionThread  --client_events-> Transport (state, prompt, transcript rows, notices)
    Transport      --inbound_audio-> AudioThread --utterances--> Dispatcher --keystrokes--> SessionThread
    AudioThread    --client_events-> Transport (flush on barge-in, speech chunks)

Everything here is plain dataclasses + ``queue.Queue``; no thread code lives in
this module.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def now() -> float:
    return time.time()


class LineKind(str, Enum):  # noqa: UP042 - StrEnum would change str(member)
    """Pre-pass classification of one output line (or block)."""

    PROSE = "prose"
    CODE = "code"
    DIFF = "diff"
    PATH = "path"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PROGRESS = "progress"
    PERMISSION_PROMPT = "permission_prompt"
    PLAN = "plan"
    QUESTION = "question"
    ERROR = "error"
    SUMMARY = "summary"  # final sentence(s) of a turn; never filtered
    INTENT = "intent"  # first sentence(s) of a turn; never filtered at minimal
    BLANK = "blank"
    UI = "ui"  # input box, status bar, box drawing, banners


class SessionState(str, Enum):  # noqa: UP042
    IDLE = "idle"
    WORKING = "working"
    AWAITING_PERMISSION = "awaiting_permission"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    AWAITING_QUESTION = "awaiting_question"  # AskUserQuestion / option pick
    STALLED = "stalled"
    DETACHED = "detached"


class PromptKind(str, Enum):  # noqa: UP042
    PERMISSION = "permission"
    PLAN = "plan"
    QUESTION = "question"
    TRUST = "trust"


# ---- events ------------------------------------------------------------------


@dataclass(slots=True)
class PaneLine:
    """One new line of Claude Code output, either from the pane or the session jsonl."""

    session_id: str
    text: str
    ts: float = field(default_factory=now)
    source: str = "pane"  # pane | jsonl
    # Set by the jsonl source: "text", "tool_use", "tool_result", "turn_start", "turn_end".
    block: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    line_id: int = field(default_factory=lambda: next(_line_ids))


@dataclass(slots=True)
class StateChanged:
    session_id: str
    state: SessionState
    detail: str = ""
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class PromptDetected:
    session_id: str
    kind: PromptKind
    title: str
    options: list[str]
    raw_lines: list[str]
    ts: float = field(default_factory=now)
    prompt_id: int = field(default_factory=lambda: next(_prompt_ids))


@dataclass(slots=True)
class PromptCleared:
    session_id: str
    prompt_id: int
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class Sentence:
    """A unit of speech after pre-pass, filtering and normalization."""

    session_id: str
    text: str  # spoken form
    raw_text: str  # pre-pass output before normalization
    raw_line_ids: list[int]
    kind: LineKind = LineKind.PROSE
    priority: bool = False  # prompts/errors: never filtered, jump the queue
    ts: float = field(default_factory=now)
    sentence_id: int = field(default_factory=lambda: next(_sentence_ids))


@dataclass(slots=True)
class SpeechChunk:
    """PCM audio for the client/speaker. ``generation`` lets barge-in discard late chunks."""

    sentence_id: int
    seq: int
    generation: int
    pcm: bytes  # int16 little-endian mono
    sample_rate: int
    final: bool = False


@dataclass(slots=True)
class Flush:
    """Barge-in: discard every chunk with generation < ``generation``."""

    generation: int
    interrupted_sentence_id: int | None = None
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class Utterance:
    text: str
    confidence: float | None = None
    source: str = "voice"  # voice | text
    client_id: str = ""
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class Heard:
    """What speech recognition made of the last utterance, before routing: shown live on
    the page so the user sees what Zordon thinks it heard (decision 0019)."""

    text: str
    confidence: float | None = None
    client_id: str = ""
    partial: bool = False  # the utterance is still going; the text will be replaced
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class Draft:
    """The text composed by voice for a session and not yet sent: ``composing`` as it grows,
    ``sent`` when it went to the agent, ``cleared`` when thrown away."""

    session_id: str
    text: str
    state: str  # composing | sent | cleared
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class Notice:
    text: str
    level: str = "info"  # info | warning | error
    session_id: str = ""
    speak: bool = False
    ts: float = field(default_factory=now)


@dataclass(slots=True)
class TranscriptRow:
    """What the client renders: one spoken sentence, user utterance or notice, with raw lines under it."""

    row_id: int
    session_id: str
    kind: str  # spoken | user | notice | raw
    text: str
    raw_lines: list[str]
    ts: float
    sentence_id: int | None = None
    spoken: bool | None = None  # False when interrupted before playback finished


_line_ids = itertools.count(1)
_prompt_ids = itertools.count(1)
_sentence_ids = itertools.count(1)


# ---- queues ------------------------------------------------------------------


class Bus:
    """All inter-thread queues in one place. Created once per agent process."""

    def __init__(self) -> None:
        self.pane_lines: queue.Queue[PaneLine] = queue.Queue()
        self.sentences: queue.Queue[Sentence] = queue.Queue()
        self.playback: queue.Queue[SpeechChunk] = queue.Queue()
        self.inbound_audio: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self.utterances: queue.Queue[Utterance] = queue.Queue()
        self.client_events: queue.Queue[Any] = queue.Queue()
        self.stop = threading.Event()
        self._generation = itertools.count(1)
        self._gen_lock = threading.Lock()
        self.generation = next(self._generation)

    def next_generation(self) -> int:
        with self._gen_lock:
            self.generation = next(self._generation)
            return self.generation

    def publish(self, event: Any) -> None:
        """Send an event to every connected client (via the transport thread)."""
        self.client_events.put(event)


def drain(q: queue.Queue) -> int:
    """Remove everything from ``q`` without blocking. Returns how many were dropped."""
    n = 0
    while True:
        try:
            q.get_nowait()
            n += 1
        except queue.Empty:
            return n
