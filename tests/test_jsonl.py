"""jsonl.py: incremental tailing of a session transcript with split writes, giant lines,
both assistant record type names, rotation and truncation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import fixtures_store as fs
from zordon.bus import PaneLine
from zordon.session.jsonl import (
    JSONL_FORMAT_VERSION,
    REJECTION_TEXT,
    JsonlEvent,
    JsonlTail,
    parse_record,
    parse_ts,
    to_pane_lines,
)

T0 = 1_790_900_000.0
SID = fs.sid(1)
CWD = "/home/u/proj"


def append(path: Path, data: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def kinds(events: list[JsonlEvent]) -> list[str]:
    return [e.kind for e in events]


def test_version_marker():
    assert JSONL_FORMAT_VERSION == "claude-code-2.1.287"


# ---- parse_record ----------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", ["assistant", "message"])
def test_assistant_blocks_both_type_names(record_type: str):
    rec = fs.assistant_record(
        SID,
        CWD,
        T0,
        [
            fs.thinking_block("let me think"),
            fs.text_block("Editing **auth.py** now."),
            fs.tool_use_block("Edit", {"file_path": "/home/u/proj/auth.py", "old_string": "a", "new_string": "b"}),
        ],
        stop_reason="end_turn",
        record_type=record_type,
    )
    events = parse_record(rec)
    assert kinds(events) == ["text", "tool_use", "turn_end"]
    text, tool, end = events
    assert text.text == "Editing **auth.py** now."
    assert text.session_id == SID and text.record_type == record_type
    assert text.ts == pytest.approx(T0)
    assert tool.name == "Edit" and tool.input["file_path"].endswith("auth.py") and tool.tool_use_id == "toolu_1"
    assert end.stop_reason == "end_turn"
    with_thinking = parse_record(rec, include_thinking=True)
    assert kinds(with_thinking) == ["thinking", "text", "tool_use", "turn_end"]
    assert with_thinking[0].text == "let me think"


def test_assistant_without_end_turn_has_no_turn_end():
    rec = fs.assistant_record(SID, CWD, T0, [fs.tool_use_block("Bash", {"command": "ls"})], stop_reason="tool_use")
    assert kinds(parse_record(rec)) == ["tool_use"]
    empty = fs.assistant_record(SID, CWD, T0, [fs.text_block("   ")])
    assert parse_record(empty) == []


def test_user_record_with_role_user_is_not_assistant():
    rec = fs.assistant_record(SID, CWD, T0, [fs.text_block("hi")], record_type="message")
    rec["message"]["role"] = "user"
    assert parse_record(rec) == []


def test_tool_result_preview_error_and_rejection():
    long = "x" * 500
    ok = parse_record(fs.tool_result_record(SID, CWD, T0, long))
    assert kinds(ok) == ["tool_result"]
    assert len(ok[0].text) == 200 and ok[0].meta["chars"] == 500
    assert not ok[0].is_error and not ok[0].is_rejection
    err = parse_record(fs.tool_result_record(SID, CWD, T0, [{"type": "text", "text": "boom"}], is_error=True))
    assert err[0].is_error and err[0].text == "boom"
    rej = parse_record(fs.tool_result_record(SID, CWD, T0, fs.REJECTION_TEXT, is_error=True))
    assert rej[0].is_rejection and rej[0].is_error
    assert REJECTION_TEXT in fs.REJECTION_TEXT
    assert rej[0].tool_use_id == "toolu_1"


def test_user_prompt_and_meta_records():
    ev = parse_record(fs.user_prompt(SID, CWD, T0, "  run the tests  "))
    assert kinds(ev) == ["user_prompt"] and ev[0].text == "run the tests"
    assert parse_record(fs.meta_user(SID, CWD, T0, "<local-command-caveat>x</local-command-caveat>")) == []
    assert kinds(parse_record(fs.turn_duration(SID, CWD, T0, 4200))) == ["turn_end"]
    assert parse_record(fs.turn_duration(SID, CWD, T0))[0].meta["duration_ms"] == 4200
    pm = parse_record(fs.permission_mode(SID, "acceptEdits"))
    assert kinds(pm) == ["permission_mode"] and pm[0].text == "acceptEdits"
    assert pm[0].session_id == SID
    ai = parse_record(fs.ai_title(SID, "Upload retry logic"))
    assert kinds(ai) == ["title"] and ai[0].text == "Upload retry logic" and ai[0].meta["source"] == "ai-title"
    ct = parse_record(fs.custom_title(SID, "my name"))
    assert ct[0].text == "my name" and ct[0].meta["source"] == "custom-title"
    assert parse_record({"type": "cost-state", "sessionId": SID}) == []
    assert parse_record({"no": "type"}) == []


def test_untimestamped_records_get_now_and_default_session():
    ev = parse_record({"type": "permission-mode", "permissionMode": "plan"}, default_session="abc", now=123.0)
    assert ev[0].ts == 123.0 and ev[0].session_id == "abc"


def test_parse_ts_forms():
    from datetime import UTC, datetime

    want = datetime(2026, 10, 1, 20, 31, tzinfo=UTC).timestamp()
    assert parse_ts("2026-10-01T20:31:00.000Z") == pytest.approx(want)
    assert parse_ts("2026-10-01T20:31:00+00:00") == pytest.approx(want)
    assert parse_ts("2026-10-01T20:31:00") == pytest.approx(want)  # naive -> UTC
    assert parse_ts(int(want * 1000)) == pytest.approx(want)
    assert parse_ts(int(want)) == pytest.approx(want)
    assert parse_ts("garbage") is None and parse_ts(None) is None


# ---- JsonlTail -----------------------------------------------------------------------------------


def test_tail_starts_at_eof_and_reads_appends(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "old prompt")]))
    tail = JsonlTail(path)
    assert tail.poll() == []  # existing content is skipped
    assert tail.offset == path.stat().st_size
    append(path, fs.jsonl_bytes([fs.assistant_record(SID, CWD, T0 + 1, [fs.text_block("hello")], stop_reason="end_turn")]))
    events = tail.poll()
    assert kinds(events) == ["text", "turn_end"]
    assert tail.poll() == []
    assert tail.session_id == SID


def test_tail_from_offset_zero_replays(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "p"), fs.ai_title(SID, "t")]))
    tail = JsonlTail(path, offset=0)
    assert kinds(tail.poll()) == ["user_prompt", "title"]


def test_tail_waits_for_newline_on_split_write(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(b"")
    tail = JsonlTail(path)
    line = fs.jsonl_bytes([fs.assistant_record(SID, CWD, T0, [fs.text_block("split across two polls")], record_type="assistant")])
    cut = len(line) // 2
    append(path, line[:cut])
    assert tail.poll() == []
    assert tail.pending_bytes() == cut
    append(path, line[cut:])
    events = tail.poll()
    assert kinds(events) == ["text"] and events[0].text == "split across two polls"
    assert tail.pending_bytes() == 0
    # A line without its newline yet, followed later by the newline and another record.
    rec2 = fs.jsonl_bytes([fs.permission_mode(SID, "plan")])
    append(path, rec2[:-1])
    assert tail.poll() == []
    append(path, b"\n" + fs.jsonl_bytes([fs.turn_duration(SID, CWD, T0 + 2)]))
    assert kinds(tail.poll()) == ["permission_mode", "turn_end"]


def test_tail_handles_a_one_megabyte_line_and_bad_json(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(b"")
    tail = JsonlTail(path)
    big = fs.assistant_record(SID, CWD, T0, [fs.text_block("y" * (1024 * 1024))], stop_reason="end_turn")
    append(path, b"this is not json\n" + fs.jsonl_bytes([big]) + fs.jsonl_bytes([fs.ai_title(SID, "after")]))
    events = tail.poll()
    assert kinds(events) == ["text", "turn_end", "title"]
    assert len(events[0].text) == 1024 * 1024
    assert tail.parse_errors == 1 and tail.records_seen == 2


def test_tail_missing_file_then_created(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    tail = JsonlTail(path)
    assert not tail.exists() and tail.poll() == []
    path.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "first")]))
    assert kinds(tail.poll()) == ["user_prompt"]


def test_tail_truncation_and_rotation(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "a"), fs.user_prompt(SID, CWD, T0, "b")]))
    tail = JsonlTail(path)
    # Truncation: the file shrinks, so it is read again from the start.
    path.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "c")]))
    ev = tail.poll()
    assert [e.text for e in ev] == ["c"] and tail.resets == 1
    # Rotation: a new inode at the same path, same size or bigger.
    rotated = tmp_path / "rotated.jsonl"
    rotated.write_bytes(fs.jsonl_bytes([fs.user_prompt(SID, CWD, T0, "d")]) + b" " * 10)
    os.replace(rotated, path)
    ev = tail.poll()
    assert [e.text for e in ev] == ["d"] and tail.resets == 2


def test_tail_incremental_turn_sequence(tmp_path: Path):
    """A realistic turn: prompt, tool_use, tool_result (rejected), text, end, duration."""
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(b"")
    tail = JsonlTail(path, include_thinking=False)
    seq = [
        fs.user_prompt(SID, CWD, T0, "touch probe_marker"),
        fs.assistant_record(SID, CWD, T0 + 1, [fs.thinking_block("hmm"), fs.tool_use_block("Bash", {"command": "touch probe_marker"})], stop_reason="tool_use"),
        fs.tool_result_record(SID, CWD, T0 + 5, fs.REJECTION_TEXT, is_error=True),
        fs.assistant_record(SID, CWD, T0 + 6, [fs.text_block("Understood, I will not create it.")], stop_reason="end_turn", record_type="message"),
        fs.turn_duration(SID, CWD, T0 + 6, 5000),
        fs.permission_mode(SID, "default"),
        fs.last_prompt(SID, "touch probe_marker"),
    ]
    got: list[JsonlEvent] = []
    for rec in seq:
        append(path, fs.jsonl_bytes([rec]))
        got.extend(tail.poll())
    assert kinds(got) == ["user_prompt", "tool_use", "tool_result", "text", "turn_end", "turn_end", "permission_mode"]
    assert got[2].is_rejection


# ---- to_pane_lines -------------------------------------------------------------------------------


def test_to_pane_lines_shapes():
    text = parse_record(fs.assistant_record(SID, CWD, T0, [fs.text_block("# Title\n\n```py\nx = 1\n```")], stop_reason="end_turn"))
    lines = to_pane_lines(text[0], SID)
    assert len(lines) == 1 and isinstance(lines[0], PaneLine)
    pl = lines[0]
    assert pl.source == "jsonl" and pl.block == "text" and pl.session_id == SID
    assert pl.text.startswith("# Title") and "```py" in pl.text  # whole block, fences intact
    assert pl.ts == pytest.approx(T0)
    end = to_pane_lines(text[1], SID)[0]
    assert end.block == "turn_end" and end.meta["stop_reason"] == "end_turn" and end.text == ""

    tool = parse_record(fs.assistant_record(SID, CWD, T0, [fs.tool_use_block("Write", {"file_path": "/p/probe.txt", "content": "hello"})]))[0]
    tl = to_pane_lines(tool, SID)[0]
    assert tl.block == "tool_use" and tl.text == "Write"
    assert tl.meta["name"] == "Write" and tl.meta["input"]["file_path"] == "/p/probe.txt"

    res = parse_record(fs.tool_result_record(SID, CWD, T0, fs.REJECTION_TEXT, is_error=True))[0]
    rl = to_pane_lines(res, SID)[0]
    assert rl.block == "tool_result" and rl.meta["is_rejection"] is True and rl.meta["is_error"] is True
    assert len(rl.text) <= 200

    mode = to_pane_lines(parse_record(fs.permission_mode(SID, "plan"))[0], SID)[0]
    assert mode.block == "permission_mode" and mode.text == "plan" and mode.meta["mode"] == "plan"
    title = to_pane_lines(parse_record(fs.custom_title(SID, "beta"))[0], SID)[0]
    assert title.block == "title" and title.text == "beta" and title.meta["source"] == "custom-title"
    unknown = JsonlEvent(kind="nope", ts=1.0, session_id=SID, record_type="x")
    assert to_pane_lines(unknown, SID) == []


def test_records_are_json_serialisable_fixtures():
    # Guard the helper: every synthetic record round-trips as one JSON line.
    recs = [fs.user_prompt(SID, CWD, T0, "a"), fs.ai_title(SID, "t"), fs.file_history_snapshot(SID, 10)]
    for raw in fs.jsonl_bytes(recs).split(b"\n"):
        if raw:
            json.loads(raw)
