"""The Codex adapter: command lines, store discovery, prompt detection, the
rollout tail and the safety rules, against the fixtures in ``eval/fixtures/codex``.

Fixture provenance (``eval/fixtures/codex/PROVENANCE.md``): the welcome, trust,
idle, working, error, exit and picker screens are live captures of codex-cli
0.160.0; every ``synthetic_*`` file was assembled from the strings in the Codex
source and its snapshot tests, because no agent turn could run without a login.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

import pytest

from zordon import agents
from zordon.agents import codex as C
from zordon.agents.base import AgentAdapter, SessionInfo
from zordon.agents.codex import CodexAdapter, RolloutTail
from zordon.bus import PromptKind

FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "codex"
SESSION_ID = "01a0ff00-0000-7000-8000-000000000001"


def fixture(name: str) -> list[str]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic CODEX_HOME with two sessions and a config.toml."""
    h = tmp_path / "codex-home"
    day = h / "sessions" / "2026" / "10" / "02"
    day.mkdir(parents=True)
    shutil.copy(FIXTURES / "synthetic_rollout.jsonl", day / f"rollout-2026-10-02T14-10-00-{SESSION_ID}.jsonl")
    other = "01a0ff00-0000-7000-8000-000000000002"
    second = day / f"rollout-2026-10-02T15-00-00-{other}.jsonl"
    second.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-10-02T22:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": other, "session_id": other, "timestamp": "2026-10-02T22:00:00.000Z", "cwd": "/home/user/other", "originator": "codex-tui", "cli_version": "0.160.0"},
                    }
                ),
                json.dumps({"timestamp": "2026-10-02T22:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello there"}]}}),
            ]
        )
        + "\n"
    )
    os.utime(day / f"rollout-2026-10-02T14-10-00-{SESSION_ID}.jsonl", (1_790_975_430, 1_790_975_430))
    os.utime(second, (1_790_978_401, 1_790_978_401))
    (h / "config.toml").write_text(
        'approval_policy = "untrusted"\nsandbox_mode = "workspace-write"\n\n[projects."/home/user/Code/project"]\ntrust_level = "trusted"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(h))
    return h


@pytest.fixture
def adapter(home: Path) -> CodexAdapter:
    return CodexAdapter()


def detect(adapter: CodexAdapter, name: str):
    return adapter.detect_prompt(adapter.parse(fixture(name)))


# ---- identity -------------------------------------------------------------------------------


def test_registry_and_info(adapter: CodexAdapter):
    assert isinstance(adapter, AgentAdapter)
    assert isinstance(agents.get_adapter("codex"), CodexAdapter)
    assert adapter.info.key == "codex" and adapter.info.binary == "codex"
    assert adapter.info.display_name == "Codex"
    assert "npm install -g @openai/codex" in adapter.info.install_hint
    assert adapter.supports_resume()
    assert adapter.allowed_modes() == ("untrusted", "on-request", "on-failure")
    assert adapter.default_launch_mode() == "on-request"
    assert adapter.mode_cycle_key() is None  # Shift+Tab cycles the collaboration mode, not approvals
    assert adapter.voice_switchable_modes() == ()
    assert adapter.uses_alternate_screen()
    assert "never" in adapter.forbidden_modes()


# ---- availability -----------------------------------------------------------------------------


def test_available_honours_the_binary_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = tmp_path / "codex"
    fake.write_text('#!/bin/sh\necho "codex-cli 0.160.0"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(C.BINARY_ENV, str(fake))
    a = CodexAdapter()
    assert a.available() == str(fake)
    assert a.version() == "codex-cli 0.160.0"
    assert a.version() == "codex-cli 0.160.0"  # cached
    monkeypatch.setenv(C.BINARY_ENV, str(tmp_path / "missing"))
    assert CodexAdapter().available() is None
    assert CodexAdapter().version() is None


# ---- command lines -----------------------------------------------------------------------------


def test_new_session_argv_per_mode(adapter: CodexAdapter, tmp_path: Path):
    spec = adapter.new_session("zid", str(tmp_path), None, None)
    assert spec.command == ["codex", "--ask-for-approval", "on-request"]
    assert spec.cwd == str(tmp_path) and spec.settings_paths == []
    assert adapter.new_session("z", "/x", "on-request", None).command == ["codex", "--ask-for-approval", "on-request"]
    # 0.160.0's -a accepts only on-request|never (live); the other policies go through -c
    assert adapter.new_session("z", "/x", "untrusted", None).command == ["codex", "--config", 'approval_policy="untrusted"']
    assert adapter.new_session("z", "/x", "on-failure", None).command == ["codex", "--config", 'approval_policy="on-failure"']
    assert adapter.new_session("z", "/x", "default", None).command[-1] == "on-request"  # Claude-style alias


def test_resume_argv(adapter: CodexAdapter):
    spec = adapter.resume_session(SESSION_ID, "/x", "untrusted", None)
    assert spec.command == ["codex", "resume", SESSION_ID, "--config", 'approval_policy="untrusted"']
    assert adapter.resume_session(SESSION_ID, "/x", None, None).command == ["codex", "resume", SESSION_ID, "--ask-for-approval", "on-request"]
    with pytest.raises(ValueError):
        adapter.resume_session("not-a-uuid", "/x", None, None)


@pytest.mark.parametrize(
    "mode",
    ["never", "full-auto", "--full-auto", "yolo", "--yolo", "danger-full-access", "bypass", "auto", "dangerously-bypass-approvals-and-sandbox", "granular", "plan", ""],
)
def test_refused_modes(adapter: CodexAdapter, mode: str):
    with pytest.raises(ValueError):
        adapter.normalize_mode(mode)
    if mode:  # an empty mode means "the default" at launch time
        with pytest.raises(ValueError):
            adapter.new_session("z", "/x", mode, None)
        with pytest.raises(ValueError):
            adapter.resume_session(SESSION_ID, "/x", mode, None)


def test_validate_command_refuses_bypass_flags_and_values():
    for argv in (
        ["codex", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "--yolo"],
        ["codex", "--full-auto"],
        ["codex", "--approve-for-me"],
        ["codex", "--not-so-yolo"],
        ["codex", "--dangerously-bypass-hook-trust"],
        ["codex", "--ask-for-approval", "never"],
        ["codex", "-a", "never"],
        ["codex", "--sandbox", "danger-full-access"],
        ["codex", "-s", "danger-full-access"],
        ["codex", "--config", 'approval_policy="never"'],
        ["codex", "-c", "sandbox_mode=danger-full-access"],
        ["codex", "-c", 'approvals_reviewer="auto_review"'],
        ["claude", "--resume", "x"],
        [],
    ):
        with pytest.raises(ValueError):
            C.validate_command(argv)
    C.validate_command(["codex", "resume", SESSION_ID, "--ask-for-approval", "on-request", "--sandbox", "workspace-write"])
    C.validate_command(["/usr/local/bin/codex", "--config", 'approval_policy="untrusted"'])


def test_bypass_flag_appears_only_in_the_refusal_list():
    """The adapter may name the bypass flags only to refuse them."""
    src = Path(C.__file__).read_text(encoding="utf-8")
    lines = src.splitlines()
    doc_end = next(i for i, ln in enumerate(lines) if ln.startswith("from __future__"))
    refused_start = next(i for i, ln in enumerate(lines) if ln.startswith("REFUSED_FLAGS"))
    refused_end = next(i for i in range(refused_start, len(lines)) if lines[i].strip() == ")")
    for flag in ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto", "--approve-for-me"):
        for i, ln in enumerate(lines):
            if flag in ln:
                assert i < doc_end or refused_start <= i <= refused_end, f"{flag} at line {i + 1} is outside the refusal list"


# ---- store discovery ---------------------------------------------------------------------------


def test_list_sessions_reads_the_rollout_store(adapter: CodexAdapter, home: Path):
    sessions = adapter.list_sessions()
    assert [s.session_id for s in sessions] == ["01a0ff00-0000-7000-8000-000000000002", SESSION_ID]  # newest first
    s = sessions[1]
    assert isinstance(s, SessionInfo) and s.agent == "codex"
    assert s.cwd == "/home/user/Code/project"
    assert s.title == "Run the test suite and fix what fails."  # first typed prompt, not <environment_context>
    assert s.permission_mode == "untrusted"  # the last turn_context wins
    assert s.extra["sandbox_mode"] == "read-only" and s.extra["version"] == "0.160.0"
    assert s.transcript_path is not None and s.transcript_path.name.endswith(f"{SESSION_ID}.jsonl")
    assert s.last_active == pytest.approx(1_790_975_464.0, abs=1)  # last record timestamp (turn_aborted)
    assert s.running_pid is None and s.tmux_target is None and s.extra["running"] == ""
    other = sessions[0]
    assert other.title == "Hello there" and other.cwd == "/home/user/other" and other.permission_mode is None


def test_find_session_and_unknown(adapter: CodexAdapter):
    s = adapter.find_session(SESSION_ID)
    assert s is not None and s.cwd == "/home/user/Code/project"
    assert adapter.find_session("01a0ff00-0000-7000-8000-00000000dead") is None
    assert adapter.find_session("nope") is None


def test_codex_home_env_and_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    assert C.codex_home() == tmp_path / "h"
    monkeypatch.delenv("CODEX_HOME")
    assert C.codex_home() == Path.home() / ".codex"
    assert CodexAdapter(codex_home=tmp_path / "x").home == tmp_path / "x"
    a = CodexAdapter()
    a.bind(tmux=None, zordon_home=tmp_path, claude_home=tmp_path / "ignored", codex_home=tmp_path / "y")
    assert a.home == tmp_path / "y" and a.zordon_home == tmp_path


def test_list_sessions_with_tmux_panes(home: Path):
    class Pane:
        def __init__(self, target, cwd, command, pid):
            self.target, self.cwd, self.command, self.pid = target, cwd, command, pid

    class Tmux:
        def list_panes(self):
            return [Pane("z:1.0", "/home/user/Code/project", "codex", 4242), Pane("z:2.0", "/home/user/other", "bash", 1)]

    a = CodexAdapter(tmux=Tmux())
    by_id = {s.session_id: s for s in a.list_sessions()}
    assert by_id[SESSION_ID].tmux_target == "z:1.0" and by_id[SESSION_ID].running_pid == 4242
    assert by_id[SESSION_ID].extra["running"] == "1"
    assert by_id["01a0ff00-0000-7000-8000-000000000002"].tmux_target is None


def test_empty_or_missing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))
    assert CodexAdapter().list_sessions() == []
    assert CodexAdapter().find_session(SESSION_ID) is None


