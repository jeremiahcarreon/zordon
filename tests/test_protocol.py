from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from zordon.transport import protocol as P

DOC = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"


def test_every_type_is_documented():
    doc = DOC.read_text()
    for t in P.INBOUND_TYPES + P.OUTBOUND_TYPES:
        assert f"`{t}`" in doc, f"{t} missing from docs/protocol.md"
    for c in P.COMMANDS:
        assert f"`{c}" in doc, f"command {c} missing from docs/protocol.md"


def test_parse_audio_frame():
    pcm = base64.b64encode(b"\x00\x01" * 320).decode()
    msg = P.parse_inbound(json.dumps({"type": "audio", "pcm": pcm, "seq": 3}))
    assert isinstance(msg, P.AudioIn)
    assert len(msg.samples()) == 640


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "audio", "pcm": "not base64!!"},
        {"type": "audio", "pcm": base64.b64encode(b"\x00").decode()},  # odd byte count
        {"type": "audio", "pcm": base64.b64encode(b"\x00" * 4000).decode()},  # too large
        {"type": "command", "name": "rm_rf"},
        {"type": "command", "name": "approve", "extra": 1},
        {"type": "text", "text": ""},
        {"type": "nope"},
        {"no_type": True},
    ],
)
def test_rejects_invalid(bad):
    with pytest.raises(P.ProtocolError):
        P.parse_inbound(json.dumps(bad))


def test_rejects_oversized():
    with pytest.raises(P.ProtocolError):
        P.parse_inbound("x" * (P.MAX_INBOUND_BYTES + 1))


def test_commands_closed_set():
    msg = P.parse_inbound(json.dumps({"type": "command", "name": "set_verbosity", "args": {"level": "normal"}}))
    assert isinstance(msg, P.CommandIn)
    assert msg.args["level"] == "normal"


def test_outbound_dump_roundtrip():
    out = P.SpeechOut(sentence_id=1, seq=2, generation=3, sample_rate=24000, pcm="AAAA", final=True)
    d = json.loads(P.dump(out))
    assert d["type"] == "speech" and d["final"] is True
