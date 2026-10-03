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
    ok, msg = upd.apply("main", run=lambda argv, **k: (calls.append(argv), R())[1], which=lambda n: "/usr/bin/uv" if n == "uv" else None, log=lambda s: None)
    assert ok and "restart" in msg
    assert calls[0][:5] == ["/usr/bin/uv", "tool", "install", "--force", "--reinstall"]
    assert calls[0][-1] == "zordon @ https://github.com/jeremiahcarreon/zordon/archive/refs/heads/main.tar.gz"
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
