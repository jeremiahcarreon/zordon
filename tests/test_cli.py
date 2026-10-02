"""``zordon`` command line: argparse surface, token show/rotate against an isolated
ZORDON_HOME, the bind/token refusal, ``--version`` and a ``serve`` run with a fake
agent on an ephemeral port."""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from zordon import __version__, cli, paths
from zordon.bus import Bus
from zordon.config import Config

from . import fixtures_store as fs


def write_config(token: str | None = None, **server: Any) -> Config:
    cfg = Config.default()
    if token is not None:
        cfg.server.token = token
    for k, v in server.items():
        setattr(cfg.server, k, v)
    cfg.save(paths.config_path())
    return cfg


# ---- surface ---------------------------------------------------------------------------------


def test_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"zordon {__version__}"


def test_module_entry_point_prints_version():
    out = subprocess.run(
        [sys.executable, "-m", "zordon", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == f"zordon {__version__}"


def test_help_lists_every_command():
    text = cli.build_parser().format_help()
    for cmd in ("serve", "doctor", "token", "sessions"):
        assert cmd in text


def test_no_command_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]):
    assert cli.main([]) == 0
    assert "usage: zordon" in capsys.readouterr().out


def test_parse_serve_options():
    p = cli.build_parser()
    a = p.parse_args(["serve", "--bind", "0.0.0.0", "--port", "9000", "--tunnel", "ngrok", "--config", "/tmp/x.toml"])
    assert a.command == "serve" and a.bind == "0.0.0.0" and a.port == 9000 and a.tunnel == "ngrok"
    assert a.config == Path("/tmp/x.toml")
    assert p.parse_args(["serve", "--tunnel"]).tunnel == "config"
    assert p.parse_args(["serve"]).tunnel is None
    assert p.parse_args(["serve", "--bind", "tailscale"]).bind == "tailscale"
    with pytest.raises(SystemExit):
        p.parse_args(["serve", "--tunnel", "teleport"])


def test_parse_doctor_and_token_options():
    p = cli.build_parser()
    d = p.parse_args(["doctor", "--download", "--json", "--probe", "--tunnel"])
    assert d.command == "doctor" and d.download and d.json and d.probe and d.tunnel
    assert p.parse_args(["doctor"]).probe is False  # paid probes are opt-in
    assert p.parse_args(["token", "show"]).token_command == "show"
    assert p.parse_args(["token", "rotate"]).token_command == "rotate"
    assert p.parse_args(["sessions"]).command == "sessions"


# ---- token -----------------------------------------------------------------------------------


def test_token_show_prints_the_token_from_the_isolated_home(capsys: pytest.CaptureFixture[str]):
    cfg = write_config(token="tok-" + secrets.token_hex(8))
    assert str(paths.config_path()).startswith(str(paths.zordon_home()))
    assert cli.main(["token", "show"]) == 0
    assert capsys.readouterr().out.strip() == cfg.server.token


def test_token_show_creates_a_config_on_first_run(capsys: pytest.CaptureFixture[str]):
    assert not paths.config_path().exists()
    assert cli.main(["token", "show"]) == 0
    out = capsys.readouterr().out.strip()
    assert paths.config_path().exists()
    assert Config.load().server.token == out and len(out) >= 24
    assert (paths.config_path().stat().st_mode & 0o777) == 0o600


def test_token_rotate_saves_a_new_token(capsys: pytest.CaptureFixture[str]):
    old = write_config(token="old-token-value-0123456789").server.token
    assert cli.main(["token", "rotate"]) == 0
    new = capsys.readouterr().out.strip()
    assert new != old and Config.load().server.token == new


def test_token_bare_defaults_to_show(capsys: pytest.CaptureFixture[str]):
    cfg = write_config(token="bare-token-0123456789abcdef")
    assert cli.main(["token"]) == 0
    assert capsys.readouterr().out.strip() == cfg.server.token


# ---- serve refusals ----------------------------------------------------------------------------


def test_serve_refuses_non_loopback_without_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    write_config(token="")

    def never(*a: Any, **k: Any) -> int:
        raise AssertionError("serve must not start")

    monkeypatch.setattr(cli, "serve", never)
    assert cli.main(["serve", "--bind", "0.0.0.0"]) == 2
    err = capsys.readouterr().err
    assert "token" in err and "0.0.0.0" in err


def test_serve_refuses_a_bad_config_file(capsys: pytest.CaptureFixture[str]):
    paths.ensure_private_dir(paths.zordon_home())
    paths.config_path().write_text('[server]\nport = "not a number"\n')
    assert cli.main(["serve"]) == 2
    assert "config" in capsys.readouterr().err.lower()


