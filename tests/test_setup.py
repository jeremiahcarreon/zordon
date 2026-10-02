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
    d = wiz.Detected(
        tmux="/usr/bin/tmux",
        claude="/usr/bin/claude",
        curl="/usr/bin/curl",
        agents={"claude-code": "/usr/bin/claude", "codex": None, "generic": ""},
    )
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
    c = wiz.interview(d, scripted("", "", "", "", "", ""), out)  # Enter for every question, agent and GPU included
    assert (c.speech, c.normalizer, c.router, c.access) == ("local", "ollama", "keyword", "local")
    text = out.getvalue()
    for must in ("Local (recommended)", "Anthropic API key", "Your Claude login", "Built-in rules", "Phone anywhere"):
        assert must in text
    assert "$0.001 per sentence" in text and "820 MB" in text


def test_interview_cloud_and_keys_and_tunnel():
    out = io.StringIO()
    d = detected()
    # speech=cloud, openai key, no elevenlabs, no groq, normalizer=anthropic + key, router=anthropic (key reused), access=tunnel
    c = wiz.interview(d, scripted("", "2", "sk-openai-test", "n", "n", "2", "sk-ant-test", "3", "2"), out)
    assert c.speech == "cloud" and c.keys["openai"] == "sk-openai-test"
    assert c.normalizer == "anthropic" and c.keys["anthropic"] == "sk-ant-test"
    assert c.router == "anthropic" and c.access == "tunnel" and c.download_cloudflared is True
    cfg = wiz.apply(c, Config.default())
    assert cfg.providers.stt == "openai" and cfg.providers.tts == "openai"
    assert cfg.providers.keys["anthropic"] == "sk-ant-test" and cfg.server.bind == "127.0.0.1"


def test_interview_ollama_without_install_offers_installer_and_14b():
    out = io.StringIO()
    d = detected(gpu="RTX 4090")
    c = wiz.interview(d, scripted("", "1", "1", "y", "1", "1"), out)
    assert c.normalizer == "ollama" and c.pull_ollama_model is True
    assert c.ollama_model == "qwen2.5:14b-instruct"
    assert "prerequisites step will offer to install it" in out.getvalue()


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
    monkeypatch.setattr(wiz, "ensure_ollama_server", lambda url, binary, out: True)
    c = wiz.Choices(normalizer="ollama", pull_ollama_model=True, download_models=False, speech="later")
    problems = wiz.run_actions(c, Config.default(), io.StringIO(), runner=runner)
    assert problems == [] and calls == [["/usr/bin/ollama", "pull", "qwen2.5:3b-instruct"]]

    calls.clear()
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
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


# ---- agent step -----------------------------------------------------------------------


def test_recommend_agent_prefers_claude_then_any_installed_then_generic():
    assert wiz.recommend(detected(agents={"claude-code": "/x/claude", "codex": None, "generic": ""})).agent == "claude-code"
    assert wiz.recommend(detected(agents={"claude-code": None, "codex": "/x/codex", "generic": ""})).agent == "codex"
    assert wiz.recommend(detected(agents={"claude-code": None, "codex": None, "generic": ""})).agent == "generic"


def test_interview_without_any_agent_points_at_the_prerequisites_step():
    out = io.StringIO()
    d = detected(claude=None, agents={"claude-code": None, "codex": None, "generic": ""})
    c = wiz.interview(d, scripted("1", "", "", "", ""), out)
    text = out.getvalue()
    assert "No coding agent found" in text and "prerequisites step" in text
    assert c.agent == "claude-code"


def test_interview_agent_choice_and_install_hint_for_missing_pick():
    out = io.StringIO()
    d = detected(agents={"claude-code": "/x/claude", "codex": None, "generic": ""})
    c = wiz.interview(d, scripted("2", "", "", "", ""), out)
    assert c.agent == "codex" and "prerequisites step will offer to install it" in out.getvalue()
    c = wiz.interview(d, scripted("3", "", "", "", ""), io.StringIO())
    assert c.agent == "generic"


# ---- prerequisites ------------------------------------------------------------------------

