from pathlib import Path

from zordon.transcript.store import TranscriptStore


def test_raw_spoken_link_and_redaction(tmp_path: Path):
    s = TranscriptStore(tmp_path / "t.db")
    r1 = s.add_raw("sess", "⏺ Edited auth.py", ts=1.0)
    r2 = s.add_raw("sess", "export API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", ts=2.0)
    sid = s.add_spoken(7, "sess", "I edited auth dot p y.", "Edited auth.py", "prose", raw_ids=[r1, r2], ts=3.0)
    assert sid > 0
    raw = s.raw_for(7)
    assert raw[0] == "⏺ Edited auth.py"
    assert "sk-ant" not in raw[1] and "[redacted]" in raw[1]

    s.mark_unspoken(7)
    rows = s.tail("sess")
    assert len(rows) == 1
    assert rows[0].spoken is False
    assert rows[0].sentence_id == 7
    assert rows[0].raw_lines == raw

    s.add_event("sess", "user", "what did you change", ts=4.0)
    rows = s.tail("sess")
    assert [r.kind for r in rows] == ["spoken", "user"]
    assert s.spoken_tail_text("sess") == ["I edited auth dot p y."]
    assert s.last_spoken("sess") == "I edited auth dot p y."
    s.close()


def test_survives_reopen(tmp_path: Path):
    p = tmp_path / "t.db"
    s = TranscriptStore(p)
    s.add_spoken(1, "a", "hello.", "hello.", "prose")
    s.close()
    s2 = TranscriptStore(p)
    assert s2.last_spoken("a") == "hello."
    assert s2.next_row_id() == 2


def test_add_raw_masks_a_key_wrapped_across_two_pane_lines(tmp_path: Path):
    """SEC-5: the second half of a hard-wrapped key must not be stored in clear."""
    from zordon.transcript.redaction import MASK

    s = TranscriptStore(tmp_path / "t.db")
    key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL"
    tail = "MNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz01234567890123456789abcdefghijklmnopqrstuvwxyzAA"
    s.add_raw("sess", f"  export ANTHROPIC_API_KEY={key}", ts=1.0)
    s.add_raw("sess", tail, ts=2.0)
    # another session's line in between must not confuse the memory
    s.add_raw("other", tail, ts=2.5)
    s.add_raw("sess", "ran the tests", ts=3.0)
    rows = s.raw_tail("sess")
    assert rows == [f"  export ANTHROPIC_API_KEY={MASK}", MASK, "ran the tests"]
    assert "MNOPQRSTUV" not in "".join(rows)
    # the other session had no wrapped predecessor, so its line is plain
    assert s.raw_tail("other") == [tail]
    s.forget_session("sess")
    s.close()
