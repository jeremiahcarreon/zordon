"""prompts.py: every pane fixture classifies as the probe report's table says."""

from __future__ import annotations

from pathlib import Path

import pytest

from zordon.bus import PromptKind
from zordon.session import prompts as P
from zordon.session.prompts import (
    PROMPTS_VERSION,
    PromptMatch,
    detect_prompt,
    is_idle_prompt,
    is_unsafe_label,
    is_working,
    no_option,
    permission_mode_from_screen,
    plan_auto_option,
    plan_manual_option,
    plan_revise_option,
    question_option,
    yes_option,
)
from zordon.session.screen import parse_capture, split_frames

PANE = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "pane"


def lines_of(name: str) -> list[str]:
    text = (PANE / name).read_text(encoding="utf-8", errors="replace")
    out = text.split("\n")
    if out and out[-1] == "":
        out.pop()
    return out


def detect(name: str) -> PromptMatch | None:
    return detect_prompt(lines_of(name))


NO_PROMPT = [
    "idle.txt",
    "plan_idle.txt",
    "working_no_spinner.txt",
    "exit.txt",
    "denial.txt",
    "denial_write_escape.txt",
    "escape_interrupt.txt",
    "literal_sendkeys.txt",
    "prose_output.txt",
    "ask_user_question_answered.txt",
]
IDLE = [
    "idle.txt",
    "plan_idle.txt",
    "denial.txt",
    "denial_write_escape.txt",
    "escape_interrupt.txt",
    "literal_sendkeys.txt",
    "prose_output.txt",
    "ask_user_question_answered.txt",
]
PROMPTS = [
    "trust_dialog.txt",
    "trust_dialog_yes_selected.txt",
    "bash_permission.txt",
    "bash_permission_no_selected.txt",
    "write_permission.txt",
    "ask_user_question.txt",
    "plan_approval.txt",
]


def test_version_marker():
    assert PROMPTS_VERSION == "claude-code-2.1.287"


# ---- trust -------------------------------------------------------------------------------


def test_trust_dialog_defaults_to_no_exit():
    m = detect("trust_dialog.txt")
    assert m is not None and m.kind is PromptKind.TRUST
    assert m.title == "Trust this folder: /home/operator/Code/zordon"
    assert m.labels == ["No, exit", "Yes, I trust this folder"]
    assert m.selected is not None and m.selected.label == "No, exit"
    assert m.question.startswith("Quick safety check:")
    assert m.confidence == 1.0
    assert not any(o.unsafe for o in m.options)
    assert yes_option(m) is None and no_option(m) is None
    assert m.raw_lines[0].strip().startswith("─") and m.raw_lines[-1].strip() == "Enter to confirm · Esc to cancel"


def test_trust_dialog_yes_selected_moves_the_pointer():
    m = detect("trust_dialog_yes_selected.txt")
    assert m is not None and m.kind is PromptKind.TRUST
    assert m.selected is not None and m.selected.label == "Yes, I trust this folder"
    assert m.options[0].selected is False


# ---- permission --------------------------------------------------------------------------


def test_bash_permission():
    m = detect("bash_permission.txt")
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert len(m.options) == 4
    assert m.header == "Bash command"
    assert m.question == "Do you want to proceed?"
    assert m.command is not None and m.command.startswith("touch /tmp/claude-1000/") and m.command.endswith("probe_marker && echo done")
    assert m.title == f"Bash command: {m.command}"
    assert m.description == "Create probe marker file in scratchpad research directory"
    assert m.target_file is None
    assert yes_option(m) == 1
    assert no_option(m) == 4
    assert [o.unsafe for o in m.options] == [False, True, True, False]
    assert m.options[1].label.startswith("Yes, and always allow access to")
    assert m.options[2].label.startswith("Yes, and switch to auto mode")
    assert m.options[0].selected and not m.options[3].selected
    assert m.confidence == 1.0
    assert m.raw_lines[-1].strip() == "Esc to cancel · Tab to amend"
    assert m.raw_lines[1] == " Bash command"


def test_bash_permission_no_selected_keeps_labels_and_moves_pointer():
    m = detect("bash_permission_no_selected.txt")
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.labels == detect("bash_permission.txt").labels
    assert m.selected is not None and m.selected.label == "No" and m.selected.index == 4
    assert no_option(m) == 4


