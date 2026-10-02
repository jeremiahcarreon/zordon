"""Uninstall: everything inside Zordon's environment goes; everything outside is offered.

Inside (removed without asking once the user confirms the uninstall):
  * ``~/.zordon`` (config, token, transcripts, models, cloudflared, hooks files)
  * the ``zordon`` tmux session Zordon created (its windows run coding agents; they are
    detached, not killed, unless the user asks)
  * the isolated tool environment (``uv tool uninstall zordon`` or ``pipx uninstall zordon``)

Outside (each one asked, default no): system packages Zordon installed on request
(tmux, node, the agent, Ollama), Ollama models it pulled, uv when install.sh
installed it. Nothing Zordon did not install is ever offered.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from zordon import manifest, paths

Runner = Callable[..., object]

REMOVAL_BY_PM: dict[str, str] = {
    "brew": "brew uninstall {pkgs}",
    "apt-get": "sudo apt-get remove -y {pkgs}",
    "dnf": "sudo dnf remove -y {pkgs}",
    "yum": "sudo yum remove -y {pkgs}",
    "pacman": "sudo pacman -Rs --noconfirm {pkgs}",
    "zypper": "sudo zypper remove -y {pkgs}",
    "apk": "sudo apk del {pkgs}",
}
NPM_GLOBALS = {"claude-code": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
NODE_PACKAGE = {"brew": "node", "apt-get": "nodejs npm", "dnf": "nodejs npm", "yum": "nodejs npm", "pacman": "nodejs npm", "zypper": "nodejs npm", "apk": "nodejs npm"}


@dataclass(slots=True)
class Item:
    key: str
    label: str
    detail: str
    outside: bool
    command: list[str] | None = None  # argv to run; None = handled in Python (paths)
    path: Path | None = None
    recommended: bool = True
    manifest_ref: tuple[str, str] | None = None


@dataclass(slots=True)
class Plan:
    inside: list[Item] = field(default_factory=list)
    outside: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _tool_manager(which: Callable[[str], str | None] = shutil.which) -> tuple[str, list[str]] | None:
    """How zordon itself was installed: uv tool, pipx, or unknown."""
    prefix = Path(sys.prefix)
    parts = {p.lower() for p in prefix.parts}
    if "uv" in parts and "tools" in parts and which("uv"):
        return "uv", [which("uv") or "uv", "tool", "uninstall", "zordon"]
    if "pipx" in parts and which("pipx"):
        return "pipx", [which("pipx") or "pipx", "uninstall", "zordon"]
    if os.environ.get("ZORDON_TOOL_MANAGER") == "uv" and which("uv"):
        return "uv", [which("uv") or "uv", "tool", "uninstall", "zordon"]
    return None


def _pm_template(which: Callable[[str], str | None], root: bool) -> tuple[str | None, str | None]:
    from zordon.prereqs import PACKAGE_MANAGERS  # noqa: PLC0415

    for name, _ in PACKAGE_MANAGERS:
        if which(name):
            tpl = REMOVAL_BY_PM[name]
            if tpl.startswith("sudo ") and (root or not which("sudo")):
                tpl = tpl[len("sudo ") :]
            return name, tpl
    return None, None


def build_plan(*, which: Callable[[str], str | None] = shutil.which, root: bool | None = None) -> Plan:
    if root is None:
        try:
            root = os.geteuid() == 0
        except AttributeError:
            root = False
    plan = Plan()
    home = paths.zordon_home()
    plan.inside.append(Item("home", "Zordon's data directory", f"{home} (config, token, transcripts, models, cloudflared)", False, path=home))
    plan.inside.append(Item("tmux-session", "the `zordon` tmux session", "windows are detached, agents keep running", False))
    tm = _tool_manager(which)
    if tm:
        plan.inside.append(Item("tool", "the isolated zordon environment", " ".join(tm[1]), False, command=tm[1]))
    else:
        plan.notes.append("zordon was not installed with uv or pipx; remove the package the way you installed it")

    pm, tpl = _pm_template(which, root)
    for e in manifest.load():
        if not e.outside:
            continue
        if e.kind == "system":
            if e.name in NPM_GLOBALS:
                npm = which("npm")
                cmd = [npm, "uninstall", "-g", NPM_GLOBALS[e.name]] if npm else None
                detail = " ".join(cmd) if cmd else "npm is gone; nothing to run"
            elif e.name == "ollama":
                cmd = ["sh", "-c", e.removal] if e.removal else None
                detail = e.removal or "remove with your package manager or the steps at https://ollama.com"
            elif e.name == "node":
                cmd = ["sh", "-c", tpl.format(pkgs=NODE_PACKAGE.get(pm or "", "nodejs npm"))] if tpl else None
                detail = cmd[2] if cmd else "no package manager found"
            else:
                cmd = ["sh", "-c", tpl.format(pkgs=e.name)] if tpl else None
                detail = cmd[2] if cmd else "no package manager found"
            plan.outside.append(Item(f"system:{e.name}", e.name, detail, True, command=cmd, recommended=False, manifest_ref=(e.kind, e.name)))
        elif e.kind == "ollama-model":
            ol = which("ollama")
            cmd = [ol, "rm", e.name] if ol else None
            plan.outside.append(Item(f"ollama-model:{e.name}", f"Ollama model {e.name}", " ".join(cmd) if cmd else "ollama is gone", True, command=cmd, recommended=True, manifest_ref=(e.kind, e.name)))
        elif e.kind == "uv":
            uv = which("uv")
            uv_home = Path(e.removal) if e.removal else Path.home() / ".local" / "share" / "uv"
            plan.outside.append(
                Item("uv", "uv (installed by install.sh)", f"{uv or '~/.local/bin/uv'} and {uv_home}", True, path=Path(uv) if uv else None, recommended=False, manifest_ref=(e.kind, e.name), command=None)
            )
    return plan


def tmux_session_exists(name: str = "zordon") -> bool:
    try:
        return subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def execute(items: list[Item], *, run: Runner = subprocess.run, log: Callable[[str], None] = print) -> list[str]:
    """Remove the given items. The tool environment goes last (it is running us). Returns problems."""
    problems: list[str] = []
    # Outside items first (their manifest updates need the data directory), then the tmux
    # session, then the data directory, and last the environment that is running us.
    rank = {"tmux-session": 1, "home": 2, "tool": 3}
    ordered = sorted(items, key=lambda i: rank.get(i.key, 0))
    for it in ordered:
        try:
            if it.key == "home" and it.path is not None:
                if it.path.exists():
                    shutil.rmtree(it.path)
                log(f"removed {it.path}")
            elif it.key == "tmux-session":
                if shutil.which("tmux") and tmux_session_exists():
                    run(["tmux", "kill-session", "-t", "=zordon"], check=False, capture_output=True)
                    log("closed the zordon tmux session")
            elif it.key == "uv":
                for p in (it.path, Path.home() / ".local" / "bin" / "uvx", Path.home() / ".local" / "share" / "uv"):
                    if p and p.exists():
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                log("removed uv")
            elif it.command:
                res = run(it.command, check=False)
                code = getattr(res, "returncode", 0)
                if code not in (0, None):
                    problems.append(f"{it.label}: `{' '.join(it.command)}` exited with {code}")
                    continue
                log(f"removed {it.label}")
            if it.manifest_ref and it.key != "home":
                try:
                    manifest.forget(*it.manifest_ref)
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            problems.append(f"{it.label}: {e}")
    return problems