from zordon import prereqs  # noqa: E402


def fake_env(missing: set[str], pm: str = "apt-get") -> prereqs.Environment:
    which = lambda name: None if name in missing else f"/usr/bin/{name}"  # noqa: E731

    class R:
        returncode = 0
        stdout = "v20.1.0"

    env = prereqs.detect(which=which, run=lambda *a, **k: R(), want_agents=("claude-code",), want_ollama=True)
    return env


def test_prereqs_detect_commands_per_package_manager():
    env = fake_env({"tmux", "claude", "ollama"})
    assert env.package_manager == "brew"  # brew is first in detection order when every binary "exists"
    tm = env.get("tmux")
    assert tm.present is None and tm.command == "brew install tmux"
    assert env.get("claude-code").command == "npm install -g @anthropic-ai/claude-code" and env.get("claude-code").needs == ("node",)
    assert env.get("ollama").required is True
    which = lambda n: f"/usr/bin/{n}" if n in ("apt-get", "sudo") else None  # noqa: E731
    env2 = prereqs.detect(which=which, run=lambda *a, **k: None, want_agents=("codex",), root=False)
    assert env2.get("tmux").command == "sudo apt-get install -y tmux"
    assert env2.get("node").command == "sudo apt-get install -y nodejs npm" and env2.get("node").present is None
    assert env2.get("codex").required and not env2.get("claude-code").required
    missing = {p.key for p in env2.missing(required_only=True)}
    assert missing == {"tmux", "node", "codex"}


def test_prereqs_install_runs_only_the_shown_command_and_respects_needs():
    env = fake_env({"tmux", "claude", "npm"})
    calls = []

    class R:
        returncode = 0

    ok, msg = prereqs.install(env.get("tmux"), env, run=lambda cmd, **k: (calls.append(cmd), R())[1])
    assert ok and calls == [["sh", "-c", "brew install tmux"]]
    ok, msg = prereqs.install(env.get("claude-code"), env, run=lambda cmd, **k: (calls.append(cmd), R())[1])
    assert not ok and "needs Node.js" in msg and len(calls) == 1  # node missing: never runs npm


def test_wizard_prerequisites_is_one_question_then_an_ordered_batch():
    env = fake_env({"tmux", "claude", "zstd"})
    out = io.StringIO()
    calls = []

    class R:
        returncode = 0

    runner = lambda cmd, **k: (calls.append(cmd), R())[1]  # noqa: E731
    c = wiz.Choices(agent="claude-code", normalizer="ollama")
    still = wiz.prerequisites(c, scripted("y", "n"), out, env=env, runner=runner)  # yes to install all, no to login
    text = out.getvalue()
    assert "Plan:" in text and "1. brew install tmux zstd" in text and "2. npm install -g @anthropic-ai/claude-code" in text
    assert calls == [["sh", "-c", "brew install tmux zstd"], ["sh", "-c", "npm install -g @anthropic-ai/claude-code"]]
    assert still == []
    # declining lists every command under Still to do; --yes never installs
    calls.clear()
    still = wiz.prerequisites(wiz.Choices(agent="claude-code"), scripted("n"), io.StringIO(), env=fake_env({"tmux"}), runner=runner)
    assert calls == [] and still == ["tmux: brew install tmux"]
    still = wiz.prerequisites(wiz.Choices(agent="claude-code"), scripted(), io.StringIO(), env=fake_env({"tmux"}), runner=runner, assume_yes=True)
    assert calls == [] and still == ["tmux: brew install tmux"]


