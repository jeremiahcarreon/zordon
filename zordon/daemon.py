"""Run ``zordon serve`` detached from the terminal, and manage it.

``zordon start`` launches ``zordon serve`` in its own session with stdout and
stderr going to ``~/.zordon/serve.log`` and the pid in ``~/.zordon/serve.pid``;
``stop`` sends SIGTERM and waits, ``status`` reports, ``logs`` tails,
``restart`` does both. This works everywhere (containers included). For
starting at login and restarting on failure, see ``zordon/service.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from zordon import paths


def pid_path() -> Path:
    return paths.zordon_home() / "serve.pid"


def log_path() -> Path:
    return paths.zordon_home() / "serve.log"


@dataclass(slots=True)
class Status:
    running: bool
    pid: int | None
    pidfile: Path
    log: Path
    stale: bool = False  # pidfile exists but no such process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"zordon" in fh.read()
    except OSError:
        return True  # not Linux: trust kill(0)


def status() -> Status:
    p = pid_path()
    if not p.exists():
        return Status(False, None, p, log_path())
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return Status(False, None, p, log_path(), stale=True)
    if _alive(pid):
        return Status(True, pid, p, log_path())
    return Status(False, pid, p, log_path(), stale=True)


def zordon_argv() -> list[str]:
    """How to invoke zordon again from a detached process: the console script when it is the
    running program, else the interpreter with -m."""
    exe = Path(sys.argv[0]).name if sys.argv else ""
    if exe == "zordon" and os.access(sys.argv[0], os.X_OK):
        return [sys.argv[0]]
    return [sys.executable, "-m", "zordon"]


def start(extra_args: Sequence[str] = (), *, wait_s: float = 8.0, popen=subprocess.Popen) -> Status:
    """Launch ``zordon serve`` detached. Raises RuntimeError when it is already running or
    dies before it is listening."""
    st = status()
    if st.running:
        raise RuntimeError(f"zordon is already running (pid {st.pid}); `zordon stop` first or `zordon restart`")
    paths.ensure_private_dir(paths.zordon_home())
    log = open(log_path(), "ab")  # noqa: SIM115 - handed to the child
    argv = [*zordon_argv(), "serve", "--no-setup", *extra_args]
    env = dict(os.environ)
    env["ZORDON_DETACHED"] = "1"
    proc = popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env, close_fds=True)
    log.close()  # the child holds its own descriptor
    pid_path().write_text(f"{proc.pid}\n")
    os.chmod(pid_path(), 0o600)
    # Give it a moment: a config error exits immediately and should be reported here.
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            pid_path().unlink(missing_ok=True)
            tail = _tail(log_path(), 15)
            raise RuntimeError(f"zordon serve exited with {code} right after starting. Last log lines:\n{tail}")
        if _listening():
            break
        time.sleep(0.2)
    return status()


def _listening() -> bool:
    """True once the log says the server is up (cheap, no network)."""
    try:
        return "listening on" in _tail(log_path(), 40)
    except OSError:
        return False


def stop(*, timeout_s: float = 10.0) -> Status:
    st = status()
    if not st.running or st.pid is None:
        if st.stale:
            pid_path().unlink(missing_ok=True)
        return status()
    try:
        os.killpg(os.getpgid(st.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(st.pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and _alive(st.pid):
        time.sleep(0.1)
    if _alive(st.pid):
        try:
            os.kill(st.pid, signal.SIGKILL)
        except OSError:
            pass
    pid_path().unlink(missing_ok=True)
    return status()


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def tail(n: int = 40) -> str:
    return _tail(log_path(), n)


def write_pidfile_for_current_process() -> None:
    """Called by serve when it runs under systemd/launchd, so status/stop still work."""
    try:
        paths.ensure_private_dir(paths.zordon_home())
        pid_path().write_text(f"{os.getpid()}\n")
        os.chmod(pid_path(), 0o600)
    except OSError:
        pass


def clear_pidfile_if_mine() -> None:
    try:
        if int(pid_path().read_text().strip()) == os.getpid():
            pid_path().unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
