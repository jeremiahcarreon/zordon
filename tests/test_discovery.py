"""discovery.py against a synthetic ~/.claude tree, the real /proc, and the command builders."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests import fixtures_store as fs
from zordon.session import discovery as D
from zordon.session.discovery import (
    ALLOWED_MODES,
    REFUSED_FLAGS,
    SessionInfo,
    encode_project_dir,
    env_scrub_prefix,
    find_session,
    hook_command,
    hook_curl_config_path,
    hook_host,
    hook_settings_json,
    jsonl_path_for,
    list_sessions,
    load_history_index,
    load_registry,
    new_session_command,
    pid_alive,
    read_head,
    read_tail,
    resume_command,
    strip_env_prefix,
    validate_command,
    write_hook_settings,
)
from zordon.session.tmux import SCRUB_NAMES

T0 = 1_790_900_000.0  # a fixed epoch in 2026


@pytest.fixture(autouse=True)
def _clear_cache():
    D.clear_cache()
    yield
    D.clear_cache()


# ---- encoding ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cwd,encoded",
    [
        ("/home/operator/Videos/Season 1 Mp4 1080p", "-home-operator-Videos-Season-1-Mp4-1080p"),
        ("/home/operator/Code/example.com", "-home-operator-Code-example-com"),
        ("/tmp/claude-1000/-home-operator-Code-zordon/x/hooktest-cwd", "-tmp-claude-1000--home-operator-Code-zordon-x-hooktest-cwd"),
        ("/home/u/proj_a", "-home-u-proj-a"),
    ],
)
def test_encode_project_dir(cwd: str, encoded: str):
    assert encode_project_dir(cwd) == encoded


def test_jsonl_path_for(tmp_path: Path):
    p = jsonl_path_for("/home/u/proj", fs.sid(1), tmp_path)
    assert p == tmp_path / "projects" / "-home-u-proj" / f"{fs.sid(1)}.jsonl"


# ---- a populated store --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = fs.make_claude_home(tmp_path)
    a, b, c = fs.sid(1), fs.sid(2), fs.sid(3)
    cwd_a = "/home/u/Code/alpha"
    cwd_b = "/home/u/Code/beta.app"
    cwd_c = "/home/u/Code/gamma"

    # A: ordinary fresh session, oldest activity, ai-title + permission-mode + last-prompt.
    fs.write_session(
        home,
        cwd_a,
        a,
        [
            fs.meta_user(a, cwd_a, T0, "<local-command-caveat>Caveat: ...</local-command-caveat>"),
            fs.user_prompt(a, cwd_a, T0 + 1, "Add retry logic to the upload handler"),
            fs.assistant_record(a, cwd_a, T0 + 3, [fs.text_block("On it.")], stop_reason="end_turn"),
            fs.ai_title(a, "Upload retry logic"),
            fs.permission_mode(a, "acceptEdits"),
            fs.last_prompt(a, "Add retry logic to the upload handler"),
            fs.turn_duration(a, cwd_a, T0 + 4),
        ],
    )
    # B: forked file whose first line is a giant snapshot (bigger than the head byte cap), a
    # custom title that beats the ai title, mode rewritten (latest wins), newest activity.
    fs.write_session(
        home,
        cwd_b,
        b,
        [
            fs.file_history_snapshot(b, 3 * 1024 * 1024),
            fs.ai_title(b, "Old title"),
            fs.permission_mode(b, "plan"),
            fs.user_prompt(b, cwd_b, T0 + 100, "Plan the beta release"),
            fs.assistant_record(b, cwd_b, T0 + 105, [fs.text_block("Plan follows.")], stop_reason="end_turn"),
            fs.custom_title(b, "beta release"),
            fs.ai_title(b, "Beta release planning"),
            fs.permission_mode(b, "default"),
            fs.last_prompt(b, "Plan the beta release"),
        ],
    )
    # C: no cwd anywhere in the head (metadata only), middle activity via history.jsonl only.
    fs.write_session(
        home,
        cwd_c,
        c,
        [fs.ai_title(c, "Gamma session"), fs.permission_mode(c, "default")],
        mtime=T0 + 50,
    )
    # Non-session files that must be ignored.
    (home / "projects" / encode_project_dir(cwd_a) / "notes.jsonl").write_text("{}\n")
    (home / "projects" / encode_project_dir(cwd_a) / a).mkdir()  # per-session side dir
    (home / "projects" / "memory-only").mkdir()
    fs.write_history(
        home,
        [
            ("/model", cwd_a, T0 + 0.5, a),
            ("Add retry logic to the upload handler", cwd_a, T0 + 1, a),
            ("What is in this repo?", cwd_c, T0 + 40, c),
            ("Plan the beta release", cwd_b, T0 + 100, b),
            ("Gone session prompt", "/home/u/Code/gone", T0 - 1000, fs.sid(9)),
        ],
    )
    return home, {"a": a, "b": b, "c": c, "cwd_a": cwd_a, "cwd_b": cwd_b, "cwd_c": cwd_c}


def test_list_sessions_fields_and_ordering(store):
    home, ids = store
    sessions = list_sessions(home)
    assert [s.session_id for s in sessions] == [ids["b"], ids["c"], ids["a"]]  # last_active desc
    by_id = {s.session_id: s for s in sessions}

    a = by_id[ids["a"]]
    assert a.cwd == ids["cwd_a"] and a.cwd_source == "record"
    assert a.title == "Upload retry logic"
    assert a.first_prompt == "Add retry logic to the upload handler"  # the isMeta caveat is skipped
    assert a.last_prompt == "Add retry logic to the upload handler"
    assert a.permission_mode == "acceptEdits"
    assert a.started_at == fs.iso(T0) and a.last_active == fs.iso(T0 + 4)
    assert a.version == "2.1.287" and a.git_branch == "main"
    assert not a.running and a.tmux_target is None and a.status is None
    assert a.project_dir == "-home-u-Code-alpha"
    assert a.jsonl_path.name == f"{ids['a']}.jsonl"

    b = by_id[ids["b"]]
    assert b.title == "beta release"  # custom-title beats ai-title
    assert b.permission_mode == "default"  # latest permission-mode record wins
    # The 3 MB first line exhausts the head byte cap, so the cwd comes from history.jsonl.
    assert b.cwd == ids["cwd_b"] and b.cwd_source == "history"
    assert b.size > 3 * 1024 * 1024
    assert b.last_active == fs.iso(T0 + 105)

    c = by_id[ids["c"]]
    assert c.cwd == ids["cwd_c"] and c.cwd_source == "history"
    assert c.first_prompt == "What is in this repo?"
    assert c.title == "Gamma session"
    assert c.last_active is None and c.last_active_ts == pytest.approx(T0 + 40)


def test_head_is_bounded_by_lines_and_bytes(tmp_path: Path):
    home = fs.make_claude_home(tmp_path)
    s = fs.sid(4)
    cwd = "/home/u/x"
    # 45 metadata lines before the first conversation record: beyond HEAD_LINES.
    recs = [fs.ai_title(s, f"t{i}") for i in range(45)] + [fs.user_prompt(s, cwd, T0, "hello")]
    path = fs.write_session(home, cwd, s, recs)
    info = SessionInfo(s, path, "x")
    read_head(path, info)
    assert info.cwd is None and info.first_prompt is None
    read_tail(path, info)
    assert info.last_active == fs.iso(T0)
    # history fills in the cwd
    fs.write_history(home, [("hello", cwd, T0, s)])
    listed = list_sessions(home)
    assert listed[0].cwd == cwd and listed[0].cwd_source == "history"
    # dirname fallback when nothing knows the path
    s2 = fs.sid(5)
    fs.write_session(home, "/home/u/y", s2, [fs.ai_title(s2, "t")])
    y = next(x for x in list_sessions(home) if x.session_id == s2)
    assert y.cwd_source == "dirname-approx" and y.cwd == "/home/u/y"


def test_tail_window_grows_past_a_giant_last_line(tmp_path: Path):
    home = fs.make_claude_home(tmp_path)
    s = fs.sid(6)
    cwd = "/home/u/z"
    recs = [
        fs.user_prompt(s, cwd, T0, "first"),
        fs.permission_mode(s, "plan"),
        fs.file_history_snapshot(s, 1_200_000),  # 1.2 MB single line at EOF
    ]
    path = fs.write_session(home, cwd, s, recs)
    info = SessionInfo(s, path, "z")
    read_tail(path, info)
    assert info.permission_mode == "plan"
    assert info.last_active == fs.iso(T0)
    assert D.tail_lines(path, start=1024, maximum=16 * 1024 * 1024)


def test_find_session(store):
    home, ids = store
    info = find_session(ids["a"], home)
    assert info is not None and info.title == "Upload retry logic"
    assert find_session(fs.sid(42), home) is None
    assert find_session("not-a-uuid", home) is None


def test_cache_invalidates_on_change(store):
    home, ids = store
    first = find_session(ids["a"], home)
    assert first is not None
    path = first.jsonl_path
    with path.open("ab") as fh:
        fh.write(fs.jsonl_bytes([fs.custom_title(ids["a"], "renamed")]))
    os.utime(path, (T0 + 999, T0 + 999))
    second = find_session(ids["a"], home)
    assert second is not None and second.title == "renamed"


def test_history_index(store):
    home, ids = store
    idx = load_history_index(home)
    assert idx[ids["a"]].project == ids["cwd_a"]
    assert idx[ids["a"]].first_prompt == "Add retry logic to the upload handler"  # "/model" is a slash command
    assert idx[fs.sid(9)].project == "/home/u/Code/gone"
    assert idx[ids["b"]].last_ts == pytest.approx(T0 + 100)
    assert load_history_index(home / "nope") == {}


# ---- registry liveness -----------------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.run(["true"], check=False)
    return proc.pid if hasattr(proc, "pid") else subprocess.Popen(["true"]).wait() or 999_999  # pragma: no cover


def test_registry_liveness(store):
    home, ids = store
    me = os.getpid()
    ticks = D.proc_start_ticks(me)
    if sys.platform.startswith("linux"):
        assert ticks is not None and ticks.isdigit()
    fs.write_registry_entry(home, me, ids["a"], ids["cwd_a"], status="waiting", tmux="zordon:@1.%1")
    # Stale pid: a child that already exited.
    child = subprocess.Popen(["true"])
    child.wait()
    fs.write_registry_entry(home, child.pid, ids["b"], ids["cwd_b"], proc_start="1")
    # Pid reuse: our pid with a wrong procStart.
    other = home / "sessions" / "reused.json"
    other.write_text(json.dumps({"pid": me, "sessionId": ids["c"], "cwd": ids["cwd_c"], "procStart": "1", "status": "busy"}))
    # A daemon spare is not a session.
    fs.write_registry_entry(home, me, fs.sid(8), "/tmp", spare=True, filename="spare.json")
    (home / "sessions" / "garbage.json").write_text("not json")
    (home / "sessions" / "daemon.status.json").write_text(json.dumps({"supervisorPid": me}))

    live = load_registry(home)
    if sys.platform.startswith("linux"):
        assert set(live) == {ids["a"]}
    else:  # pragma: no cover - no /proc
        assert ids["a"] in live and ids["b"] not in live

    sessions = {s.session_id: s for s in list_sessions(home)}
    a = sessions[ids["a"]]
    assert a.running and a.running_pid == me and a.status == "waiting" and a.tmux_target == "zordon:@1.%1"
    assert not sessions[ids["b"]].running
    if sys.platform.startswith("linux"):
        assert not sessions[ids["c"]].running

    assert pid_alive(me)
    assert pid_alive(me, ticks)
    assert not pid_alive(child.pid, "1")
    if sys.platform.startswith("linux"):
        assert not pid_alive(me, "1")
    assert not pid_alive(2**22 - 1)


class _FakeTmux:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    def pane_exists(self, target: str) -> bool:
        return target in self.existing


def test_registry_tmux_target_checked_against_tmux(store):
    home, ids = store
    fs.write_registry_entry(home, os.getpid(), ids["a"], ids["cwd_a"], tmux="zordon:@1.%1")
    with_pane = find_session(ids["a"], home, tmux=_FakeTmux({"zordon:@1.%1"}))
    without = find_session(ids["a"], home, tmux=_FakeTmux(set()))
    assert with_pane is not None and with_pane.tmux_target == "zordon:@1.%1"
    assert without is not None and without.tmux_target is None and without.running


# ---- command builders ----------------------------------------------------------------------------


EMPTY_ENV: dict[str, str] = {}
FIXED_PREFIX = ["env"] + [a for name in sorted(SCRUB_NAMES) for a in ("-u", name)]


def test_resume_and_new_session_commands():
    s = fs.sid(1)
    assert resume_command(s, environ=EMPTY_ENV) == FIXED_PREFIX + ["claude", "--resume", s]
    assert resume_command(s, Path("/x/hooks.json"), "manual", environ=EMPTY_ENV) == FIXED_PREFIX + [
        "claude", "--resume", s, "--settings", "/x/hooks.json", "--permission-mode", "default",
    ]
    assert resume_command(s, None, "acceptEdits")[-2:] == ["--permission-mode", "acceptEdits"]
    assert new_session_command(s, environ=EMPTY_ENV) == FIXED_PREFIX + ["claude", "--session-id", s]
    assert new_session_command(s, Path("/x/h.json"), "plan", environ=EMPTY_ENV) == FIXED_PREFIX + [
        "claude", "--session-id", s, "--settings", "/x/h.json", "--permission-mode", "plan",
    ]
    for mode in ALLOWED_MODES:
        argv = resume_command(s, None, mode)
        assert argv[-1] == mode
        assert not any(flag in argv for flag in REFUSED_FLAGS)
        assert not any("bypass" in a.lower() for a in argv)
        assert strip_env_prefix(argv)[0] == "claude"


def test_commands_unset_secrets_and_claude_markers_from_the_parent_environment():
    """RR-7 / SEC-2: the pane process never sees provider keys or nesting markers."""
    environ = {
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "OPENAI_API_KEY": "sk-x",
        "GITHUB_TOKEN": "ghp",
        "DEPLOY_SECRET": "s",
        "CLAUDE_CODE_EXECPATH": "/x",
        "CLAUDE_CONFIG_DIR": "/alt",
        "PATH": "/bin",
    }
    prefix = env_scrub_prefix(environ)
    assert prefix[0] == "env" and prefix[1::2] == ["-u"] * (len(prefix) // 2)
    unset = prefix[2::2]
    for name in SCRUB_NAMES + ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "DEPLOY_SECRET", "CLAUDE_CODE_EXECPATH"):
        assert name in unset
    assert "CLAUDE_CONFIG_DIR" not in unset and "PATH" not in unset
    argv = new_session_command(fs.sid(2), None, "default", environ=environ)
    assert argv[: len(prefix)] == prefix
    assert argv[len(prefix) :] == ["claude", "--session-id", fs.sid(2), "--permission-mode", "default"]
    # The live environment is the default.
    live = new_session_command(fs.sid(2))
    assert live[0] == "env" and "claude" in live
    for name in SCRUB_NAMES:
        assert name in live


def test_strip_env_prefix_only_accepts_unsets():
    s = fs.sid(1)
    assert strip_env_prefix(["claude", "--resume", s]) == ["claude", "--resume", s]
    assert strip_env_prefix(["env", "-u", "A_TOKEN", "-u", "B", "claude", "x"]) == ["claude", "x"]
    for bad in (
        ["env", "ANTHROPIC_API_KEY=sk", "claude"],
        ["env", "-i", "claude"],
        ["env", "-u", "claude"],
        ["env", "-u", "1bad", "claude"],
        ["env", "-u", "A", "bash", "-c", "claude"],
        ["env"],
    ):
        with pytest.raises(ValueError):
            strip_env_prefix(bad)
        with pytest.raises(ValueError):
            validate_command(bad)
    validate_command(["env", "-u", "CLAUDECODE", "claude", "--permission-mode", "plan"])


@pytest.mark.parametrize("bad", ["bypassPermissions", "bypass", "yolo", "", "dangerously", "Default"])
def test_builders_refuse_bad_modes(bad: str):
    s = fs.sid(1)
    if bad == "":
        assert strip_env_prefix(resume_command(s, None, bad)) == ["claude", "--resume", s]  # falsy mode: not passed
        return
    with pytest.raises(ValueError):
        resume_command(s, None, bad)
    with pytest.raises(ValueError):
        new_session_command(s, None, bad)


def test_builders_refuse_a_mode_string_in_the_settings_slot():
    with pytest.raises(TypeError):
        resume_command(fs.sid(1), "plan")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        new_session_command(fs.sid(1), "/x/hooks.json")  # type: ignore[arg-type]


def test_builders_refuse_bad_ids():
    with pytest.raises(ValueError):
        resume_command("0b0b0b0b")
    with pytest.raises(ValueError):
        new_session_command("../../etc/passwd")


def test_validate_command_refuses_every_bypass_shape():
    s = fs.sid(1)
    for flag in REFUSED_FLAGS:
        with pytest.raises(ValueError):
            validate_command(["claude", "--resume", s, flag])
    with pytest.raises(ValueError):
        validate_command(["claude", "--permission-mode", "bypassPermissions"])
    with pytest.raises(ValueError):
        validate_command(["claude", "--permission-mode=bypassPermissions"])
    with pytest.raises(ValueError):
        validate_command(["claude", "--settings", '{"permissions":{"defaultMode":"bypassPermissions"}}'])
    with pytest.raises(ValueError):
        validate_command(["claude", "--settings", '{"skipDangerousModePermissionPrompt": true}'])
    with pytest.raises(ValueError):
        validate_command(["bash", "-c", "claude"])
    validate_command(["claude", "--settings", '{"permissions":{"defaultMode":"plan"}}'])
    assert "--dangerously-skip-permissions" in REFUSED_FLAGS
    assert "--allow-dangerously-skip-permissions" in REFUSED_FLAGS
    assert "--bare" in REFUSED_FLAGS and "--safe-mode" in REFUSED_FLAGS


# ---- hook settings ---------------------------------------------------------------------------


def test_hook_settings_json_shape():
    rc = Path("/home/u/.zordon/hooks/abc12345.curlrc")
    data = hook_settings_json(8765, rc)
    hooks = data["hooks"]
    assert set(hooks) == {"Notification", "UserPromptSubmit", "Stop", "PermissionRequest"}
    notif = hooks["Notification"]
    assert len(notif) == 1
    assert notif[0]["matcher"] == "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog"
    handler = notif[0]["hooks"][0]
    assert handler["type"] == "command" and handler["async"] is True and handler["timeout"] == 5
    assert handler["command"] == (
        "curl -s -m 2 -X POST -H 'Content-Type: application/json' "
        "-K /home/u/.zordon/hooks/abc12345.curlrc --data-binary @- "
        "http://127.0.0.1:8765/hooks/claude >/dev/null 2>&1 || true"
    )
    assert "matcher" not in hooks["Stop"][0]
    assert hooks["Stop"][0]["hooks"][0]["command"].endswith("|| true")
    # The PermissionRequest hook (decision 0019): synchronous, long timeout, prints the
    # decision; "no opinion" when Zordon cannot be reached so Claude Code draws its dialog.
    perm = hooks["PermissionRequest"][0]["hooks"][0]
    assert perm["timeout"] == 900 and "async" not in perm
    assert "/hooks/permission" in perm["command"] and perm["command"].endswith("|| printf '%s' '{}'")
    assert "PermissionRequest" not in hook_settings_json(8765, rc, permission=False)["hooks"]
    only = hook_settings_json(8765, rc, events=("Notification",), permission=False)
    assert set(only["hooks"]) == {"Notification"}
    with pytest.raises(ValueError):
        hook_settings_json(8765, rc, events=("PermissionRequest",))
    with pytest.raises(ValueError):
        hook_settings_json(0, rc)
    with pytest.raises(ValueError):
        hook_settings_json(8765, "")
    json.dumps(data)  # serialisable
    # A path with spaces is one shell word.
    cmd = hook_command(8765, Path("/home/u/my zordon/h.curlrc"))
    assert "-K '/home/u/my zordon/h.curlrc'" in cmd


def test_hook_secret_never_appears_on_the_curl_command_line(tmp_path: Path):
    """SEC-6: the secret lives only in the 0600 curl config read with ``-K``."""
    secret = "s3cr3t_" + "x" * 20
    path = write_hook_settings(tmp_path / "hooks" / "abc12345.json", 8765, secret)
    text = path.read_text()
    assert secret not in text
    rc = hook_curl_config_path(path)
    assert rc == tmp_path / "hooks" / "abc12345.curlrc"
    assert rc.read_text() == f'header = "X-Zordon-Hook-Secret: {secret}"\n'
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600
    for event in json.loads(text)["hooks"].values():
        command = event[0]["hooks"][0]["command"]
        assert secret not in command
        assert f"-K {rc}" in command
        assert "X-Zordon-Hook-Secret" not in command
    with pytest.raises(ValueError):
        write_hook_settings(tmp_path / "hooks" / "bad.json", 8765, "short")
    with pytest.raises(ValueError):
        write_hook_settings(tmp_path / "hooks" / "bad.json", 8765, "has a quote ' in it xxxxxxxxxx")
    assert not (tmp_path / "hooks" / "bad.json").exists()


def test_hook_host_follows_the_bind_address():
    """SEC-8: the POST goes where the server listens; wildcard binds mean loopback."""
    assert hook_host("127.0.0.1") == "127.0.0.1"
    assert hook_host("0.0.0.0") == "127.0.0.1"
    assert hook_host("") == "127.0.0.1" and hook_host(None) == "127.0.0.1"
    assert hook_host("::") == "127.0.0.1"
    assert hook_host("100.101.102.103") == "100.101.102.103"
    assert hook_host("localhost") == "localhost"
    assert hook_host("::1") == "[::1]" and hook_host("[::1]") == "[::1]"
    with pytest.raises(ValueError):
        hook_host("127.0.0.1 evil")
    cmd = hook_command(8765, Path("/x/h.curlrc"), host="100.101.102.103")
    assert "http://100.101.102.103:8765/hooks/claude" in cmd
    cmd = hook_command(8765, Path("/x/h.curlrc"), host="0.0.0.0")
    assert "http://127.0.0.1:8765/hooks/claude" in cmd
    data = hook_settings_json(9000, Path("/x/h.curlrc"), host="100.64.0.9")
    assert "http://100.64.0.9:9000/hooks/claude" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_write_hook_settings_is_private(tmp_path: Path):
    secret = "k" * 24
    path = write_hook_settings(tmp_path / "hooks" / "abc12345.json", 8765, secret)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert data == hook_settings_json(8765, hook_curl_config_path(path))
    assert D.hook_settings_path(tmp_path, fs.sid(1)) == tmp_path / "hooks" / f"{fs.sid(1)[:8]}.json"
    # Rewriting keeps the mode and replaces the content atomically.
    write_hook_settings(path, 9000, secret, host="0.0.0.0")
    assert "127.0.0.1:9000" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    write_hook_settings(path, 9000, secret, host="100.1.2.3")
    assert "http://100.1.2.3:9000/hooks/claude" in path.read_text()
    # Removal takes the curl config with it; a second removal is fine.
    D.remove_hook_settings(path)
    assert not path.exists() and not hook_curl_config_path(path).exists()
    D.remove_hook_settings(path)
    D.remove_hook_settings(None)
