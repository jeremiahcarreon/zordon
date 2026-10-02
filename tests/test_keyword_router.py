from __future__ import annotations

import pytest

from zordon.routing import commands
from zordon.routing.base import (
    RouteContext,
    RouteResult,
    effective_destination,
    extract_argument,
    forbidden_permission_phrase,
    fuzzy_match_sessions,
    normalize_utterance,
)
from zordon.routing.keyword import KeywordRouter, KeywordYesNo


@pytest.fixture
def router() -> KeywordRouter:
    return KeywordRouter()


@pytest.fixture
def ctx() -> RouteContext:
    return RouteContext(
        session_state="idle",
        transcript_tail=["I edited auth dot py."],
        focused_session="s1",
        session_names=["zordon", "api", "/home/me/code/frontend"],
        commands=list(commands.COMMAND_NAMES),
    )


# ---- helpers -------------------------------------------------------------------------


def test_normalize_strips_punctuation_case_and_fillers():
    assert normalize_utterance("Um, please MUTE!") == "mute"
    assert normalize_utterance("Don’t do it.") == "don't do it"
    assert normalize_utterance("  uh   repeat   that, please  ") == "repeat that"
    assert normalize_utterance("") == ""
    assert normalize_utterance("dont") == "don't"


@pytest.mark.parametrize(
    "text",
    ["always allow", "yes, always", "don't ask again", "do not ask again", "yes to all", "allow all", "skip permissions", "Yes and always allow it"],
)
def test_forbidden_phrases(text: str):
    assert forbidden_permission_phrase(text)


@pytest.mark.parametrize("text", ["yes", "no", "allow", "go ahead", "sure thing", "all good"])
def test_not_forbidden(text: str):
    assert not forbidden_permission_phrase(text)


def test_fuzzy_match_sessions_tiers():
    names = ["zordon", "api", "/home/me/code/frontend", "API docs"]
    assert fuzzy_match_sessions("zordon", names) == ["zordon"]
    # exact beats contains: "api" is exact, "API docs" only contains
    assert fuzzy_match_sessions("api", names) == ["api"]
    assert fuzzy_match_sessions("frontend", names) == ["/home/me/code/frontend"]
    assert fuzzy_match_sessions("the front end", names) == []
    assert fuzzy_match_sessions("", names) == []
    assert set(fuzzy_match_sessions("docs", names)) == {"API docs"}


def test_extract_argument_levels_modes_and_toggles(ctx: RouteContext):
    assert extract_argument("set_verbosity", "set verbosity to technical", ctx) == "technical"
    assert extract_argument("set_verbosity", "be more verbose", ctx) == "more"
    assert extract_argument("set_verbosity", "less detail", ctx) == "less"
    assert extract_argument("set_verbosity", "set verbosity", ctx) is None
    assert extract_argument("set_tool_chatter", "turn on tool chatter", ctx) == "on"
    assert extract_argument("set_tool_chatter", "stop telling me about tool calls", ctx) == "off"
    assert extract_argument("set_permission_mode", "switch to plan mode", ctx) == "plan"
    assert extract_argument("set_permission_mode", "accept edits mode", ctx) == "acceptEdits"
    assert extract_argument("set_permission_mode", "back to default permissions", ctx) == "default"
    assert extract_argument("set_permission_mode", "skip permissions please", ctx) == "bypass"
    assert extract_argument("focus", "switch to the api session", ctx) == "api"
    assert extract_argument("mute", "mute", ctx) is None


def test_effective_destination_policy():
    assert effective_destination(RouteResult("transcript_query", 0.9), 0.85) == "transcript_query"
    assert effective_destination(RouteResult("transcript_query", 0.8), 0.85) == "claude_code"
    assert effective_destination(RouteResult("unclear", 0.99), 0.85) == "claude_code"
    assert effective_destination(RouteResult("bogus", 0.99), 0.85) == "claude_code"
    assert effective_destination(RouteResult("shim_command", 0.98, command="mute"), 0.85) == "shim_command"


# ---- route ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance, command, argument",
    [
        ("mute", "mute", None),
        ("Please mute.", "mute", None),
        ("can you mute now", "mute", None),
        ("be quiet", "mute", None),
        ("unmute", "unmute", None),
        ("you can talk again", "unmute", None),
        ("stop", "stop", None),
        ("cancel that", "stop", None),
        ("repeat that", "repeat", None),
        ("what did you say", "repeat", None),
        ("status", "status", None),
        ("are you still working", "status", None),
        ("set verbosity to technical", "set_verbosity", "technical"),
        ("minimal verbosity", "set_verbosity", "minimal"),
        ("be more verbose", "set_verbosity", "more"),
        ("less detail", "set_verbosity", "less"),
        ("turn on tool chatter", "set_tool_chatter", "on"),
        ("stop telling me about tool calls", "set_tool_chatter", "off"),
        ("list sessions", "list_sessions", None),
        ("what sessions are there", "list_sessions", None),
        ("switch to plan mode", "set_permission_mode", "plan"),
        ("accept edits mode", "set_permission_mode", "acceptEdits"),
        ("back to default permissions", "set_permission_mode", "default"),
        ("delete the session", "delete", None),
        ("kill this session", "delete", None),
        ("detach", "detach", None),
        ("leave this session running", "detach", None),
    ],
)
def test_shim_commands_match_with_high_confidence(router, ctx, utterance, command, argument):
    r = router.route(utterance, ctx)
    assert r.destination == "shim_command"
    assert r.command == command
    assert r.argument == argument
    assert r.confidence >= 0.95


