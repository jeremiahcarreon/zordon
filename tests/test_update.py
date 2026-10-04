"""Update checks against a mocked GitHub raw endpoint; apply() through a fake tool manager."""

from __future__ import annotations

import httpx

from zordon import __version__
from zordon import update as upd
from zordon.cli import main


def client_with(version: str | None, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if version is None:
            return httpx.Response(status)
        return httpx.Response(200, text=f'"""doc"""\n\n__version__ = "{version}"\n')

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_version_comparison():
    assert upd.is_newer("0.2.0", "0.1.0") and upd.is_newer("1.0.0", "0.9.9") and upd.is_newer("0.1.1", "0.1.0")
    assert not upd.is_newer("0.1.0", "0.1.0") and not upd.is_newer("0.0.9", "0.1.0")
    assert upd.version_tuple("1.2.3-rc1") == (1, 2, 3)


def test_check_reports_newer_and_caches():
    st = upd.check("main", force=True, client=client_with("99.0.0"), now=1000.0)
    assert st.available and st.latest == "99.0.0" and st.current == __version__ and st.error == ""
    assert st.command == "zordon update"
    # Within the interval the cache answers; a client that would fail is never consulted.
    st2 = upd.check("main", client=client_with(None, 500), now=1000.0 + 60)
    assert st2.available and st2.latest == "99.0.0"
    # After the interval the network is asked again.
    st3 = upd.check("main", client=client_with(__version__), now=1000.0 + upd.CHECK_INTERVAL_S + 1)
    assert not st3.available and st3.latest == __version__


def test_check_offline_never_raises():
    st = upd.check("main", force=True, client=client_with(None, 503), now=5.0)
    assert not st.available and "HTTPStatusError" in st.error


def test_disabled_by_env_or_config(monkeypatch):
    assert not upd.disabled(True)
    assert upd.disabled(False)
    monkeypatch.setenv("ZORDON_NO_UPDATE_CHECK", "1")
    assert upd.disabled(True)


def test_apply_uses_the_installing_tool(monkeypatch):
    calls = []

    class R:
        returncode = 0

    monkeypatch.setenv("ZORDON_TOOL_MANAGER", "uv")
    monkeypatch.setattr(upd, "installed_extras", lambda: [])
    ok, msg = upd.apply("main", run=lambda argv, **k: (calls.append(argv), R())[1], which=lambda n: "/usr/bin/uv" if n == "uv" else None, log=lambda s: None)
    assert ok and "restart" in msg
    assert calls[0][:5] == ["/usr/bin/uv", "tool", "install", "--force", "--reinstall"]
    assert calls[0][-1] == "zordon @ https://github.com/jeremiahcarreon/zordon/archive/refs/heads/main.tar.gz"
    # The extras the environment carries ride along: a reinstall without [gpu] dropped the
    # CUDA libraries and put recognition back on the CPU.
    monkeypatch.setattr(upd, "installed_extras", lambda: ["gpu"])
    ok, _ = upd.apply("main", run=lambda argv, **k: (calls.append(argv), R())[1], which=lambda n: "/usr/bin/uv" if n == "uv" else None, log=lambda s: None)
    assert ok and calls[-1][-1] == "zordon[gpu] @ https://github.com/jeremiahcarreon/zordon/archive/refs/heads/main.tar.gz"
    monkeypatch.delenv("ZORDON_TOOL_MANAGER")
    ok, msg = upd.apply("main", run=lambda *a, **k: R(), which=lambda n: None, log=lambda s: None)
    assert not ok and "not installed with uv or pipx" in msg


def test_cli_update_check_only(monkeypatch, capsys):
    monkeypatch.setattr(upd, "fetch_latest_version", lambda channel, timeout=3.0, client=None: "99.0.0")
    assert main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "99.0.0 is available" in out
    monkeypatch.setattr(upd, "fetch_latest_version", lambda channel, timeout=3.0, client=None: __version__)
    assert main(["update", "--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_serve_rechecks_while_running(monkeypatch):
    """The check repeats on the interval for as long as serve runs, installs when
    configured and announces each new version once (banner + one spoken notice)."""
    import threading
    import time

    from zordon.cli import start_update_check
    from zordon.config import Config

    answers = iter([__version__, "99.0.0", "99.0.0", "99.1.0"])
    seen: list[str] = []

    def fake_check(channel):
        latest = next(answers, "99.1.0")
        seen.append(latest)
        return upd.UpdateStatus(__version__, latest, upd.is_newer(latest, __version__), time.time(), channel)

    applied: list[str] = []
    monkeypatch.setattr(upd, "check", fake_check)
    monkeypatch.setattr(upd, "apply", lambda channel, log=print: (applied.append(channel) or True, "ok"))

    class Bus:
        def __init__(self):
            self.stop = threading.Event()
            self.published: list = []

        def publish(self, ev):
            self.published.append(ev)

    class Agent:
        bus = Bus()
        update_status = None

    agent = Agent()
    cfg = Config.default()
    start_update_check(agent, cfg, skip=False, interval_s=0.02)
    deadline = time.time() + 3
    # Wait for the fourth check to have been *processed*, not just answered: the
    # second announcement (banner + notice) is what the assertions below count.
    while len(agent.bus.published) < 4 and time.time() < deadline:
        time.sleep(0.01)
    agent.bus.stop.set()
    assert seen[:4] == [__version__, "99.0.0", "99.0.0", "99.1.0"]
    assert applied == ["main", "main"]  # 99.0.0 once (not again on the repeat), then 99.1.0
    kinds = [type(e).__name__ for e in agent.bus.published]
    assert kinds.count("UpdateOut") == 2 and kinds.count("Notice") == 2
    notice = next(e for e in agent.bus.published if type(e).__name__ == "Notice")
    assert notice.speak and "99.0.0" in notice.text and "Restart" in notice.text
    assert agent.update_status["latest"] == "99.1.0" and agent.update_status["installed"] is True


def test_serve_check_skipped_when_disabled(monkeypatch):
    from zordon.cli import start_update_check
    from zordon.config import Config

    called = []
    monkeypatch.setattr(upd, "check", lambda channel: called.append(channel))
    cfg = Config.default()
    cfg.update.check = False
    start_update_check(object(), cfg, skip=False, interval_s=0.01)
    import time

    time.sleep(0.05)
    assert called == []