def test_permission_summary_from_config(adapter: CodexAdapter):
    s = adapter.permission_summary("/home/user/Code/project/sub", None)
    assert "not explicitly allowed" in s and "workspace-write" in s and "this folder is trusted" in s
    s2 = adapter.permission_summary("/elsewhere", "on-request")
    assert "when the model asks" in s2 and "not yet trusted" in s2
    assert adapter.mode_label("on-request") == "when the model asks"


# ---- prompt detection ------------------------------------------------------------------------------


def test_exec_approval(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_exec.txt")
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.title == "Would you like to run the following command?"
    assert m.command == "python -m pytest -q"
    assert m.description == "Run the project's unit tests to see what fails."
    assert m.header == "Run command" and m.extra["codex_kind"] == "exec"
    assert m.labels == [
        "Yes, proceed",
        "Yes, and don't ask again for commands that start with `python -m pytest`",
        "No, and tell Codex what to do differently",
    ]
    assert [o.unsafe for o in m.options] == [False, True, False]
    assert m.selected is not None and m.selected.index == 1
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 3
    assert m.extra["key1"] == "y" and m.extra["key2"] == "p" and "key3" not in m.extra
    assert m.extra["shortcut3"] == "esc"
    assert m.confidence >= 0.9
    assert m.raw_lines[0] == "Would you like to run the following command?"
    assert m.raw_lines[-1] == "Press enter to confirm or esc to cancel"


def test_exec_approval_with_session_option_and_plain_decline(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_exec_session.txt")
    assert m is not None and m.command == "npm install"
    assert [o.unsafe for o in m.options] == [False, True, False, False]
    assert m.selected is not None and m.selected.index == 2  # the pointer sits on the unsafe row
    assert adapter.yes_option(m) == 1  # never the selected, widening one
    assert adapter.no_option(m) == 3  # "No, continue without running it" over the abort-and-redirect


def test_patch_approval(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_patch.txt")
    assert m is not None and m.extra["codex_kind"] == "patch" and m.header == "Edit files"
    assert m.target_file == "/home/user/Code/project/README.md"
    assert m.description == "The model wants to apply changes"
    assert m.command is None
    assert [o.unsafe for o in m.options] == [False, True, False]
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 3


def test_permissions_approval(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_permissions.txt")
    assert m is not None and m.extra["codex_kind"] == "permissions"
    assert m.extra["permission_rule"] == "network; read `/tmp/readme.txt`; write `/tmp/out.txt`"
    assert [o.unsafe for o in m.options] == [False, True, True, False]
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 4
    assert m.extra["key2"] == "r" and m.extra["key4"] == "d"


def test_network_approval(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_network.txt")
    assert m is not None and m.extra["codex_kind"] == "network"
    assert m.title == 'Do you want to approve network access to "example.com"?'
    assert m.labels[0] == "Yes, just this once"
    assert [o.unsafe for o in m.options] == [False, True, True, False]
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 4


def test_narrow_pane_wraps_title_and_labels(adapter: CodexAdapter):
    m = detect(adapter, "synthetic_approval_exec_narrow_40.txt")
    assert m is not None and m.kind is PromptKind.PERMISSION
    assert m.title == "Would you like to run the following command?"
    assert m.labels == ["Yes, proceed", "No, and tell Codex what to do differently"]
    assert adapter.yes_option(m) == 1 and adapter.no_option(m) == 2
    assert m.extra["key1"] == "y"


def test_unsafe_labels():
    assert not C.is_unsafe_label("Yes, proceed")
    assert not C.is_unsafe_label("Yes, just this once")
    assert not C.is_unsafe_label("Yes, grant these permissions for this turn")
    for label in (
        "Yes, and don't ask again for commands that start with `git`",
        "Yes, and don't ask again for this command in this session",
        "Yes, and don't ask again for these files",
        "Yes, and allow this host for this conversation",
        "Yes, and allow this host in the future",
        "Yes, and allow these permissions for this session",
        "Yes, grant these permissions for this session",
        "Yes, grant for this turn with strict auto review",
    ):
        assert C.is_unsafe_label(label), label


def test_persistent_block_is_never_the_no(adapter: CodexAdapter):
    from zordon.session.prompts import PromptMatch, PromptOption

    m = PromptMatch(
        kind=PromptKind.PERMISSION,
        title="t",
        question="q",
        options=[PromptOption(1, "Yes, just this once"), PromptOption(2, "No, and block this host in the future"), PromptOption(3, "Cancel this request")],
        raw_lines=[],
        confidence=1.0,
    )
    assert adapter.no_option(m) == 3
    m.options.pop()
    assert adapter.no_option(m) is None


def test_trust_dialog_live_capture(adapter: CodexAdapter):
    m = detect(adapter, "trust_dialog.txt")
    assert m is not None and m.kind is PromptKind.TRUST
    assert m.header == "Folder access" and m.question.startswith("Trust this folder?")
    assert m.extra["path"] == "/home/operator/Code/zordon/.scratch/codex"
    assert m.labels == ["Trust and continue", "Back to Agent Command Center"]
    assert m.selected is not None and m.selected.index == 1  # Codex preselects "trust"; the manager confirms first
    assert adapter.trust_accept_option(m) == 1 and adapter.trust_decline_option(m) == 2
    assert adapter.yes_option(m) is None  # a trust dialog is not a permission prompt


def test_welcome_login_screens(adapter: CodexAdapter):
    m = detect(adapter, "welcome_login.txt")
    assert m is not None and m.kind is PromptKind.QUESTION and m.extra["codex_login"] == "menu"
    assert m.labels == ["Sign in with ChatGPT", "Sign in with Device Code", "Provide your own API key"]
    assert m.selected is not None and m.selected.index == 1
    assert adapter.question_option(m, "Provide your own API key") == 3
    m2 = detect(adapter, "welcome_login_apikey_selected.txt")
    assert m2 is not None and m2.selected is not None and m2.selected.index == 3
    m3 = detect(adapter, "apikey_entry.txt")
    assert m3 is not None and m3.kind is PromptKind.QUESTION and m3.extra["codex_login"] == "api_key"
    assert m3.options == []
    assert adapter.question_option(m3, 1) is None


@pytest.mark.parametrize(
    "name",
    [
        "idle.txt",
        "idle_typed.txt",
        "working.txt",
        "working_reconnecting.txt",
        "turn_error.txt",
        "quit_typed.txt",
        "exited.txt",
        "synthetic_turn_done.txt",  # a numbered Yes/No-looking list in prose, composer visible
        "resume_picker.txt",
        "warning_overlay.txt",
    ],
)
def test_no_prompt_on_ordinary_screens(adapter: CodexAdapter, name: str):
    assert detect(adapter, name) is None


def test_scrollback_of_an_answered_prompt_is_not_a_prompt(adapter: CodexAdapter):
    lines = fixture("synthetic_approval_exec.txt") + ["", "› Ask Codex to do anything", "", "  Model 0.0 default · ~/Code/project"]
    assert adapter.detect_prompt(adapter.parse(lines)) is None
    assert adapter.is_idle(adapter.parse(lines))


# ---- idle / working / exited -----------------------------------------------------------------------


def test_idle_working_exited_on_live_captures(adapter: CodexAdapter):
    idle = adapter.parse(fixture("idle.txt"))
    assert adapter.is_idle(idle) and adapter.input_quiet(idle)
    assert not adapter.is_working(idle) and not adapter.exited(idle)
    assert adapter.collaboration_mode_from_screen(idle) == "default"
    assert adapter.permission_mode_from_screen(idle) is None

    typed = adapter.parse(fixture("idle_typed.txt"))
    assert adapter.is_idle(typed) and not adapter.is_working(typed)

    for name in ("working.txt", "working_reconnecting.txt", "working_reconnecting_long.txt"):
        w = adapter.parse(fixture(name))
        assert adapter.is_working(w), name
        assert not adapter.is_idle(w), name
        assert not adapter.exited(w), name

    err = adapter.parse(fixture("turn_error.txt"))
    assert adapter.is_idle(err) and not adapter.is_working(err)

    done = adapter.parse(fixture("synthetic_turn_done.txt"))
    assert adapter.is_idle(done) and not adapter.is_working(done)

    gone = adapter.parse(fixture("exited.txt"))
    assert adapter.exited(gone) and not adapter.is_idle(gone) and not adapter.is_working(gone)
    assert not adapter.exited(adapter.parse(fixture("welcome_login.txt")))
    assert not adapter.exited(adapter.parse([]))
    assert adapter.exited(adapter.parse(["user@host:~/proj$ "]))


def test_approval_overlay_is_neither_idle_nor_working(adapter: CodexAdapter):
    scr = adapter.parse(fixture("synthetic_approval_exec.txt"))
    assert not adapter.is_idle(scr) and not adapter.is_working(scr) and not adapter.exited(scr)


def test_plan_mode_footer():
    a = CodexAdapter()
    scr = a.parse(["› Ask Codex to do anything", "", "  ? for shortcuts · Plan mode (⇧tab to cycle)       100% context left"])
    assert a.collaboration_mode_from_screen(scr) == "plan"
    assert a.is_idle(scr)


# ---- transcript ---------------------------------------------------------------------------------------


def blocks(lines):
    return [(pl.block, pl.text) for pl in lines]


def test_rollout_tail_replays_the_synthetic_session(home: Path):
    path = next((home / "sessions").rglob(f"*{SESSION_ID}.jsonl"))
    tail = RolloutTail(path, "zordon-id", offset=0)
    lines = tail.poll()
    assert all(pl.source == "jsonl" and pl.session_id == "zordon-id" for pl in lines)
    kinds = [pl.block for pl in lines]
    assert kinds == [
        "turn_start",
        "permission_mode",
        "user_prompt",
        "text",
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
        "text",
        "turn_end",
        "turn_start",
        "permission_mode",
        "user_prompt",
        "turn_end",
    ]
    texts = [pl.text for pl in lines if pl.block == "text"]
    assert texts == [
        "I'll start by running the tests.",
        'The failing test expected `parse("")` to return an empty list; `parser.py` now returns `[]` and all 42 tests pass.',
    ]  # agent_message / item_completed copies are not spoken twice
    tools = [pl for pl in lines if pl.block == "tool_use"]
    assert [t.text for t in tools] == ["shell: python -m pytest -q", "apply_patch: project/parser.py", "shell: python -m pytest -q"]
    assert tools[0].meta["name"] == "shell" and tools[0].meta["input"]["command"][2] == "python -m pytest -q"
    assert tools[0].meta["tool_use_id"] == "call-1"
    results = [pl for pl in lines if pl.block == "tool_result"]
    assert results[0].meta["is_error"] and results[0].text.startswith("Exit code: 1")
    assert not results[1].meta["is_error"] and results[1].text.startswith("Success.")
    assert not results[2].meta["is_error"] and results[2].meta["chars"] == len("Exit code: 0\n42 passed in 2.28s")
    ends = [pl for pl in lines if pl.block == "turn_end"]
    assert ends[0].meta["stop_reason"] == "task_complete" and ends[0].meta["duration_ms"] == 25100
    assert ends[1].meta["stop_reason"] == "interrupted"
    modes = [pl for pl in lines if pl.block == "permission_mode"]
    assert [m.text for m in modes] == ["on-request", "untrusted"]
    assert modes[0].meta["sandbox_mode"] == "workspace-write"
    assert tail.last_mode == "untrusted"
    assert tail.records_seen == 26 and tail.parse_errors == 0
    assert tail.poll() == []


def test_rollout_tail_incremental_and_partial_lines(tmp_path: Path):
    path = tmp_path / "rollout-2026-10-02T14-10-00-01a0ff00-0000-7000-8000-000000000009.jsonl"
    path.write_text("")
    tail = RolloutTail(path, "sid", start_at_end=True)
    assert tail.poll() == []
    rec = {"timestamp": "2026-10-02T21:10:08.000Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello."}]}}
    raw = json.dumps(rec)
    with path.open("a") as fh:
        fh.write(raw[:20])
    assert tail.poll() == []  # partial line kept
    with path.open("a") as fh:
        fh.write(raw[20:] + "\n")
    out = tail.poll()
    assert blocks(out) == [("text", "Hello.")]
    assert out[0].ts == pytest.approx(1790975408.0)
    with path.open("a") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"timestamp": "2026-10-02T21:10:09.000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t", "error": {"message": "boom"}}}) + "\n")
    out = tail.poll()
    assert tail.parse_errors == 1
    assert blocks(out) == [("text", "Codex error: boom"), ("turn_end", "")]
    assert out[0].meta["is_error"] and out[1].meta["stop_reason"] == "error"


