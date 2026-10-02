"""Manifest + uninstall plan/execute, with fake runners (nothing is removed for real)."""

from __future__ import annotations

from pathlib import Path

from zordon import manifest, paths
from zordon import uninstall as un


def test_manifest_roundtrip_and_idempotence():
    manifest.record("system", "tmux", command="apt-get install -y tmux")
    manifest.record("system", "tmux", command="apt-get install -y tmux")
    manifest.record("ollama-model", "qwen2.5:3b-instruct")
    entries = manifest.load()
    assert [(e.kind, e.name) for e in entries] == [("system", "tmux"), ("ollama-model", "qwen2.5:3b-instruct")]
    assert manifest.has("system", "tmux")
    manifest.forget("system", "tmux")
    assert not manifest.has("system", "tmux")
    assert (manifest.manifest_path().stat().st_mode & 0o777) == 0o600


def test_plan_offers_only_what_zordon_installed(monkeypatch):
    manifest.record("system", "tmux")
    manifest.record("system", "node")
    manifest.record("system", "claude-code")
    manifest.record("ollama-model", "qwen2.5:3b-instruct")
    manifest.record("uv", "uv", removal="/home/u/.local/share/uv")
    which = lambda n: f"/usr/bin/{n}" if n in ("apt-get", "npm", "ollama", "uv", "sudo") else None  # noqa: E731
    monkeypatch.setenv("ZORDON_TOOL_MANAGER", "uv")
    plan = un.build_plan(which=which, root=False)
    inside = {i.key for i in plan.inside}
    assert inside == {"home", "tmux-session", "tool"}
    outside = {i.key: i for i in plan.outside}
    assert outside["system:tmux"].command == ["sh", "-c", "sudo apt-get remove -y tmux"]
    assert outside["system:node"].command == ["sh", "-c", "sudo apt-get remove -y nodejs npm"]
    assert outside["system:claude-code"].command == ["/usr/bin/npm", "uninstall", "-g", "@anthropic-ai/claude-code"]
    assert outside["ollama-model:qwen2.5:3b-instruct"].command == ["/usr/bin/ollama", "rm", "qwen2.5:3b-instruct"]
    assert outside["ollama-model:qwen2.5:3b-instruct"].recommended is True
    assert outside["system:tmux"].recommended is False
    assert "uv" in outside and outside["uv"].recommended is False
    # root drops sudo
    plan_root = un.build_plan(which=which, root=True)
    assert {i.key: i for i in plan_root.outside}["system:tmux"].command == ["sh", "-c", "apt-get remove -y tmux"]


def test_execute_removes_home_runs_commands_last_tool_and_updates_manifest(tmp_path: Path):
    manifest.record("system", "tmux")
    manifest.record("ollama-model", "m")
    home = paths.zordon_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("x")
    calls: list[list[str]] = []

    class R:
        returncode = 0

    def runner(cmd, **kw):
        calls.append(list(cmd))
        return R()

    items = [
        un.Item("tool", "env", "uv tool uninstall zordon", False, command=["uv", "tool", "uninstall", "zordon"]),
        un.Item("home", "data", str(home), False, path=home),
        un.Item("system:tmux", "tmux", "apt-get remove -y tmux", True, command=["sh", "-c", "apt-get remove -y tmux"], manifest_ref=("system", "tmux")),
    ]
    logs: list[str] = []
    problems = un.execute(items, run=runner, log=logs.append)
    assert problems == []
    assert not home.exists()
    assert calls[-1] == ["uv", "tool", "uninstall", "zordon"]  # the environment that runs us goes last
    assert calls[0] == ["sh", "-c", "apt-get remove -y tmux"]
    # the manifest lived in home and is gone with it; a fresh load is empty
    assert manifest.load() == []


def test_execute_reports_failures_and_continues(tmp_path: Path):
    class R:
        returncode = 1

    items = [
        un.Item("system:tmux", "tmux", "", True, command=["sh", "-c", "false"]),
        un.Item("ollama-model:m", "Ollama model m", "", True, command=["ollama", "rm", "m"]),
    ]
    problems = un.execute(items, run=lambda cmd, **kw: R(), log=lambda s: None)
    assert len(problems) == 2 and all("exited with 1" in p for p in problems)
