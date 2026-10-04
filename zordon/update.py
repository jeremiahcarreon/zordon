"""Updates for installs that track the GitHub repository (no PyPI release yet).

``check()`` fetches the version string published on the configured channel
(``main`` by default) and compares it with the running one. The result is cached
in ``~/.zordon/update-check.json`` so a check happens at most every few hours;
``ZORDON_NO_UPDATE_CHECK=1`` or ``[update] check = false`` turns it off.

``apply()`` reinstalls zordon from the channel's tarball through whatever
installed it (uv tool or pipx). ``zordon serve`` runs the check in the
background, applies the update when ``[update] auto`` is on, and tells the
terminal and every connected client that a restart picks it up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from zordon import __version__, paths

REPO = "https://github.com/jeremiahcarreon/zordon"
RAW = "https://raw.githubusercontent.com/jeremiahcarreon/zordon"
DEFAULT_CHANNEL = "main"
CHECK_INTERVAL_S = 6 * 3600
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


@dataclass(slots=True)
class UpdateStatus:
    current: str
    latest: str | None
    available: bool
    checked_at: float
    channel: str
    error: str = ""
    installed: bool = False  # an update was applied this run; restart to use it

    @property
    def command(self) -> str:
        return "restart zordon serve" if self.installed else "zordon update"


def cache_path() -> Path:
    return paths.zordon_home() / "update-check.json"


def version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[.\-+]", v):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def source_url(channel: str = DEFAULT_CHANNEL) -> str:
    return f"{REPO}/archive/refs/heads/{channel}.tar.gz"


def fetch_latest_version(channel: str = DEFAULT_CHANNEL, *, timeout: float = 3.0, client: httpx.Client | None = None) -> str:
    url = f"{RAW}/{channel}/zordon/__init__.py"
    c = client or httpx.Client(timeout=timeout, follow_redirects=True)
    # raw.githubusercontent.com caches for a few minutes; a per-minute query string keeps a
    # forced check (`zordon update`) from reading a stale copy that still says "current".
    resp = c.get(url, params={"t": int(time.time() // 60)}, headers={"Cache-Control": "no-cache"})
    resp.raise_for_status()
    m = VERSION_RE.search(resp.text)
    if not m:
        raise ValueError("no __version__ in the published source")
    return m.group(1)


def load_cached() -> UpdateStatus | None:
    p = cache_path()
    try:
        data = json.loads(p.read_text())
        return UpdateStatus(**{k: data[k] for k in UpdateStatus.__dataclass_fields__ if k in data})  # type: ignore[arg-type]
    except (OSError, ValueError, TypeError):
        return None


def save(status: UpdateStatus) -> None:
    p = cache_path()
    try:
        paths.ensure_private_dir(p.parent)
        p.write_text(json.dumps(asdict(status)) + "\n")
    except OSError:
        pass


def disabled(config_check: bool = True) -> bool:
    return bool(os.environ.get("ZORDON_NO_UPDATE_CHECK")) or not config_check


def check(
    channel: str = DEFAULT_CHANNEL,
    *,
    force: bool = False,
    timeout: float = 3.0,
    client: httpx.Client | None = None,
    now: float | None = None,
) -> UpdateStatus:
    """Return the update status, from cache when checked recently, else from the network.
    Never raises: a network failure yields ``available=False`` with ``error`` set."""
    now = time.time() if now is None else now
    cached = load_cached()
    if cached and not force and cached.channel == channel and now - cached.checked_at < CHECK_INTERVAL_S and cached.current == __version__:
        return cached
    try:
        latest = fetch_latest_version(channel, timeout=timeout, client=client)
        status = UpdateStatus(__version__, latest, is_newer(latest, __version__), now, channel)
    except Exception as e:  # noqa: BLE001 - offline is normal
        status = UpdateStatus(__version__, cached.latest if cached else None, False, now, channel, error=f"{type(e).__name__}: {e}")
    save(status)
    return status


def tool_manager(which=shutil.which) -> tuple[str, list[str]] | None:
    """How zordon was installed and the argv that reinstalls it from a given source.
    The source placeholder ``{src}`` is filled in by ``apply``."""
    prefix = Path(sys.prefix)
    parts = {p.lower() for p in prefix.parts}
    uv = which("uv")
    pipx = which("pipx")
    if ("uv" in parts and "tools" in parts and uv) or (os.environ.get("ZORDON_TOOL_MANAGER") == "uv" and uv):
        return "uv", [uv, "tool", "install", "--force", "--reinstall", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", "zordon @ {src}"]
    if "pipx" in parts and pipx:
        return "pipx", [pipx, "install", "--force", "{src}"]
    return None


def apply(channel: str = DEFAULT_CHANNEL, *, run=subprocess.run, which=shutil.which, log=print) -> tuple[bool, str]:
    """Reinstall zordon from the channel tarball. Returns (ok, message)."""
    tm = tool_manager(which)
    if tm is None:
        return False, "zordon was not installed with uv or pipx; update it the way you installed it (pip install --upgrade from the repository)"
    name, argv = tm
    argv = [a.replace("{src}", source_url(channel)) for a in argv]
    log(f"$ {' '.join(argv)}")
    try:
        res = run(argv, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{name}: {e}"
    code = getattr(res, "returncode", 1)
    if code != 0:
        return False, f"{name} exited with {code}"
    return True, f"updated through {name}; restart zordon to use the new version"
