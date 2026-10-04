"""Prompts that arrive through Claude Code's ``PermissionRequest`` hook (decision 0019).

Claude Code runs the hook *before* it draws a permission dialog, a question menu or
the plan-approval menu, with the exact tool name and input as JSON, and waits for
the hook's answer. Zordon's hook handler POSTs that to ``/hooks/permission`` and
prints whatever Zordon answers; the session manager turns the payload into a
``PromptMatch`` (so the rest of Zordon sees an ordinary prompt), speaks it, and
resolves it with the user's yes, no, option or plan feedback:

* allow: ``{"decision": {"behavior": "allow"}}``, with ``updatedInput`` carrying
  the answers for the question tool;
* deny: ``{"decision": {"behavior": "deny", "message": ...}}``; the message is what
  Claude sees ("The user said no", or the plan feedback).

An empty answer (``{}``) means "no opinion": Claude Code draws its own dialog and
the screen reader of decision 0007 takes over, exactly as before this hook
existed. That is also what the handler prints when Zordon cannot be reached.

Only the user's explicit answer ever becomes a decision; nothing here decides on
its own. Verified live against Claude Code 2.1.288 (permissions, the question
tool with ``updatedInput.answers``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from zordon.session.prompts import PromptKind, PromptMatch, PromptOption

HOOK_SOURCE = "hook"
QUESTION_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"
SHELL_TOOLS = frozenset({"Bash", "Shell", "PowerShell"})
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "NotebookEdit"})
WRITE_TOOLS = frozenset({"Write"})
MAX_TITLE = 220


@dataclass(slots=True)
class HookPrompt:
    """One pending hook request and, once the user answered, its decision."""

    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    match: PromptMatch
    event: Event = field(default_factory=Event)
    decision: dict[str, Any] | None = None
    # AskUserQuestion: every question, answered one at a time by voice.
    questions: list[dict[str, Any]] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    question_index: int = 0

    @property
    def pending(self) -> bool:
        return self.decision is None


def allow(updated_input: dict[str, Any] | None = None) -> dict[str, Any]:
    decision: dict[str, Any] = {"behavior": "allow"}
    if updated_input is not None:
        decision["updatedInput"] = updated_input
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def deny(message: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": message}}}


NO_OPINION: dict[str, Any] = {}


def build(payload: dict[str, Any]) -> HookPrompt | None:
    """A ``HookPrompt`` for a PermissionRequest payload, or None when it is not one we handle."""
    if payload.get("hook_event_name") not in (None, "PermissionRequest"):
        return None
    tool = str(payload.get("tool_name") or "")
    raw_input = payload.get("tool_input")
    tool_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
    sid = str(payload.get("session_id") or "")
    if not tool:
        return None
    if tool == QUESTION_TOOL:
        questions = [q for q in tool_input.get("questions") or [] if isinstance(q, dict) and q.get("question")]
        if not questions:
            return None
        hp = HookPrompt(sid, tool, tool_input, question_match(questions[0], 1, len(questions)), questions=questions)
        return hp
    if tool == PLAN_TOOL:
        return HookPrompt(sid, tool, tool_input, plan_match(tool_input))
    return HookPrompt(sid, tool, tool_input, permission_match(tool, tool_input))


def question_match(q: dict[str, Any], number: int, total: int) -> PromptMatch:
    options = []
    for i, o in enumerate(q.get("options") or [], start=1):
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if label:
            options.append(PromptOption(i, label, description=str(o.get("description") or "").strip()))
    question = str(q.get("question") or "").strip()
    title = question if total == 1 else f"({number} of {total}) {question}"
    return PromptMatch(
        kind=PromptKind.QUESTION,
        title=_clip(title),
        question=question,
        options=options,
        raw_lines=[],
        confidence=1.0,
        header=str(q.get("header") or ""),
        extra={"source": HOOK_SOURCE, "tool": QUESTION_TOOL, "multi": "1" if q.get("multiSelect") else "0"},
    )


def plan_match(tool_input: dict[str, Any]) -> PromptMatch:
    plan = str(tool_input.get("plan") or "").strip()
    first = next((ln.strip().lstrip("#").strip() for ln in plan.splitlines() if ln.strip()), "")
    title = f"Plan ready: {first}" if first else "Plan ready"
    return PromptMatch(
        kind=PromptKind.PLAN,
        title=_clip(title),
        question=plan,
        options=[
            PromptOption(1, "Yes, manually approve edits"),
            PromptOption(2, "Tell Claude what to change"),
            PromptOption(3, "No"),
        ],
        raw_lines=plan.splitlines()[:40],
        confidence=1.0,
        header="Ready to code?",
        extra={"source": HOOK_SOURCE, "tool": PLAN_TOOL},
    )


def permission_match(tool: str, tool_input: dict[str, Any]) -> PromptMatch:
    short = tool.rsplit("__", 1)[-1] if tool.startswith("mcp__") else tool
    command = description = None
    target_file = None
    if short in SHELL_TOOLS:
        command = str(tool_input.get("command") or "").strip()
        description = str(tool_input.get("description") or "").strip() or None
        title = f"Bash command: {command}" if command else "Bash command"
        header = "Bash command"
    elif short in EDIT_TOOLS:
        target_file = _path(tool_input)
        title = f"Edit file {_short_path(target_file)}" if target_file else "Edit file"
        header = "Edit file"
    elif short in WRITE_TOOLS:
        target_file = _path(tool_input)
        title = f"Write file {_short_path(target_file)}" if target_file else "Write file"
        header = "Write file"
    else:
        gist = _gist(tool_input)
        title = f"{short}: {gist}" if gist else f"Use the {short} tool"
        header = short
    return PromptMatch(
        kind=PromptKind.PERMISSION,
        title=_clip(title),
        question="Do you want to proceed?",
        options=[PromptOption(1, "Yes"), PromptOption(2, "No")],
        raw_lines=[],
        confidence=1.0,
        command=command,
        target_file=target_file,
        header=header,
        description=description or "",
        extra={"source": HOOK_SOURCE, "tool": tool},
    )


def is_hook(match: PromptMatch | None) -> bool:
    return match is not None and match.extra.get("source") == HOOK_SOURCE


def answers_input(hp: HookPrompt) -> dict[str, Any]:
    """``updatedInput`` for an answered question tool: the questions as sent plus the answers."""
    return {"questions": hp.questions, "answers": dict(hp.answers)}


def _path(tool_input: dict[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _short_path(path: str | None) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if home and path.startswith(home + "/") else path


def _gist(tool_input: dict[str, Any]) -> str:
    for key in ("description", "command", "query", "pattern", "url", "prompt", "file_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1] + "…"
