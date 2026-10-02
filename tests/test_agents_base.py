"""The generic adapter (``zordon.agents.base.BaseAdapter``) and the registry.

Synthetic screens only: the generic adapter knows no specific tool, so the
fixtures are the shapes most terminal tools share. The detector must stay
conservative (a false prompt narrows voice input to yes/no), so the false
positives below matter as much as the hits.
"""

from __future__ import annotations

import pytest

from zordon import agents
from zordon.agents.base import (
    GENERIC_INFO,
    AgentAdapter,
    BaseAdapter,
    HookRequest,
    LaunchSpec,
    SessionInfo,
)
from zordon.bus import PromptKind
from zordon.session.prompts import PromptMatch, PromptOption


@pytest.fixture
def adapter() -> BaseAdapter:
    return BaseAdapter()


def detect(adapter: BaseAdapter, lines: list[str]) -> PromptMatch | None:
    return adapter.detect_prompt(adapter.parse(lines))


# ---- prompts that must be detected ------------------------------------------------------


@pytest.mark.parametrize(
    "lines, question",
    [
        (["$ tool run", "About to delete 3 files.", "Proceed? (y/n) "], "Proceed?"),
        (["Installing deps...", "Continue? [Y/n]"], "Continue?"),
        (["Overwrite config.toml? [y/N]: "], "Overwrite config.toml?"),
        (["Apply 2 edits to main.py (yes/no):"], "Apply 2 edits to main.py"),
        (["Run `make test`? [yes/no] "], "Run `make test`?"),
    ],
    ids=["y/n", "Y/n", "y/N", "yes/no", "yes/no-brackets"],
)
def test_inline_yes_no_prompts(adapter: BaseAdapter, lines: list[str], question: str):
    m = detect(adapter, lines)
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.question == question and m.title == question
    assert m.labels == ["Yes", "No"]
    assert m.extra.get("inline_yn") == "1"
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 2
    assert m.confidence >= 0.8


def test_numbered_yes_no_menu_with_question(adapter: BaseAdapter):
    m = detect(adapter, ["Do you want to run `rm -rf build`?", "", "  1. Yes", "  2. No", ""])
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.question == "Do you want to run `rm -rf build`?"
    assert [(o.index, o.label) for o in m.options] == [(1, "Yes"), (2, "No")]
    assert "inline_yn" not in m.extra
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 2
    assert m.raw_lines[0] == "Do you want to run `rm -rf build`?"


def test_pointer_and_unsafe_labels(adapter: BaseAdapter):
    m = detect(adapter, ["Allow network access?", "❯ 1. Yes", "  2. Yes, always allow", "  3. No"])
    assert m is not None
    assert m.selected is not None and m.selected.index == 1
    unsafe = [o.label for o in m.options if o.unsafe]
    assert unsafe == ["Yes, always allow"]
    assert adapter.yes_option(m) == 1  # never the widening variant
    assert adapter.no_option(m) == 3


@pytest.mark.parametrize(
    "label",
    [
        "Yes, and don't ask again",
        "Yes, for this session",
        "Allow all future commands",
        "Yes, switch to auto mode",
        "Approve (auto-approve from now on)",
    ],
)
def test_widening_labels_are_unsafe_and_never_the_yes(adapter: BaseAdapter, label: str):
    m = detect(adapter, ["Allow this?", f"  1. {label}", "  2. No"])
    assert m is not None
    assert m.options[0].unsafe
    assert adapter.yes_option(m) is None  # the only yes-ish option widens permissions
    assert adapter.no_option(m) == 2


def test_allow_deny_wording_counts_as_yes_no(adapter: BaseAdapter):
    m = detect(adapter, ["Allow this command?", "> 1) Allow", "  2) Allow for this session", "  3) Deny"])
    assert m is not None
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 3
    assert m.options[1].unsafe and m.selected is not None and m.selected.index == 1


def test_lettered_menu_is_numbered_top_down_and_carries_the_keys(adapter: BaseAdapter):
    m = detect(adapter, ["Apply the patch?", "[a] Approve", "[d] Deny"])
    assert m is not None
    assert [(o.index, o.label) for o in m.options] == [(1, "Approve"), (2, "Deny")]
    assert m.extra == {"key1": "a", "key2": "d"}
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 2


def test_menu_without_a_question_line_is_lower_confidence(adapter: BaseAdapter):
    m = detect(adapter, ["some output", "", "1. Yes", "2. No"])
    assert m is not None
    assert m.question == "" and m.title == "Permission request"
    assert m.confidence < 0.8


def test_menu_with_a_hint_line_below_it(adapter: BaseAdapter):
    m = detect(adapter, ["Continue with the install?", "  1. Yes", "  2. No", "", "Use arrow keys, Enter to confirm"])
    assert m is not None and m.question == "Continue with the install?"


# ---- false positives that must stay quiet ---------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        ["Here is what I found:", "1. The config file", "2. The main module", "3. The tests", "$ "],
        ["Would you like me to also update the docs?", "I can do that next.", "> "],
        ["Pick a file?", "1. Yes", "2. Maybe"],  # no no-ish option
        ["Steps:", "1. Yes we can", "", "", "2. No we can't", "$ "],  # a blank run breaks the menu
        ["Earlier:", "1. Yes", "2. No", "that was then", "and more text", "and more", "end"],  # menu not at the bottom
        ["1. Yes", "2. No", "user@host:~$ "],  # menu above a shell prompt
        ["user@host:~/proj$ "],
        ["⠋ Thinking..."],
        ["Done.", "> "],
        ["(y/n) was the question earlier", "and here is the answer", "> "],
        ["y/n"],  # the cue alone, no brackets
        [],
    ],
    ids=[
        "numbered-list",
        "prose-question",
        "yes-without-no",
        "menu-split-by-blanks",
        "menu-far-above",
        "menu-above-shell",
        "shell",
        "spinner",
        "input-box",
        "cue-not-at-end",
        "bare-cue",
        "empty",
    ],
)
def test_no_prompt_on_ordinary_screens(adapter: BaseAdapter, lines: list[str]):
    assert detect(adapter, lines) is None


