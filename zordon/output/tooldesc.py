"""Spoken descriptions of tool calls and tool results.

The session jsonl gives tool calls as ``{name, input}`` and tool results as
text with an ``is_error`` flag; the pane gives rendered headers such as
``Write(probe.txt)``. Both end up here so the wording is in one place.

``describe_tool_use`` returns a TOOL_CALL (or a QUESTION / PLAN placeholder for
the two tools whose content the session thread speaks through the prompt
path). ``meta["touches_file"]`` is what the verbosity filter uses at ``normal``.
"""

from __future__ import annotations

import re
from typing import Any

from zordon.bus import LineKind
from zordon.output.prepass import Tagged, speak_path, strip_ansi

__all__ = ["describe_tool_use", "describe_tool_result", "tool_input_from_pane", "summarize_tests"]

_EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Update", "StrReplace", "ApplyPatch"}
_WRITE_TOOLS = {"Write", "Create", "CreateFile"}
_READ_TOOLS = {"Read", "View", "Cat"}
_SEARCH_TOOLS = {"Grep", "Glob", "Search", "LS", "List", "Find", "LSP"}
_AGENT_TOOLS = {"Agent", "Task", "SubAgent", "Subagent"}
_WEB_TOOLS = {"WebFetch", "WebSearch", "Fetch", "Browse"}
_TASK_TOOLS = {
    "TodoWrite",
    "TodoRead",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
    "TaskStop",
}

_REJECTION_TEXT = re.compile(
    r"(?:The user doesn't want to proceed with this tool use|The tool use was rejected|"
    r"User rejected|Permission denied by user|user declined|was denied)",
    re.IGNORECASE,
)

# Test runner summaries. Each yields (passed, failed) or None.
_PYTEST = re.compile(
    r"\b(?:(?P<a>\d+) (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b[, ]*)+.*?\bin [\d.]+s\b"
)
_PYTEST_PASSED = re.compile(r"\b(\d+) passed\b")
_PYTEST_FAILED = re.compile(r"\b(\d+) failed\b")
_PYTEST_ERRORS = re.compile(r"\b(\d+) errors?\b")
_JEST = re.compile(r"Tests:\s+(?P<body>[^\n]*?\b\d+ total)")
_JEST_PART = re.compile(r"(\d+) (failed|passed|skipped|todo)")
_MOCHA_PASS = re.compile(r"\b(\d+) passing\b")
_MOCHA_FAIL = re.compile(r"\b(\d+) failing\b")
_CARGO = re.compile(
    r"test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed"
)
_GO_OK = re.compile(r"^ok\s+\S+", re.MULTILINE)
_GO_FAIL = re.compile(r"^(?:FAIL\s+\S+|--- FAIL:)", re.MULTILINE)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s", re.MULTILINE)
_UNITTEST_FAILED = re.compile(
    r"^FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", re.MULTILINE
)
_UNITTEST_OK = re.compile(r"^OK\b", re.MULTILINE)


def _basename(path: Any) -> str:
    return speak_path(str(path)) if path else ""


