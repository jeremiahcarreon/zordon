"""The guided setup: detection -> recommendation -> interview -> config -> actions."""

from __future__ import annotations

import io

import pytest

from zordon import paths
from zordon import setup as wiz
from zordon.cli import first_run_needs_setup, main
from zordon.config import Config


def scripted(*answers: str):
    it = iter(answers)

    def ask(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration as e:
            raise EOFError from e

    return ask


def detected(**kw) -> wiz.Detected:
    d = wiz.Detected(tmux="/usr/bin/tmux", claude="/usr/bin/claude", curl="/usr/bin/curl")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_recommend_prefers_key_then_ollama_then_claude_cli_then_passthrough():
    assert wiz.recommend(detected(anthropic_key_env=True)).normalizer == "anthropic"
    c = wiz.recommend(detected(ollama_server=True, ollama_models=["llama3:8b"]))
    assert c.normalizer == "ollama" and c.pull_ollama_model is True
    c = wiz.recommend(detected(ollama_server=True, ollama_models=["qwen2.5:3b-instruct"]))
    assert c.normalizer == "ollama" and c.pull_ollama_model is False
    assert wiz.recommend(detected()).normalizer == "claude-cli"
    assert wiz.recommend(detected(claude=None)).normalizer == "passthrough"
    assert wiz.recommend(detected(typesafe_key_env=True)).router == "jev"
    assert wiz.recommend(detected()).router == "keyword"


def test_interview_defaults_on_enter_and_eof():
    out = io.StringIO()
    d = detected(ollama_server=True, ollama_models=["qwen2.5:3b-instruct"], gpu="RTX")
    c = wiz.interview(d, scripted("", "", "", "", ""), out)  # Enter for every question, GPU question included
    assert (c.speech, c.normalizer, c.router, c.access) == ("local", "ollama", "keyword", "local")
    text = out.getvalue()
    for must in ("Local (recommended)", "Anthropic API key", "Your Claude login", "Built-in rules", "Phone anywhere"):
        assert must in text
    assert "$0.001 per sentence" in text and "820 MB" in text


def test_interview_cloud_and_keys_and_tunnel():
    out = io.StringIO()
    d = detected()
    # speech=cloud, openai key, no elevenlabs, no groq, normalizer=anthropic + key, router=anthropic (key reused), access=tunnel
    c = wiz.interview(d, scripted("2", "sk-openai-test", "n", "n", "2", "sk-ant-test", "3", "2"), out)
    assert c.speech == "cloud" and c.keys["openai"] == "sk-openai-test"
    assert c.normalizer == "anthropic" and c.keys["anthropic"] == "sk-ant-test"
    assert c.router == "anthropic" and c.access == "tunnel" and c.download_cloudflared is True
    cfg = wiz.apply(c, Config.default())
    assert cfg.providers.stt == "openai" and cfg.providers.tts == "openai"
    assert cfg.providers.keys["anthropic"] == "sk-ant-test" and cfg.server.bind == "127.0.0.1"


def test_interview_ollama_without_install_offers_installer_and_14b():
    out = io.StringIO()
    d = detected(gpu="RTX 4090")
    c = wiz.interview(d, scripted("1", "1", "n", "y", "1", "1"), out)
    assert c.normalizer == "ollama" and c.install_ollama is False and c.pull_ollama_model is True
    assert c.ollama_model == "qwen2.5:14b-instruct"
    assert "Install it later from https://ollama.com" in out.getvalue()


def test_apply_never_writes_bypass_and_validates():
    c = wiz.Choices(access="lan")
    cfg = wiz.apply(c, Config.default())
    assert cfg.server.bind == "0.0.0.0" and cfg.server.token
    assert "bypass" not in str(cfg.to_dict()).lower()


def test_actions_pull_and_installer_need_consent(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class R:
        returncode = 0

    def runner(cmd, **kw):
        calls.append(list(cmd))
        return R()

    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    c = wiz.Choices(normalizer="ollama", pull_ollama_model=True, download_models=False, speech="later")
    problems = wiz.run_actions(c, Config.default(), io.StringIO(), runner=runner)
    assert problems == [] and calls == [["/usr/bin/ollama", "pull", "qwen2.5:3b-instruct"]]

    calls.clear()
    c.install_ollama = True
    wiz.run_actions(c, Config.default(), io.StringIO(), runner=runner)
    assert calls[0][0] == "sh" and "ollama.com/install.sh" in calls[0][2]

    calls.clear()
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    c.install_ollama = False
    problems = wiz.run_actions(c, Config.default(), io.StringIO(), runner=runner)
    assert calls == [] and any("ollama pull" in p for p in problems)


def test_run_assume_yes_writes_config_without_asking(monkeypatch):
    monkeypatch.setattr(wiz, "detect", lambda url: detected(claude="/usr/bin/claude"))
    out = io.StringIO()
    cfg, c, problems = wiz.run(assume_yes=True, out=out, do_actions=False)
    assert paths.config_path().exists() and cfg.providers.normalizer == "claude-cli"
    assert "Your session token is" in out.getvalue() and problems == []
    again = Config.load()
    assert again.providers.normalizer == "claude-cli" and again.server.token == cfg.server.token


def test_cli_setup_and_first_run_rule(monkeypatch, capsys):
    monkeypatch.setattr(wiz, "detect", lambda url: detected())
    assert main(["setup", "--yes", "--no-download"]) == 0
    assert "Config written" in capsys.readouterr().out
    assert paths.config_path().exists()
    # With a config present, serve never runs the wizard; with --no-setup neither.
    assert first_run_needs_setup(None, no_setup=False) is False
    paths.config_path().unlink()
    assert first_run_needs_setup(None, no_setup=True) is False
    monkeypatch.setattr("sys.stdin", io.StringIO())  # not a tty
    assert first_run_needs_setup(None, no_setup=False) is False


@pytest.mark.parametrize("answer,expected", [("y", True), ("", True), ("n", False), ("no", False)])
def test_yes_parsing(answer, expected):
    assert wiz._yes(scripted(answer), "ok?", default=True) is expected
