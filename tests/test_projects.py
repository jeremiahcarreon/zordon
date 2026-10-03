"""Projects (decision 0018): the store, the folder picker, the manager flows, the bypass
launch option and the edit-scope hook."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_manager import claude_argv, drain
from tests.test_manager import env as env  # noqa: F401, PLC0414 - fixture re-export
from zordon import projects as prj
from zordon.session import discovery
from zordon.session.manager import SessionError, scope_reason
from zordon.session.prompts import BYPASS_DIALOG_TITLE, PromptKind, detect_prompt

# ---- store -------------------------------------------------------------------------------


def test_store_roundtrip_order_and_lookup(tmp_path: Path):
    store = prj.ProjectStore(tmp_path / "projects.json")
    a = store.add(prj.new_project(str(tmp_path / "a"), name="Alpha", permission_mode="auto"))
    b = store.add(prj.new_project(str(tmp_path / "b"), name="Beta", permission_mode="bypassPermissions", scope_edits=False))
    store.touch(a)
    again = prj.ProjectStore(tmp_path / "projects.json")
    assert [p.name for p in again.list()] == ["Alpha", "Beta"]  # most recently used first
    assert again.by_directory(str(tmp_path / "b")).id == b.id
    assert again.by_name("beta").id == b.id and again.by_name("alp").id == a.id and again.by_name("zzz") is None
    assert again.get(b.id).bypass and not again.get(b.id).scope_edits
    assert (tmp_path / "projects.json").stat().st_mode & 0o777 == 0o600
    assert again.remove(a.id) and not again.remove(a.id)
    assert [p.name for p in prj.ProjectStore(tmp_path / "projects.json").list()] == ["Beta"]


def test_store_keeps_a_damaged_file_aside(tmp_path: Path):
    p = tmp_path / "projects.json"
    p.write_text("{not json")
    store = prj.ProjectStore(p)
    assert store.list() == []
    assert (tmp_path / "projects.json.broken").exists()


def test_new_project_refuses_unknown_mode(tmp_path: Path):
    with pytest.raises(prj.ProjectError):
        prj.new_project(str(tmp_path), permission_mode="yolo")


# ---- folder picker ---------------------------------------------------------------------


def test_browse_stays_inside_home(tmp_path: Path):
    home = tmp_path / "home"
    (home / "Code" / "site").mkdir(parents=True)
    (home / "Code" / "site" / ".git").mkdir()
    (home / ".hidden").mkdir()
    (home / "notes.txt").write_text("x")
    listing = prj.browse(None, home=str(home))
    assert listing.path == str(home.resolve()) and listing.parent is None
    assert [e.name for e in listing.entries] == ["Code"]  # hidden and files are skipped
    deeper = prj.browse(str(home / "Code"), home=str(home))
    assert deeper.parent == str(home.resolve()) and deeper.entries[0].has_git
    with pytest.raises(prj.ProjectError):
        prj.browse(str(tmp_path), home=str(home))
    with pytest.raises(prj.ProjectError):
        prj.browse(str(home / "Code" / ".." / ".." / ".."), home=str(home))


def test_create_directory_rules(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    made = prj.create_directory(str(home), "My App", home=str(home))
    assert made == str(home / "My App") and os.path.isdir(made)
    assert prj.create_directory(str(home), "My App", home=str(home)) == made  # empty: reused
    (home / "My App" / "f").write_text("x")
    with pytest.raises(prj.ProjectError, match="already exists"):
        prj.create_directory(str(home), "My App", home=str(home))
    for bad in ("../x", "a/b", ".hidden", "", "x" * 70):
        with pytest.raises(prj.ProjectError):
            prj.create_directory(str(home), bad, home=str(home))
    with pytest.raises(prj.ProjectError, match="home"):
        prj.create_directory(str(tmp_path), "outside", home=str(home))


# ---- launcher: bypass only with allow_bypass ----------------------------------------------


def test_bypass_mode_needs_the_project_flag():
    sid = "0a0a0a0a-0000-4000-8000-00000000000a"
    with pytest.raises(ValueError):
        discovery.new_session_command(sid, None, "bypassPermissions")
    argv = discovery.new_session_command(sid, None, "bypassPermissions", allow_bypass=True)
    assert discovery.strip_env_prefix(argv) == ["claude", "--session-id", sid, "--permission-mode", "bypassPermissions"]
    argv = discovery.resume_command(sid, None, "bypassPermissions", allow_bypass=True)
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    # The dangerous flag stays refused whatever the project says.
    with pytest.raises(ValueError):
        discovery.validate_command(["claude", "--dangerously-skip-permissions"], allow_bypass=True)
    with pytest.raises(ValueError):
        discovery.validate_command(["claude", "--settings", '{"permissions":{"defaultMode":"bypassPermissions"}}'], allow_bypass=True)


def test_scope_hook_settings(tmp_path: Path):
    settings = discovery.hook_settings_json(8765, tmp_path / "c.curlrc", scope=True)
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    cmd = pre[0]["hooks"][0]["command"]
    assert "/hooks/scope" in cmd and "async" not in pre[0]["hooks"][0]
    assert '"permissionDecision": "deny"' in cmd  # fail closed when Zordon is unreachable
    assert "PreToolUse" not in discovery.hook_settings_json(8765, tmp_path / "c.curlrc")["hooks"]
    written = discovery.write_hook_settings(tmp_path / "h.json", 8765, "s3cr3t-s3cr3t-s3cr3t", scope=True)
    assert "PreToolUse" in json.loads(written.read_text())["hooks"]


# ---- scope decision --------------------------------------------------------------------------


def test_scope_reason(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    agent_home = tmp_path / ".claude"
    agent_home.mkdir()
    assert scope_reason(str(proj / "src" / "a.py"), str(proj), agent_home) is None
    assert scope_reason("src/a.py", str(proj), agent_home) is None  # relative to the project
    assert scope_reason(str(agent_home / "scratch" / "x.md"), str(proj), agent_home) is None
    assert "outside" in scope_reason(str(tmp_path / "other" / "b.py"), str(proj), agent_home)
    assert "outside" in scope_reason(str(proj) + "/../other/b.py", str(proj), agent_home)
    assert scope_reason("", str(proj), agent_home) is not None
    link = tmp_path / "proj" / "escape"
    link.symlink_to(tmp_path)
    assert "outside" in scope_reason(str(link / "x.py"), str(proj), agent_home)  # symlinks resolved


# ---- the bypass warning dialog -------------------------------------------------------------


BYPASS_SCREEN = [
    "",
    " WARNING: Claude Code running in Bypass Permissions mode",
    "",
    " In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially dangerous commands.",
    " This mode should only be used in a sandboxed container/VM that has restricted internet access and can easily be restored if damaged.",
    "",
    " By proceeding, you accept all responsibility for actions taken while running in Bypass Permissions mode.",
    "",
    " ❯ No, exit",
    "   Yes, I accept",
    "",
    " Enter to confirm · Esc to cancel",
]


def test_bypass_warning_is_a_trust_kind_prompt():
    m = detect_prompt(BYPASS_SCREEN)
    assert m is not None and m.kind is PromptKind.TRUST and m.title == BYPASS_DIALOG_TITLE
    assert [o.label for o in m.options] == ["No, exit", "Yes, I accept"] and m.options[0].selected
    assert m.extra["dialog"] == "bypass" and m.confidence == 1.0


# ---- manager flows ---------------------------------------------------------------------------


def test_create_open_pause_forget(env, tmp_path: Path, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    row = mgr.create_project(str(home), "Site", permission_mode="auto")
    assert row["running"] and row["focused"] and os.path.isdir(home / "Site")
    sid = row["session_id"]
    command = claude_argv(tmux.windows[0][3])
    assert command[:3] == ["claude", "--session-id", sid] and command[command.index("--permission-mode") + 1] == "auto"
    settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
    assert "PreToolUse" in settings["hooks"]  # scope_edits defaults on
    saved = mgr.projects.get(row["id"])
    assert saved.session_id == sid and saved.tmux_target == tmux.windows[0][0]
    assert mgr.sessions[sid].scope_dir == str((home / "Site").resolve()) and mgr.sessions[sid].title == "Site"

    # admin: nothing focused, pane untouched
    mgr.admin()
    assert mgr.focused() is None and tmux.pane_exists(saved.tmux_target)
    listed = mgr.list_projects()
    assert listed[0]["name"] == "Site" and listed[0]["running"] and not listed[0]["focused"]

    # open: the attached session is simply refocused
    assert mgr.open_project(row["id"])["session_id"] == sid and mgr.focused() == sid

    # the same folder cannot become a second project
    with pytest.raises(SessionError, match="already a project"):
        mgr.create_project(str(home / "Site"), "", existing=True)

    # forget leaves files and pane alone
    assert mgr.forget_project(row["id"])
    assert os.path.isdir(home / "Site") and tmux.pane_exists(saved.tmux_target)
    assert mgr.list_projects() == [] and mgr.sessions[sid].project_id is None


def test_open_reconnects_to_a_live_pane_or_starts_fresh(env, tmp_path: Path, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    row = mgr.create_project(str(home), "Api")
    pid, sid, target = row["id"], row["session_id"], mgr.projects.get(row["id"]).tmux_target
    # Zordon restarts: sessions are gone but the pane lives on.
    mgr.sessions.clear()
    mgr._focused = None
    again = mgr.open_project(pid)
    assert again["session_id"] == sid and mgr.sessions[sid].target == target and not mgr.sessions[sid].owned
    assert mgr.sessions[sid].detail == "reconnected"
    # Pane gone and the conversation unknown to the agent store: start a new one in the folder.
    mgr.sessions.clear()
    tmux.alive[target] = False
    fresh = mgr.open_project(pid)
    assert fresh["session_id"] != sid and fresh["running"]
    assert tmux.windows[-1][2] == str((home / "Api").resolve())
    assert mgr.projects.get(pid).session_id == fresh["session_id"]


def test_bypass_project_launches_in_bypass_and_may_accept_the_warning(env, tmp_path: Path, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    row = mgr.create_project(str(home), "Yolo", permission_mode="bypassPermissions", scope_edits=True)
    sid = row["session_id"]
    command = claude_argv(tmux.windows[0][3])
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" not in command
    s = mgr.sessions[sid]
    assert s.bypass_allowed and s.scope_dir
    # The warning dialog shows on the normal screen; accept picks "Yes, I accept".
    tmux.set_screen(s.target, BYPASS_SCREEN, alt=False)
    mgr._poll_session(s)
    assert s.current_prompt is not None and s.current_prompt.title == BYPASS_DIALOG_TITLE
    drain(bus)
    assert mgr.accept_trust(sid)
    keys = [c for c in tmux.calls if c[0] in ("key", "enter")]
    assert keys[-1] in (("enter", s.target), ("key", s.target, "Enter"))


def test_bypass_warning_in_a_plain_session_is_not_accepted(env, tmp_path: Path):
    mgr, bus, tmux, clock, proj = env
    sid = mgr.start(str(proj))
    s = mgr.sessions[sid]
    assert not s.bypass_allowed
    tmux.set_screen(s.target, BYPASS_SCREEN, alt=False)
    mgr._poll_session(s)
    assert s.current_prompt is not None and s.current_prompt.title == BYPASS_DIALOG_TITLE
    drain(bus)
    assert not mgr.accept_trust(sid)
    notices = [e for e in drain(bus) if type(e).__name__ == "Notice"]
    assert notices and "not set up for that" in notices[0].text
    assert not [c for c in tmux.calls if c[0] in ("key", "enter") and c[1] == s.target]


def test_codex_projects_cannot_bypass(env, tmp_path: Path, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if "codex" not in mgr.adapters:
        pytest.skip("codex adapter not built in this environment")
    with pytest.raises(SessionError, match="cannot run without approvals"):
        mgr.create_project(str(home), "Cx", agent="codex", permission_mode="bypassPermissions")
    assert not os.path.exists(home / "Cx") or mgr.projects.list() == []


def test_scope_decision_denies_outside_edits(env, tmp_path: Path, monkeypatch):
    mgr, bus, tmux, clock, proj = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    row = mgr.create_project(str(home), "Scoped")
    sid = row["session_id"]
    inside = {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(home / "Scoped" / "a.py")}}
    assert mgr.scope_decision(inside) == {}
    outside = {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(home / "other.py")}}
    out = mgr.scope_decision(outside)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "outside" in out["hookSpecificOutput"]["permissionDecisionReason"]
    scratch = {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(mgr.claude_home / "scratch.md")}}
    assert mgr.scope_decision(scratch) == {}
    # An unknown session with a cwd that is a scoped project is still checked; no project: no opinion.
    unknown = {"session_id": "nope", "cwd": str(home / "Scoped"), "tool_input": {"file_path": "/etc/passwd"}}
    assert mgr.scope_decision(unknown)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert mgr.scope_decision({"session_id": "nope", "cwd": str(tmp_path), "tool_input": {"file_path": "/etc/passwd"}}) == {}
    # scope_edits off: never an opinion
    free = mgr.create_project(str(home), "Free", scope_edits=False)
    assert mgr.scope_decision({"session_id": free["session_id"], "tool_input": {"file_path": "/etc/passwd"}}) == {}
