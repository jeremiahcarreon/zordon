"""screen.py: ANSI stripping, screen anatomy and the capture differ, on real fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from zordon.session import screen as S
from zordon.session.screen import (
    NBSP,
    Screen,
    align,
    diff_captures,
    diff_screens,
    display_line,
    held_tail,
    normalize_line,
    parse_capture,
    parse_screen,
    split_frames,
    strip_ansi,
)

PANE = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "pane"

STATIC = [
    p.name
    for p in sorted(PANE.glob("*.txt"))
    if not p.name.endswith(".ansi.txt") and "frames" not in p.name
]
FRAME_FILES = ["spinner_frames.txt", "streaming_frames.txt", "spinner_frames_plan.txt"]


def load(name: str) -> Screen:
    return parse_capture((PANE / name).read_text(encoding="utf-8", errors="replace"))


# ---- strip_ansi / normalize ------------------------------------------------------------


def test_strip_ansi_csi_osc8_bel_and_charset():
    s = "\x1b[38;5;174m \x1b[1mClaude\x1b[0m \x1b]8;id=1;https://x.invalid/a\x1b\\link\x1b]8;;\x1b\\ end"
    assert strip_ansi(s) == " Claude link end"
    bel = "\x1b]8;;https://x.invalid\x07text\x1b]8;;\x07 \x1b]0;title\x07tail"
    assert strip_ansi(bel) == "text tail"
    assert strip_ansi("\x1b(B\x1b)0abc\x1b[?25h\x1b[2J") == "abc"
    assert strip_ansi("a\rb\x0fc\td") == "abc\td"
    assert strip_ansi("plain") == "plain"


def test_strip_ansi_unterminated_osc_does_not_eat_following_lines():
    assert strip_ansi("\x1b]8;id=x;https://y\nnext") == "\nnext"


@pytest.mark.parametrize("name", [p.name for p in sorted(PANE.glob("*.ansi.txt"))])
def test_every_ansi_fixture_strips_to_its_plain_twin(name: str):
    plain = PANE / name.replace(".ansi.txt", ".txt")
    if not plain.exists():
        pytest.skip("no plain twin")
    got = [normalize_line(line) for line in (PANE / name).read_text(encoding="utf-8").split("\n")]
    want = [line.rstrip(" ") for line in plain.read_text(encoding="utf-8").split("\n")]
    assert got == want


def test_normalize_keeps_nbsp_and_display_form_drops_it():
    line = "❯" + NBSP + "   "
    assert normalize_line(line) == "❯" + NBSP
    assert display_line(line) == "❯"
    assert normalize_line("abc   ") == "abc"


# ---- parse_screen -----------------------------------------------------------------------


def test_idle_screen_has_ghost_input_and_no_content():
    scr = load("idle.txt")
    assert scr.input_box is not None
    assert scr.input_box.text == 'Try "write a test for <filepath>"'
    assert scr.input_box.looks_like_ghost
    assert scr.content == []
    assert scr.spinner is None
    assert scr.status_mode == "manual"
    assert scr.turn_ended
    assert not scr.exited
    assert scr.effort_hint


def test_literal_sendkeys_typed_text_is_in_the_input_box():
    scr = load("literal_sendkeys.txt")
    assert scr.input_box is not None
    assert scr.input_box.text == 'echo "Enter;C-c;$(whoami);`id`"'
    assert not scr.input_box.looks_like_ghost
    assert scr.content == []


def test_prose_output_content_keeps_bullet_and_continuation_lines():
    scr = load("prose_output.txt")
    c = scr.content
    assert scr.input_box is not None and scr.input_box.text == ""
    assert any(line.startswith("● tmux = terminal multiplexer.") for line in c)
    assert "  Client-server model: server hold sessions, clients attach." in c
    assert "  tmux new -s work" in c and "  tmux attach -t work" in c
    assert "  - ~/.tmux.conf" in c and "  - ~/.config/tmux/tmux.conf" in c
    assert c[-1] == "✻ Worked for 1s · done 8:38 PM"
    assert scr.done_line is not None
    assert (scr.done_line.verb, scr.done_line.duration, scr.done_line.clock) == ("Worked", "1s", "8:38 PM")
    assert scr.done_line.secs == 1
    assert scr.turn_ended
    assert "❯ In one short paragraph explain what tmux is, then show a 3-line bash code block, then list two file paths." in c
    assert len(scr.user_echoes) == 3
    # No UI in content.
    for line in c:
        assert not S.RULE.match(line) and not S.BANNER.match(line) and not S.INPUT_BOX.match(line)
        assert not S.STATUS_MODE.match(line) and not S.EFFORT_HINT.match(line)


def test_nbsp_bullet_line_is_content():
    scr = load("prose_output.txt")
    assert "●" + NBSP + "User answered Claude's questions:" in scr.content


def test_working_no_spinner_is_not_ended():
    scr = load("working_no_spinner.txt")
    assert scr.input_box is not None and scr.spinner is None
    assert not scr.turn_ended
    assert scr.content[-1] == "  99"
    assert held_tail(scr) == ["  99"]


def test_escape_interrupt_ends_the_turn():
    scr = load("escape_interrupt.txt")
    assert scr.turn_ended
    assert S.INTERRUPTED.match(scr.content[-1])
    assert held_tail(scr) == []


def test_denial_fixtures_end_with_done_lines():
    for name in ("denial.txt", "denial_write_escape.txt", "ask_user_question_answered.txt"):
        scr = load(name)
        assert scr.turn_ended, name
        assert scr.done_line is not None, name
    assert load("denial.txt").done_line.verb == "Sautéed"
    assert any(S.REJECTED_WRITE.match(line) for line in load("denial_write_escape.txt").content)


def test_exit_screen():
    scr = load("exit.txt")
    assert scr.exited
    assert scr.input_box is None
    assert scr.status_mode is None


def test_status_mode_words():
    assert load("plan_idle.txt").status_mode == "plan"
    assert load("idle.txt").status_mode == "manual"
    assert load("bash_permission.txt").status_mode is None


def test_prompt_block_is_split_from_content():
    scr = load("bash_permission.txt")
    assert scr.input_box is None
    assert S.RULE.match(scr.prompt_block[0])
    assert scr.prompt_block[1] == " Bash command"
    assert scr.prompt_block[-1].strip() == "Esc to cancel · Tab to amend"
    assert scr.content[-1].startswith("  ⎿  $ touch ")
    assert all("Do you want to proceed?" not in line for line in scr.content)
    wp = load("write_permission.txt")
    assert wp.prompt_block[1] == " Create file"
    ask = load("ask_user_question.txt")
    assert ask.prompt_block[1].strip() == "☐ Indentation"
    plan = load("plan_approval.txt")
    assert plan.prompt_block[1].strip() == "Ready to code?"
    assert plan.content == []  # the "/plan to preview ✕" popup bar is UI
    trust = load("trust_dialog.txt")
    assert trust.prompt_block[1].strip() == "Accessing workspace:"


def test_spinner_parsed_and_masked():
    frames = split_frames((PANE / "spinner_frames.txt").read_text(encoding="utf-8"))
    scr = parse_screen(frames[33][2])
    assert scr.spinner is not None
    assert (scr.spinner.glyph, scr.spinner.verb, scr.spinner.secs, scr.spinner.tokens) == ("✽", "Zesting", 4, "77")
    assert all(not S.SPINNER.match(line) for line in scr.content)
    plan_frames = split_frames((PANE / "spinner_frames_plan.txt").read_text(encoding="utf-8"))
    spinners = [parse_screen(lines).spinner for _, _, lines in plan_frames]
    assert all(sp is not None for sp in spinners)
    assert any(sp.extra == "thinking with medium effort" for sp in spinners if sp)
    assert any(sp.tokens == "1.1k" for sp in spinners if sp)
    # The lone blinking bullet "●" is not content.
    streaming = split_frames((PANE / "streaming_frames.txt").read_text(encoding="utf-8"))
    f8 = parse_screen(streaming[8][2])
    assert "●" not in f8.content
    assert not any(line.startswith("  ⎿" + NBSP + "Tip:") or "Tip: See an artifact" in line for line in f8.content)


def test_content_key_ignores_spinner_and_blink():
    frames = split_frames((PANE / "spinner_frames.txt").read_text(encoding="utf-8"))
    by_n = {n: parse_screen(lines) for n, _, lines in frames}
    assert by_n[3].content_key == by_n[4].content_key  # glyph only
    assert by_n[12].content_key == by_n[17].content_key  # blink only
    assert by_n[9].content_key != by_n[10].content_key  # real content


def test_parse_screen_accepts_ansi_lines():
    raw = (PANE / "bash_permission.ansi.txt").read_text(encoding="utf-8").split("\n")
    scr = parse_screen(raw, ansi=True)
    assert scr.prompt_block[1] == " Bash command"


# ---- align / diff -----------------------------------------------------------------------


def test_align_handles_append_scroll_and_in_place_replacement():
    assert align(["A", "B", "C"], ["A", "B", "C", "D"]) == 3
    assert align(["A", "B", "C", "D"], ["C", "D", "E"]) == 2
    assert align(["A", "B", "C", "D"], ["A", "B", "X", "Y"]) == 2
    assert align(["", "X"], ["", "Y"]) == 1
    assert align([], ["A"]) == 0
    assert align(["A"], []) == 0


def test_diff_first_screen_emits_all_content_and_no_ui():
    scr = load("prose_output.txt")
    new = diff_screens(None, scr)
    assert new == scr.content
    assert not any(S.RULE.match(line) or S.INPUT_BOX.match(line) for line in new)


def test_diff_unchanged_screen_is_empty_and_blink_is_collapsed():
    frames = split_frames((PANE / "spinner_frames.txt").read_text(encoding="utf-8"))
    by_n = {n: parse_screen(lines) for n, _, lines in frames}
    assert diff_screens(by_n[12], by_n[12]) == []
    assert diff_screens(by_n[12], by_n[17]) == []  # "  Listing…" -> "● Listing…"
    assert diff_screens(by_n[3], by_n[4]) == []  # spinner glyph only


def test_diff_exit_screen_emits_nothing():
    assert diff_screens(load("prose_output.txt"), load("exit.txt")) == []


def _replay(name: str) -> tuple[list[str], list[list[str]], Screen]:
    frames = split_frames((PANE / name).read_text(encoding="utf-8"))
    assert frames, name
    prev: Screen | None = None
    transcript: list[str] = []
    seen_content: list[list[str]] = []
    for _, _, lines in frames:
        cur = parse_screen(lines)
        seen_content.append(cur.content)
        transcript.extend(diff_screens(prev, cur))
        prev = cur
    assert prev is not None
    return transcript, seen_content, prev


@pytest.mark.parametrize("name", FRAME_FILES)
def test_replay_never_emits_spinner_or_ui_lines(name: str):
    transcript, _, _ = _replay(name)
    for line in transcript:
        assert not S.SPINNER.match(line), line
        assert not S.INPUT_BOX.match(line), line
        assert not S.RULE.match(line) and not S.STATUS_MODE.match(line), line
        assert not S.BANNER.match(line) and not S.NOTICE.match(line), line
        assert not S.EFFORT_HINT.match(line), line
        assert "Tip: See an artifact" not in line


@pytest.mark.parametrize("name", FRAME_FILES)
def test_replay_each_content_line_once_and_final_frame_covered(name: str):
    transcript, seen, last = _replay(name)
    nonblank = [line for line in transcript if line.strip()]
    assert len(nonblank) == len(set(nonblank)), "a content line was emitted twice"
    # Everything emitted was really on screen at some point.
    everything = {line for content in seen for line in content}
    assert all(line in everything for line in nonblank)
    # The final frame's content (plus whatever is still held) is in the transcript, in order.
    final = [line for line in last.content if line.strip()]
    flushed = nonblank + [line for line in held_tail(last) if line.strip() and line not in nonblank]
    positions = [flushed.index(line) for line in final]
    assert positions == sorted(positions)
    assert len(positions) == len(final)


def test_replay_spinner_frames_transcript_matches_last_frame_modulo_tool_lifecycle():
    transcript, _, last = _replay("spinner_frames.txt")
    nonblank = [line for line in transcript if line.strip()]
    final = [line for line in last.content if line.strip()]
    extra = [line for line in nonblank if line not in final]
    # The only extra line is the tool-call description that was replaced in place by
    # "Listed 1 directory"; the never-settled "Running 1 shell command…" was held back.
    assert extra == ["● Listing all files in current directory"]
    assert not any("Running 1 shell command" in line for line in nonblank)
    assert nonblank[-1] == "✻ Worked for 4s · done 8:33 PM"


def test_replay_streaming_frames_partial_line_is_never_emitted():
    transcript, _, last = _replay("streaming_frames.txt")
    nonblank = [line for line in transcript if line.strip()]
    assert "  -" not in nonblank  # the half-rendered list item from frame 14
    assert "  - ~/.config/tmux/tmux.conf" in nonblank
    final = [line for line in last.content if line.strip()]
    # After the scroll, the transcript's tail is exactly the final frame.
    assert nonblank[-len(final) :] == final
    assert held_tail(last) == []


def test_replay_plan_frames_only_content_once_then_spinner_only():
    transcript, _, last = _replay("spinner_frames_plan.txt")
    nonblank = [line for line in transcript if line.strip()]
    assert nonblank[0] == "❯ Plan how you would add a README to this project"
    assert len(nonblank) == 3
    # The wrapped command's continuation line is still held while the spinner runs.
    assert len(held_tail(last)) == 1
    assert last.spinner is not None


def test_diff_captures_wrapper_reports_live_region():
    frames = split_frames((PANE / "spinner_frames.txt").read_text(encoding="utf-8"))
    d = diff_captures(frames[2][2], frames[3][2])
    assert d.new_lines == []
    assert not d.changed
    assert d.live_region and S.SPINNER.match(d.live_region[0])
    d2 = diff_captures(frames[33][2], frames[34][2])
    assert d2.changed and "  Listed 1 directory" in d2.new_lines


def test_split_frames_shape():
    frames = split_frames((PANE / "spinner_frames.txt").read_text(encoding="utf-8"))
    assert len(frames) == 36
    assert frames[0][:2] == (0, 0)
    assert all(len(lines) in (45, 46) for _, _, lines in frames)
