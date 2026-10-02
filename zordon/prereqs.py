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
    pkg: str = ""  # package-manager package name(s); batched into one install command


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


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:  # not POSIX
        return False


def detect_package_manager(which: Which = shutil.which, *, root: bool | None = None) -> tuple[str | None, str | None]:
    """First package manager found, with its install template. ``sudo `` is dropped when
    running as root (containers, some servers) or when sudo itself is not installed."""
    root = is_root() if root is None else root
    for name, template in PACKAGE_MANAGERS:
        if which(name):
            if template.startswith("sudo ") and (root or not which("sudo")):
                template = template[len("sudo ") :]
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
    root: bool | None = None,
) -> Environment:
    pm, template = detect_package_manager(which, root=root)
    env = Environment(system=platform.system(), package_manager=pm, install_template=template, node_major=node_major(which, run))

    def pkg(pkgs: str) -> str | None:
        return template.format(pkgs=pkgs) if template else None

    env.checks.append(
        Prereq("tmux", "tmux", "Zordon drives the coding agent inside a tmux pane", "tmux", True, pkg("tmux"), present=which("tmux"), pkg="tmux")
    )
    env.checks.append(
        Prereq(
            "curl", "curl", "Claude Code's hook handlers tell Zordon about prompts through curl", "curl", want_ollama, pkg("curl"), present=which("curl"), pkg="curl"
        )
    )
    env.checks.append(
        Prereq(
            "zstd",
            "zstd",
            "Ollama's installer unpacks its download with zstd",
            "zstd",
            want_ollama and env.system != "Darwin",
            pkg("zstd"),
            present=which("zstd"),
            pkg="zstd",
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
            pkg=NODE_PACKAGE.get(pm or "", "nodejs npm"),
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
            needs=("curl", "zstd") if env.system != "Darwin" else (),
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


# ---- batched installation -----------------------------------------------------------------

UPDATE_FIRST = {"apt-get": "{sudo}apt-get update"}  # fresh machines have empty package lists


@dataclass(slots=True)
class Step:
    label: str
    command: str
    keys: list[str]  # prerequisite keys this step satisfies
    kind: str  # pm | npm | script
    terminal: bool = False  # needs the real tty (sudo password, installer prompts)


def plan_steps(env: Environment, missing: list[Prereq] | None = None) -> list[Step]:
    """Everything missing, as the fewest commands in dependency order: one package-manager
    command for all packages (sudo asks once), then npm globals, then standalone installers."""
    missing = env.missing(required_only=True) if missing is None else missing
    steps: list[Step] = []
    pm_items = [p for p in missing if p.pkg and p.command and env.install_template]
    if pm_items:
        tpl = env.install_template or ""
        sudo = "sudo " if tpl.startswith("sudo ") else ""
        pkgs = " ".join(dict.fromkeys(" ".join(p.pkg for p in pm_items).split()))
        cmd = tpl.format(pkgs=pkgs)
        pre = UPDATE_FIRST.get(env.package_manager or "")
        if pre:
            cmd = pre.format(sudo=sudo) + " && " + cmd
        steps.append(Step(f"Install {', '.join(p.label for p in pm_items)}", cmd, [p.key for p in pm_items], "pm", terminal=bool(sudo)))
    for p in missing:
        if p.command and not p.pkg and p.command.startswith("npm "):
            steps.append(Step(f"Install {p.label}", p.command, [p.key], "npm", terminal=False))
    for p in missing:
        if p.command and not p.pkg and not p.command.startswith("npm "):
            steps.append(Step(f"Install {p.label}", p.command, [p.key], "script", terminal=True))
    return steps


def run_steps(
    steps: list[Step],
    env: Environment,
    *,
    run: Runner = subprocess.run,
    log: Callable[[str], None] | None = None,
    on_step: Callable[[Step], None] | None = None,
) -> dict[str, tuple[bool, str]]:
    """Run the steps in order. A failed step marks its keys failed and skips later steps
    that depend on them. Returns key -> (ok, message)."""
    results: dict[str, tuple[bool, str]] = {}
    failed: set[str] = set()
    for step in steps:
        blocked = sorted({n for k in step.keys for n in (env.get(k).needs if env.get(k) else ())} & failed)
        if blocked:
            for k in step.keys:
                results[k] = (False, f"skipped: needs {', '.join(blocked)} which failed")
                failed.add(k)
            continue
        if on_step:
            on_step(step)
        if log:
            log(f"$ {step.command}")
        try:
            res = run(["sh", "-c", step.command], check=False)
            code = getattr(res, "returncode", 1)
        except (OSError, subprocess.SubprocessError) as e:
            code, err = 1, str(e)
        else:
            err = f"exited with {code}"
        for k in step.keys:
            p = env.get(k)
            if code == 0:
                if p is not None:
                    p.present = p.binary
                results[k] = (True, f"{p.label if p else k} installed")
            else:
                failed.add(k)
                results[k] = (False, f"`{step.command}` {err}")
    return results
