"""The full-screen setup and uninstall UIs, driven headlessly with Textual's pilot.

Nothing here touches the network or runs a real install: detection is faked,
``prereqs.detect`` sees a fake PATH, and every command goes through a recording runner.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, Input, Static

from zordon import manifest, paths, prereqs
from zordon import setup as wiz
from zordon import uninstall as un
from zordon.cli import main
from zordon.tui.setup import (
    EXIT_CANCELLED,
    AccessScreen,
    AgentScreen,
    DoneScreen,
    DownloadScreen,
    NormalizerScreen,
    PrereqRow,
    PrereqScreen,
    RouterScreen,
    SetupApp,
    SpeechScreen,
    WelcomeScreen,
    parse_options,
)
from zordon.tui.uninstall import UninstallApp
from zordon.tui.widgets import Chooser, LogPanel

SIZE = (110, 48)  # roomy, so every button is on screen for pilot.click
REAL_PREREQ_DETECT = prereqs.detect


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


class Recorder:
    """A ``subprocess.run``-shaped fake that records argv and returns ``returncode``."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return type("R", (), {"returncode": self.returncode})()


@pytest.fixture
def quiet_machine(monkeypatch):
    """Everything installed, nothing to download, no Ollama binary on PATH."""
    monkeypatch.setattr(wiz, "detect", lambda url: detected(models_present=["a", "b", "c", "d"]))
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    which = lambda n: f"/usr/bin/{n}"  # noqa: E731

    class R:
        returncode = 0
        stdout = "v20.1.0"

    monkeypatch.setattr(prereqs, "detect", lambda **kw: REAL_PREREQ_DETECT(which=which, run=lambda *a, **k: R(), **kw))


def fake_prereqs(monkeypatch, missing: set[str]):
    which = lambda n: None if n in missing else f"/usr/bin/{n}"  # noqa: E731

    class R:
        returncode = 0
        stdout = "v20.1.0"

    monkeypatch.setattr(prereqs, "detect", lambda **kw: REAL_PREREQ_DETECT(which=which, run=lambda *a, **k: R(), **kw))


# ---- option text ---------------------------------------------------------------------------


def test_parse_options_reads_the_plain_wizard_text():
    heading, opts = parse_options(wiz.SPEECH_TEXT)
    assert heading.startswith("Speech: how do you want")
    assert [t for t, _ in opts] == ["Local (recommended)", "Cloud", "Decide later"]
    assert "820 MB" in opts[0][1] and "Needs API keys" in opts[1][1]
    heading, opts = parse_options(wiz.ROUTER_TEXT)
    assert heading.startswith("Routing: who decides") and 'controlling Zordon ("mute")?' in heading
    assert len(opts) == 3 and opts[1][0] == "TypeSafe Jev"
    assert len(parse_options(wiz.NORMALIZER_TEXT)[1]) == 4 and len(parse_options(wiz.ACCESS_TEXT)[1]) == 4


# ---- setup flow ------------------------------------------------------------------------------


async def test_setup_enter_all_the_way_takes_the_recommended_defaults(quiet_machine):
    app = SetupApp(runner=Recorder())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WelcomeScreen)
        for screen in (AgentScreen, SpeechScreen, NormalizerScreen, RouterScreen, AccessScreen):
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, screen)
            # every option shows its trade-off text, not just a label
            cards = app.screen.query_one(Chooser).choices
            assert all(c.desc for c in cards)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PrereqScreen)
        assert not list(app.screen.query(PrereqRow))
        assert not paths.config_path().exists()  # nothing written before the Downloads step
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DownloadScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert paths.config_path().exists()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DoneScreen)
        card = str(app.screen.query_one(".summary-card", Static).render())
        assert app.cfg.server.token in card and "zordon start" in card and "Start Zordon" in card
        await pilot.click("#exit")
    expected = wiz.recommend(detected(models_present=["a", "b", "c", "d"]))
    assert app.choices == expected
    assert app.return_value == "done" and app.return_code == 0 and app.problems == []
    assert app.cfg.providers.normalizer == "claude-cli"