def test_parse_record_edge_cases():
    assert C.parse_rollout_record({"type": "response_item"}) == []
    assert C.parse_rollout_record({"type": 5, "payload": {}}) == []
    # developer / system messages and injected context are never prose
    dev = {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "x"}]}}
    assert C.parse_rollout_record(dev) == []
    env = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>..."}]}}
    assert C.parse_rollout_record(env) == []
    # reasoning only when asked for
    rs = {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}}
    assert C.parse_rollout_record(rs) == []
    assert [e.kind for e in C.parse_rollout_record(rs, include_thinking=True)] == ["thinking"]
    # a plan item is prose; other item_completed kinds are duplicates
    plan = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "Plan", "text": "1. do x"}}}
    assert [e.text for e in C.parse_rollout_record(plan)] == ["1. do x"]
    dup = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "x"}]}}}
    assert C.parse_rollout_record(dup) == []
    # a function call with unparseable arguments still names the tool
    fc = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{broken", "call_id": "c"}}
    (e,) = C.parse_rollout_record(fc)
    assert e.kind == "tool_use" and e.text == "exec_command" and e.input == {"raw": "{broken"}
    fc2 = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls -la"}), "call_id": "c"}}
    assert C.parse_rollout_record(fc2)[0].text == "exec_command: ls -la"


def test_transcript_source_for_a_session_zordon_started(adapter: CodexAdapter, home: Path):
    cwd = "/home/user/Code/project"
    assert adapter.transcript_source("zid", cwd, None) is None  # not launched by us, unknown id
    adapter.new_session("zid", cwd, None, None)
    # the existing rollout predates the launch: not ours
    assert adapter.transcript_source("zid", cwd, None) is None
    day = home / "sessions" / "2026" / "10" / "02"
    new_id = "01a0ff00-0000-7000-8000-00000000000a"
    newp = day / f"rollout-2026-10-02T16-00-00-{new_id}.jsonl"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    newp.write_text(
        json.dumps({"timestamp": now_iso, "type": "session_meta", "payload": {"id": new_id, "session_id": new_id, "timestamp": now_iso, "cwd": cwd, "cli_version": "0.160.0"}}) + "\n"
        + json.dumps({"timestamp": now_iso, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi."}]}}) + "\n"
    )
    src = adapter.transcript_source("zid", cwd, None)
    assert src is not None and isinstance(src, RolloutTail) and src.path == newp
    out = src.poll()
    assert blocks(out) == [("text", "Hi.")] and out[0].session_id == "zid"  # replayed from the start
    # a launch in another directory does not grab this file
    adapter.new_session("zid2", "/home/user/elsewhere", None, None)
    assert adapter.transcript_source("zid2", "/home/user/elsewhere", None) is None


def test_transcript_source_for_a_resumed_session_starts_at_the_end(adapter: CodexAdapter):
    info = adapter.find_session(SESSION_ID)
    src = adapter.transcript_source(SESSION_ID, "/home/user/Code/project", info)
    assert src is not None and src.poll() == []
    src2 = adapter.transcript_source(SESSION_ID, "/home/user/Code/project", None)  # by id alone
    assert src2 is not None and src2.poll() == []
    assert adapter.transcript_path(SESSION_ID) == src2.path


# ---- misc -----------------------------------------------------------------------------------------


def test_describe_call():
    assert C.describe_call("shell", {"command": ["bash", "-lc", "git status"]}) == "shell: git status"
    assert C.describe_call("shell", {"command": ["ls", "-la"]}) == "shell: ls -la"
    assert C.describe_call("apply_patch", {"input": "*** Begin Patch\n*** Add File: a.py\n*** Update File: b.py\n*** End Patch"}) == "apply_patch: a.py, b.py"
    assert C.describe_call("read_file", {"path": "/x/y.py"}) == "read_file: /x/y.py"
    assert C.describe_call("mystery", {}) == "mystery"


def test_fixture_files_carry_no_secrets():
    """The live captures were masked before commit; keep them that way."""
    key = re.compile(r"sk-[A-Za-z0-9_-]{8,}(?!\[masked\])")
    for p in FIXTURES.iterdir():
        if p.suffix != ".txt" and p.suffix != ".jsonl":
            continue
        text = p.read_bytes().decode("utf-8", errors="replace")
        for m in key.finditer(text):
            assert m.group(0).startswith("sk-proj-[masked]"), f"{p.name}: {m.group(0)}"
        assert "req_" not in text or "req_[masked]" in text, p.name
