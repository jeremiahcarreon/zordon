"""Start Zordon at login and keep it running: a systemd user unit on Linux, a launchd
agent on macOS. Nothing here needs root; both live in the user's own directories.

``zordon service install [--tunnel] [--bind ADDR]`` writes the unit, enables and
starts it. ``uninstall`` stops, disables and removes it. ``status`` reports.
Containers and other systems without systemd get a clear message and the
``zordon start`` alternative.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from zordon import paths
from zordon.daemon import zordon_argv

UNIT_NAME = "zordon.service"
LAUNCHD_LABEL = "io.zordon.serve"


@dataclass(slots=True)
class ServiceInfo:
    kind: str  # systemd | launchd | none
    path: Path | None
    installed: bool
    active: bool | None
    note: str = ""


def _systemd_available(run=subprocess.run) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        res = run(["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def kind(run=subprocess.run) -> str:
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux" and _systemd_available(run):
        return "systemd"
    return "none"


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "systemd" / "user" / UNIT_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def serve_command(extra: Sequence[str] = ()) -> list[str]:
    argv = zordon_argv()
    # systemd/launchd need absolute paths; `python -m zordon` already is.
    argv[0] = shutil.which(argv[0]) or str(Path(argv[0]).resolve())
    return [*argv, "serve", "--no-setup", *extra]


def systemd_unit(extra: Sequence[str] = ()) -> str:
    cmd = " ".join(_quote(a) for a in serve_command(extra))
    home = paths.zordon_home()
    return f"""[Unit]
Description=Zordon voice interface for your coding agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={cmd}
Restart=on-failure
RestartSec=3
Environment=ZORDON_HOME={home}
Environment=ZORDON_DETACHED=1
# tmux and the agent live in the user's session; keep its PATH
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

[Install]
WantedBy=default.target
"""


def launchd_plist(extra: Sequence[str] = ()) -> str:
    args = "".join(f"        <string>{_xml(a)}</string>\n" for a in serve_command(extra))
    home = paths.zordon_home()
    log = home / "serve.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>{_xml(str(log))}</string>
    <key>StandardErrorPath</key><string>{_xml(str(log))}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ZORDON_HOME</key><string>{_xml(str(home))}</string>
        <key>ZORDON_DETACHED</key><string>1</string>
        <key>PATH</key><string>{_xml(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))}</string>
    </dict>
</dict>
</plist>
"""


def install(extra: Sequence[str] = (), *, run=subprocess.run) -> ServiceInfo:
    k = kind(run)
    if k == "systemd":
        p = unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(systemd_unit(extra))
        for argv in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT_NAME]):
            res = run(argv, capture_output=True, text=True)
            if res.returncode != 0:
                return ServiceInfo(k, p, True, False, f"`{' '.join(argv)}` failed: {(res.stderr or res.stdout).strip()}")
        note = ""
        if shutil.which("loginctl"):
            note = "To keep it running after you log out: loginctl enable-linger $USER"
        return ServiceInfo(k, p, True, True, note)
    if k == "launchd":
        p = plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(launchd_plist(extra))
        run(["launchctl", "unload", str(p)], capture_output=True, text=True)
        res = run(["launchctl", "load", "-w", str(p)], capture_output=True, text=True)
        if res.returncode != 0:
            return ServiceInfo(k, p, True, False, f"launchctl load failed: {(res.stderr or res.stdout).strip()}")
        return ServiceInfo(k, p, True, True)
    return ServiceInfo(k, None, False, None, "no systemd user session or launchd here (a container?); use `zordon start` to run detached")


def uninstall(*, run=subprocess.run) -> ServiceInfo:
    k = kind(run)
    if k == "systemd":
        p = unit_path()
        run(["systemctl", "--user", "disable", "--now", UNIT_NAME], capture_output=True, text=True)
        existed = p.exists()
        p.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        return ServiceInfo(k, p, False, False, "removed" if existed else "was not installed")
    if k == "launchd":
        p = plist_path()
        run(["launchctl", "unload", str(p)], capture_output=True, text=True)
        existed = p.exists()
        p.unlink(missing_ok=True)
        return ServiceInfo(k, p, False, False, "removed" if existed else "was not installed")
    return ServiceInfo(k, None, False, None, "no service manager here")


def info(*, run=subprocess.run) -> ServiceInfo:
    k = kind(run)
    if k == "systemd":
        p = unit_path()
        res = run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True, text=True)
        return ServiceInfo(k, p, p.exists(), res.returncode == 0, (res.stdout or "").strip())
    if k == "launchd":
        p = plist_path()
        res = run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True)
        return ServiceInfo(k, p, p.exists(), res.returncode == 0)
    return ServiceInfo(k, None, False, None, "no service manager here; `zordon status` shows the detached process")


def _quote(a: str) -> str:
    return a if all(c.isalnum() or c in "-_./=:@" for c in a) else '"' + a.replace('"', '\\"') + '"'


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
