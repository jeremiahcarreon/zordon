"""Projects: what a non-technical user works on, instead of sessions and panes.

A project is a directory under the user's home plus how its agent is launched
there: which agent, which permission mode (including bypass, when the user chose
that for the project), whether file edits are scoped to the directory, the
agent's last session id (so the conversation resumes), and the tmux pane it last
ran in (so a still-running pane is reconnected instead of relaunched).

Projects live in ``~/.zordon/projects.json``; nothing else about them is kept
anywhere. The web client offers two actions: start a new project (walkthrough:
folder, agent, how much to ask) and continue a previous one. Everything about
tmux stays behind those two doors.

``browse`` and ``create_directory`` are the folder picker's backend. They never
leave the user's home directory, never follow a symlink out of it, and never
delete anything.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from zordon import paths

BYPASS_MODE = "bypassPermissions"  # the one spelling; see discovery.BYPASS_MODE and decision 0018
LAUNCH_MODES_WITH_BYPASS: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "dontAsk", BYPASS_MODE)
RUNNERS: tuple[str, ...] = ("terminal", "headless")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
HIDDEN_PREFIX = "."


class ProjectError(ValueError):
    """A project operation the user can fix (bad name, outside home, exists)."""


@dataclass(slots=True)
class Project:
    id: str
    name: str
    directory: str
    agent: str = "claude-code"
    permission_mode: str = "default"
    scope_edits: bool = True
    talk_first: bool = True  # ask questions and state a plan before changing anything (decision 0019)
    runner: str = "terminal"  # terminal (a tmux pane you can look at) | headless (claude -p, decision 0020)
    session_id: str | None = None  # the agent's last session in this project (resumes it)
    headless_pid: int | None = None  # the claude -p process of a headless project, while it runs
    model: str | None = None  # --model for new launches (alias or id); None = Claude Code's default
    effort: str | None = None  # --effort for new launches; None = Claude Code's default
    tmux_target: str | None = None  # where it last ran; reconnected when still alive
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def bypass(self) -> bool:
        return self.permission_mode == BYPASS_MODE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        if not clean.get("id") or not clean.get("directory"):
            raise ValueError("project record without id or directory")
        clean.setdefault("name", os.path.basename(str(clean["directory"]).rstrip("/")))
        return cls(**clean)


def projects_path() -> Path:
    return paths.zordon_home() / "projects.json"


class ProjectStore:
    """The projects file. Every write is atomic and 0600; reads tolerate a missing file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or projects_path()
        self._items: dict[str, Project] = {}
        self._loaded = False

    # ---- persistence ---------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # A damaged file is kept aside, not overwritten: the user may want it.
            try:
                self.path.rename(self.path.with_suffix(".json.broken"))
            except OSError:
                pass
            return
        items = raw.get("projects", []) if isinstance(raw, dict) else raw
        for item in items if isinstance(items, list) else []:
            try:
                p = Project.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._items[p.id] = p

    def save(self) -> None:
        paths.ensure_private_dir(self.path.parent)
        data = {"version": 1, "projects": [p.to_dict() for p in self.list()]}
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # ---- queries --------------------------------------------------------------------

    def list(self) -> list[Project]:
        self._load()
        return sorted(self._items.values(), key=lambda p: p.last_used_at, reverse=True)

    def get(self, project_id: str) -> Project | None:
        self._load()
        return self._items.get(project_id)

    def by_directory(self, directory: str) -> Project | None:
        self._load()
        want = _norm(directory)
        for p in self._items.values():
            if _norm(p.directory) == want:
                return p
        return None

    def by_session(self, session_id: str) -> Project | None:
        self._load()
        for p in self._items.values():
            if p.session_id == session_id:
                return p
        return None

    def by_name(self, name: str) -> Project | None:
        """Case-insensitive name match, for "open project api" by voice."""
        self._load()
        want = _fold(name)
        if not want:
            return None
        exact = [p for p in self._items.values() if _fold(p.name) == want]
        if exact:
            return exact[0]
        partial = [p for p in self._items.values() if want in _fold(p.name) or _fold(p.name) in want]
        return partial[0] if len(partial) == 1 else None

    # ---- mutation -------------------------------------------------------------------

    def add(self, project: Project) -> Project:
        self._load()
        self._items[project.id] = project
        self.save()
        return project

    def update(self, project: Project, **changes: Any) -> Project:
        self._load()
        for k, v in changes.items():
            setattr(project, k, v)
        self._items[project.id] = project
        self.save()
        return project

    def touch(self, project: Project) -> None:
        self.update(project, last_used_at=time.time())

    def remove(self, project_id: str) -> bool:
        self._load()
        if project_id not in self._items:
            return False
        del self._items[project_id]
        self.save()
        return True