def test_every_command_example_routes_to_its_command(router, ctx):
    for cmd in commands.SHIM_COMMANDS:
        for example in cmd.examples:
            r = router.route(example, ctx)
            assert r.destination == "shim_command", example
            assert r.command == cmd.name, example


def test_focus_fuzzy_session_names(router, ctx):
    r = router.route("switch to the API session", ctx)
    assert (r.destination, r.command, r.argument) == ("shim_command", "focus", "api")
    r = router.route("focus the frontend session", ctx)
    assert (r.command, r.argument) == ("focus", "/home/me/code/frontend")
    r = router.route("go to zordon", ctx)
    assert (r.command, r.argument) == ("focus", "zordon")
    r = router.route("go to the other project", ctx)
    assert (r.command, r.argument) == ("focus", "other")
    # Unknown name with an explicit session cue: still focus, but not fast-path final.
    r = router.route("switch to the foo session", ctx)
    assert (r.command, r.argument) == ("focus", "foo")
    assert 0.85 <= r.confidence < 0.95
    # No cue and no known name: not a shim command at all.
    r = router.route("switch to using tabs", ctx)
    assert r.destination == "claude_code"


def test_focus_ambiguous_name_keeps_raw_argument(router):
    ctx = RouteContext(session_state="idle", session_names=["api-docs", "api-server"])
    r = router.route("switch to the api session", ctx)
    assert r.command == "focus"
    assert r.argument == "api"  # dispatcher asks which one


@pytest.mark.parametrize(
    "utterance",
    [
        "what did you just change",
        "what did it change",
        "What file did it just change?",
        "what was the commit message",
        "did the tests pass",
        "what did it say",
        "what did it say about the migration",
        "how many lines did it edit",
        "which test failed",
        "so, what did you change",
    ],
)
def test_transcript_query_cues(router, ctx, utterance):
    r = router.route(utterance, ctx)
    assert r.destination == "transcript_query"
    assert r.confidence == pytest.approx(0.9)


@pytest.mark.parametrize(
    "utterance",
    [
        "change what you did",
        "stop retrying on 500s",
        "yes",
        "add retry logic to the upload handler",
        "run the tests again",
        "can you check if the tests pass now",
        "tell me what you changed and then revert it",
        "delete the old migration files",
        "mute the logger in the tests",
        "focus on the error handling first",
        "set the log level to debug",
        "explain why the build is failing",
    ],
)
def test_everything_else_is_claude_code_below_threshold(router, ctx, utterance):
    r = router.route(utterance, ctx)
    assert r.destination == "claude_code"
    assert r.confidence == pytest.approx(0.6)


def test_empty_or_filler_only_is_unclear(router, ctx):
    assert router.route("", ctx).destination == "unclear"
    assert router.route("um uh", ctx).destination == "unclear"


# ---- yes / no ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    ["yes", "Yeah.", "yep", "yup", "sure", "ok", "okay", "go ahead", "do it", "approve", "allow", "confirmed", "affirmative", "proceed", "yes please", "sure, go ahead", "okay yes"],
)
def test_yes_vocabulary(router, utterance):
    r = router.yes_no(utterance)
    assert r.answer == "yes"
    assert r.confidence >= 0.95


@pytest.mark.parametrize(
    "utterance",
    ["no", "nope", "don't", "do not", "deny", "reject", "cancel", "stop", "negative", "no thanks", "don't do it", "no, don't"],
)
def test_no_vocabulary(router, utterance):
    r = router.yes_no(utterance)
    assert r.answer == "no"
    assert r.confidence >= 0.95


@pytest.mark.parametrize(
    "utterance",
    ["yeah I guess, actually no", "yes no", "run the tests first", "maybe", "yes but only this once", "", "hmm"],
)
def test_unclear_answers(router, utterance):
    r = router.yes_no(utterance)
    assert r.answer == "unclear"
    assert r.confidence == 0.0


@pytest.mark.parametrize("utterance", ["always allow", "yes always", "yes and don't ask again", "allow all", "yes to all"])
def test_forbidden_phrase_is_unclear_with_reason_always(router, utterance):
    r = router.yes_no(utterance)
    assert isinstance(r, KeywordYesNo)
    assert r.answer == "unclear"
    assert r.confidence == 0.0
    assert r.reason == "always"


# ---- prompt score --------------------------------------------------------------------


def test_prompt_score_levels(router, pane_fixture):
    permission = pane_fixture("bash_permission.txt").splitlines()
    question = pane_fixture("ask_user_question.txt").splitlines()
    assert router.prompt_score(permission) == pytest.approx(0.95)
    assert router.prompt_score(question) == pytest.approx(0.95)
    assert router.prompt_score(["⏺ Edited auth.py", "Running tests..."]) == 0.0
    assert router.prompt_score(["Shall I continue?"]) == pytest.approx(0.5)
    assert router.prompt_score([]) == 0.0
    assert router.prompt_score(["Do you want to proceed?", "❯ 1. Yes", "  2. No"]) == pytest.approx(0.95)
