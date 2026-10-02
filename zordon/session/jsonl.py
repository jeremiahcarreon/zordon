"""Tail a Claude Code session transcript (``<project>/<session id>.jsonl``) for
assistant content blocks, tool results and turn boundaries.

The file is appended one record per completed content block within about half a
second (decision 0003), so this is the clean prose source: ``text`` blocks are
markdown, ``tool_use`` blocks give ``{name, input}``, and ``user`` records with
``tool_result`` blocks give outcomes. Prompts never appear here; those come from
the pane.

Format facts (``JSONL_FORMAT_VERSION``, decision 0002): the assistant record's
``type`` is ``"message"`` in the bulk of the store and ``"assistant"`` in some
fresh sessions, so both are accepted and the role is what counts; single lines can
exceed 1 MB; the file is written asynchronously, so a poll may see a partial last
line, which is kept until its newline arrives.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zordon.bus import PaneLine

log = logging.getLogger("zordon.session.jsonl")

JSONL_FORMAT_VERSION = "claude-code-2.1.287"

REJECTION_TEXT = "The user doesn't want to proceed"
RESULT_PREVIEW_CHARS = 200
MAX_READ_PER_POLL = 16 * 1024 * 1024
MAX_PARTIAL_LINE = 64 * 1024 * 1024  # give up on a line this long: the file is not what we think

ASSISTANT_TYPES = ("assistant", "message")
EVENT_KINDS = (
    "text",
    "tool_use",
    "thinking",
    "tool_result",
    "turn_end",
    "permission_mode",
    "title",
    "user_prompt",
)


@dataclass(slots=True)
class JsonlEvent:
    kind: str  # one of EVENT_KINDS
    ts: float  # record timestamp (epoch seconds) or the time it was read
    session_id: str
    record_type: str  # the raw record "type"
    text: str = ""  # text block / tool result preview / title / mode / prompt
    name: str = ""  # tool_use: tool name
    input: dict[str, Any] = field(default_factory=dict)  # tool_use: tool input
    is_error: bool = False  # tool_result
    is_rejection: bool = False  # tool_result: "The user doesn't want to proceed"
    stop_reason: str = ""  # turn_end from an assistant record
    tool_use_id: str = ""  # tool_use / tool_result
    meta: dict[str, Any] = field(default_factory=dict)


# ---- record parsing -----------------------------------------------------------------------


def parse_record(
    rec: dict[str, Any],
    *,
    include_thinking: bool = False,
    default_session: str = "",
    now: float | None = None,
) -> list[JsonlEvent]:
    """Events for one store record (0..n). Unknown record types produce nothing."""
    rtype = rec.get("type")
    if not isinstance(rtype, str):
        return []
    sid = rec.get("sessionId") if isinstance(rec.get("sessionId"), str) else default_session
    ts = parse_ts(rec.get("timestamp")) or (now if now is not None else time.time())
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}

    def ev(kind: str, **kw: Any) -> JsonlEvent:
        return JsonlEvent(kind=kind, ts=ts, session_id=sid, record_type=rtype, **kw)

    out: list[JsonlEvent] = []
    if rtype in ASSISTANT_TYPES and msg.get("role") == "assistant":
        for block in _blocks(msg.get("content")):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    out.append(ev("text", text=text))
            elif btype == "tool_use":
                name = block.get("name") if isinstance(block.get("name"), str) else ""
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                out.append(ev("tool_use", name=name, input=inp, tool_use_id=str(block.get("id") or "")))
            elif btype == "thinking" and include_thinking:
                thought = block.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    out.append(ev("thinking", text=thought))
        if msg.get("stop_reason") == "end_turn":
            out.append(ev("turn_end", stop_reason="end_turn", meta={"model": msg.get("model")}))
        return out

    if rtype == "user":
        content = msg.get("content")
        if isinstance(content, list):
            for block in _blocks(content):
                if block.get("type") != "tool_result":
                    continue
                full = result_text(block.get("content"))
                out.append(
                    ev(
                        "tool_result",
                        text=full[:RESULT_PREVIEW_CHARS],
                        is_error=bool(block.get("is_error")),
                        is_rejection=REJECTION_TEXT in full,
                        tool_use_id=str(block.get("tool_use_id") or ""),
                        meta={"chars": len(full)},
                    )
                )
        elif isinstance(content, str) and not rec.get("isMeta") and not rec.get("isCompactSummary"):
            if content.strip() and not content.lstrip().startswith("<"):
                out.append(ev("user_prompt", text=content.strip()))
        return out

    if rtype == "system" and rec.get("subtype") == "turn_duration":
        return [ev("turn_end", stop_reason="turn_duration", meta={"duration_ms": rec.get("durationMs")})]

    if rtype == "permission-mode":
        mode = rec.get("permissionMode")
        if isinstance(mode, str) and mode:
            return [ev("permission_mode", text=mode)]
        return []

    if rtype in ("ai-title", "custom-title"):
        title = rec.get("aiTitle") if rtype == "ai-title" else rec.get("customTitle")
        if isinstance(title, str) and title.strip():
            return [ev("title", text=title.strip(), meta={"source": rtype})]
        return []

    return []


def result_text(content: Any) -> str:
    """Flatten a tool_result ``content`` (string or list of text blocks) to one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def parse_ts(value: Any) -> float | None:
    """ISO-8601 (``Z`` or offset) or epoch ms/s -> epoch seconds; None when absent/invalid."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def _blocks(content: Any) -> Iterable[dict[str, Any]]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


# ---- tailing ---------------------------------------------------------------------------------


class JsonlTail:
    """Incremental reader for one transcript file.

    Starts at EOF by default (``offset=None`` and ``start_at_end=True``) so only
    new records are reported; pass ``offset=0`` to replay a file. Handles a
    partial trailing line (kept until its newline arrives), truncation (offset
    beyond the new size) and rotation (a different inode at the same path).
    """

    def __init__(
        self,
        path: Path | str,
        *,
        offset: int | None = None,
        start_at_end: bool = True,
        include_thinking: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.include_thinking = include_thinking
        self.session_id = session_id or self.path.stem
        self._buf = b""
        self._ino: int | None = None
        self._offset = 0
        self.records_seen = 0
        self.parse_errors = 0
        self.resets = 0
        if offset is not None:
            self._offset = max(0, int(offset))
            self._remember_inode()
        elif start_at_end:
            try:
                st = self.path.stat()
                self._offset = st.st_size
                self._ino = st.st_ino
            except OSError:
                self._offset = 0

    @property
    def offset(self) -> int:
        return self._offset

    def exists(self) -> bool:
        return self.path.is_file()

    def _remember_inode(self) -> None:
        try:
            self._ino = self.path.stat().st_ino
        except OSError:
            self._ino = None

    def poll(self) -> list[JsonlEvent]:
        """Read whatever was appended since the last poll and return its events."""
        try:
            st = self.path.stat()
        except OSError:
            return []  # not written yet, or gone: keep waiting
        if self._ino is None:
            self._ino = st.st_ino
        elif st.st_ino != self._ino:
            log.info("%s was replaced; reading the new file from the start", self.path.name)
            self._reset(st.st_ino)
        if st.st_size < self._offset:
            log.info("%s shrank from %d to %d; reading from the start", self.path.name, self._offset, st.st_size)
            self._reset(st.st_ino)
        if st.st_size == self._offset:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read(MAX_READ_PER_POLL)
        except OSError as e:
            log.debug("read failed on %s: %s", self.path.name, e)
            return []
        self._offset += len(data)
        return self._consume(data)

    def _reset(self, ino: int) -> None:
        self._buf = b""
        self._offset = 0
        self._ino = ino
        self.resets += 1

    def _consume(self, data: bytes) -> list[JsonlEvent]:
        self._buf += data
        if b"\n" not in self._buf:
            if len(self._buf) > MAX_PARTIAL_LINE:
                log.warning("%s: discarding an unterminated %d-byte line", self.path.name, len(self._buf))
                self._buf = b""
            return []
        head, self._buf = self._buf.rsplit(b"\n", 1)
        events: list[JsonlEvent] = []
        now = time.time()
        for raw in head.split(b"\n"):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                self.parse_errors += 1
                continue
            if not isinstance(rec, dict):
                continue
            self.records_seen += 1
            events.extend(
                parse_record(
                    rec, include_thinking=self.include_thinking, default_session=self.session_id, now=now
                )
            )
        return events

    def pending_bytes(self) -> int:
        """Bytes of an unterminated last line waiting for its newline."""
        return len(self._buf)


# ---- bus adaptation ------------------------------------------------------------------------------


def to_pane_lines(event: JsonlEvent, session_id: str) -> list[PaneLine]:
    """One ``PaneLine`` per event with ``source="jsonl"`` and ``block`` set.

    ``text`` events carry the whole markdown block in ``text`` (the pre-pass
    handles fenced code across lines); ``tool_use`` carries the tool name as text
    and ``{name, input}`` in ``meta``; ``turn_end`` carries ``stop_reason``.
    """
    meta: dict[str, Any] = {"record_type": event.record_type}
    if event.kind == "text":
        text = event.text
    elif event.kind == "tool_use":
        text = event.name
        meta.update({"name": event.name, "input": event.input, "tool_use_id": event.tool_use_id})
    elif event.kind == "tool_result":
        text = event.text
        meta.update(
            {
                "is_error": event.is_error,
                "is_rejection": event.is_rejection,
                "tool_use_id": event.tool_use_id,
                **event.meta,
            }
        )
    elif event.kind == "turn_end":
        text = ""
        meta.update({"stop_reason": event.stop_reason, **event.meta})
    elif event.kind == "permission_mode":
        text = event.text
        meta["mode"] = event.text
    elif event.kind == "title":
        text = event.text
        meta.update(event.meta)
    elif event.kind in ("thinking", "user_prompt"):
        text = event.text
    else:
        return []
    return [PaneLine(session_id=session_id, text=text, ts=event.ts, source="jsonl", block=event.kind, meta=meta)]


def file_size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0
