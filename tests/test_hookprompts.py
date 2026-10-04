"""The PermissionRequest hook (decision 0019): payload -> prompt -> the user's decision."""

from __future__ import annotations

import threading
import time
from typing import Any

from tests.test_manager import drain, started
from tests.test_manager import env as env  # noqa: F401, PLC0414 - fixture re-export
from zordon.bus import PromptCleared, PromptDetected, SessionState, StateChanged
from zordon.session import hookprompts as H
from zordon.session.prompts import PromptKind

# Payloads as Claude Code 2.1.288 sends them (captured live).
BASH = {
    "session_id": "SID",
    "transcript_path": "/home/u/.claude/projects/-home-u-proj/SID.jsonl",
    "cwd": "/home/u/proj",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "touch /home/u/proj/marker.txt", "description": "Create marker file"},
    "permission_suggestions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}],
}
QUESTION = {
    "session_id": "SID",
    "cwd": "/home/u/proj",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "Which database should the app use?",
                "header": "Database",
                "options": [
                    {"label": "PostgreSQL", "description": "Reliable relational database."},
                    {"label": "MySQL", "description": "Widely used."},
                    {"label": "SQLite", "description": "A single file, no server."},
                ],
                "multiSelect": False,
            }
        ]
    },
}
PLAN = {
    "session_id": "SID",
    "cwd": "/home/u/proj",
    "permission_mode": "plan",
    "hook_event_name": "PermissionRequest",
    "tool_name": "ExitPlanMode",
    "tool_input": {"plan": "# Add retries to the uploader\n\n1. Wrap the request in a retry loop.\n2. Add a test."},
}


def with_sid(payload: dict[str, Any], sid: str) -> dict[str, Any]:
    return dict(payload, session_id=sid)


# ---- building prompts ---------------------------------------------------------------------


def test_build_permission_question_and_plan():
    bash = H.build(BASH)
    assert bash is not None and bash.match.kind is PromptKind.PERMISSION
    assert bash.match.title == "Bash command: touch /home/u/proj/marker.txt"
    assert bash.match.command == "touch /home/u/proj/marker.txt" and bash.match.description == "Create marker file"
    assert bash.match.labels == ["Yes", "No"] and H.is_hook(bash.match)

    q = H.build(QUESTION)
    assert q is not None and q.match.kind is PromptKind.QUESTION
    assert q.match.title == "Which database should the app use?" and q.match.labels == ["PostgreSQL", "MySQL", "SQLite"]
    assert q.match.options[2].description == "A single file, no server." and len(q.questions) == 1

    plan = H.build(PLAN)
    assert plan is not None and plan.match.kind is PromptKind.PLAN
    assert plan.match.title == "Plan ready: Add retries to the uploader" and "retry loop" in plan.match.question

    edit = H.build(dict(BASH, tool_name="Edit", tool_input={"file_path": "/home/u/proj/a.py", "old_string": "x", "new_string": "y"}))
    assert edit is not None and edit.match.title == "Edit file /home/u/proj/a.py" and edit.match.target_file == "/home/u/proj/a.py"
    other = H.build(dict(BASH, tool_name="WebFetch", tool_input={"url": "https://example.com"}))
    assert other is not None and other.match.title == "WebFetch: https://example.com"
    assert H.build(dict(BASH, hook_event_name="PreToolUse")) is None
    assert H.build(dict(QUESTION, tool_input={"questions": []})) is None