def new_project(
    directory: str,
    *,
    name: str | None = None,
    agent: str = "claude-code",
    permission_mode: str = "default",
    scope_edits: bool = True,
    talk_first: bool = True,
    runner: str = "terminal",
) -> Project:
    if permission_mode not in LAUNCH_MODES_WITH_BYPASS:
        raise ProjectError(f"permission mode {permission_mode!r} is not one of {LAUNCH_MODES_WITH_BYPASS}")
    if runner not in RUNNERS:
        raise ProjectError(f"runner {runner!r} is not one of {RUNNERS}")
    directory = _norm(directory)
    return Project(
        id=str(uuid.uuid4()),
        name=(name or os.path.basename(directory.rstrip("/")) or directory).strip(),
        directory=directory,
        agent=agent,
        permission_mode=permission_mode,
        scope_edits=bool(scope_edits),
        talk_first=bool(talk_first),
        runner=runner,
    )


# ---- the folder picker ------------------------------------------------------------------


@dataclass(slots=True)
class Entry:
    name: str
    path: str
    has_git: bool = False
    project_id: str | None = None


@dataclass(slots=True)
class Listing:
    path: str
    parent: str | None
    home: str
    entries: list[Entry]
    can_create: bool


def home_dir(home: str | None = None) -> str:
    return _norm(home or os.environ.get("HOME") or str(Path.home()))


def inside_home(path: str, home: str | None = None) -> bool:
    """True when ``path`` (resolved, symlinks followed) is the home directory or under it."""
    h = home_dir(home)
    p = _norm(path)
    return p == h or p.startswith(h.rstrip("/") + "/")


def browse(path: str | None = None, *, home: str | None = None, store: ProjectStore | None = None, show_hidden: bool = False) -> Listing:
    """Subdirectories of ``path`` (default: home). Refuses to leave home."""
    h = home_dir(home)
    target = _norm(path) if path else h
    if not inside_home(target, h):
        raise ProjectError("Zordon only browses inside your home directory.")
    if not os.path.isdir(target):
        raise ProjectError(f"{target} is not a folder.")
    entries: list[Entry] = []
    try:
        names = sorted(os.listdir(target), key=str.lower)
    except OSError as e:
        raise ProjectError(f"cannot list {target}: {e.strerror or e}") from e
    for name in names:
        if name.startswith(HIDDEN_PREFIX) and not show_hidden:
            continue
        full = os.path.join(target, name)
        if not os.path.isdir(full) or os.path.islink(full) and not inside_home(full, h):
            continue
        proj = store.by_directory(full) if store is not None else None
        entries.append(Entry(name=name, path=full, has_git=os.path.isdir(os.path.join(full, ".git")), project_id=proj.id if proj else None))
    parent = os.path.dirname(target.rstrip("/")) if target != h else None
    return Listing(path=target, parent=parent, home=h, entries=entries, can_create=os.access(target, os.W_OK))


def validate_name(name: str) -> str:
    n = (name or "").strip()
    if not NAME_RE.match(n):
        raise ProjectError("Use letters, numbers, spaces, dots, dashes or underscores for the name (up to 64), starting with a letter or number.")
    if n in (".", ".."):
        raise ProjectError("That is not a folder name.")
    return n


def create_directory(parent: str, name: str, *, home: str | None = None) -> str:
    """``mkdir parent/name`` inside home. An existing *empty* folder is fine; anything else is an error."""
    h = home_dir(home)
    parent_n = _norm(parent) if parent else h
    if not inside_home(parent_n, h):
        raise ProjectError("New projects go inside your home directory.")
    if not os.path.isdir(parent_n):
        raise ProjectError(f"{parent_n} is not a folder.")
    n = validate_name(name)
    full = os.path.join(parent_n, n)
    if os.path.exists(full):
        if os.path.isdir(full) and not os.listdir(full):
            return full
        raise ProjectError(f"{full} already exists. Pick another name, or continue it as an existing folder.")
    try:
        os.makedirs(full, mode=0o755)
    except OSError as e:
        raise ProjectError(f"cannot create {full}: {e.strerror or e}") from e
    return full


def _norm(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
