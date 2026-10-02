"""DispatcherThread with a scripted router, a fake SessionControl, a real TranscriptStore
and a speak() recorder. Every branch of the dispatcher is exercised synchronously via
``handle()``; one test runs the real thread loop over the bus."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from zordon.bus import Bus, LineKind, PromptDetected, PromptKind, SessionState, Utterance
from zordon.config import Config
from zordon.providers import ProviderError
from zordon.routing.base import RouteResult, YesNoResult
from zordon.routing.dispatcher import DispatcherThread, match_option, resolve_verbosity
from zordon.routing.keyword import KeywordRouter
from zordon.routing.transcript_query import TranscriptAnswerer
from zordon.transcript.store import TranscriptStore

# ---- fakes --------------------------------------------------------------------------------


@dataclass
class FakeSummary:
    session_id: str
    directory: str
    title: str
    state: SessionState = SessionState.IDLE
    last_active: float | None = None
    attached: bool = True
    running: bool = True


class FakeSessions:
    """Implements SessionControl; records every call."""

    def __init__(self, summaries: list[FakeSummary], focused: str | None) -> None:
        self.summaries = summaries
        self._focused = focused
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.states: dict[str, SessionState] = {s.session_id: s.state for s in summaries}
        self.prompts: dict[str, PromptDetected] = {}
        self.pane: dict[str, list[str]] = {}
        self.ok = True
        self.modes: dict[str, str] = {}

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a in self.calls if n == name]

    # SessionControl
    def list_sessions(self):
        for s in self.summaries:
            s.state = self.states.get(s.session_id, s.state)
        return list(self.summaries)

    def focused(self):
        return self._focused

    def focus(self, session_id):
        self._rec("focus", session_id)
        self._focused = session_id

    def state_of(self, session_id):
        return self.states.get(session_id, SessionState.IDLE)

    def current_prompt(self, session_id):
        return self.prompts.get(session_id)

    def start(self, directory, permission_mode=None):
        self._rec("start", directory, permission_mode)
        return "new"

    def resume(self, session_id, permission_mode=None):
        self._rec("resume", session_id, permission_mode)

    def detach(self, session_id):
        self._rec("detach", session_id)

    def delete(self, session_id):
        self._rec("delete", session_id)

    def send_text(self, session_id, text):
        self._rec("send_text", session_id, text)

    def send_escape(self, session_id):
        self._rec("send_escape", session_id)

    def approve(self, session_id):
        self._rec("approve", session_id)
        return self.ok

    def deny(self, session_id):
        self._rec("deny", session_id)
        return self.ok

    def plan_approve(self, session_id):
        self._rec("plan_approve", session_id)
        return self.ok

    def plan_revise(self, session_id, feedback):
        self._rec("plan_revise", session_id, feedback)
        return self.ok

    def plan_deny(self, session_id):
        self._rec("plan_deny", session_id)
        return self.ok

    def answer_question(self, session_id, option):
        self._rec("answer_question", session_id, option)
        return self.ok

    def accept_trust(self, session_id):
        self._rec("accept_trust", session_id)
        return self.ok

    def decline_trust(self, session_id):
        self._rec("decline_trust", session_id)
        return self.ok

    def set_permission_mode(self, session_id, mode):
        self._rec("set_permission_mode", session_id, mode)
        self.modes[session_id] = mode
        return self.ok

    def permission_summary(self, session_id):
        return f"Permission mode is {self.modes.get(session_id, 'default')}."

    def last_pane_lines(self, session_id, n=10):
        return self.pane.get(session_id, [])[-n:]

    def hook_event(self, payload):
        self._rec("hook_event", payload)


class FakeRouter:
    """Scripted: answers come from dicts keyed by utterance, else defaults; can raise."""

    name = "fake"

    def __init__(self) -> None:
        self.routes: dict[str, RouteResult] = {}
        self.yes: dict[str, YesNoResult] = {}
        self.default_route = RouteResult("claude_code", 0.99)
        self.default_yes = YesNoResult("unclear", 0.0)
        self.raise_on: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def route(self, utterance, ctx):
        self.calls.append(("route", utterance))
        if utterance in self.raise_on:
            raise RuntimeError("router exploded")
        return self.routes.get(utterance, self.default_route)

    def yes_no(self, utterance):
        self.calls.append(("yes_no", utterance))
        if utterance in self.raise_on:
            raise ProviderError("router down")
        return self.yes.get(utterance, self.default_yes)

    def prompt_score(self, lines):
        return 0.0


@dataclass
class Settings:
    verbosity: str = "minimal"
    tool_chatter: bool = False
    muted: bool = False
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def settings(self) -> dict:
        return {"verbosity": self.verbosity, "tool_chatter": self.tool_chatter, "muted": self.muted}

    def set_verbosity(self, level):
        self.calls.append(("set_verbosity", level))
        self.verbosity = level

    def set_tool_chatter(self, enabled):
        self.calls.append(("set_tool_chatter", enabled))
        self.tool_chatter = enabled

    def set_muted(self, muted):
        self.calls.append(("set_muted", muted))
        self.muted = muted


class Harness:
    def __init__(self, tmp_path: Path, router=None, focused: str | None = "s1") -> None:
        self.bus = Bus()
        self.config = Config()
        self.router = router or FakeRouter()
        self.sessions = FakeSessions(
            [
                FakeSummary("s1", "/home/me/code/zordon", "zordon"),
                FakeSummary("s2", "/home/me/code/api", "api", SessionState.WORKING),
                FakeSummary("s3", "/home/me/code/api-docs", "api docs"),
            ],
            focused,
        )
        self.store = TranscriptStore(tmp_path / "t.db")
        self.spoken: list[tuple[str, str, LineKind]] = []
        self.settings = Settings()
        self.dispatcher = DispatcherThread(
            self.bus,
            self.config,
            self.router,
            self.sessions,
            self.store,
            self.speak,
            self.settings,
            answerer=TranscriptAnswerer(None),
        )

    def speak(self, text: str, session_id: str, kind: LineKind) -> None:
        assert isinstance(kind, LineKind)  # the pipeline's speak_now reads kind.value
        self.spoken.append((text, session_id, kind))

    def say(self, text: str, source: str = "voice") -> None:
        self.dispatcher.handle(Utterance(text=text, source=source))

    def said(self) -> list[str]:
        return [t for t, _, _ in self.spoken]

    def events(self, sid: str = "s1") -> list[tuple[str, str]]:
        return [(r.kind, r.text) for r in self.store.tail(sid, 50) if r.kind != "spoken"]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    harness = Harness(tmp_path)
    yield harness
    harness.store.close()


# ---- routing ----------------------------------------------------------------------------------


def test_claude_code_goes_to_the_pane_and_the_store(h: Harness):
    h.say("add retry logic to the upload handler")
    assert h.sessions.called("send_text") == [("s1", "add retry logic to the upload handler")]
    assert ("user", "add retry logic to the upload handler") in h.events()
    assert h.spoken == []


def test_low_confidence_and_unclear_go_to_claude_code(h: Harness):
    h.router.routes["what did you change"] = RouteResult("transcript_query", 0.7)
    h.router.routes["mmm"] = RouteResult("unclear", 0.99)
    h.router.routes["mute"] = RouteResult("shim_command", 0.5, command="mute")
    h.say("what did you change")
    h.say("mmm")
    h.say("mute")
    assert [a[1] for a in h.sessions.called("send_text")] == ["what did you change", "mmm", "mute"]
    assert h.settings.calls == []


def test_transcript_query_never_touches_the_pane(h: Harness):
    h.store.add_spoken(1, "s1", "I edited auth dot py, changing eight lines.", "Edited auth.py", "prose", ts=1.0)
    h.store.add_spoken(2, "s1", "All forty-two tests pass.", "tests pass", "prose", ts=2.0)
    h.router.routes["did the tests pass"] = RouteResult("transcript_query", 0.95)
    h.say("did the tests pass")
    assert h.sessions.called("send_text") == []
    assert len(h.spoken) == 1
    text, sid, kind = h.spoken[0]
    assert "tests pass" in text and sid == "s1" and kind is LineKind.PROSE
    kinds = h.events()
    assert ("user", "did the tests pass") in kinds
    assert any(k == "notice" and "tests pass" in t for k, t in kinds)


def test_transcript_query_with_empty_transcript(h: Harness):
    h.router.routes["what did it say"] = RouteResult("transcript_query", 0.95)
    h.say("what did it say")
    assert h.said() == ["I haven't heard anything from this session yet."]
    assert h.sessions.called("send_text") == []


def test_text_source_takes_the_same_path(h: Harness):
    h.router.routes["what did it say"] = RouteResult("transcript_query", 0.95)
    h.say("what did it say", source="text")
    h.say("fix the lint errors", source="text")
    assert h.sessions.called("send_text") == [("s1", "fix the lint errors")]


def test_empty_utterance_is_ignored(h: Harness):
    h.say("   ")
    assert h.router.calls == [] and h.spoken == [] and h.sessions.calls == []


def test_detached_session_is_not_typed_into(h: Harness):
    h.sessions.states["s1"] = SessionState.DETACHED
    h.say("run the tests")
    assert h.sessions.called("send_text") == []
    assert "detached" in h.said()[0].lower()


# ---- shim commands -------------------------------------------------------------------------


def _shim(h: Harness, utterance: str, command: str, argument: str | None = None) -> None:
    h.router.routes[utterance] = RouteResult("shim_command", 0.98, command=command, argument=argument)
    h.say(utterance)


def test_mute_unmute_stop_repeat(h: Harness):
    _shim(h, "mute", "mute")
    assert h.settings.muted is True and h.said()[-1] == "Muted."
    _shim(h, "unmute", "unmute")
    assert h.settings.muted is False and h.said()[-1] == "Unmuted."
    _shim(h, "stop", "stop")
    assert h.sessions.called("send_escape") == [("s1",)]
    assert h.said()[-1] == "Stopped."
    _shim(h, "repeat that", "repeat")
    assert h.said()[-1] == "Nothing to repeat yet."
    h.store.add_spoken(1, "s1", "I committed the change.", "committed", "prose")
    _shim(h, "say that again", "repeat")
    assert h.spoken[-1] == ("I committed the change.", "s1", LineKind.PROSE)
    assert h.sessions.called("send_text") == []


def test_status_speaks_state_and_last_line(h: Harness):
    h.sessions.states["s1"] = SessionState.WORKING
    h.sessions.pane["s1"] = ["⏺ Running tests", "", "  42 passed  "]
    _shim(h, "status", "status")
    assert h.said()[-1] == "zordon is working. Last line: 42 passed"
    h.sessions.states["s1"] = SessionState.AWAITING_PERMISSION
    h.sessions.pane["s1"] = []
    _shim(h, "status", "status")
    assert h.said()[-1] == "zordon is waiting on a permission prompt."


def test_set_verbosity_validates_and_resolves_relative_levels(h: Harness):
    _shim(h, "set verbosity to technical", "set_verbosity", "technical")
    assert h.settings.verbosity == "technical" and h.said()[-1] == "Verbosity set to technical."
    _shim(h, "less detail", "set_verbosity", "less")
    assert h.settings.verbosity == "normal"
    _shim(h, "be more verbose", "set_verbosity", "more")
    assert h.settings.verbosity == "technical"
    _shim(h, "set verbosity to debug", "set_verbosity", "debug")
    assert h.settings.verbosity == "technical"
    assert h.said()[-1] == "Verbosity can be minimal, normal or technical."
    _shim(h, "set verbosity", "set_verbosity", None)
    assert h.said()[-1] == "Verbosity can be minimal, normal or technical."


def test_set_tool_chatter_on_off_and_toggle(h: Harness):
    _shim(h, "turn on tool chatter", "set_tool_chatter", "on")
    assert h.settings.tool_chatter is True and h.said()[-1] == "Tool chatter on."
    _shim(h, "stop telling me about tool calls", "set_tool_chatter", "off")
    assert h.settings.tool_chatter is False
    _shim(h, "tool chatter", "set_tool_chatter", None)
    assert h.settings.tool_chatter is True


def test_focus_resolves_asks_when_ambiguous_and_reports_unknown(h: Harness):
    _shim(h, "switch to the zordon session", "focus", "zordon")
    assert h.sessions.called("focus") == [("s1",)]
    assert h.said()[-1] == "Switched to zordon."

    # "api" matches both the title "api" and "api docs" by containment; exact title wins.
    _shim(h, "switch to the api session", "focus", "api")
    assert h.sessions.called("focus")[-1] == ("s2",)
    assert h.sessions.focused() == "s2"

    # Truly ambiguous: a word shared by two sessions' paths asks, then the next utterance picks.
    h.sessions.summaries.append(FakeSummary("s4", "/home/me/code/docs-site", "docs site"))
    _shim(h, "go to docs", "focus", "docs")
    assert h.said()[-1].startswith("Which one:")
    h.say("docs site")
    assert h.sessions.called("focus")[-1] == ("s4",)
    assert h.said()[-1] == "Switched to docs site."

    _shim(h, "switch to the banana session", "focus", "banana")
    assert h.said()[-1].startswith("I don't see a session called banana.")

    _shim(h, "focus", "focus", None)
    assert h.said()[-1].startswith("Which session?")


def test_focus_other_picks_the_only_other_session(h: Harness):
    h.sessions.summaries = h.sessions.summaries[:2]
    _shim(h, "go to the other project", "focus", "other")
    assert h.sessions.called("focus") == [("s2",)]


def test_list_sessions_speaks_titles_and_states(h: Harness):
    _shim(h, "list sessions", "list_sessions")
    assert h.said()[-1] == "There are 3 sessions: zordon, idle; api, working; api docs, idle."


def test_set_permission_mode_only_voice_switchable(h: Harness):
    _shim(h, "switch to plan mode", "set_permission_mode", "plan")
    assert h.sessions.called("set_permission_mode") == [("s1", "plan")]
    assert h.said()[-1] == "Permission mode is plan."
    _shim(h, "switch to auto mode", "set_permission_mode", "auto")
    assert h.sessions.called("set_permission_mode") == [("s1", "plan")]
    assert "can't switch to auto mode by voice" in h.said()[-1]
    _shim(h, "skip permissions", "set_permission_mode", "bypass")
    assert h.sessions.called("set_permission_mode") == [("s1", "plan")]
    _shim(h, "what permission mode is this", "set_permission_mode", None)
    assert h.said()[-1] == "Permission mode is plan."
    h.sessions.ok = False
    _shim(h, "back to default permissions", "set_permission_mode", "default")
    assert h.said()[-1] == "I couldn't switch the permission mode."


def test_delete_requires_strict_yes_on_the_next_utterance(h: Harness):
    _shim(h, "delete the session", "delete")
    assert h.said()[-1] == "Delete the session zordon? Say yes to confirm."
    assert h.sessions.called("delete") == []
    h.say("yes")
    assert h.sessions.called("delete") == [("s1",)]
    assert h.said()[-1] == "Deleted zordon."

    _shim(h, "kill this session", "delete")
    h.say("yeah I guess, actually no")
    assert h.sessions.called("delete") == [("s1",)]
    assert h.said()[-1] == "Okay, not deleting."
    # The confirmation is consumed; it is not sent to Claude Code.
    assert h.sessions.called("send_text") == []

    _shim(h, "delete the session", "delete")
    h.say("no")
    assert h.sessions.called("delete") == [("s1",)]
    # A stale confirmation does not fire later.
    h.say("yes")
    assert h.sessions.called("delete") == [("s1",)]
    assert h.sessions.called("send_text") == [("s1", "yes")]


def test_detach(h: Harness):
    _shim(h, "detach", "detach")
    assert h.sessions.called("detach") == [("s1",)]
    assert h.said()[-1] == "Detached from zordon. It keeps running."


def test_unknown_command_is_refused(h: Harness):
    _shim(h, "self destruct", "self_destruct")
    assert h.said()[-1] == "I don't know that command."
    h.router.routes["x"] = RouteResult("shim_command", 0.99, command=None)
    h.say("x")
    assert h.said()[-1] == "I don't know that command."
    assert h.sessions.calls == []


def test_no_focused_session(tmp_path: Path):
    h = Harness(tmp_path, focused=None)
    h.say("list sessions")
    assert h.said()[-1].startswith("There are 3 sessions")
    h.say("switch to the api session")
    assert h.sessions.called("focus") == [("s2",)]
    h.sessions._focused = None
    h.say("run the tests")
    assert "No session is focused" in h.said()[-1]
    assert h.sessions.called("send_text") == []
    h.say("stop")
    assert h.said()[-1] == "No session is focused."
    h.store.close()


# ---- permission state -------------------------------------------------------------------------


def _await_permission(h: Harness, kind: PromptKind = PromptKind.PERMISSION) -> None:
    h.sessions.states["s1"] = SessionState.AWAITING_PERMISSION
    h.sessions.prompts["s1"] = PromptDetected("s1", kind, "Bash command", ["Yes", "Yes, and always allow", "No"], [])


def test_permission_yes_and_no_at_threshold(h: Harness):
    _await_permission(h)
    h.router.yes["yes"] = YesNoResult("yes", 0.98)
    h.router.yes["no"] = YesNoResult("no", 0.97)
    h.say("yes")
    assert h.sessions.called("approve") == [("s1",)]
    assert h.said()[-1] == "Approved."
    h.say("no")
    assert h.sessions.called("deny") == [("s1",)]
    assert h.said()[-1] == "Denied."
    assert h.sessions.called("send_text") == []
    assert all(c[0] == "yes_no" for c in h.router.calls)


def test_permission_low_confidence_and_unclear_are_read_back(h: Harness):
    _await_permission(h)
    h.router.yes["yes"] = YesNoResult("yes", 0.9)  # below voice.yes_no_confidence (0.95)
    h.say("yes")
    assert h.sessions.called("approve") == []
    assert h.said()[-1] == "I heard: yes. Yes or no?"
    h.say("yeah I guess, actually no")
    assert h.sessions.called("approve") == [] and h.sessions.called("deny") == []
    assert h.said()[-1] == "I heard: yeah I guess, actually no. Yes or no?"
    assert h.sessions.called("send_text") == []


def test_permission_always_allow_is_refused_without_asking_the_router(h: Harness):
    _await_permission(h)
    h.router.yes["yes always allow"] = YesNoResult("yes", 0.99)
    h.say("yes always allow")
    assert h.sessions.called("approve") == []
    assert h.said()[-1] == "I can't grant always-allow by voice. Yes or no?"
    assert h.router.calls == []


def test_permission_approve_failure_is_spoken(h: Harness):
    _await_permission(h)
    h.sessions.ok = False
    h.router.yes["yes"] = YesNoResult("yes", 0.99)
    h.say("yes")
    assert h.said()[-1] == "I couldn't approve that."


def test_trust_prompt_uses_trust_methods(h: Harness):
    _await_permission(h, PromptKind.TRUST)
    h.router.yes["yes"] = YesNoResult("yes", 0.99)
    h.router.yes["no"] = YesNoResult("no", 0.99)
    h.say("yes")
    assert h.sessions.called("accept_trust") == [("s1",)]
    assert h.sessions.called("approve") == []
    h.say("no")
    assert h.sessions.called("decline_trust") == [("s1",)]


def test_safe_shim_commands_still_work_during_a_prompt(h: Harness):
    _await_permission(h)
    h.say("mute")
    assert h.settings.muted is True
    assert h.sessions.called("approve") == [] and h.sessions.called("deny") == []
    h.say("repeat that")
    assert h.said()[-1] == "Nothing to repeat yet."
    # "stop" is not a safe shim here: it goes through the gate as a no.
    h.router.yes["stop"] = YesNoResult("no", 0.99)
    h.say("stop")
    assert h.sessions.called("deny") == [("s1",)]
    assert h.sessions.called("send_escape") == []


# ---- plan approval -------------------------------------------------------------------------------


def _await_plan(h: Harness) -> None:
    h.sessions.states["s1"] = SessionState.AWAITING_PLAN_APPROVAL
    h.sessions.prompts["s1"] = PromptDetected("s1", PromptKind.PLAN, "Plan", ["Yes, and use auto mode", "Yes, manually approve edits", "Tell Claude what to change"], [])


def test_plan_approve_revise_deny(h: Harness):
    _await_plan(h)
    h.router.yes["looks good go ahead"] = YesNoResult("yes", 0.97)
    h.say("looks good go ahead")
    assert h.sessions.called("plan_approve") == [("s1",)]
    assert h.said()[-1].startswith("Plan approved.")

    h.say("change the second step to use postgres instead")
    assert h.said()[-1] == "What should change?"
    assert h.sessions.called("plan_revise") == []
    h.say("use postgres instead of sqlite and skip the migration")
    assert h.sessions.called("plan_revise") == [("s1", "use postgres instead of sqlite and skip the migration")]
    assert h.said()[-1] == "Sent your changes to Claude Code."

    h.router.yes["no"] = YesNoResult("no", 0.99)
    h.say("no")
    assert h.sessions.called("plan_deny") == [("s1",)]
    assert h.said()[-1] == "Plan rejected."

    h.say("hmm let me think")
    assert h.said()[-1] == "Approve, revise, or deny the plan?"
    h.say("yes and use auto mode")
    assert h.sessions.called("plan_approve") == [("s1",)]
    assert "manual edits" in h.said()[-1]
    assert h.sessions.called("send_text") == []


def test_plan_revise_pending_is_dropped_if_state_changed(h: Harness):
    _await_plan(h)
    h.say("revise it")
    assert h.said()[-1] == "What should change?"
    h.sessions.states["s1"] = SessionState.IDLE
    h.say("run the tests")
    assert h.sessions.called("plan_revise") == []
    assert h.sessions.called("send_text") == [("s1", "run the tests")]


# ---- AskUserQuestion ----------------------------------------------------------------------------


def test_question_options_by_label_and_ordinal(h: Harness):
    h.sessions.states["s1"] = SessionState.AWAITING_QUESTION
    h.sessions.prompts["s1"] = PromptDetected("s1", PromptKind.QUESTION, "Indentation", ["Tabs", "Spaces", "Type something.", "Chat about this"], [])
    h.say("spaces please")
    assert h.sessions.called("answer_question") == [("s1", 2)]
    assert h.said()[-1] == "Picked Spaces."
    h.say("the first one")
    assert h.sessions.called("answer_question")[-1] == ("s1", 1)
    h.say("banana")
    assert h.said()[-1] == "The options are: 1, Tabs; 2, Spaces; 3, Type something.; 4, Chat about this. Which one?"
    assert h.sessions.called("send_text") == []


def test_question_without_readable_options(h: Harness):
    h.sessions.states["s1"] = SessionState.AWAITING_QUESTION
    h.say("tabs")
    assert "couldn't read the options" in h.said()[-1]


def test_match_option_and_resolve_verbosity_pure_helpers():
    opts = ["Tabs", "Spaces", "Type something.", "Chat about this"]
    assert match_option("tabs", opts) == 1
    assert match_option("option 2", opts) == 2
    assert match_option("number three", opts) == 3
    assert match_option("chat about it", opts) == 4
    assert match_option("nine", opts) is None
    assert match_option("", opts) is None
    assert match_option("tabs", []) is None
    assert resolve_verbosity(None, "minimal") is None
    assert resolve_verbosity("MORE", "technical") == "technical"
    assert resolve_verbosity("less", "normal") == "minimal"


# ---- robustness -------------------------------------------------------------------------------------


def test_router_exception_is_contained(h: Harness):
    h.router.raise_on.add("boom")
    h.say("boom")
    assert h.said()[-1] == "Something went wrong handling that."
    assert h.sessions.called("send_text") == []
    # The dispatcher keeps working.
    h.say("run the tests")
    assert h.sessions.called("send_text") == [("s1", "run the tests")]


def test_provider_error_in_yes_no_is_contained(h: Harness):
    _await_permission(h)
    h.router.raise_on.add("yes")
    h.say("yes")
    assert h.said()[-1] == "Something went wrong handling that."
    assert h.sessions.called("approve") == []


def test_speak_failure_does_not_kill_handling(tmp_path: Path):
    h = Harness(tmp_path)

    def bad_speak(text, sid, kind):
        raise RuntimeError("tts down")

    h.dispatcher.speak = bad_speak
    h.router.routes["mute"] = RouteResult("shim_command", 0.98, command="mute")
    h.say("mute")
    assert h.settings.muted is True
    h.store.close()


def test_thread_loop_consumes_the_bus(tmp_path: Path):
    h = Harness(tmp_path)
    h.dispatcher.poll_interval = 0.05
    h.dispatcher.start()
    try:
        h.bus.utterances.put(Utterance(text="run the tests"))
        h.bus.utterances.put(Utterance(text="mute", source="text"))
        deadline = time.time() + 3
        while h.dispatcher.handled < 2 and time.time() < deadline:
            time.sleep(0.02)
        # FakeRouter's default is claude_code for everything, so both are typed, in order.
        assert [a[1] for a in h.sessions.called("send_text")] == ["run the tests", "mute"]
    finally:
        h.dispatcher.stop()
        h.dispatcher.join(timeout=2)
        assert not h.dispatcher.is_alive()
        h.store.close()


def test_with_real_keyword_router_end_to_end(tmp_path: Path):
    h = Harness(tmp_path, router=KeywordRouter())
    h.store.add_spoken(1, "s1", "I edited auth dot py.", "Edited auth.py", "prose", ts=1.0)
    h.store.add_spoken(2, "s1", "All tests pass.", "tests pass", "prose", ts=2.0)
    h.say("mute")
    assert h.settings.muted is True
    h.say("what did you just change")
    assert h.sessions.called("send_text") == []
    assert "auth dot py" in h.said()[-1]
    h.say("change what you did")
    assert h.sessions.called("send_text") == [("s1", "change what you did")]
    h.say("stop retrying on 500s")
    assert h.sessions.called("send_text")[-1] == ("s1", "stop retrying on 500s")
    h.say("stop")
    assert h.sessions.called("send_escape") == [("s1",)]
    h.say("switch to the api session")
    assert h.sessions.focused() == "s2"
    h.store.close()


def test_speak_kinds_are_line_kinds(h: Harness):
    """Every kind the dispatcher speaks with must be a LineKind: speak_now reads ``kind.value``."""
    _await_permission(h)
    h.say("always allow")  # question
    h.router.yes["yes"] = YesNoResult("yes", 0.99)
    h.say("yes")  # ack
    h.sessions.states["s1"] = SessionState.IDLE
    _shim(h, "self destruct", "self_destruct")  # error
    h.router.routes["what did it say"] = RouteResult("transcript_query", 0.95)
    h.say("what did it say")  # answer
    kinds = [k for _, _, k in h.spoken]
    assert kinds == [LineKind.QUESTION, LineKind.SUMMARY, LineKind.ERROR, LineKind.PROSE]