def test_serve_without_tmux_exits_3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    write_config()
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: pytest.fail("must not serve"))
    assert cli.main(["serve"]) == 3
    assert "tmux" in capsys.readouterr().err


def test_serve_tailscale_without_cli_exits_3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    write_config()
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: pytest.fail("must not serve"))
    assert cli.main(["serve", "--bind", "tailscale"]) == 3
    assert "tailscale" in capsys.readouterr().err


def test_resolve_tailscale_ip_uses_the_first_v4_address():
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:] == ["ip", "-4"]
        return subprocess.CompletedProcess(argv, 0, "100.101.102.103\n", "")

    assert cli.resolve_tailscale_ip(run=run, which=lambda n: "/usr/bin/tailscale") == "100.101.102.103"
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "not logged in")  # noqa: E731
    with pytest.raises(cli.CliError) as e:
        cli.resolve_tailscale_ip(run=bad, which=lambda n: "/usr/bin/tailscale")
    assert e.value.code == 3


def test_apply_overrides_sets_bind_and_port():
    cfg = Config.default()
    cli.apply_overrides(cfg, bind="0.0.0.0", port=9999)
    assert cfg.server.bind == "0.0.0.0" and cfg.server.port == 9999
    cfg.server.token = ""
    with pytest.raises(cli.CliError) as e:
        cli.apply_overrides(cfg, bind="192.168.1.5", port=None)
    assert e.value.code == 2


def test_resolve_tunnel_provider():
    cfg = Config.default()
    assert cli.resolve_tunnel(cfg, None) is None
    assert cli.resolve_tunnel(cfg, "config") == "cloudflared"
    cfg.tunnel.provider = "ngrok"
    assert cli.resolve_tunnel(cfg, "config") == "ngrok"
    assert cli.resolve_tunnel(cfg, "cloudflared") == "cloudflared"


def test_serve_tunnel_without_binary_downloads_it_first(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """DOC-03: `zordon serve --tunnel` fetches cloudflared on first use instead of refusing."""
    write_config()
    from zordon import assets, doctor

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    downloads: list[str] = []

    def fake_download(downloader=None):
        downloads.append("cloudflared")
        return "/home/u/.zordon/bin/cloudflared"

    served: list[str | None] = []
    monkeypatch.setattr(doctor, "download_cloudflared", fake_download)
    monkeypatch.setattr(cli, "serve", lambda cfg, *, tunnel_provider, warm_up: served.append(tunnel_provider) or 0)
    assert cli.main(["serve", "--tunnel"]) == 0
    assert downloads == ["cloudflared"] and served == ["cloudflared"]
    assert "first use" in capsys.readouterr().err


def test_serve_tunnel_download_failure_exits_3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    write_config()
    from zordon import assets, doctor

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(assets, "find_binary", lambda name: None)

    def fail(downloader=None):
        raise OSError("network down")

    monkeypatch.setattr(doctor, "download_cloudflared", fail)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: pytest.fail("must not serve"))
    assert cli.main(["serve", "--tunnel"]) == 3
    err = capsys.readouterr().err
    assert "cloudflared" in err and "network down" in err
    # ngrok is never downloaded
    assert cli.main(["serve", "--tunnel", "ngrok"]) == 3
    assert "install ngrok" in capsys.readouterr().err


# ---- sessions ------------------------------------------------------------------------------------


