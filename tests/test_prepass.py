"""Pre-pass tests: replay ``eval/prepass_cases.jsonl`` and the real pane fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zordon.bus import LineKind
from zordon.output.prepass import (
    PrepassState,
    Tagged,
    flush_prepass,
    looks_like_path,
    prepass_markdown,
    prepass_pane_line,
    speak_path,
    speakable,
    strip_ansi,
)
from zordon.output.tooldesc import describe_tool_result, describe_tool_use, summarize_tests

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "prepass_cases.jsonl"
PANE = ROOT / "eval" / "fixtures" / "pane"

NOISE = {LineKind.BLANK}


def _load_cases() -> list[dict]:
    out = []
    for i, line in enumerate(CASES.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            d = json.loads(line)
            d.setdefault("id", f"case{i}")
            out.append(d)
    return out


def run_case(source: str, text: str) -> list[Tagged]:
    state = PrepassState()
    if source == "markdown":
        return [t for t in prepass_markdown(text, state) if t.kind not in NOISE]
    out: list[Tagged] = []
    for line in text.split("\n"):
        out.extend(prepass_pane_line(line, state))
    out.extend(flush_prepass(state))
    return [t for t in out if t.kind not in NOISE]


def replay_pane(text: str) -> list[Tagged]:
    state = PrepassState()
    out: list[Tagged] = []
    for line in text.splitlines():
        if line.startswith("=====FRAME"):
            continue
        out.extend(prepass_pane_line(line, state))
    out.extend(flush_prepass(state))
    return out


_CASES = _load_cases()


def test_case_file_has_enough_coverage():
    assert len(_CASES) >= 40
    sources = {c["source"] for c in _CASES}
    assert sources == {"pane", "markdown"}


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_eval_case(case: dict):
    got = run_case(case["source"], case["input"])
    expect = case["expect"]
    summary = [(t.kind.value, t.spoken) for t in got]
    assert len(got) == len(expect), f"expected {len(expect)} items, got {summary}"
    for i, (t, e) in enumerate(zip(got, expect, strict=True)):
        assert t.kind.value == e["kind"], (
            f"item {i}: kind {t.kind.value} != {e['kind']} in {summary}"
        )
        if "spoken" in e:
            assert t.spoken == e["spoken"], f"item {i}: spoken {t.spoken!r} != {e['spoken']!r}"
        if "text" in e:
            assert t.text == e["text"]
        for k, v in (e.get("meta") or {}).items():
            assert t.meta.get(k) == v, f"item {i}: meta[{k}]={t.meta.get(k)!r} != {v!r}"


# ---- invariants over every case -----------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_ui_and_progress_never_speak(case: dict):
    for t in run_case(case["source"], case["input"]):
        if t.kind in (LineKind.UI, LineKind.PROGRESS, LineKind.BLANK):
            assert t.spoken is None
        if t.spoken is not None:
            assert "\x1b" not in t.spoken
            assert "●" not in t.spoken and "⎿" not in t.spoken and "❯" not in t.spoken
            assert "```" not in t.spoken and "**" not in t.spoken


# ---- fixtures ------------------------------------------------------------------------


def _fixture(name: str) -> str:
    p = PANE / name
    if not p.exists():
        pytest.skip(f"fixture {name} not present")
    return p.read_text(encoding="utf-8", errors="replace")


def _spoken(items: list[Tagged]) -> list[tuple[str, str | None]]:
    return [
        (t.kind.value, t.spoken)
        for t in items
        if t.kind not in (LineKind.BLANK, LineKind.UI, LineKind.PROGRESS)
    ]


def test_fixture_prose_output():
    items = replay_pane(_fixture("prose_output.txt"))
    kinds = _spoken(items)
    assert ("tool_call", "writing probe.txt") in kinds
    assert ("error", "interrupted") in kinds
    assert ("tool_result", "the write to probe.txt was denied") in kinds
    assert ("path", ".tmux.conf") in kinds
    assert ("path", "tmux.conf") in kinds
    assert ("code", "a code block, 3 lines") in kinds, (
        "the tmux shell snippet collapses to one placeholder"
    )
    prose = [s for k, s in kinds if k == "prose"]
    assert any("terminal multiplexer" in (s or "") for s in prose)
    assert any(s and s.startswith("Client-server model") for s in prose)
    # Four completed turns -> four summary markers, in order after their done lines.
    markers = [t for t in items if t.kind is LineKind.SUMMARY and t.meta.get("marker")]
    assert len(markers) == 4
    # Nothing from the chrome is ever spoken.
    for t in items:
        if t.kind in (LineKind.UI, LineKind.PROGRESS):
            assert t.spoken is None
        if t.spoken:
            assert "PLUGIN" not in t.spoken and "mode on" not in t.spoken
            assert "─" not in t.spoken


def test_fixture_spinner_frames():
    text = _fixture("spinner_frames.txt")
    items = replay_pane(text)
    spinners = [t for t in items if t.kind is LineKind.PROGRESS]
    assert len(spinners) >= 30
    assert all(t.spoken is None for t in spinners)
    kinds = _spoken(items)
    assert ("tool_call", "running: listing all files in current directory") in kinds
    assert ("tool_call", "listed a directory") in kinds
    assert ("tool_call", "running a shell command") in kinds  # "Running 1 shell command…"
    # The ls -la output is tool output, never prose.
    assert not any(k == "prose" and s and "drwx" in s for k, s in kinds)
    assert any(k == "prose" and s and s.startswith("Repo near-empty") for k, s in kinds)
    # Banner and ghost text never leak.
    for t in items:
        if t.spoken:
            assert "Claude Code v" not in t.spoken
            assert "write a test for" not in t.spoken


def test_fixture_streaming_frames():
    items = replay_pane(_fixture("streaming_frames.txt"))
    kinds = _spoken(items)
    assert ("prose", "Tabs. Noted for this session.") in kinds
    assert ("tool_result", "answered: Tabs") in kinds
    assert ("code", "a code block, 3 lines") in kinds
    # Partially rendered fragments (a lone ● or -) are UI, not prose.
    assert not any(k == "prose" and s in ("●", "-") for k, s in kinds)
    assert any(t.kind is LineKind.SUMMARY and t.meta.get("marker") for t in items)


def test_fixture_plan_prompt_is_ui():
    name = "plan_denied.txt" if (PANE / "plan_denied.txt").exists() else "plan_approval.txt"
    items = replay_pane(_fixture(name))
    spoken = [t for t in items if t.spoken]
    # Plan body, options and footer are all UI for the pre-pass; the manager speaks the prompt.
    for t in spoken:
        assert "Yes, and use auto mode" not in t.spoken
        assert "manually approve edits" not in t.spoken
        assert "Ready to code" not in t.spoken
        assert "ctrl+g" not in t.spoken


@pytest.mark.parametrize(
    "name",
    [
        "idle.txt",
        "plan_idle.txt",
        "trust_dialog.txt",
        "exit.txt",
        "literal_sendkeys.txt",
        "working_no_spinner.txt",
        "bash_permission.txt",
        "write_permission.txt",
        "ask_user_question.txt",
    ],
)
def test_fixture_prompt_and_idle_screens_say_little(name: str):
    items = replay_pane(_fixture(name))
    spoken = [t.spoken for t in items if t.spoken]
    forbidden = (
        "Do you want to",
        "Esc to cancel",
        "Enter to select",
        "Enter to confirm",
        "trust this folder",
        "always allow",
        "switch to auto mode",
        "Chat about this",
        "Type something",
        "mode on",
        "Claude Code v",
        "claude --resume",
    )
    for s in spoken:
        for f in forbidden:
            assert f not in s, f"{name}: {f!r} leaked into spoken text {s!r}"


def test_ansi_fixture_matches_plain_fixture():
    plain = _fixture("prose_output.txt")
    ansi = _fixture("prose_output.ansi.txt")
    a = [
        (t.kind, t.spoken) for t in replay_pane(ansi) if t.kind not in (LineKind.BLANK, LineKind.UI)
    ]
    b = [
        (t.kind, t.spoken)
        for t in replay_pane(plain)
        if t.kind not in (LineKind.BLANK, LineKind.UI)
    ]
    assert a == b


# ---- helpers -------------------------------------------------------------------------


def test_strip_ansi_handles_sgr_and_osc():
    s = "\x1b[38;5;211m●\x1b[39m \x1b[1mWrite\x1b[0m(\x1b]8;id=1;file:///x/probe.txt\x1b\\probe.txt\x1b]8;;\x1b\\)"
    assert strip_ansi(s) == "● Write(probe.txt)"
    assert strip_ansi("plain") == "plain"
    assert strip_ansi("a\x07b\x1b]0;title\x07c") == "abc"


def test_speak_path():
    assert speak_path("/a/b/auth.py") == "auth.py"
    assert speak_path("~/.config/tmux/tmux.conf") == "tmux.conf"
    assert speak_path("docs/") == "docs"
    assert speak_path("auth.py") == "auth.py"
    assert speak_path("zordon/output/prepass.py.") == "prepass.py"


def test_looks_like_path():
    assert looks_like_path("~/.tmux.conf")
    assert looks_like_path("zordon/output/prepass.py")
    assert looks_like_path("README.md")
    assert not looks_like_path("No badges for things that do not exist")
    assert not looks_like_path("v1.2")
    assert not looks_like_path("e.g.")


def test_speakable_reduces_markup_links_paths_and_emoji():
    s = speakable("See **[docs](https://x.y/z)** at ~/.zordon/config.toml or https://a.b/c ✅ `x`")
    assert s == "See docs at config.toml or a link x"


# ---- tool descriptions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "inp", "spoken", "touches"),
    [
        (
            "Edit",
            {"file_path": "/x/auth.py", "old_string": "a", "new_string": "b"},
            "editing auth.py",
            True,
        ),
        ("MultiEdit", {"file_path": "/x/y/z.ts"}, "editing z.ts", True),
        ("NotebookEdit", {"notebook_path": "nb/a.ipynb"}, "editing a.ipynb", True),
        ("Write", {"file_path": "probe.txt", "content": "hello"}, "writing probe.txt", True),
        (
            "Bash",
            {"command": "pytest -q", "description": "Run the tests"},
            "running: Run the tests",
            False,
        ),
        ("Bash", {"command": "ls -la"}, "running a shell command", False),
        ("Read", {"file_path": "/x/bus.py"}, "reading bus.py", False),
        ("Grep", {"pattern": "def keep"}, "searching the codebase", False),
        ("Glob", {"pattern": "**/*.py"}, "searching the codebase", False),
        ("Agent", {"prompt": "do a thing"}, "delegating to a sub agent", False),
        ("Task", {"prompt": "do a thing"}, "delegating to a sub agent", False),
        ("WebFetch", {"url": "https://x"}, "looking something up on the web", False),
        ("WebSearch", {"query": "x"}, "looking something up on the web", False),
        ("TodoWrite", {"todos": []}, "updating the task list", False),
        ("TaskCreate", {"subject": "x"}, "updating the task list", False),
        ("FrobnicateThing", {}, "using the FrobnicateThing tool", False),
        ("mcp__server__do_thing", {}, "using the do_thing tool", False),
    ],
)
def test_describe_tool_use(name, inp, spoken, touches):
    t = describe_tool_use(name, inp)
    assert t.kind is LineKind.TOOL_CALL
    assert t.spoken == spoken
    assert t.meta["touches_file"] is touches


def test_describe_tool_use_placeholders():
    q = describe_tool_use(
        "AskUserQuestion", {"questions": [{"question": "Tabs or spaces?", "options": []}]}
    )
    assert q.kind is LineKind.QUESTION and q.spoken is None and q.meta["placeholder"]
    assert q.meta["question"] == "Tabs or spaces?"
    p = describe_tool_use("ExitPlanMode", {"plan": "..."})
    assert p.kind is LineKind.PLAN and p.spoken is None and p.meta["placeholder"]


def test_describe_tool_use_never_speaks_secrets_from_input():
    t = describe_tool_use(
        "Bash", {"command": "export API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"}
    )
    assert "sk-ant" not in (t.spoken or "")


@pytest.mark.parametrize(
    ("text", "flags", "kind", "spoken"),
    [
        (
            "The user doesn't want to proceed with this tool use. The tool use was rejected",
            {},
            LineKind.TOOL_RESULT,
            "that was denied",
        ),
        ("anything", {"is_rejection": True}, LineKind.TOOL_RESULT, "that was denied"),
        ("===== 12 passed, 1 warning in 0.34s =====", {}, LineKind.TOOL_RESULT, "tests passed"),
        ("2 failed, 10 passed in 1.2s", {}, LineKind.TOOL_RESULT, "tests failed"),
        ("2 failed, 10 passed in 1.2s", {"is_error": True}, LineKind.TOOL_RESULT, "tests failed"),
        ("Tests:       3 failed, 40 passed, 43 total", {}, LineKind.TOOL_RESULT, "tests failed"),
        ("Tests:       43 passed, 43 total", {}, LineKind.TOOL_RESULT, "tests passed"),
        (
            "test result: ok. 5 passed; 0 failed; 0 ignored",
            {},
            LineKind.TOOL_RESULT,
            "tests passed",
        ),
        ("  12 passing (2s)\n  1 failing", {}, LineKind.TOOL_RESULT, "tests failed"),
        ("ok  \tzordon/pkg\t0.012s", {}, LineKind.TOOL_RESULT, "tests passed"),
        ("Ran 7 tests in 0.004s\n\nOK", {}, LineKind.TOOL_RESULT, "tests passed"),
        ("Ran 7 tests in 0.004s\n\nFAILED (failures=2)", {}, LineKind.TOOL_RESULT, "tests failed"),
        ("some output", {}, LineKind.TOOL_RESULT, "done"),
        ("", {}, LineKind.TOOL_RESULT, "done"),
        # A failed tool call is a TOOL_RESULT (spoken only with tool chatter or at technical),
        # never a Zordon error: the agent explains failures in its own words.
        (
            "Error: ENOENT: no such file",
            {"is_error": True},
            LineKind.TOOL_RESULT,
            "that failed: ENOENT: no such file",
        ),
        (
            "Traceback (most recent call last):\n  File x\nValueError: bad value",
            {"is_error": True},
            LineKind.TOOL_RESULT,
            "that failed: ValueError: bad value",
        ),
        ("", {"is_error": True}, LineKind.TOOL_RESULT, "that failed"),
    ],
)
def test_describe_tool_result(text, flags, kind, spoken):
    t = describe_tool_result(text, **flags)
    assert t.kind is kind
    assert t.spoken == spoken


def test_test_summary_counts():
    assert summarize_tests("2 failed, 10 passed in 1.2s") == (10, 2)
    assert summarize_tests("Tests:       3 failed, 40 passed, 43 total") == (40, 3)
    assert summarize_tests("no tests here") is None


# ---- state behaviour -------------------------------------------------------------------


def test_code_block_state_survives_blank_lines_and_flushes():
    st = PrepassState()
    assert prepass_pane_line("    def a():", st) == []
    assert st.inside_code_block and st.code_lines == ["    def a():"]
    blank = prepass_pane_line("", st)
    assert [t.kind for t in blank] == [LineKind.BLANK]
    assert st.inside_code_block
    assert prepass_pane_line("        return 1", st) == []
    out = flush_prepass(st)
    assert len(out) == 1 and out[0].kind is LineKind.CODE and out[0].meta["lines"] == 2
    assert not st.inside_code_block and flush_prepass(st) == []


def test_done_line_resets_turn_and_prompt_state():
    st = PrepassState()
    prepass_pane_line("● Working on it.", st)
    assert st.turn_started
    prepass_pane_line(" Do you want to proceed?", st)
    assert st.inside_prompt
    out = prepass_pane_line("✻ Worked for 4s · done 8:33 PM", st)
    assert not st.turn_started and not st.inside_prompt
    assert [t.kind for t in out] == [LineKind.UI, LineKind.SUMMARY]
    assert out[1].meta["marker"] and out[1].meta["duration"] == "4s"


def test_prompt_region_ends_at_next_tool_bullet_even_without_footer():
    st = PrepassState()
    prepass_pane_line(" Create file", st)
    assert st.inside_prompt
    assert prepass_pane_line(" probe.txt", st)[0].kind is LineKind.UI
    out = prepass_pane_line("● Write(probe.txt)", st)
    assert out[-1].kind is LineKind.TOOL_CALL and not st.inside_prompt


def test_markdown_does_not_need_a_state():
    out = prepass_markdown("Hello there. Bye.")
    assert [t.spoken for t in out] == ["Hello there.", "Bye."]


def test_pane_line_never_raises_on_garbage():
    st = PrepassState()
    for junk in (
        "\x00\x01\x02",
        "│",
        "│ │ │ │",
        "╭",
        "╰",
        "┌",
        "└",
        "\x1b[",
        "  ⎿  ",
        "●",
        "❯",
        "1.",
        "-",
    ):
        prepass_pane_line(junk, st)
    flush_prepass(st)
