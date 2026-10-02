import pytest

from zordon.bus import LineKind as K
from zordon.output.verbosity import keep


@pytest.mark.parametrize("level", ["minimal", "normal", "technical"])
@pytest.mark.parametrize("kind", [K.PERMISSION_PROMPT, K.PLAN, K.QUESTION, K.ERROR, K.SUMMARY])
def test_never_filtered(level, kind):
    assert keep(kind, level, tool_chatter=False)


@pytest.mark.parametrize("level", ["minimal", "normal", "technical"])
def test_ui_and_progress_dropped_everywhere(level):
    assert not keep(K.UI, level, tool_chatter=True)
    assert not keep(K.PROGRESS, level, tool_chatter=True)
    assert not keep(K.BLANK, level, tool_chatter=True)


def test_minimal_speaks_intent_and_summary_only():
    assert keep(K.INTENT, "minimal", False)
    assert keep(K.SUMMARY, "minimal", False)
    assert not keep(K.PROSE, "minimal", False)
    assert not keep(K.TOOL_CALL, "minimal", False)
    assert not keep(K.DIFF, "minimal", False)
    assert not keep(K.CODE, "minimal", False)


def test_normal_adds_file_names_not_counts():
    assert keep(K.PROSE, "normal", False)
    assert keep(K.PATH, "normal", False)
    assert keep(K.TOOL_CALL, "normal", False, touches_file=True)
    assert not keep(K.TOOL_CALL, "normal", False, touches_file=False)
    assert not keep(K.DIFF, "normal", False)


def test_technical_keeps_everything():
    for k in (K.PROSE, K.PATH, K.TOOL_CALL, K.TOOL_RESULT, K.DIFF, K.CODE):
        assert keep(k, "technical", False)


def test_tool_chatter_is_independent_toggle():
    assert keep(K.TOOL_CALL, "minimal", True)
    assert keep(K.TOOL_RESULT, "minimal", True)
    assert not keep(K.DIFF, "minimal", True)