def test_write_permission():
    m = detect("write_permission.txt")
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert len(m.options) == 3
    assert m.header == "Create file"
    assert m.target_file == "probe.txt"
    assert m.title == "Create file probe.txt"
    assert m.question == "Do you want to create probe.txt?"
    assert m.command is None
    assert yes_option(m) == 1 and no_option(m) == 3
    assert [o.unsafe for o in m.options] == [False, True, False]
    assert "switch to accept edits" in m.options[1].label
    assert m.confidence == 1.0


def test_permission_without_footer_is_lower_confidence():
    lines = lines_of("bash_permission.txt")
    lines = [line for line in lines if "Esc to cancel · Tab to amend" not in line]
    m = detect_prompt(lines)
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.confidence < 1.0
    assert yes_option(m) == 1 and no_option(m) == 4


def test_permission_question_alone_is_not_a_prompt():
    lines = ["● Do you want to proceed?", "", " Do you want to proceed?", "  (just prose)"]
    assert detect_prompt(lines) is None


# ---- question (AskUserQuestion) ----------------------------------------------------------


def test_ask_user_question():
    m = detect("ask_user_question.txt")
    assert m is not None and m.kind is PromptKind.QUESTION
    assert m.header == "Indentation"
    assert m.question == "Do you prefer tabs or spaces for indentation?"
    assert m.title == m.question
    assert m.labels == ["Tabs", "Spaces", "Type something.", "Chat about this"]
    assert m.options[0].selected
    assert m.options[0].description == "Indent with tab characters; width adjustable per editor."
    assert m.options[1].description == "Indent with space characters; consistent rendering everywhere."
    assert not any(o.unsafe for o in m.options)
    assert m.confidence == 1.0
    assert question_option(m, 2) == 2
    assert question_option(m, "spaces") == 2
    assert question_option(m, "Type something") == 3
    assert question_option(m, 9) is None and question_option(m, "nope") is None
    assert yes_option(m) is None and no_option(m) is None


# ---- plan ------------------------------------------------------------------------------


def test_plan_approval():
    m = detect("plan_approval.txt")
    assert m is not None and m.kind is PromptKind.PLAN
    assert m.title == "Plan ready: Plan: write README.md for Zordon"
    assert m.question.startswith("Claude has written up a plan")
    assert m.labels == ["Yes, and use auto mode", "Yes, manually approve edits", "Tell Claude what to change"]
    assert m.options[0].unsafe and m.options[0].selected
    assert not m.options[1].unsafe and not m.options[2].unsafe
    assert plan_manual_option(m) == 2
    assert plan_revise_option(m) == 3
    assert plan_auto_option(m) == 1
    assert yes_option(m) is None  # there is no plain "Yes" on a plan prompt
    assert no_option(m) is None  # and no "No": Escape rejects
    assert m.options[2].description == "shift+tab to approve with this feedback"
    assert m.extra["plan_file"].endswith("plan-how-you-would-modular-teacup.md")
    assert m.confidence == 1.0
    assert m.raw_lines[1].strip() == "Ready to code?"


def test_plan_without_header_still_detected_with_lower_confidence():
    lines = lines_of("plan_approval.txt")
    cut = next(i for i, line in enumerate(lines) if "Would you like to proceed?" in line)
    tail = lines[cut - 1 :]  # the rule above the question and everything below
    m = detect_prompt(tail)
    assert m is not None and m.kind is PromptKind.PLAN
    assert m.title == "Plan ready"
    assert m.confidence < 1.0
    assert plan_manual_option(m) == 2


# ---- negatives and state primitives ------------------------------------------------------


@pytest.mark.parametrize("name", NO_PROMPT)
def test_no_prompt_on_non_prompt_fixtures(name: str):
    assert detect(name) is None


@pytest.mark.parametrize("name", IDLE)
def test_idle_fixtures(name: str):
    lines = lines_of(name)
    assert is_idle_prompt(lines)
    assert not is_working(lines)


@pytest.mark.parametrize("name", PROMPTS + ["working_no_spinner.txt", "exit.txt"])
def test_not_idle_fixtures(name: str):
    assert not is_idle_prompt(lines_of(name))


def test_working_no_spinner_is_neither_idle_nor_spinner_working():
    lines = lines_of("working_no_spinner.txt")
    assert detect_prompt(lines) is None
    assert not is_idle_prompt(lines)
    assert not is_working(lines)  # the manager adds "content advanced"
    assert not P.turn_ended(lines)


def test_exit_fixture_is_exited():
    lines = lines_of("exit.txt")
    assert P.exited(lines)
    assert detect_prompt(lines) is None
    assert not is_idle_prompt(lines)