async def test_setup_cloud_keys_tunnel_and_back(quiet_machine, monkeypatch):
    fetched: list[str] = []
    from zordon import doctor

    monkeypatch.setattr(doctor, "download_cloudflared", lambda downloader=None: fetched.append("cloudflared") or "/x/cloudflared")
    app = SetupApp(runner=Recorder())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#next")  # welcome -> agent (mouse)
        await pilot.pause()
        await pilot.press("2")  # codex: not installed, still selectable
        await pilot.pause()
        assert app.screen.query_one(Chooser).value == "codex"
        await pilot.press("1", "enter")
        await pilot.pause()
        assert isinstance(app.screen, SpeechScreen)
        await pilot.press("2")  # cloud: key inputs appear
        await pilot.pause()
        await pilot.click("#key-openai")
        await pilot.press(*"sk-openai-test")
        await pilot.press("enter")  # Enter in the input commits the screen
        await pilot.pause()
        assert isinstance(app.screen, NormalizerScreen)
        assert app.choices.speech == "cloud" and app.choices.keys["openai"] == "sk-openai-test"
        assert app.choices.download_models is False
        await pilot.press("escape")  # back keeps the answer
        await pilot.pause()
        assert isinstance(app.screen, SpeechScreen) and app.screen.query_one(Chooser).value == "cloud"
        assert app.screen.query_one("#key-openai", Input).value == "sk-openai-test"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("2")  # anthropic + key
        await pilot.pause()
        await pilot.click("#key-anthropic")
        await pilot.press(*"sk-ant-test", "enter")
        await pilot.pause()
        assert isinstance(app.screen, RouterScreen)
        await pilot.press("3")  # anthropic router reuses the key: no input shown
        await pilot.pause()
        assert not app.screen.query("#key-anthropic")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AccessScreen)
        await pilot.press("2", "enter")  # tunnel
        await pilot.pause()
        assert isinstance(app.screen, PrereqScreen)
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DoneScreen)
        await pilot.click("#serve")
    c = app.choices
    assert (c.agent, c.speech, c.normalizer, c.router, c.access) == ("claude-code", "cloud", "anthropic", "anthropic", "tunnel")
    assert c.keys["anthropic"] == "sk-ant-test" and c.download_cloudflared is True and fetched == ["cloudflared"]
    assert app.return_value == "serve" and app.serve_argv() == ["--tunnel"]
    assert app.cfg.providers.stt == "openai" and app.cfg.providers.keys["anthropic"] == "sk-ant-test"
    assert "bypass" not in str(app.cfg.to_dict()).lower()


async def test_setup_ollama_with_gpu_offers_the_14b_toggle(quiet_machine, monkeypatch):
    monkeypatch.setattr(wiz, "detect", lambda url: detected(gpu="RTX 4090", ollama_server=True, ollama_models=["qwen2.5:3b-instruct"]))
    app = SetupApp(runner=Recorder())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("enter", "enter", "enter")  # welcome, agent, speech
        await pilot.pause()
        assert isinstance(app.screen, NormalizerScreen) and app.screen.query_one(Chooser).value == "ollama"
        toggle = app.screen.query_one("#use-14b", Checkbox)
        assert toggle.value is False
        await pilot.click("#use-14b")
        await pilot.pause()
        assert toggle.value is True
        await pilot.click("#next")
        await pilot.pause()
        assert isinstance(app.screen, RouterScreen)
        await pilot.press("q")  # quit asks first
        await pilot.pause()
        assert app.screen.__class__.__name__ == "Confirm"
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, RouterScreen)
        await pilot.press("q", "y")
        await pilot.pause()
    assert app.choices.ollama_model == "qwen2.5:14b-instruct" and app.choices.pull_ollama_model is True
    assert app.return_value == "cancelled" and app.return_code == EXIT_CANCELLED
    assert not paths.config_path().exists()