def test_sessions_prints_the_picker_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    home = fs.make_claude_home(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    sid = fs.sid(7)
    cwd = "/home/u/Code/alpha"
    t0 = time.time() - 300
    fs.write_session(
        home,
        cwd,
        sid,
        [
            fs.user_prompt(sid, cwd, t0, "Add retry logic to the upload handler"),
            fs.assistant_record(sid, cwd, t0 + 2, [fs.text_block("Done.")], stop_reason="end_turn"),
            fs.custom_title(sid, "Upload retry"),
        ],
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)  # no tmux: discovery alone
    assert cli.main(["sessions"]) == 0
    out = capsys.readouterr().out
    assert sid[:8] in out and "alpha" in out and "Upload retry" in out and "SESSION" in out


def test_sessions_with_empty_store(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["sessions"]) == 0
    assert "No Claude Code sessions" in capsys.readouterr().out


# ---- serve end to end with a fake agent -----------------------------------------------------------


class _FakeAgent:
    """Just enough ``AgentAPI`` for ``create_app`` plus the lifecycle ``serve`` drives."""

    instances: list[_FakeAgent] = []

    def __init__(self, config: Config) -> None:
        from .fake_agent import FakeSessions

        self.config = config
        self.bus = Bus()
        self.sessions = FakeSessions()
        self.version = __version__
        self.hook_secret = secrets.token_urlsafe(32)
        self.tunnel_url: str | None = None
        self.warnings = ["tts: pretend kokoro is missing"]
        self.started = False
        self.stopped = False
        self.warm = None
        _FakeAgent.instances.append(self)

    def start(self, *, warm_up: bool = False) -> None:
        self.started = True
        self.warm = warm_up

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def settings(self) -> dict[str, Any]:
        return {"verbosity": "minimal", "tool_chatter": False, "muted": False, "providers": {}}

    def set_tunnel_url(self, url: str | None) -> None:
        self.tunnel_url = url


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_starts_serves_and_stops(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import httpx

    import zordon.app

    port = _free_port()
    write_config(token="serve-test-token-0123456789", port=port)
    monkeypatch.setattr(zordon.app, "Agent", _FakeAgent)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    seen: dict[str, Any] = {}

    def fake_wait(signals: Any = ()) -> None:
        # The server is up while we "wait for a signal": prove it answers.
        seen["health"] = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5).json()
        seen["auth"] = httpx.post(f"http://127.0.0.1:{port}/auth", json={"token": "wrong"}, timeout=5).status_code

    monkeypatch.setattr(cli, "wait_for_signal", fake_wait)
    assert cli.main(["serve", "--no-warm-up"]) == 0
    agent = _FakeAgent.instances[-1]
    assert agent.started and agent.stopped and agent.warm is False
    assert seen["health"]["ok"] is True and seen["auth"] == 401
    out, err = capsys.readouterr()
    assert f"listening on http://127.0.0.1:{port}" in out
    assert "warning: tts: pretend kokoro is missing" in err
    assert "serve-test-token" not in out + err  # existing config: the token is not reprinted


def test_serve_first_run_prints_the_token_once(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import zordon.app

    port = _free_port()
    monkeypatch.setattr(zordon.app, "Agent", _FakeAgent)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(cli, "wait_for_signal", lambda signals=(): None)
    assert cli.main(["serve", "--port", str(port), "--no-warm-up"]) == 0
    out = capsys.readouterr().out
    token = Config.load().server.token
    assert token in out and "Wrote" in out
    assert Config.load().server.port == 8765  # the --port override is not persisted


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_serve_port_in_use_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import zordon.app

    monkeypatch.setattr(zordon.app, "Agent", _FakeAgent)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(cli, "wait_for_signal", lambda signals=(): pytest.fail("must not wait"))
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        write_config(token="port-test-token-0123456789", port=port)
        assert cli.main(["serve", "--no-warm-up"]) == 2
    assert _FakeAgent.instances[-1].stopped
    assert "could not listen" in capsys.readouterr().err


def test_format_sessions_handles_missing_fields():
    class Info:
        session_id = "abcdef12-0000-0000-0000-000000000000"
        running = True
        tmux_target = "zordon:@1.%1"
        last_active_ts = 0.0
        directory = "/x/y"
        display_title = "t"

    text = cli.format_sessions([Info()])
    assert "abcdef12" in text and "running (zordon:@1.%1)" in text and "/x/y" in text


def test_doctor_json_through_the_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from zordon import doctor

    monkeypatch.setattr(doctor, "run_checks", lambda cfg, opts: doctor.Report("0.0", "x", [doctor.Check("python", "OK", "3.12")]))
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["checks"][0]["name"] == "python"


def test_package_version_has_one_source():
    """PKG-7: pyproject reads the version from zordon/__init__.py (hatch dynamic version)."""
    import importlib.metadata
    import tomllib

    import zordon

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert "version" in pyproject["project"].get("dynamic", [])
    assert pyproject["tool"]["hatch"]["version"]["path"] == "zordon/__init__.py"
    try:
        installed = importlib.metadata.version("zordon")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("zordon is not installed in this interpreter")
    assert installed == zordon.__version__


def test_core_dependency_bounds():
    """PKG-1 / PKG-8 / DOC-16: uvicorn must support ws='websockets-sansio' (0.35+); the
    core package is not capped at <3.14 (only kokoro-onnx is); typesafe-sdk is pinned <0.8."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    uvicorn = next(d for d in project["dependencies"] if d.startswith("uvicorn"))
    assert ">=0.35" in uvicorn
    assert project["requires-python"] == ">=3.12"
    core = project["dependencies"]
    assert any(d.startswith("kokoro-onnx") and "python_version < '3.14'" in d for d in core)
    assert any(d.startswith("faster-whisper") for d in core)
    assert project["optional-dependencies"]["local"] == []  # kept as an alias only
    assert project["optional-dependencies"]["jev"] == ["typesafe-sdk>=0.7.2,<0.8"]