def test_decision_shapes():
    assert H.allow() == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert H.deny("no")["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "no"}
    q = H.build(QUESTION)
    q.answers["Which database should the app use?"] = "SQLite"
    upd = H.allow(H.answers_input(q))["hookSpecificOutput"]["decision"]["updatedInput"]
    assert upd["answers"] == {"Which database should the app use?": "SQLite"} and upd["questions"] == QUESTION["tool_input"]["questions"]


# ---- the manager round trip ------------------------------------------------------------------


def _ask(mgr, payload: dict[str, Any], wait_sid: str | None = None) -> tuple[threading.Thread, list[dict[str, Any]]]:
    """Run permission_request on a thread, the way the transport's executor does."""
    out: list[dict[str, Any]] = []
    t = threading.Thread(target=lambda: out.append(mgr.permission_request(payload, timeout_s=5.0)), daemon=True)
    t.start()
    sid = wait_sid or payload["session_id"]
    for _ in range(100):
        if mgr.sessions[sid].hook_prompt is not None:
            break
        time.sleep(0.01)
    return t, out


def test_permission_by_voice_through_the_hook(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    drain(bus)
    t, out = _ask(mgr, with_sid(BASH, sid))
    s = mgr.sessions[sid]
    assert s.hook_prompt is not None and s.state is SessionState.AWAITING_PERMISSION and s.permission_mode == "default"
    ev = drain(bus)
    prompt = next(e for e in ev if isinstance(e, PromptDetected))
    assert prompt.kind is PromptKind.PERMISSION and prompt.options == ["Yes", "No"] and prompt.title.startswith("Bash command:")
    assert mgr.current_match(sid).description == "Create marker file"  # what prompt_speech reads out
    # Polls while the hook waits keep the prompt (the screen shows no dialog).
    mgr._poll_session(s)
    assert s.current_prompt is not None and s.state is SessionState.AWAITING_PERMISSION
    assert mgr.approve(sid)
    t.join(2)
    assert out == [H.allow()]
    assert s.hook_prompt is None and s.current_prompt is None and s.state is SessionState.WORKING
    kinds = [type(e).__name__ for e in drain(bus)]
    assert "PromptCleared" in kinds and "StateChanged" in kinds
    assert not [c for c in tmux.calls if c[0] in ("key", "literal") and c[1] == target]  # nothing typed


def test_deny_question_and_plan_through_the_hook(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    drain(bus)
    t, out = _ask(mgr, with_sid(BASH, sid))
    assert mgr.deny(sid)
    t.join(2)
    assert out[0]["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "The user said no."}

    t, out = _ask(mgr, with_sid(QUESTION, sid))
    s = mgr.sessions[sid]
    assert s.state is SessionState.AWAITING_QUESTION
    assert not mgr.answer_question(sid, "Oracle")
    assert mgr.answer_question(sid, "sqlite")
    t.join(2)
    dec = out[0]["hookSpecificOutput"]["decision"]
    assert dec["behavior"] == "allow" and dec["updatedInput"]["answers"] == {"Which database should the app use?": "SQLite"}

    t, out = _ask(mgr, with_sid(PLAN, sid))
    assert s.state is SessionState.AWAITING_PLAN_APPROVAL and s.permission_mode == "plan"
    assert mgr.plan_revise(sid, "add a timeout too")
    t.join(2)
    assert out[0]["hookSpecificOutput"]["decision"]["message"] == "The user wants changes to the plan: add a timeout too"
    t, out = _ask(mgr, with_sid(PLAN, sid))
    assert mgr.plan_approve(sid)
    t.join(2)
    assert out[0] == H.allow()
    t, out = _ask(mgr, with_sid(PLAN, sid))
    assert mgr.plan_deny(sid)
    t.join(2)
    assert out[0]["hookSpecificOutput"]["decision"]["message"] == "The user rejected the plan."


def test_two_questions_are_asked_one_at_a_time(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    two = dict(QUESTION, tool_input={"questions": [QUESTION["tool_input"]["questions"][0], {"question": "Tabs or spaces?", "options": [{"label": "Tabs"}, {"label": "Spaces"}]}]})
    drain(bus)
    t, out = _ask(mgr, with_sid(two, sid))
    s = mgr.sessions[sid]
    assert s.current_prompt.title == "(1 of 2) Which database should the app use?"
    assert mgr.answer_question(sid, 1)
    assert s.hook_prompt is not None and s.current_prompt.title == "(2 of 2) Tabs or spaces?"
    assert mgr.answer_question(sid, "Spaces")
    t.join(2)
    assert out[0]["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {"Which database should the app use?": "PostgreSQL", "Tabs or spaces?": "Spaces"}


def test_unknown_session_and_timeout_give_no_opinion(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    assert mgr.permission_request(with_sid(BASH, "nobody"), timeout_s=1.0) == {}
    # By cwd: an attached pane whose Claude session id Zordon does not know.
    assert mgr.sessions[sid].cwd == str(proj)
    t, out = _ask(mgr, dict(BASH, session_id="other-id", cwd=str(proj)), wait_sid=sid)
    assert mgr.sessions[sid].hook_prompt is not None
    assert mgr.approve(sid)
    t.join(2)
    assert out == [H.allow()]
    # Nobody answers: no opinion, prompt dropped, Claude Code draws its own dialog.
    drain(bus)
    res = mgr.permission_request(with_sid(BASH, sid), timeout_s=0.3)
    assert res == {} and mgr.sessions[sid].hook_prompt is None and mgr.sessions[sid].current_prompt is None
    assert any(isinstance(e, PromptCleared) for e in drain(bus))


def test_second_request_while_one_waits_gets_no_opinion(env):
    mgr, bus, tmux, clock, proj = env
    sid, target = started(env)
    t, out = _ask(mgr, with_sid(BASH, sid))
    assert mgr.permission_request(with_sid(QUESTION, sid), timeout_s=1.0) == {}
    assert mgr.approve(sid)
    t.join(2)
    assert out == [H.allow()]
    assert isinstance(drain(bus)[-1], StateChanged)
