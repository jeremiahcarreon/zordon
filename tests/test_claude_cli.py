"""The headless Claude Code normalizer, driven by tests/fake_claude_p.py."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from zordon.config import Config
from zordon.output.normalizer import ClaudeCliNormalizer, make_normalizer
from zordon.output.normalizer.claude_cli import build_command, child_env
from zordon.providers import ProviderError, ProviderNotConfigured

FAKE = Path(__file__).resolve().parent / "fake_claude_p.py"


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``claude`` executable on PATH that runs the fake."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    return script


def make(fake_claude: Path, tmp_path: Path, **kw) -> ClaudeCliNormalizer:
    kw.setdefault("cwd", tmp_path / "cwd")
    kw.setdefault("timeout", 5.0)
    return ClaudeCliNormalizer(str(fake_claude), **kw)


def test_missing_binary_is_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ProviderNotConfigured):
        ClaudeCliNormalizer("claude", cwd=tmp_path, warm=False)


def test_declares_turn_granularity_and_rewrites_a_turn(fake_claude: Path, tmp_path: Path):
    n = make(fake_claude, tmp_path, warm=False)
    assert n.granularity == "turn"
    out = n.normalize_turn("Edited auth.py, 8 lines changed. tests pass 42/42.")
    assert out == "SPOKEN: Edited auth.py, 8 lines changed. tests pass 42/42."
    assert n.requests == 1 and n.last_latency_s is not None
    # The per-sentence form routes through the same request.
    assert n.normalize("Done.", []) == "SPOKEN: Done."
    n.close()


def test_each_request_is_a_fresh_process_and_the_warm_spare_is_used(fake_claude: Path, tmp_path: Path, monkeypatch):
    env_file = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_FILE", str(env_file))
    n = make(fake_claude, tmp_path, warm=True)
    deadline = time.time() + 5
    while n._spare is None and time.time() < deadline:
        time.sleep(0.05)
    assert n._spare is not None, "a warm process should be waiting"
    first = n._spare
    n.normalize_turn("hello world")
    assert not first.alive(), "the used process is killed: nothing carries over"
    deadline = time.time() + 5
    while (n._spare is None or n._spare is first) and time.time() < deadline:
        time.sleep(0.05)
    assert n._spare is not None and n._spare is not first, "a new spare is spawned after a request"
    n.close()
    assert n._spare is None


def test_child_environment_is_scrubbed_and_cwd_is_private(fake_claude: Path, tmp_path: Path, monkeypatch):
    env_file = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    n = make(fake_claude, tmp_path, warm=False)
    n.normalize_turn("x")
    dump = json.loads(env_file.read_text())
    env = dump["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert dump["cwd"] == str(tmp_path / "cwd")
    argv = dump["argv"]
    for flag in ("-p", "--input-format", "--output-format", "--no-session-persistence", "--max-turns", "--system-prompt"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert "--bare" not in argv  # --bare would refuse the subscription login
    n.close()


def test_markdown_in_the_answer_is_cleaned(fake_claude: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "markdown")
    n = make(fake_claude, tmp_path, warm=False)
    out = n.normalize_turn("Fix lint.")
    assert "**" not in out and "- extra" not in out and out.startswith("SPOKEN: Fix lint.")


@pytest.mark.parametrize("mode", ["error", "crash"])
def test_failures_raise_provider_error(fake_claude: Path, tmp_path: Path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    n = make(fake_claude, tmp_path, warm=False)
    with pytest.raises(ProviderError):
        n.normalize_turn("x")
    assert n.failures == 1


def test_timeout_kills_the_process(fake_claude: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "5")
    n = make(fake_claude, tmp_path, warm=False, timeout=0.5)
    t0 = time.time()
    with pytest.raises(ProviderError):
        n.normalize_turn("x")
    assert time.time() - t0 < 3


def test_transcript_question_goes_through_the_same_process(fake_claude: Path, tmp_path: Path):
    n = make(fake_claude, tmp_path, warm=False)
    assert n.answer_transcript_query("what changed?", []) == "I don't see that in the transcript."
    assert n.answer_transcript_query("what changed?", ["editing auth.py", "Done."]) == "It changed auth dot py."


def test_factory_auto_prefers_api_key_then_cli_then_passthrough(fake_claude: Path, tmp_path: Path, monkeypatch):
    cfg = Config.default()
    assert cfg.providers.normalizer == "auto"
    n = make_normalizer(cfg)
    assert n.name == "claude-cli"
    getattr(n, "close", lambda: None)()

    cfg.providers.keys["anthropic"] = "sk-ant-test"
    assert make_normalizer(cfg).name == "anthropic"

    cfg.providers.keys["anthropic"] = ""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert make_normalizer(cfg).name == "passthrough"

    cfg.providers.normalizer = "claude-cli"
    assert make_normalizer(cfg).name == "passthrough"  # explicit but unavailable: degrade, never raise


def test_build_command_shape():
    cmd = build_command("/usr/bin/claude", "claude-sonnet-5")
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_X": "y", "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h", "ANTHROPIC_API_KEY": "k"})
    assert env == {"CLAUDE_CONFIG_DIR": "/c", "HOME": "/h", "ANTHROPIC_API_KEY": "k"}
    assert shutil.which("sh")  # sanity for the fixture script
