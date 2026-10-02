"""``ClaudeCodeAdapter`` delegates to the session modules; these tests pin the
delegation (command shape, modes, prompt detection on the real pane fixtures,
transcript source) without ever running the ``claude`` binary.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests import fixtures_store as fs
from tests.conftest import read_fixture
from zordon import agents
from zordon.agents.base import AgentAdapter, HookRequest, LaunchSpec, SessionInfo
from zordon.agents.claude_code import CLAUDE_INFO, ClaudeCodeAdapter, JsonlTranscript
from zordon.bus import PaneLine, PromptKind
from zordon.session import discovery, permissions, prompts


def lines_of(name: str) -> list[str]:
    out = read_fixture(name).split("\n")
    if out and out[-1] == "":
        out.pop()
    return [line.rstrip(" ") for line in out]


@pytest.fixture
def adapter(tmp_path: Path) -> ClaudeCodeAdapter:
    a = ClaudeCodeAdapter(None, claude_home=fs.make_claude_home(tmp_path), zordon_home=tmp_path / "zh")
    return a


def test_info_and_registry(adapter: ClaudeCodeAdapter):
    assert isinstance(adapter, AgentAdapter)
    assert adapter.info is CLAUDE_INFO
    assert adapter.info.key == "claude-code" and adapter.info.display_name == "Claude Code"
    assert adapter.info.binary == "claude" and "npm install" in adapter.info.install_hint
    assert isinstance(agents.get_adapter("claude-code"), ClaudeCodeAdapter)
    assert adapter.uses_alternate_screen() and adapter.supports_resume()


def test_availability_follows_path(adapter: ClaudeCodeAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert adapter.available() is None
    assert adapter.version() is None  # never runs anything when the binary is missing
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/bin/sh\necho 'fake-claude 9.9.9 (test)'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    fresh = ClaudeCodeAdapter(None, claude_home=tmp_path)
    assert fresh.available() == str(fake)
    assert fresh.version() == "fake-claude 9.9.9 (test)"
    fake.write_text("#!/bin/sh\necho changed\n")
    assert fresh.version() == "fake-claude 9.9.9 (test)"  # cached


# ---- launching ----------------------------------------------------------------------------


def test_new_session_command_shape_without_hooks(adapter: ClaudeCodeAdapter, tmp_path: Path):
    sid = fs.sid(1)
    spec = adapter.new_session(sid, str(tmp_path), None, None)
    assert isinstance(spec, LaunchSpec)
    argv = discovery.strip_env_prefix(spec.command)
    assert argv == ["claude", "--session-id", sid, "--permission-mode", "default"]
    assert spec.command[0] == "env" and "-u" in spec.command
    assert spec.cwd == str(tmp_path)
    assert spec.settings_paths == []
    assert "CLAUDECODE" in spec.env_scrub_names  # tmux.scrub_names(): nesting markers plus any *_API_KEY present
    for flag in spec.command:
        assert "dangerously" not in flag and "bypass" not in flag.lower()


def test_resume_command_with_hooks_writes_settings_and_curl_config(adapter: ClaudeCodeAdapter, tmp_path: Path):
    sid = fs.sid(2)
    req = HookRequest(port=8765, secret="s3cr3t-s3cr3t-s3cr3t", host="0.0.0.0", zordon_home=tmp_path / "zh", session_id=sid)
    spec = adapter.resume_session(sid, str(tmp_path), "acceptEdits", req)
    argv = discovery.strip_env_prefix(spec.command)
    assert argv[:3] == ["claude", "--resume", sid]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    settings = Path(argv[argv.index("--settings") + 1])
    assert settings == discovery.hook_settings_path(tmp_path / "zh", sid)
    assert spec.settings_paths == [settings, discovery.hook_curl_config_path(settings)]
    assert all(p.is_file() for p in spec.settings_paths)
    text = settings.read_text()
    assert "s3cr3t" not in text and "http://127.0.0.1:8765/hooks/claude" in text  # wildcard bind -> loopback
    assert "PermissionRequest" not in text


def test_hook_write_failure_launches_without_hooks(adapter: ClaudeCodeAdapter, tmp_path: Path):
    sid = fs.sid(3)
    req = HookRequest(port=8765, secret="short", host="127.0.0.1", zordon_home=tmp_path / "zh", session_id=sid)
    spec = adapter.new_session(sid, str(tmp_path), None, req)
    assert "--settings" not in spec.command and spec.settings_paths == []


def test_modes(adapter: ClaudeCodeAdapter):
    assert adapter.allowed_modes() == discovery.ALLOWED_MODES
    assert adapter.voice_switchable_modes() == permissions.VOICE_SWITCHABLE
    assert adapter.default_launch_mode() == "default"
    assert adapter.mode_cycle_key() == "BTab"
    assert adapter.forbidden_modes() == permissions.FORBIDDEN_TARGET_MODES
    assert adapter.normalize_mode("accept edits") == "acceptEdits"
    assert adapter.normalize_mode("manual") == "default"
    with pytest.raises(ValueError):
        adapter.normalize_mode("bypassPermissions")
    with pytest.raises(ValueError):
        adapter.new_session(fs.sid(4), "/tmp", "bypassPermissions", None)
    assert adapter.mode_label("acceptEdits") == "accept edits"


# ---- discovery ------------------------------------------------------------------------------


def test_list_and_find_sessions_map_to_agent_session_info(adapter: ClaudeCodeAdapter, tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sid = fs.sid(5)
    now = time.time()
    path = fs.write_session(
        adapter.home,
        str(proj),
        sid,
        [fs.user_prompt(sid, str(proj), now - 60, "hello there"), fs.permission_mode(sid, "plan")],
    )
    fs.write_registry_entry(adapter.home, os.getpid(), sid, str(proj), status="waiting", tmux="zordon:@1.%1")
    found = adapter.list_sessions()
    assert len(found) == 1
    info = found[0]
    assert isinstance(info, SessionInfo)
    assert info.agent == "claude-code" and info.session_id == sid and info.cwd == str(proj)
    assert info.title == "hello there"
    assert info.permission_mode == "plan"
    assert info.transcript_path == path
    assert info.running_pid == os.getpid() and info.tmux_target == "zordon:@1.%1"
    assert info.extra["running"] == "1" and info.extra["status"] == "waiting"
    assert adapter.status_hint(sid) == "waiting"
    assert adapter.find_session(sid) is not None and adapter.find_session(fs.sid(99)) is None
    assert adapter.status_hint(fs.sid(99)) is None


# ---- screen: the real fixtures through the adapter --------------------------------------------------


@pytest.mark.parametrize(
    "name, kind",
    [
        ("bash_permission.txt", PromptKind.PERMISSION),
        ("write_permission_question_wrapped.txt", PromptKind.PERMISSION),
        ("plan_approval.txt", PromptKind.PLAN),
        ("ask_user_question.txt", PromptKind.QUESTION),
        ("trust_dialog.txt", PromptKind.TRUST),
    ],
)
def test_detects_the_fixture_prompts(adapter: ClaudeCodeAdapter, name: str, kind: PromptKind):
    screen = adapter.parse(lines_of(name))
    m = adapter.detect_prompt(screen)
    assert m is not None and m.kind is kind
    assert m == prompts.detect_prompt(screen)
    assert not adapter.is_idle(screen)


def test_prompt_option_helpers_delegate(adapter: ClaudeCodeAdapter):
    perm = adapter.detect_prompt(adapter.parse(lines_of("bash_permission.txt")))
    assert perm is not None
    assert adapter.yes_option(perm) == 1 and adapter.no_option(perm) == 4
    plan = adapter.detect_prompt(adapter.parse(lines_of("plan_approval.txt")))
    assert plan is not None
    manual = adapter.plan_approve_option(plan)
    assert manual is not None and plan.option(manual).label == "Yes, manually approve edits"
    assert manual != prompts.plan_auto_option(plan)
    revise = adapter.plan_revise_option(plan)
    assert revise is not None and plan.option(revise).label == "Tell Claude what to change"
    ask = adapter.detect_prompt(adapter.parse(lines_of("ask_user_question.txt")))
    assert ask is not None
    assert adapter.question_option(ask, 1) == 1 and adapter.question_option(ask, ask.options[1].label) == 2
    trust = adapter.detect_prompt(adapter.parse(lines_of("trust_dialog.txt")))
    assert trust is not None
    assert trust.option(adapter.trust_accept_option(trust)).label == "Yes, I trust this folder"
    assert trust.option(adapter.trust_decline_option(trust)).label == "No, exit"


@pytest.mark.parametrize("name", ["idle.txt", "plan_idle.txt", "status_accept_edits.txt"])
def test_idle_fixtures(adapter: ClaudeCodeAdapter, name: str):
    screen = adapter.parse(lines_of(name))
    assert adapter.is_idle(screen) and adapter.input_quiet(screen)
    assert not adapter.is_working(screen) and not adapter.exited(screen)
    assert adapter.detect_prompt(screen) is None


def test_working_exit_and_mode_fixtures(adapter: ClaudeCodeAdapter):
    assert adapter.is_working(adapter.parse(lines_of("spinner_frames.txt")))
    assert not adapter.is_idle(adapter.parse(lines_of("working_no_spinner.txt")))
    assert adapter.exited(adapter.parse(lines_of("exit.txt")))
    assert not adapter.exited(adapter.parse(lines_of("idle.txt")))
    assert adapter.permission_mode_from_screen(adapter.parse(lines_of("idle.txt"))) == "default"
    assert adapter.permission_mode_from_screen(adapter.parse(lines_of("status_accept_edits.txt"))) == "acceptEdits"
    assert adapter.permission_mode_from_screen(adapter.parse(lines_of("plan_idle.txt"))) == "plan"


# ---- transcript --------------------------------------------------------------------------------


def test_transcript_source_yields_pane_lines(adapter: ClaudeCodeAdapter, tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sid = fs.sid(6)
    assert adapter.transcript_source(sid, str(proj), None) is None  # not written yet
    now = time.time()
    path = fs.write_session(
        adapter.home,
        str(proj),
        sid,
        [
            fs.user_prompt(sid, str(proj), now - 5, "say hi"),
            fs.permission_mode(sid, "acceptEdits"),
            fs.assistant_record(sid, str(proj), now - 4, [fs.text_block("Hello from the jsonl.")], stop_reason="end_turn"),
        ],
    )
    assert adapter.transcript_path(sid, str(proj)) == path
    src = adapter.transcript_source(sid, str(proj), None)  # a session Zordon started: replay from the start
    assert isinstance(src, JsonlTranscript) and src.path == path
    lines = src.poll()
    assert lines and all(isinstance(ln, PaneLine) and ln.source == "jsonl" and ln.session_id == sid for ln in lines)
    blocks = [ln.block for ln in lines]
    assert "permission_mode" in blocks and "text" in blocks and "turn_end" in blocks
    assert next(ln.text for ln in lines if ln.block == "text") == "Hello from the jsonl."
    assert src.last_mode == "acceptEdits"
    assert src.poll() == []
    # A resumed/attached session (info given) starts at the end of the file.
    info = SessionInfo(agent="claude-code", session_id=sid, cwd=str(proj), transcript_path=path)
    tail = adapter.transcript_source(sid, str(proj), info)
    assert tail is not None and tail.poll() == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write(fs.jsonl_bytes([fs.assistant_record(sid, str(proj), now, [fs.text_block("Later.")])]).decode())
    assert [ln.text for ln in tail.poll() if ln.block == "text"] == ["Later."]


# ---- hooks / permissions -------------------------------------------------------------------------


def test_hook_hint_and_permission_summary(adapter: ClaudeCodeAdapter, tmp_path: Path):
    payload = {
        "session_id": fs.sid(7),
        "transcript_path": "/x/y.jsonl",
        "cwd": "/x",
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs permission to run a Bash command",
    }
    hint = adapter.hook_hint(payload)
    assert hint is not None and hint.kind == "prompt" and hint.score > 0
    assert adapter.hook_hint({"session_id": ""}) is None
    assert adapter.hook_hint(["not", "a", "dict"]) is None  # type: ignore[arg-type]
    sentence = adapter.permission_summary(str(tmp_path), "plan")
    assert "plan" in sentence
    assert adapter.permission_summary("", None).startswith(permissions.UNKNOWN_MODE_TEXT[:10])