def test_denial_renderings_are_detected():
    assert any(P.INTERRUPTED.match(line) for line in lines_of("denial.txt"))
    assert any(P.REJECTED_WRITE.match(line) for line in lines_of("denial_write_escape.txt"))
    assert any(P.INTERRUPTED.match(line) for line in lines_of("escape_interrupt.txt"))
    m = next(m for m in map(P.REJECTED_WRITE.match, lines_of("denial_write_escape.txt")) if m)
    assert m.group("file") == "probe.txt"


def test_literal_sendkeys_input_box_holds_typed_text():
    scr = parse_capture((PANE / "literal_sendkeys.txt").read_text(encoding="utf-8"))
    assert scr.input_box is not None
    assert scr.input_box.text == 'echo "Enter;C-c;$(whoami);`id`"'
    assert detect_prompt(scr) is None
    assert is_idle_prompt(scr)


def test_spinner_frames_never_look_like_prompts_and_are_working():
    for fname in ("spinner_frames.txt", "streaming_frames.txt", "spinner_frames_plan.txt"):
        for _, _, lines in split_frames((PANE / fname).read_text(encoding="utf-8")):
            assert detect_prompt(lines) is None, fname
            scr = parse_capture("\n".join(lines))
            if scr.spinner is not None:
                assert is_working(scr) and not is_idle_prompt(scr)


def test_permission_mode_from_screen():
    assert permission_mode_from_screen(lines_of("idle.txt")) == "default"
    assert permission_mode_from_screen(lines_of("plan_idle.txt")) == "plan"
    assert permission_mode_from_screen(lines_of("bash_permission.txt")) is None
    row = ["─" * 160, "❯ ", "─" * 160, "  ⏸ accept edits mode on · ← 3 agents"]
    assert permission_mode_from_screen(row) == "acceptEdits"
    row[-1] = "  ⏸ bypass permissions mode on"
    assert permission_mode_from_screen(row) == "bypassPermissions"
    row[-1] = "  ⏸ don't ask mode on"
    assert permission_mode_from_screen(row) == "dontAsk"
    row[-1] = "  ⏸ auto mode on"
    assert permission_mode_from_screen(row) == "auto"


def test_unsafe_label_phrases():
    assert is_unsafe_label("Yes, and always allow access to /x from this project")
    assert is_unsafe_label("Yes, and switch to auto mode · auto mode handles these prompts for you")
    assert is_unsafe_label("Yes, and switch to accept edits (auto-approve file edits) for this session")
    assert is_unsafe_label("Yes, and don't ask again for this command")
    assert is_unsafe_label("Yes, and use auto mode")
    assert not is_unsafe_label("Yes")
    assert not is_unsafe_label("No")
    assert not is_unsafe_label("Yes, manually approve edits")


def test_regex_exact_strings():
    assert P.DONE_LINE.match("✻ Sautéed for 1s · done 8:36 PM")
    assert P.DONE_LINE.match("✻ Cogitated for 1m 12s · done 10:05 AM")
    assert not P.DONE_LINE.match("✻ Worked for 4s")
    assert P.SPINNER.match("· Zesting… (1s · ↓ 5 tokens)")
    assert P.SPINNER.match("✻ Perambulating…")
    m = P.SPINNER.match("· Grooving… (12s · ↓ 1.1k tokens · thinking with medium effort)")
    assert m and m.group("tokens") == "1.1k" and m.group("extra") == "thinking with medium effort"
    assert not P.SPINNER.match("● Running 1 shell command…")
    assert P.INPUT_BOX.match("❯ ") and P.INPUT_BOX.match("❯ typed")
    assert not P.INPUT_BOX.match("❯ echoed text")
    assert P.USER_ECHO.match("❯ echoed text")
    assert P.TOOL_BULLET.match("● Listing all files") and P.TOOL_BULLET.match("● User answered Claude's questions:")
    assert P.TOOL_RESULT.match("  ⎿  $ ls -la")
    assert P.TOOL_RESULT.match("  ⎿  Interrupted · What should Claude do instead?")
    assert P.TOOL_SUMMARY.match("  Listed 1 directory") and P.TOOL_SUMMARY.match("● Running 1 shell command…")
    assert P.RULE.match("─" * 160) and P.RULE.match("  " + "─" * 156)
    assert P.DOTTED_RULE.match("╌" * 156)
    assert P.EXIT_RESUME.match("claude --resume 0b0b0b0b-0000-4000-8000-00000000000b")
    assert P.MENU_NO.match("   4. No") and P.MENU_NO.match(" ❯ 3. No")
    assert P.PERM_FILE_Q.match(" Do you want to make this edit to auth.py?").group("target") == "auth.py"
