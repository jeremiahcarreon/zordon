"""Helpers that build a synthetic ``~/.claude`` tree in a temp directory.

Record shapes follow what was observed in Claude Code 2.1.287's store (decision
0002): conversation records carry ``parentUuid, uuid, timestamp, cwd, sessionId,
version, gitBranch``; metadata records (``ai-title``, ``custom-title``,
``permission-mode``, ``last-prompt``) have no timestamp; ``history.jsonl`` lines
have exactly ``display, pastedContents, timestamp (ms), project, sessionId``; the
registry ``sessions/<pid>.json`` has ``pid, sessionId, cwd, startedAt, procStart,
version, kind, status, tmux``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from zordon.session.discovery import encode_project_dir, proc_start_ticks

VERSION = "2.1.287"


def make_claude_home(root: Path) -> Path:
    home = root / "claude-home"
    (home / "projects").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    return home


def sid(n: int = 1) -> str:
    """A deterministic, valid session uuid for test ``n``."""
    return str(uuid.UUID(int=0x11DDE9FD43CC44438F40572EBF530000 + n))


def iso(seconds: float) -> str:
    """Epoch seconds -> the store's ISO-8601 ``Z`` form with milliseconds."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int((seconds % 1) * 1000):03d}Z"


# ---- records ---------------------------------------------------------------------------------


def conv_record(
    rec_type: str,
    session_id: str,
    cwd: str,
    ts: float,
    message: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": session_id,
        "version": VERSION,
        "gitBranch": "main",
        "type": rec_type,
        "uuid": str(uuid.uuid4()),
        "timestamp": iso(ts),
    }
    if message is not None:
        rec["message"] = message
    rec.update(extra)
    return rec


def user_prompt(session_id: str, cwd: str, ts: float, text: str, **extra: Any) -> dict[str, Any]:
    return conv_record(
        "user",
        session_id,
        cwd,
        ts,
        {"role": "user", "content": text},
        promptSource="typed",
        origin={"kind": "human"},
        **extra,
    )


def meta_user(session_id: str, cwd: str, ts: float, text: str) -> dict[str, Any]:
    """A local-command caveat: ``isMeta`` and angle-bracket content, never a prompt."""
    return conv_record("user", session_id, cwd, ts, {"role": "user", "content": text}, isMeta=True)


def assistant_record(
    session_id: str,
    cwd: str,
    ts: float,
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str | None = None,
    record_type: str = "message",
    model: str = "claude-haiku-4-5",
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "type": "message",
    }
    return conv_record(record_type, session_id, cwd, ts, msg, requestId="req_x")


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def thinking_block(text: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "sig"}


def tool_use_block(name: str, tool_input: dict[str, Any], tool_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result_record(
    session_id: str,
    cwd: str,
    ts: float,
    content: str | list[dict[str, Any]],
    *,
    tool_id: str = "toolu_1",
    is_error: bool = False,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    return conv_record("user", session_id, cwd, ts, {"role": "user", "content": [block]}, toolUseResult={})


REJECTION_TEXT = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what you are doing."
)


def turn_duration(session_id: str, cwd: str, ts: float, ms: int = 4200) -> dict[str, Any]:
    return conv_record("system", session_id, cwd, ts, subtype="turn_duration", durationMs=ms)


def meta(rec_type: str, session_id: str, **fields: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"type": rec_type, "sessionId": session_id}
    rec.update(fields)
    return rec


def ai_title(session_id: str, title: str) -> dict[str, Any]:
    return meta("ai-title", session_id, aiTitle=title)


def custom_title(session_id: str, title: str) -> dict[str, Any]:
    return meta("custom-title", session_id, customTitle=title)


def permission_mode(session_id: str, mode: str) -> dict[str, Any]:
    return meta("permission-mode", session_id, permissionMode=mode)


def last_prompt(session_id: str, text: str) -> dict[str, Any]:
    return meta("last-prompt", session_id, leafUuid=str(uuid.uuid4()), lastPrompt=text)


def file_history_snapshot(session_id: str, size: int) -> dict[str, Any]:
    """A giant metadata line (real files have lines over 1 MB)."""
    return {"type": "file-history-snapshot", "messageId": str(uuid.uuid4()), "snapshot": {"blob": "x" * size}, "sessionId": session_id}


# ---- files -------------------------------------------------------------------------------


def jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in records)


def write_session(claude_home: Path, cwd: str, session_id: str, records: list[dict[str, Any]], mtime: float | None = None) -> Path:
    pdir = claude_home / "projects" / encode_project_dir(cwd)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{session_id}.jsonl"
    path.write_bytes(jsonl_bytes(records))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_history(claude_home: Path, entries: list[tuple[str, str, float, str]]) -> Path:
    """``entries``: (display, project, epoch_seconds, session_id)."""
    path = claude_home / "history.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for display, project, ts, session_id in entries:
            fh.write(
                json.dumps(
                    {
                        "display": display,
                        "pastedContents": {},
                        "timestamp": int(ts * 1000),
                        "project": project,
                        "sessionId": session_id,
                    }
                )
                + "\n"
            )
    return path


def write_registry_entry(
    claude_home: Path,
    pid: int,
    session_id: str,
    cwd: str,
    *,
    proc_start: str | None = None,
    status: str = "idle",
    kind: str = "interactive",
    tmux: str | None = None,
    filename: str | None = None,
    **extra: Any,
) -> Path:
    entry: dict[str, Any] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": 1787756876626,
        "procStart": proc_start if proc_start is not None else (proc_start_ticks(pid) or "0"),
        "version": VERSION,
        "peerProtocol": 1,
        "kind": kind,
        "entrypoint": "cli",
        "status": status,
        "updatedAt": 1787962759501,
    }
    if tmux:
        entry["tmux"] = tmux
    entry.update(extra)
    path = claude_home / "sessions" / (filename or f"{pid}.json")
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def write_settings(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path