def _first_str(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def tool_input_from_pane(name: str, arg: str) -> dict[str, Any]:
    """Turn the argument text of a rendered ``Name(arg)`` header into an input dict."""
    arg = arg.strip()
    head = arg.split(" · ", 1)[0].strip()
    if not head:
        return {}
    if name in _EDIT_TOOLS | _WRITE_TOOLS | _READ_TOOLS:
        return {"file_path": head}
    if name in {"Bash", "Shell", "Run"}:
        return {"command": head}
    if name in _SEARCH_TOOLS:
        m = re.search(r'pattern:\s*"?([^",]+)"?', head)
        return {"pattern": m.group(1) if m else head}
    if name in _WEB_TOOLS:
        return {"url": head}
    if name in _AGENT_TOOLS:
        return {"description": head}
    return {"arg": head}


def describe_tool_use(name: str, input: dict[str, Any] | None) -> Tagged:  # noqa: A002
    """One short spoken clause for a tool call."""
    inp = dict(input or {})
    display_name = name
    short = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    meta: dict[str, Any] = {"tool": name, "touches_file": False}
    kind = LineKind.TOOL_CALL
    spoken: str | None

    if short in _EDIT_TOOLS:
        base = _basename(_first_str(inp, "file_path", "notebook_path", "path"))
        spoken = f"editing {base}" if base else "editing a file"
        meta["touches_file"] = True
        meta["file"] = _first_str(inp, "file_path", "notebook_path", "path")
    elif short in _WRITE_TOOLS:
        base = _basename(_first_str(inp, "file_path", "path"))
        spoken = f"writing {base}" if base else "writing a file"
        meta["touches_file"] = True
        meta["file"] = _first_str(inp, "file_path", "path")
    elif short in {"Bash", "Shell", "Run", "PowerShell"}:
        desc = _first_str(inp, "description")
        spoken = f"running: {desc}" if desc else "running a shell command"
        meta["command"] = _first_str(inp, "command")
        if desc:
            meta["description"] = desc
    elif short in _READ_TOOLS:
        base = _basename(_first_str(inp, "file_path", "path"))
        spoken = f"reading {base}" if base else "reading a file"
        meta["file"] = _first_str(inp, "file_path", "path")
    elif short in _SEARCH_TOOLS:
        spoken = "searching the codebase"
    elif short in _AGENT_TOOLS:
        spoken = "delegating to a sub agent"
    elif short in _WEB_TOOLS:
        spoken = "looking something up on the web"
    elif short in _TASK_TOOLS:
        spoken = "updating the task list"
    elif short == "AskUserQuestion":
        kind = LineKind.QUESTION
        spoken = None  # the session thread speaks the actual question from the pane
        meta["placeholder"] = True
        qs = inp.get("questions")
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            meta["question"] = str(qs[0].get("question", ""))
        display_name = "asked a question"
    elif short == "ExitPlanMode":
        kind = LineKind.PLAN
        spoken = None  # plan approval is spoken from the pane prompt
        meta["placeholder"] = True
        display_name = "plan ready for approval"
    elif short == "EnterPlanMode":
        spoken = "entering plan mode"
    elif short == "Skill":
        skill = _first_str(inp, "skill", "name")
        spoken = f"using the {skill} skill" if skill else "using a skill"
    else:
        spoken = f"using the {short} tool"

    text = display_name if kind is not LineKind.TOOL_CALL else (spoken or short)
    return Tagged(kind, text, spoken, f"{name}({_render_arg(inp)})", meta)


def _render_arg(inp: dict[str, Any]) -> str:
    v = _first_str(
        inp,
        "file_path",
        "notebook_path",
        "command",
        "pattern",
        "url",
        "query",
        "description",
        "arg",
    )
    return v[:120]


def summarize_tests(text: str) -> tuple[int, int] | None:
    """Recognise pytest / jest / mocha / cargo / go / unittest summaries -> (passed, failed)."""
    t = strip_ansi(text)
    m = _JEST.search(t)
    if m:
        passed = failed = 0
        for n, what in _JEST_PART.findall(m["body"]):
            if what == "passed":
                passed += int(n)
            elif what == "failed":
                failed += int(n)
        return passed, failed
    m = _CARGO.search(t)
    if m:
        return int(m["passed"]), int(m["failed"])
    if _PYTEST.search(t):
        passed = sum(int(x) for x in _PYTEST_PASSED.findall(t))
        failed = sum(int(x) for x in _PYTEST_FAILED.findall(t)) + sum(
            int(x) for x in _PYTEST_ERRORS.findall(t)
        )
        if passed or failed:
            return passed, failed
    mp, mf = _MOCHA_PASS.search(t), _MOCHA_FAIL.search(t)
    if mp or mf:
        return (int(mp.group(1)) if mp else 0), (int(mf.group(1)) if mf else 0)
    if _UNITTEST_RAN.search(t):
        ran = int(_UNITTEST_RAN.search(t).group(1))  # type: ignore[union-attr]
        mf = _UNITTEST_FAILED.search(t)
        if mf:
            failed = int(mf.group(1) or 0) + int(mf.group(2) or 0)
            return max(ran - failed, 0), failed
        if _UNITTEST_OK.search(t):
            return ran, 0
    if _GO_FAIL.search(t):
        return 0, len(_GO_FAIL.findall(t))
    if _GO_OK.search(t):
        return len(_GO_OK.findall(t)), 0
    return None


def _short_error_line(text: str) -> str:
    lines = [ln.strip() for ln in strip_ansi(text).splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[-1] if any("Traceback" in ln for ln in lines) else lines[0]
    line = re.sub(r"^(?:error|exception|fatal)\s*:?\s*", "", line, flags=re.IGNORECASE)
    if len(line) > 120:
        cut = line[:120].rsplit(" ", 1)[0]
        line = cut + "…"
    return line


def describe_tool_result(text: str, is_error: bool = False, is_rejection: bool = False) -> Tagged:
    """TOOL_RESULT or ERROR with a short spoken form."""
    clean = strip_ansi(text or "")
    first = next((ln.strip() for ln in clean.splitlines() if ln.strip()), "")
    display = first[:200]
    meta: dict[str, Any] = {"is_error": bool(is_error), "literal": True}

    if is_rejection or _REJECTION_TEXT.search(clean):
        meta["rejected"] = True
        return Tagged(LineKind.TOOL_RESULT, display or "denied", "that was denied", clean, meta)

    tests = summarize_tests(clean)
    if tests is not None:
        passed, failed = tests
        meta.update(passed=passed, failed=failed)
        if failed:
            return Tagged(LineKind.TOOL_RESULT, display, "tests failed", clean, meta)
        return Tagged(LineKind.TOOL_RESULT, display, "tests passed", clean, meta)

    if is_error:
        # A failed tool call is the agent's business: it sees the error and says what it
        # means in its own words. Spoken only with tool chatter or at technical verbosity
        # (TOOL_RESULT), never as a Zordon error: "error: Exit code 1" after every failed
        # command was noise the listener could do nothing with.
        short = _short_error_line(clean)
        spoken = f"that failed: {short}" if short else "that failed"
        return Tagged(LineKind.TOOL_RESULT, display or "error", spoken, clean, meta)

    return Tagged(LineKind.TOOL_RESULT, display or "done", "done", clean, meta)