# ---- idle / working / exited heuristics ---------------------------------------------------------


def test_idle_working_exited_heuristics(adapter: BaseAdapter):
    assert adapter.is_idle(adapter.parse(["Done.", "> "]))
    assert adapter.is_idle(adapter.parse(["Done.", "❯ "]))
    assert adapter.input_quiet(adapter.parse(["Done.", "› type here"]))
    assert not adapter.is_idle(adapter.parse(["⠋ Thinking..."]))
    assert adapter.is_working(adapter.parse(["⠋ Thinking..."]))
    assert adapter.is_working(adapter.parse(["working on it", "(esc to interrupt)"]))
    assert not adapter.is_working(adapter.parse(["Done.", "> "]))
    assert adapter.exited(adapter.parse(["user@host:~/proj$ "]))
    assert adapter.exited(adapter.parse(["output", "# "]))
    assert not adapter.exited(adapter.parse(["Done.", "> "]))
    assert not adapter.exited(adapter.parse([]))
    assert adapter.permission_mode_from_screen(adapter.parse(["⏸ plan mode on"])) is None
    assert not adapter.uses_alternate_screen()


def test_yes_no_helpers_on_hand_built_matches(adapter: BaseAdapter):
    m = PromptMatch(
        kind=PromptKind.PERMISSION,
        title="t",
        question="q",
        options=[PromptOption(1, "Yes, always", unsafe=True), PromptOption(2, "ok"), PromptOption(3, "cancel")],
        raw_lines=[],
        confidence=1.0,
    )
    assert adapter.yes_option(m) == 2
    assert adapter.no_option(m) == 3
    assert adapter.plan_approve_option(m) is None and adapter.plan_revise_option(m) is None
    assert adapter.trust_accept_option(m) is None and adapter.trust_decline_option(m) is None
    assert adapter.question_option(m, 2) == 2 and adapter.question_option(m, 9) is None
    assert adapter.question_option(m, "Cancel") == 3 and adapter.question_option(m, "nope") is None


# ---- the rest of the surface ---------------------------------------------------------------


def test_generic_adapter_surface(adapter: BaseAdapter, tmp_path):
    assert isinstance(adapter, AgentAdapter)
    assert adapter.info is GENERIC_INFO and adapter.info.key == "generic"
    assert adapter.available() == ""  # always "installed": it needs no binary
    assert adapter.version() is None
    assert not adapter.supports_resume()
    assert adapter.allowed_modes() == () and adapter.voice_switchable_modes() == ()
    assert adapter.default_launch_mode() is None and adapter.mode_cycle_key() is None
    assert adapter.forbidden_modes() == frozenset()
    with pytest.raises(NotImplementedError):
        adapter.new_session("x", str(tmp_path), None, None)
    with pytest.raises(NotImplementedError):
        adapter.resume_session("x", str(tmp_path), None, None)
    with pytest.raises(ValueError):
        adapter.normalize_mode("bypassPermissions")
    with pytest.raises(ValueError):
        adapter.normalize_mode("plan")
    assert adapter.list_sessions() == [] and adapter.find_session("x") is None
    assert adapter.transcript_source("x", str(tmp_path), None) is None
    assert adapter.hook_hint({"session_id": "x"}) is None
    assert adapter.status_hint("x") is None
    assert "permission settings" in adapter.permission_summary(str(tmp_path), None)
    assert adapter.mode_label("plan") == "plan"
    adapter.bind(tmux="T", zordon_home=tmp_path, claude_home=tmp_path / "ignored")
    assert adapter.tmux == "T" and adapter.zordon_home == tmp_path


def test_registry_and_availability():
    assert set(agents.ADAPTERS) == {"claude-code", "codex", "generic"}
    assert agents.DEFAULT_AGENT == "claude-code"
    assert isinstance(agents.get_adapter("generic"), BaseAdapter)
    assert isinstance(agents.get_adapter(" Generic "), BaseAdapter)
    assert agents.get_adapter(None).info.key == "claude-code"
    with pytest.raises(ValueError):
        agents.get_adapter("vim")
    found = agents.available_agents()
    assert set(found) == set(agents.ADAPTERS)
    assert found["generic"] == ""  # a broken or missing adapter is None, never an exception


def test_dataclasses_have_sane_defaults(tmp_path):
    spec = LaunchSpec(command=["x"], cwd="/tmp")
    assert spec.settings_paths == [] and spec.env_scrub_names == []
    info = SessionInfo(agent="generic", session_id="s", cwd="/tmp")
    assert info.extra == {} and info.transcript_path is None
    req = HookRequest(port=1, secret="s", host="127.0.0.1", zordon_home=tmp_path, session_id="s")
    assert req.port == 1


def test_tall_pane_with_blank_rows_below_the_prompt_still_detects():
    """Real panes pad the bottom with empty rows; the prompt sits above them (found live)."""
    from zordon.agents.base import BaseAdapter

    a = BaseAdapter()
    lines = ['bash-5.2$ read -p "Apply these changes? (y/n): " a', "Apply these changes? (y/n):"] + [""] * 40
    m = a.detect_prompt(a.parse(lines))
    assert m is not None and m.extra.get("inline_yn") and a.yes_option(m) == 1