async def test_prerequisites_install_all_runs_the_batch_in_order_and_offers_login(quiet_machine, monkeypatch):
    fake_prereqs(monkeypatch, {"tmux", "claude"})
    runner = Recorder()
    monkeypatch.setattr(prereqs.shutil, "which", lambda n: f"/usr/bin/{n}")  # for open_for_login
    app = SetupApp(runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.press("enter")
            await pilot.pause()
        assert isinstance(app.screen, PrereqScreen)
        rows = {r.prereq.key: r for r in app.screen.query(PrereqRow)}
        assert set(rows) == {"tmux", "claude-code"}
        plan = str(app.screen.query_one("#prereq-plan", Static).render())
        assert "brew install tmux" in plan and "npm install -g @anthropic-ai/claude-code" in plan
        assert app.screen.query_one("#next", Button).has_class("hidden")
        await pilot.click("#install-all")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # one package-manager command first, npm globals after: dependency order, no per-item clicks
        assert runner.calls == [["sh", "-c", "brew install tmux"], ["sh", "-c", "npm install -g @anthropic-ai/claude-code"]]
        assert rows["tmux"].state == "done" and rows["claude-code"].state == "done"
        assert manifest.has("system", "tmux") and manifest.has("system", "claude-code")
        nxt = app.screen.query_one("#next", Button)
        assert not nxt.has_class("hidden") and str(nxt.label) == "Continue"
        assert app.screen.query_one("#install-all", Button).disabled
        log_text = "".join(str(line) for line in app.screen.query_one(LogPanel).rich_log.lines)
        assert "brew install tmux" in log_text and "npm install -g" in log_text  # every step in the same panel
        await pilot.press("escape")  # back to Reach
        await pilot.pause()
        assert isinstance(app.screen, AccessScreen)


async def test_prerequisites_batch_skips_steps_whose_dependency_failed(quiet_machine, monkeypatch):
    fake_prereqs(monkeypatch, {"tmux", "npm", "claude"})  # node missing: npm global must not run
    runner = Recorder(returncode=1)
    app = SetupApp(runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.press("enter")
            await pilot.pause()
        await pilot.click("#install-all")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(runner.calls) == 1 and runner.calls[0][2].startswith("brew install ")  # only the pm batch ran
        rows = {r.prereq.key: r for r in app.screen.query(PrereqRow)}
        assert rows["claude-code"].state == "failed" and "needs node" in str(rows["claude-code"].query_one(".prereq--status", Static).render())
        assert str(app.screen.query_one("#next", Button).label) == "Continue anyway"


async def test_downloads_screen_reports_problems_and_streams_output(quiet_machine, monkeypatch):
    monkeypatch.setattr(wiz, "detect", lambda url: detected(ollama_binary="/usr/bin/ollama", ollama_server=True, ollama_models=[], models_present=["a", "b", "c", "d"]))
    monkeypatch.setattr(wiz.shutil, "which", lambda n: "/usr/bin/ollama" if n == "ollama" else None)
    runner = Recorder(returncode=1)
    app = SetupApp(runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(7):
            await pilot.press("enter")
            await pilot.pause()
        assert isinstance(app.screen, DownloadScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert runner.calls == [["/usr/bin/ollama", "pull", "qwen2.5:3b-instruct"]]
        assert app.problems == ["`ollama pull qwen2.5:3b-instruct` failed; run it by hand"]
        log = "".join(str(line) for line in app.screen.query_one(LogPanel).rich_log.lines)
        assert "Pulling qwen2.5:3b-instruct" in log and "config written" in log
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DoneScreen)
        await pilot.click("#exit")
    assert app.return_code == 3


async def test_no_download_skips_prerequisites_and_downloads(quiet_machine):
    app = SetupApp(do_actions=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.press("enter")
            await pilot.pause()
        assert isinstance(app.screen, DoneScreen) and paths.config_path().exists()


# ---- uninstall -------------------------------------------------------------------------------


def plan_for(home: Path) -> un.Plan:
    plan = un.Plan()
    plan.inside.append(un.Item("home", "Zordon's data directory", str(home), False, path=home))
    plan.inside.append(un.Item("tool", "the isolated zordon environment", "uv tool uninstall zordon", False, command=["uv", "tool", "uninstall", "zordon"]))
    plan.outside.append(un.Item("system:tmux", "tmux", "apt-get remove -y tmux", True, command=["sh", "-c", "apt-get remove -y tmux"], recommended=False, manifest_ref=("system", "tmux")))
    plan.outside.append(
        un.Item("ollama-model:qwen2.5:3b-instruct", "Ollama model qwen2.5:3b-instruct", "ollama rm qwen2.5:3b-instruct", True, command=["ollama", "rm", "qwen2.5:3b-instruct"], recommended=True, manifest_ref=("ollama-model", "qwen2.5:3b-instruct"))
    )
    plan.notes.append("a note")
    return plan


async def test_uninstall_runs_inside_items_and_only_checked_outside_items():
    home = paths.zordon_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("x")
    runner = Recorder()
    app = UninstallApp(plan_for(home), runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        boxes = {cb.id: cb for cb in app.screen.query(Checkbox)}
        assert boxes["item-system_tmux"].value is False and boxes["item-ollama-model_qwen2_5_3b-instruct"].value is True
        await pilot.click("#uninstall")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "Confirm"
        await pilot.click("#confirm-yes")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert str(app.screen.query_one("#cancel", Button).label) == "Close"
        await pilot.press("escape")
    assert runner.calls == [["ollama", "rm", "qwen2.5:3b-instruct"], ["uv", "tool", "uninstall", "zordon"]]
    assert not home.exists() and app.problems == [] and app.return_code == 0


async def test_uninstall_cancel_and_decline_touch_nothing():
    home = paths.zordon_home()
    home.mkdir(parents=True, exist_ok=True)
    runner = Recorder()
    app = UninstallApp(plan_for(home), runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#item-system_tmux")  # tick, then change your mind at the confirmation
        await pilot.pause()
        await pilot.click("#uninstall")
        await pilot.pause()
        await pilot.click("#confirm-no")
        await pilot.pause()
        await pilot.press("escape")
    assert runner.calls == [] and home.exists() and app.return_value == "cancelled"


async def test_uninstall_reports_failures_with_exit_3():
    home = paths.zordon_home()
    home.mkdir(parents=True, exist_ok=True)
    app = UninstallApp(plan_for(home), runner=Recorder(returncode=1))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#uninstall")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("q")
    assert len(app.problems) == 2 and app.return_code == 3


# ---- cli wiring ------------------------------------------------------------------------------


class TtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


class TtyStdout(io.StringIO):
    def isatty(self) -> bool:
        return True


def tty(monkeypatch) -> TtyStdout:
    """Pretend both ends are terminals. Called inside the test body: pytest's capture
    re-installs its own streams between the setup and call phases."""
    monkeypatch.setattr(sys, "stdin", TtyStdin(""))
    out = TtyStdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(wiz, "detect", lambda url: detected())
    return out


def test_cli_setup_uses_the_tui_on_a_terminal(monkeypatch):
    out = tty(monkeypatch)
    import zordon.tui.setup as tui

    seen: dict = {}

    def fake(config_path, *, do_actions, serve=None):
        seen["args"] = (config_path, do_actions)
        return 0

    monkeypatch.setattr(tui, "run_setup_tui", fake)
    assert main(["setup", "--no-download"]) == 0
    assert seen["args"] == (None, False) and "Config written" not in out.getvalue()


def test_cli_setup_falls_back_to_plain_when_the_tui_cannot_import(monkeypatch):
    out = tty(monkeypatch)
    monkeypatch.setitem(sys.modules, "zordon.tui.setup", None)  # `import` raises ImportError
    assert main(["setup", "--no-download"]) == 0
    assert "Config written" in out.getvalue() and paths.config_path().exists()


def test_cli_setup_falls_back_to_plain_when_the_tui_cannot_start(monkeypatch):
    out = tty(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    assert main(["setup", "--no-download"]) == 0
    assert "Config written" in out.getvalue() and paths.config_path().exists()


def test_cli_setup_plain_flag_skips_the_tui(monkeypatch):
    out = tty(monkeypatch)
    import zordon.tui.setup as tui

    monkeypatch.setattr(tui, "run_setup_tui", lambda *a, **k: pytest.fail("TUI must not run with --plain"))
    assert main(["setup", "--plain", "--no-download"]) == 0
    assert "Config written" in out.getvalue()


def test_cli_serve_first_run_honors_the_tunnel_choice_and_cancel(monkeypatch):
    out = tty(monkeypatch)
    from zordon import cli

    served: list = []
    monkeypatch.setattr(cli, "check_dependencies", lambda **kw: None)
    monkeypatch.setattr(cli, "serve", lambda cfg, *, tunnel_provider, warm_up=True, **kw: served.append(tunnel_provider) or 0)

    def tui_serve_now(config_path, *, do_actions, serve=None):
        from zordon.config import Config

        cfg, _ = Config.load_or_create(config_path)
        cfg.save()
        return serve(["--tunnel"])

    monkeypatch.setattr(cli, "run_setup_tui_or_none", tui_serve_now)
    assert main(["serve"]) == 0 and served == ["cloudflared"]

    paths.config_path().unlink()
    monkeypatch.setattr(cli, "run_setup_tui_or_none", lambda *a, **k: EXIT_CANCELLED)
    assert main(["serve"]) == 0 and served == ["cloudflared", None]
    assert "Setup skipped; writing defaults." in out.getvalue() and paths.config_path().exists()

    paths.config_path().unlink()
    monkeypatch.setattr(cli, "run_setup_tui_or_none", lambda *a, **k: 0)  # the user pressed Exit
    assert main(["serve"]) == 0 and served == ["cloudflared", None]
    assert "Start with `zordon serve` when ready" in out.getvalue()
