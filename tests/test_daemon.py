"""Detached mode and the service files, without launching a real zordon."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from zordon import daemon, paths, service
from zordon.cli import main


def test_status_without_pidfile_and_with_stale_pidfile():
    st = daemon.status()
    assert not st.running and st.pid is None
    paths.ensure_private_dir(paths.zordon_home())
    daemon.pid_path().write_text("999999999\n")
    st = daemon.status()
    assert not st.running and st.stale


def test_start_detaches_writes_pidfile_and_detects_listening(monkeypatch):
    """A fake child that prints the listening line stands in for zordon serve."""
    recorded = {}

    class Proc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        recorded["argv"] = argv
        recorded["kw"] = kw
        kw["stdout"].write(b"Zordon 0.2.1 listening on http://127.0.0.1:8765\n")
        kw["stdout"].flush()
        return Proc()

    monkeypatch.setattr(daemon, "_alive", lambda pid: pid == 4242)
    st = daemon.start(["--port", "8765"], popen=fake_popen, wait_s=2.0)
    assert st.running and st.pid == 4242
    assert recorded["argv"][-4:] == ["serve", "--no-setup", "--port", "8765"]
    assert recorded["kw"]["start_new_session"] is True and recorded["kw"]["env"]["ZORDON_DETACHED"] == "1"
    assert (daemon.pid_path().stat().st_mode & 0o777) == 0o600
    with pytest.raises(RuntimeError, match="already running"):
        daemon.start(popen=fake_popen)


def test_start_reports_an_immediate_exit_with_the_log_tail(monkeypatch):
    class Proc:
        pid = 777

        def poll(self):
            return 2

    def fake_popen(argv, **kw):
        kw["stdout"].write(b"zordon: config error: server.bind=0.0.0.0 requires a token\n")
        return Proc()

    with pytest.raises(RuntimeError, match="exited with 2") as e:
        daemon.start(popen=fake_popen)
    assert "requires a token" in str(e.value)
    assert not daemon.pid_path().exists()


def test_stop_terminates_a_real_child():
    paths.ensure_private_dir(paths.zordon_home())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    daemon.pid_path().write_text(f"{child.pid}\n")
    # _alive checks /proc cmdline for "zordon"; use the raw kill path by patching the check
    import zordon.daemon as d

    orig = d._alive
    d._alive = lambda pid: child.poll() is None
    try:
        st = daemon.stop(timeout_s=5.0)
    finally:
        d._alive = orig
    assert not st.running and child.poll() is not None and not daemon.pid_path().exists()


def test_pidfile_helpers_for_service_runs():
    daemon.write_pidfile_for_current_process()
    assert int(daemon.pid_path().read_text()) == os.getpid()
    daemon.clear_pidfile_if_mine()
    assert not daemon.pid_path().exists()


def test_systemd_unit_and_launchd_plist_contents(monkeypatch):
    monkeypatch.setattr(service, "zordon_argv", lambda: [sys.executable, "-m", "zordon"])
    unit = service.systemd_unit(["--tunnel"])
    assert f"ExecStart={sys.executable} -m zordon serve --no-setup --tunnel" in unit
    assert "Restart=on-failure" in unit and f"ZORDON_HOME={paths.zordon_home()}" in unit and "WantedBy=default.target" in unit
    plist = service.launchd_plist([])
    assert "<string>io.zordon.serve</string>" in plist and "<string>serve</string>" in plist and "KeepAlive" in plist
    assert str(paths.zordon_home() / "serve.log") in plist


def test_service_install_without_a_manager_points_at_start(monkeypatch):
    monkeypatch.setattr(service, "kind", lambda run=None: "none")
    info = service.install()
    assert not info.installed and "zordon start" in info.note
    assert main(["service", "install"]) == 3


def test_service_install_systemd_writes_unit_and_enables(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(service, "kind", lambda run=None: "systemd")
    monkeypatch.setattr(service, "zordon_argv", lambda: [sys.executable, "-m", "zordon"])
    calls = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    info = service.install(["--bind", "tailscale"], run=lambda argv, **k: (calls.append(argv), R())[1])
    assert info.installed and info.active and info.path == tmp_path / "cfg" / "systemd" / "user" / "zordon.service"
    assert "--bind tailscale" in info.path.read_text()
    assert ["systemctl", "--user", "daemon-reload"] in calls and ["systemctl", "--user", "enable", "--now", "zordon.service"] in calls
    info2 = service.uninstall(run=lambda argv, **k: (calls.append(argv), R())[1])
    assert not info2.installed and not info.path.exists()


def test_cli_stop_when_nothing_runs(capsys):
    assert main(["stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_status_shows_the_tunnel_url_written_by_serve(monkeypatch, capsys):
    from zordon import cli, daemon

    cli._write_tunnel_url("https://quiet-ocean-1234.trycloudflare.com")
    assert cli.tunnel_url_path().read_text().strip().endswith("trycloudflare.com")
    monkeypatch.setattr(daemon, "status", lambda: daemon.Status(True, 4242, daemon.pid_path(), daemon.log_path()))
    monkeypatch.setattr(cli, "_fetch_health", lambda cfg: "responding")
    assert main(["status", "--qr"]) == 0
    out = capsys.readouterr().out
    assert "tunnel: https://quiet-ocean-1234.trycloudflare.com" in out and "zordon token show" in out
    assert "█" in out or "▄" in out  # the QR
    cli._write_tunnel_url(None)
    assert not cli.tunnel_url_path().exists()
