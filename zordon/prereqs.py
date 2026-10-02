"""System prerequisites: detect what is missing and how to install it here.

Zordon assumes nothing about the machine. For each prerequisite we know the
package for every common package manager, whether it needs sudo, and what to
do after installing (log in, pull a model). The setup wizard shows the exact
command and runs it only after an explicit yes; ``zordon doctor`` prints the
same command as the fix. Nothing here runs without being asked.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

Which = Callable[[str], str | None]
Runner = Callable[..., object]

# Package managers in detection order (first found wins).
PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("brew", "brew install {pkgs}"),
    ("apt-get", "sudo apt-get install -y {pkgs}"),
    ("dnf", "sudo dnf install -y {pkgs}"),
    ("yum", "sudo yum install -y {pkgs}"),
    ("pacman", "sudo pacman -S --noconfirm {pkgs}"),
    ("zypper", "sudo zypper install -y {pkgs}"),
    ("apk", "sudo apk add {pkgs}"),
)

NODE_PACKAGE = {"brew": "node", "apt-get": "nodejs npm", "dnf": "nodejs npm", "yum": "nodejs npm", "pacman": "nodejs npm", "zypper": "nodejs npm", "apk": "nodejs npm"}
NODE_MIN_MAJOR = 18
NODE_FALLBACK = "https://nodejs.org/en/download (any version 18 or newer); then run `zordon setup` again"


@dataclass(slots=True)
class Prereq:
    key: str
    label: str
    why: str
    binary: str  # what we look for on PATH
    required: bool  # False = only for the chosen path (e.g. Ollama)
    command: str | None  # how to install it here, None when we cannot say
    needs: tuple[str, ...] = ()  # other prereq keys that must be present first
    after: str = ""  # what to do once installed (login, etc.)
    present: str | None = None  # path when found
    detail: str = ""  # version or note


@dataclass(slots=True)
class Environment:
    system: str
    package_manager: str | None
    install_template: str | None
    node_major: int | None
    checks: list[Prereq] = field(default_factory=list)

    def missing(self, *, required_only: bool = False) -> list[Prereq]:
        return [p for p in self.checks if p.present is None and (p.required or not required_only)]

    def get(self, key: str) -> Prereq | None:
        return next((p for p in self.checks if p.key == key), None)


def detect_package_manager(which: Which = shutil.which) -> tuple[str | None, str | None]:
    for name, template in PACKAGE_MANAGERS:
        if which(name):
            return name, template
    return None, None


def node_major(which: Which = shutil.which, run: Runner = subprocess.run) -> int | None:
    node = which("node")
    if not node:
        return None
    try:
        out = run([node, "--version"], capture_output=True, text=True, timeout=5)
        text = str(getattr(out, "stdout", "")).strip().lstrip("v")
        return int(text.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def detect(
    *,
    which: Which = shutil.which,
    run: Runner = subprocess.run,
    want_agents: tuple[str, ...] = ("claude-code",),
    want_ollama: bool = False,
) -> Environment:
    pm, template = detect_package_manager(which)
    env = Environment(system=platform.system(), package_manager=pm, install_template=template, node_major=node_major(which, run))

    def pkg(pkgs: str) -> str | None:
        return template.format(pkgs=pkgs) if template else None

    env.checks.append(
        Prereq("tmux", "tmux", "Zordon drives the coding agent inside a tmux pane", "tmux", True, pkg("tmux"), present=which("tmux"))
    )
    env.checks.append(
        Prereq(
            "curl", "curl", "Claude Code's hook handlers tell Zordon about prompts through curl", "curl", False, pkg("curl"), present=which("curl")
        )
    )
    node_needed = any(a in want_agents for a in ("claude-code", "codex"))
    node_ok = env.node_major is not None and env.node_major >= NODE_MIN_MAJOR
    node_cmd = pkg(NODE_PACKAGE.get(pm or "", "nodejs npm")) if pm else None
    env.checks.append(
        Prereq(
            "node",
            "Node.js 18+ and npm",
            "Claude Code and Codex are installed with npm",
            "npm",
            node_needed,
            node_cmd,
            present=(which("npm") if node_ok else None),
            detail=(f"node {env.node_major}" if env.node_major else "not found") + ("" if node_ok else f"; need {NODE_MIN_MAJOR}+: {NODE_FALLBACK}"),
        )
    )
    env.checks.append(
        Prereq(
            "claude-code",
            "Claude Code",
            "the coding agent Zordon talks to",
            "claude",
            "claude-code" in want_agents,
            "npm install -g @anthropic-ai/claude-code",
            needs=("node",),
            after="run `claude` once in a terminal to log in, then exit it",
            present=which("claude"),
        )
    )
    env.checks.append(
        Prereq(
            "codex",
            "Codex CLI",
            "the coding agent Zordon talks to",
            "codex",
            "codex" in want_agents,
            "npm install -g @openai/codex",
            needs=("node",),
            after="run `codex` once in a terminal to sign in, then exit it",
            present=which("codex"),
        )
    )
    env.checks.append(
        Prereq(
            "ollama",
            "Ollama",
            "runs the local model that rewrites output into spoken English",
            "ollama",
            want_ollama,
            "curl -fsSL https://ollama.com/install.sh | sh" if env.system != "Darwin" else "brew install ollama",
            after="start it with `ollama serve` (the installer usually does); Zordon pulls the model",
            present=which("ollama"),
        )
    )
    return env


def install(prereq: Prereq, env: Environment, *, run: Runner = subprocess.run) -> tuple[bool, str]:
    """Run the install command with the user's terminal attached (sudo may prompt).
    Returns (ok, message). Never called without the user's yes."""
    if not prereq.command:
        return False, f"no install command known for {prereq.label} on this system"
    for dep in prereq.needs:
        d = env.get(dep)
        if d is not None and d.present is None:
            return False, f"{prereq.label} needs {d.label} first"
    try:
        res = run(["sh", "-c", prereq.command], check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run `{prereq.command}`: {e}"
    code = getattr(res, "returncode", 1)
    if code != 0:
        return False, f"`{prereq.command}` exited with {code}"
    return True, f"{prereq.label} installed"


def login_command(key: str) -> list[str] | None:
    return {"claude-code": ["claude"], "codex": ["codex"]}.get(key)


def open_for_login(key: str, *, run: Runner = subprocess.run) -> bool:
    """Open the agent in the user's terminal so they can log in; returns when they exit it."""
    cmd = login_command(key)
    if not cmd or not shutil.which(cmd[0]):
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    try:
        res = run(cmd, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(res, "returncode", 1) == 0


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