def test_plan_steps_batches_apt_with_update_and_orders_dependencies():
    which = lambda n: f"/usr/bin/{n}" if n in ("apt-get",) else None  # noqa: E731
    env = prereqs.detect(which=which, run=lambda *a, **k: None, want_agents=("claude-code",), want_ollama=True, root=True)
    steps = prereqs.plan_steps(env)
    assert [s.kind for s in steps] == ["pm", "npm", "script"]
    assert steps[0].command == "apt-get update && apt-get install -y tmux curl zstd nodejs npm"
    assert steps[1].command == "npm install -g @anthropic-ai/claude-code"
    assert steps[2].command.startswith("curl -fsSL https://ollama.com/install.sh") and steps[2].terminal
    assert steps[0].terminal is False  # root: no sudo prompt
    env2 = prereqs.detect(which=lambda n: f"/usr/bin/{n}" if n in ("apt-get", "sudo") else None, run=lambda *a, **k: None, want_agents=(), root=False)
    assert prereqs.plan_steps(env2)[0].command.startswith("sudo apt-get update && sudo apt-get install -y tmux") and prereqs.plan_steps(env2)[0].terminal


def test_run_steps_marks_dependents_of_a_failed_step():
    which = lambda n: f"/usr/bin/{n}" if n in ("apt-get",) else None  # noqa: E731
    env = prereqs.detect(which=which, run=lambda *a, **k: None, want_agents=("claude-code",), root=True)
    steps = prereqs.plan_steps(env)

    class R:
        returncode = 1

    calls = []
    results = prereqs.run_steps(steps, env, run=lambda cmd, **k: (calls.append(cmd), R())[1])
    assert len(calls) == 1  # the pm batch failed; the npm step never ran
    assert results["node"][0] is False and results["claude-code"][0] is False and "needs node" in results["claude-code"][1]


def test_wizard_prerequisites_offers_login_after_installing_an_agent(monkeypatch):
    env = fake_env({"claude"})
    calls = []

    class R:
        returncode = 0

    runner = lambda cmd, **k: (calls.append(cmd), R())[1]  # noqa: E731
    monkeypatch.setattr(prereqs.shutil, "which", lambda n: "/usr/bin/claude")
    still = wiz.prerequisites(wiz.Choices(agent="claude-code"), scripted("y", "y"), io.StringIO(), env=env, runner=runner)
    assert still == []
    assert calls[0] == ["sh", "-c", "npm install -g @anthropic-ai/claude-code"] and calls[1] == ["claude"]


def test_prereqs_drop_sudo_for_root_or_without_sudo():
    which_apt = lambda n: f"/usr/bin/{n}" if n in ("apt-get", "sudo") else None  # noqa: E731
    assert prereqs.detect_package_manager(which_apt, root=False) == ("apt-get", "sudo apt-get install -y {pkgs}")
    assert prereqs.detect_package_manager(which_apt, root=True) == ("apt-get", "apt-get install -y {pkgs}")
    no_sudo = lambda n: "/usr/bin/apt-get" if n == "apt-get" else None  # noqa: E731
    assert prereqs.detect_package_manager(no_sudo, root=False) == ("apt-get", "apt-get install -y {pkgs}")
    env = prereqs.detect(which=no_sudo, run=lambda *a, **k: None, want_agents=(), want_ollama=True, root=True)
    assert env.get("tmux").command == "apt-get install -y tmux"
    assert env.get("ollama").needs == ("curl", "zstd")


def test_run_actions_reports_model_checks_by_status(monkeypatch):
    """Doctor checks carry .status; a FAIL lands in the problems list, OK does not."""
    from zordon import doctor

    def fake_model_checks(cfg, opts, downloader=None):
        return [doctor.Check("model a", doctor.OK, "present"), doctor.Check("model b", doctor.FAIL, "download failed: 403", "retry")]

    monkeypatch.setattr(doctor, "model_checks", fake_model_checks)
    c = wiz.Choices(speech="local", download_models=True, normalizer="passthrough")
    problems = wiz.run_actions(c, Config.default(), io.StringIO(), runner=lambda *a, **k: None)
    assert problems == ["model b: download failed: 403"]


def test_run_actions_reports_when_the_ollama_server_cannot_start(monkeypatch):
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    monkeypatch.setattr(wiz, "ensure_ollama_server", lambda url, binary, out: False)
    c = wiz.Choices(normalizer="ollama", pull_ollama_model=True, download_models=False, speech="later")
    problems = wiz.run_actions(c, Config.default(), io.StringIO(), runner=lambda *a, **k: None)
    assert problems and "server is not running" in problems[0]
