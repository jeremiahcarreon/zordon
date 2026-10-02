"""The only module that shells out to tmux.

One ``run()`` does every call through ``subprocess.run`` with a timeout, UTF-8
decoding (``errors="replace"``) and a ``TmuxError`` on failure. The constructor
takes an optional socket name (``tmux -L``) so tests use a private server and
never see the user's sessions.

Keystroke safety (design, "Keystroke injection is literal"):

* ``send_literal`` always uses ``send-keys -l --`` so nothing is interpreted as a
  key name, strips every C0 control character first, and refuses empty text;
* ``send_enter`` is a separate call, by design;
* ``send_key`` only accepts names from ``KEY_ALLOWLIST``.

Pane targets have the registry's shape ``session:@window.%pane`` (decision 0002),
produced by ``new-window -P -F '#{session_name}:#{window_id}.#{pane_id}'``.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger("zordon.session.tmux")

TARGET_FORMAT = "#{session_name}:#{window_id}.#{pane_id}"
PANE_FIELDS = (
    "#{session_name}",
    "#{window_id}",
    "#{pane_id}",
    "#{pane_pid}",
    "#{pane_current_path}",
    "#{pane_current_command}",
    "#{alternate_on}",
    "#{pane_width}",
    "#{pane_height}",
)
_SEP = "\t"

KEY_ALLOWLIST: frozenset[str] = frozenset(
    {"Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "C-u", "C-c", "Space"}
    | {str(n) for n in range(1, 10)}
)

_C0 = re.compile(r"[\x00-\x1f\x7f]")
_TARGET_RE = re.compile(r"^[^:\s]+:@\d+\.%\d+$")


class TmuxError(RuntimeError):
    """A tmux command failed, timed out or the binary is missing."""


@dataclass(frozen=True, slots=True)
class PaneInfo:
    target: str  # "session:@win.%pane"
    session: str
    window_id: str  # "@3"
    pane_id: str  # "%7"
    pid: int
    cwd: str
    command: str  # pane_current_command (the foreground process name)
    alternate_on: bool
    width: int
    height: int


def strip_control(text: str) -> str:
    """Drop every C0 control character (and DEL); newlines and tabs become spaces."""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return _C0.sub("", text)


def is_pane_target(target: str) -> bool:
    return bool(_TARGET_RE.match(target))


def pane_id_of(target: str) -> str | None:
    """``"%7"`` for ``"zordon:@3.%7"`` (or a bare ``"%7"``), else None."""
    if target.startswith("%") and target[1:].isdigit():
        return target
    if is_pane_target(target):
        return target.rsplit(".", 1)[1]
    return None


class Tmux:
    def __init__(self, binary: str = "tmux", socket: str | None = None) -> None:
        self.binary = binary
        self.socket = socket

    # ---- the one shell-out ---------------------------------------------------------

    def run(self, *args: str, timeout: float = 2.0) -> str:
        cmd = [self.binary]
        if self.socket:
            cmd += ["-L", self.socket]
        cmd += list(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as e:
            raise TmuxError(f"tmux binary not found: {self.binary!r}") from e
        except subprocess.TimeoutExpired as e:
            raise TmuxError(f"tmux {args[0] if args else ''} timed out after {timeout}s") from e
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise TmuxError(f"tmux {args[0] if args else ''} failed ({proc.returncode}): {err}")
        return proc.stdout.decode("utf-8", errors="replace")

    # ---- server / sessions ----------------------------------------------------------

    def binary_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def server_alive(self) -> bool:
        try:
            self.run("list-sessions")
        except TmuxError:
            return False
        return True

    def has_session(self, name: str) -> bool:
        try:
            self.run("has-session", "-t", f"={name}")
        except TmuxError:
            return False
        return True

    def ensure_session(self, name: str, cwd: str | None = None, width: int = 160, height: int = 45) -> str:
        """Create the detached session ``name`` when it does not exist. Returns the name."""
        if self.has_session(name):
            return name
        args = ["new-session", "-d", "-s", name, "-x", str(width), "-y", str(height)]
        if cwd:
            args += ["-c", cwd]
        self.run(*args)
        log.info("created tmux session %s", name)
        return name

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", f"={name}")

    def kill_server(self) -> None:
        """Only sensible on a private socket (tests)."""
        try:
            self.run("kill-server")
        except TmuxError:
            pass

    # ---- panes ---------------------------------------------------------------------

    def list_panes(self) -> list[PaneInfo]:
        try:
            out = self.run("list-panes", "-a", "-F", _SEP.join(PANE_FIELDS))
        except TmuxError as e:
            if "no server running" in str(e) or "failed to connect" in str(e):
                return []
            raise
        panes: list[PaneInfo] = []
        for line in out.splitlines():
            parts = line.split(_SEP)
            if len(parts) != len(PANE_FIELDS):
                continue
            session, win, pane, pid, cwd, command, alt, w, h = parts
            panes.append(
                PaneInfo(
                    target=f"{session}:{win}.{pane}",
                    session=session,
                    window_id=win,
                    pane_id=pane,
                    pid=_int(pid),
                    cwd=cwd,
                    command=command,
                    alternate_on=alt == "1",
                    width=_int(w),
                    height=_int(h),
                )
            )
        return panes

    def pane_exists(self, target: str) -> bool:
        try:
            self._display(target, "#{pane_id}")
        except TmuxError:
            return False
        return True

    def _display(self, target: str, fmt: str) -> str:
        """``display-message -p`` for ``target``, verified against the pane id.

        tmux resolves a target whose window or pane is gone to the session's
        current pane instead of failing (``zt:@1.%1`` -> ``%0``) and prints an
        empty line for an unknown bare pane id, so the answer is checked before
        it is trusted.
        """
        out = self.run("display-message", "-p", "-t", target, "#{pane_id}" + _SEP + fmt)
        pane_id, _, value = out.rstrip("\n").partition(_SEP)
        if not pane_id:
            raise TmuxError(f"can't find pane: {target}")
        want = pane_id_of(target)
        if want is not None and pane_id != want:
            raise TmuxError(f"pane {target} is gone (tmux resolved it to {pane_id})")
        return value

    def new_window(
        self,
        session: str,
        name: str,
        cwd: str,
        command: Sequence[str],
        width: int = 160,
        height: int = 45,
    ) -> str:
        """Open a detached window in ``session`` running ``command``; returns its pane target.

        The window is resized to ``width`` x ``height`` afterwards and the size is
        verified (tmux ``window-size latest`` has been seen to pick another size).
        """
        if not command:
            raise ValueError("command must not be empty")
        target = self.run(
            "new-window",
            "-d",
            "-t",
            f"={session}",
            "-n",
            name,
            "-c",
            cwd,
            "-P",
            "-F",
            TARGET_FORMAT,
            shlex.join(list(command)),
        ).strip()
        if not is_pane_target(target):
            raise TmuxError(f"unexpected new-window output: {target!r}")
        self.resize_window(target, width, height)
        log.info("opened pane %s in %s (%dx%d)", target, cwd, width, height)
        return target

    def new_session(
        self, name: str, cwd: str, command: Sequence[str], width: int = 160, height: int = 45
    ) -> str:
        """Create a detached session whose first window runs ``command``; returns its pane target."""
        if not command:
            raise ValueError("command must not be empty")
        target = self.run(
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            cwd,
            "-x",
            str(width),
            "-y",
            str(height),
            "-P",
            "-F",
            TARGET_FORMAT,
            shlex.join(list(command)),
        ).strip()
        if not is_pane_target(target):
            raise TmuxError(f"unexpected new-session output: {target!r}")
        self.resize_window(target, width, height)
        return target

    def resize_window(self, target: str, width: int, height: int) -> tuple[int, int]:
        """Resize the window holding ``target`` and return the size tmux reports afterwards."""
        self.run("resize-window", "-t", target, "-x", str(width), "-y", str(height))
        got = self.window_size(target)
        if got != (width, height):
            log.warning("pane %s is %dx%d, wanted %dx%d", target, got[0], got[1], width, height)
        return got

    def window_size(self, target: str) -> tuple[int, int]:
        out = self._display(target, "#{window_width}x#{window_height}").strip()
        w, _, h = out.partition("x")
        return _int(w), _int(h)

    def kill_window(self, target: str) -> None:
        self.run("kill-window", "-t", target)

    def alternate_on(self, target: str) -> bool:
        return self._display(target, "#{alternate_on}").strip() == "1"

    def pane_pid(self, target: str) -> int | None:
        try:
            out = self._display(target, "#{pane_pid}").strip()
        except TmuxError:
            return None
        return _int(out) or None

    def pane_dead(self, target: str) -> bool:
        try:
            return self._display(target, "#{pane_dead}").strip() == "1"
        except TmuxError:
            return True

    # ---- capture -------------------------------------------------------------------

    def capture(
        self, target: str, ansi: bool = False, history_lines: int = 0, *, with_ansi: bool = False
    ) -> list[str]:
        """The visible pane as lines. Trailing U+0020 is stripped; U+00A0 is preserved.

        ``history_lines`` adds that many scrollback lines (``-S -N``); the Claude Code
        TUI runs on the alternate screen, which has none, so the default is 0.
        ``with_ansi`` is an alias of ``ansi`` (the name used in architecture.md).
        """
        args = ["capture-pane", "-p", "-J", "-t", target]
        if ansi or with_ansi:
            args.append("-e")
        if history_lines:
            args += ["-S", f"-{int(history_lines)}"]
        out = self.run(*args)
        lines = out.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return [line.rstrip(" ") for line in lines]

    # ---- keystrokes ----------------------------------------------------------------

    def send_literal(self, target: str, text: str) -> None:
        """Type ``text`` into the pane exactly, with control characters removed.

        Never sends Enter; call ``send_enter`` separately.
        """
        clean = strip_control(text)
        if not clean.strip():
            raise ValueError("refusing to send empty text")
        self.run("send-keys", "-t", target, "-l", "--", clean)
        log.debug("sent %d literal characters to %s", len(clean), target)

    def send_enter(self, target: str) -> None:
        self.run("send-keys", "-t", target, "Enter")

    def send_key(self, target: str, key: str) -> None:
        if key not in KEY_ALLOWLIST:
            raise ValueError(f"key {key!r} is not in the allowlist")
        self.run("send-keys", "-t", target, key)


def _int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0
