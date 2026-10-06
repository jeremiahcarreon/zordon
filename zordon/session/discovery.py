"""Session discovery from Claude Code's own store, and the only ``claude`` command
line builders in the package.

Verified against Claude Code ``STORE_FORMAT_VERSION`` (decision 0002):

* transcripts live at ``<claude_home>/projects/<encoded cwd>/<session uuid>.jsonl``;
  the encoding (every non-alphanumeric character becomes ``-``) is lossy, so the
  real cwd comes from a record's ``cwd`` field, then ``history.jsonl``, then the
  directory name flagged as approximate;
* metadata records (``ai-title``, ``custom-title``, ``permission-mode``,
  ``last-prompt``) have no timestamp and are rewritten; the latest wins and sits
  within ~30 KB of EOF, so the tail is read in a growing window;
* a forked file's first ``cwd`` can be megabytes in, so the head read is bounded
  by lines and bytes;
* ``<claude_home>/sessions/<pid>.json`` lists live processes; an entry is alive
  only when ``kill -0`` succeeds and ``/proc/<pid>/stat`` field 22 equals its
  ``procStart`` (the pid may have been reused).

Safety: ``resume_command`` and ``new_session_command`` are the only places a
``claude`` argv is built. They accept modes from ``ALLOWED_MODES`` only and run
``validate_command`` so no refused flag and no bypass mode can ever be emitted.
Both prefix the command with ``env -u NAME ...`` for every variable in
``tmux.scrub_names()`` so provider keys and Claude Code's own nesting markers
never reach the pane process even when the tmux server's environment has them.

Hooks: the curl handler reads the shared secret from a 0600 curl config file
(``-K <file>``) next to the settings file, so the secret is never on a command
line (``/proc/<pid>/cmdline`` is world-readable). The POST goes to the address
the server listens on (``hook_host``), not blindly to 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zordon.session.tmux import scrub_names

log = logging.getLogger("zordon.session.discovery")

STORE_FORMAT_VERSION = "claude-code-2.1.287"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEAD_LINES = 40
HEAD_BYTES = 2 * 1024 * 1024
TAIL_START = 256 * 1024
TAIL_MAX = 8 * 1024 * 1024
PROJECT_DIR_MAX = 200  # longer names are truncated with a hash suffix by Claude Code (not reproduced)

ALLOWED_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "dontAsk")
MODE_ALIASES: dict[str, str] = {"manual": "default"}
# Flags Zordon refuses to ever pass to claude. The first two bypass permission
# checks; the third loads unreviewed code; the last two disable settings hooks,
# which Zordon relies on for the Notification signal (decisions 0007, 0009).
REFUSED_FLAGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-load-development-channels",
    "--bare",
    "--safe-mode",
)
REFUSED_SETTINGS_KEYS: tuple[str, ...] = ("skipDangerousModePermissionPrompt",)

HOOK_MATCHER = "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog"
HOOK_EVENTS: tuple[str, ...] = ("Notification", "UserPromptSubmit", "Stop")
HOOK_SECRET_HEADER = "X-Zordon-Hook-Secret"
HOOK_DEFAULT_HOST = "127.0.0.1"
_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$|^\[[0-9A-Fa-f:.]+\]$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---- paths ---------------------------------------------------------------------------


def encode_project_dir(cwd: str) -> str:
    """Claude Code's ``<project>`` directory name: every non-[A-Za-z0-9] char -> ``-``.

    Lossy; never decode it. Names over 200 characters get a hash suffix upstream,
    which is not reproduced here (the prediction is only used to find a new
    session's file, and the picker falls back to the ``cwd`` fields).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def projects_dir(claude_home: Path) -> Path:
    return Path(claude_home) / "projects"


def sessions_dir(claude_home: Path) -> Path:
    return Path(claude_home) / "sessions"


def history_path(claude_home: Path) -> Path:
    return Path(claude_home) / "history.jsonl"


def jsonl_path_for(cwd: str, session_id: str, claude_home: Path) -> Path:
    """Where Claude Code will write (or has written) this session's transcript."""
    return projects_dir(claude_home) / encode_project_dir(cwd) / f"{session_id}.jsonl"


# ---- data ----------------------------------------------------------------------------


@dataclass
class SessionInfo:
    session_id: str
    jsonl_path: Path
    project_dir: str
    cwd: str | None = None
    cwd_source: str = "unknown"  # record | history | dirname-approx | unknown
    title: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    started_at: str | None = None  # ISO-8601 from the first timestamped record
    last_active: str | None = None  # ISO-8601 from the last timestamped record
    last_active_ts: float = 0.0  # epoch seconds; falls back to the file mtime
    mtime: float = 0.0
    size: int = 0
    git_branch: str | None = None
    version: str | None = None
    permission_mode: str | None = None  # latest permission-mode record: default|acceptEdits|plan|auto|dontAsk|bypassPermissions
    is_bg: bool = False
    running: bool = False
    running_pid: int | None = None
    tmux_target: str | None = None  # registry "tmux" field, "session:@win.%pane"
    status: str | None = None  # registry status: idle | busy | waiting
    registry: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def directory(self) -> str:
        return self.cwd or ""

    @property
    def display_title(self) -> str:
        return self.title or self.first_prompt or self.last_prompt or self.session_id[:8]


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    project: str | None
    first_prompt: str | None
    last_ts: float  # epoch seconds of the newest history line for the session


# ---- head / tail readers -------------------------------------------------------------------


def read_head(path: Path, info: SessionInfo) -> None:
    """First ``HEAD_LINES`` lines (``HEAD_BYTES`` cap): cwd, start time, version, first prompt."""
    read = 0
    with path.open("rb") as fh:
        for i, raw in enumerate(fh):
            read += len(raw)
            if i >= HEAD_LINES or read > HEAD_BYTES:
                break
            rec = _loads(raw)
            if rec is None:
                continue
            if info.cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                info.cwd, info.cwd_source = rec["cwd"], "record"
            if info.started_at is None and rec.get("timestamp"):
                info.started_at = str(rec["timestamp"])
            if rec.get("sessionKind") == "bg":
                info.is_bg = True
            info.version = info.version or _str_or_none(rec.get("version"))
            info.git_branch = info.git_branch or _str_or_none(rec.get("gitBranch"))
            if info.first_prompt is None:
                info.first_prompt = first_prompt_of(rec)
            if info.cwd and info.first_prompt and info.started_at:
                break


def first_prompt_of(rec: dict[str, Any]) -> str | None:
    """The text of a typed human prompt record, or None for anything else."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    msg = rec.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(t for t in texts if t) or None
    if not isinstance(content, str) or not content.strip() or content.lstrip().startswith("<"):
        return None
    return content.strip()


def tail_lines(path: Path, start: int = TAIL_START, maximum: int = TAIL_MAX) -> list[bytes]:
    """Complete non-empty lines from the end of the file, growing the window x4 until
    at least one complete line is found (a single line can exceed 1 MB)."""
    size = path.stat().st_size
    window = start
    while True:
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            data = fh.read()
        lines = data.split(b"\n")
        if size > window:
            lines = lines[1:]  # the first piece is a partial line
        lines = [line for line in lines if line.strip()]
        if lines or window >= maximum or window >= size:
            return lines
        window *= 4


def read_tail(path: Path, info: SessionInfo) -> None:
    """Latest timestamp, title, permission mode and last prompt from the end of the file."""
    want = {"timestamp", "ai-title", "custom-title", "permission-mode", "last-prompt"}
    for raw in reversed(tail_lines(path)):
        rec = _loads(raw)
        if rec is None:
            continue
        t = rec.get("type")
        if "timestamp" in want and rec.get("timestamp"):
            info.last_active = str(rec["timestamp"])
            want.discard("timestamp")
            info.git_branch = _str_or_none(rec.get("gitBranch")) or info.git_branch
            info.version = _str_or_none(rec.get("version")) or info.version
        if t == "custom-title" and "custom-title" in want:
            if rec.get("customTitle"):
                info.title = str(rec["customTitle"])
                want.discard("ai-title")
            want.discard("custom-title")
        elif t == "ai-title" and "ai-title" in want:
            if rec.get("aiTitle") and not info.title:
                info.title = str(rec["aiTitle"])
            want.discard("ai-title")
        elif t == "permission-mode" and "permission-mode" in want:
            info.permission_mode = _str_or_none(rec.get("permissionMode"))
            want.discard("permission-mode")
        elif t == "last-prompt" and "last-prompt" in want and rec.get("lastPrompt"):
            info.last_prompt = str(rec["lastPrompt"])
            want.discard("last-prompt")
        if not want:
            break


# ---- history index -----------------------------------------------------------------------


def load_history_index(claude_home: Path) -> dict[str, HistoryEntry]:
    """``sessionId -> (real project path, first typed prompt, newest timestamp)``.

    ``history.jsonl`` is small (one line per submitted prompt) and carries the real
    cwd, so it is the cheap way to map ids to paths. Slash commands are not prompts.
    """
    idx: dict[str, HistoryEntry] = {}
    path = history_path(claude_home)
    if not path.exists():
        return idx
    try:
        with path.open("rb") as fh:
            for raw in fh:
                rec = _loads(raw)
                if rec is None:
                    continue
                sid = rec.get("sessionId")
                if not isinstance(sid, str):
                    continue
                ts = _epoch(rec.get("timestamp"))
                disp = rec.get("display") if isinstance(rec.get("display"), str) else ""
                prompt = None if disp.startswith("/") or not disp.strip() else disp.strip()
                prev = idx.get(sid)
                if prev is None:
                    idx[sid] = HistoryEntry(rec.get("project"), prompt, ts)
                else:
                    idx[sid] = HistoryEntry(
                        prev.project or rec.get("project"),
                        prev.first_prompt or prompt,
                        max(prev.last_ts, ts),
                    )
    except OSError as e:
        log.warning("could not read %s: %s", path, e)
    return idx


# ---- running-process registry -------------------------------------------------------------


def proc_start_ticks(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` (start time in clock ticks); None off Linux."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return stat[stat.rindex(")") + 2 :].split()[19]
    except (ValueError, IndexError):
        return None


def pid_alive(pid: int, proc_start: str | int | None = None) -> bool:
    """``kill -0`` succeeds and, on Linux, the start ticks match ``proc_start``."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    except (OSError, TypeError, ValueError):
        return False
    if proc_start is None or not sys.platform.startswith("linux"):
        return True
    ticks = proc_start_ticks(int(pid))
    if ticks is None:
        return True  # /proc unreadable: fall back to kill -0 only
    return ticks == str(proc_start)


def load_registry(claude_home: Path) -> dict[str, dict[str, Any]]:
    """``sessionId -> live registry entry`` for processes that are really running."""
    live: dict[str, dict[str, Any]] = {}
    sdir = sessions_dir(claude_home)
    if not sdir.is_dir():
        return live
    for p in sorted(sdir.glob("*.json")):
        if p.name.startswith("daemon."):
            continue
        try:
            entry = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("spare"):
            continue
        pid = entry.get("pid")
        sid = entry.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        if not pid_alive(pid, entry.get("procStart")):
            continue
        live[sid] = entry
    return live


# ---- listing ---------------------------------------------------------------------------------


_cache: dict[Path, tuple[tuple[int, int], SessionInfo]] = {}


def _info_for(path: Path, project_dir: str) -> SessionInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        info = cached[1]
    else:
        info = SessionInfo(path.stem, path, project_dir, mtime=st.st_mtime, size=st.st_size)
        try:
            read_head(path, info)
            read_tail(path, info)
        except OSError as e:
            log.debug("skipping %s: %s", path, e)
            return None
        _cache[path] = (key, info)
    # Return a copy so callers can decorate it (running state) without touching the cache.
    return SessionInfo(**{k: v for k, v in info.__dict__.items()})


def list_sessions(claude_home: Path, tmux: Any | None = None) -> list[SessionInfo]:
    """Every session in the store, newest activity first, with running state attached.

    ``tmux`` is accepted for signature compatibility (the registry already carries
    the pane target); when given and it has ``pane_exists``, a registry target that
    no longer exists is dropped from ``tmux_target``.
    """
    claude_home = Path(claude_home)
    history = load_history_index(claude_home)
    registry = load_registry(claude_home)
    out: list[SessionInfo] = []
    pdir = projects_dir(claude_home)
    if not pdir.is_dir():
        return out
    for proj in sorted(pdir.iterdir()):
        if not proj.is_dir():
            continue
        for path in sorted(proj.glob("*.jsonl")):
            if not UUID_RE.match(path.stem):
                continue
            info = _info_for(path, proj.name)
            if info is None:
                continue
            _decorate(info, history.get(info.session_id), registry.get(info.session_id), tmux)
            out.append(info)
    out.sort(key=lambda s: (s.last_active_ts, s.mtime), reverse=True)
    return out


def _decorate(
    info: SessionInfo,
    hist: HistoryEntry | None,
    entry: dict[str, Any] | None,
    tmux: Any | None,
) -> None:
    if info.cwd is None and hist and hist.project:
        info.cwd, info.cwd_source = hist.project, "history"
    if info.first_prompt is None and hist:
        info.first_prompt = hist.first_prompt
    if info.cwd is None and entry and isinstance(entry.get("cwd"), str):
        info.cwd, info.cwd_source = entry["cwd"], "registry"
    if info.cwd is None:
        info.cwd, info.cwd_source = approximate_cwd(info.project_dir), "dirname-approx"
    info.title = info.title or info.first_prompt or info.last_prompt
    ts = _epoch(info.last_active) if info.last_active else 0.0
    if not ts and hist:
        ts = hist.last_ts
    info.last_active_ts = ts or info.mtime
    if entry:
        info.running = True
        info.registry = entry
        info.running_pid = entry.get("pid")
        info.status = _str_or_none(entry.get("status"))
        target = entry.get("tmux")
        if isinstance(target, str) and target:
            if tmux is not None and hasattr(tmux, "pane_exists"):
                try:
                    target = target if tmux.pane_exists(target) else None
                except Exception as e:  # noqa: BLE001 - tmux failures must not break the picker
                    log.debug("pane check failed for %s: %s", target, e)
            info.tmux_target = target
        if entry.get("kind") == "bg":
            info.is_bg = True


def approximate_cwd(project_dir: str) -> str:
    """Lossy guess at a path from the directory name; only used as a last resort."""
    return project_dir.replace("-", "/")


def find_session(session_id: str, claude_home: Path, tmux: Any | None = None) -> SessionInfo | None:
    """One session by id (file stem), or None."""
    claude_home = Path(claude_home)
    if not UUID_RE.match(session_id):
        return None
    pdir = projects_dir(claude_home)
    if not pdir.is_dir():
        return None
    for proj in sorted(pdir.iterdir()):
        path = proj / f"{session_id}.jsonl"
        if proj.is_dir() and path.is_file():
            info = _info_for(path, proj.name)
            if info is None:
                return None
            history = load_history_index(claude_home)
            registry = load_registry(claude_home)
            _decorate(info, history.get(session_id), registry.get(session_id), tmux)
            return info
    return None


def running_sessions(claude_home: Path) -> dict[str, dict[str, Any]]:
    """Live registry entries keyed by session id (thin alias with a clearer name)."""
    return load_registry(Path(claude_home))


# ---- command builders -------------------------------------------------------------------------


BYPASS_MODE = "bypassPermissions"


def normalize_mode(mode: str, *, allow_bypass: bool = False) -> str:
    """Map a user-facing or status-row mode name onto ``ALLOWED_MODES``; refuse the rest.

    ``allow_bypass`` admits ``bypassPermissions`` as well. Only the project launcher
    passes it, for a project whose owner chose that mode when creating it (decision
    0018); voice, the mode switcher and the config default never do.
    """
    m = MODE_ALIASES.get(mode, mode)
    if m == BYPASS_MODE and allow_bypass:
        return m
    if m not in ALLOWED_MODES:
        raise ValueError(f"permission mode {mode!r} is not allowed; choose one of {ALLOWED_MODES}")
    return m


def env_scrub_prefix(environ: Mapping[str, str] | None = None) -> list[str]:
    """``["env", "-u", NAME, ...]`` for every name ``tmux.scrub_names`` reports.

    ``env -u`` of a variable that is not set is a no-op, so the prefix is safe
    whatever the pane's environment turns out to be.
    """
    argv = ["env"]
    for name in scrub_names(environ):
        argv += ["-u", name]
    return argv


def strip_env_prefix(argv: Sequence[str]) -> list[str]:
    """``argv`` without a leading ``env -u NAME ...`` prefix; refuses any other ``env`` use.

    Only unsets are allowed: an assignment (``NAME=value``) or an ``env`` option
    other than ``-u`` could re-introduce a secret or a bypass setting.
    """
    argv = list(argv)
    if not argv or argv[0] != "env":
        return argv
    i = 1
    while i < len(argv) and argv[i] == "-u":
        if i + 1 >= len(argv) or not _ENV_NAME_RE.match(argv[i + 1]):
            raise ValueError("env -u needs a variable name")
        i += 2
    rest = argv[i:]
    if not rest or rest[0] != "claude":
        raise ValueError("only 'env -u NAME ...' may precede 'claude'")
    return rest


def validate_command(argv: Sequence[str], *, allow_bypass: bool = False) -> None:
    """Raise ValueError if ``argv`` would widen permissions or disable hooks.

    A leading ``env -u NAME ...`` prefix (``env_scrub_prefix``) is allowed; the
    rest must start with ``claude``. The skip-permissions flags in ``REFUSED_FLAGS``
    are refused always; the bypass *mode* only with ``allow_bypass`` (a project the
    user configured that way), and never through a settings file.
    """
    argv = strip_env_prefix(argv)
    if not argv or argv[0] != "claude":
        raise ValueError("a claude command line must start with 'claude'")
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue  # the free-text value of --append-system-prompt
        base = arg.split("=", 1)[0]
        if base == "--append-system-prompt" and "=" not in arg:
            skip_next = True
            continue
        if base in REFUSED_FLAGS:
            raise ValueError(f"refused flag {base!r}")
        if base == "--permission-mode":
            value = arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            if value == BYPASS_MODE and allow_bypass:
                continue
            if value not in ALLOWED_MODES:
                raise ValueError(f"permission mode {value!r} is not allowed")
        elif BYPASS_MODE in arg and not (allow_bypass and i > 0 and argv[i - 1] == "--permission-mode"):
            raise ValueError("bypassPermissions is never passed")
        if base == "--settings" and i + 1 < len(argv) and argv[i + 1].lstrip().startswith("{"):
            _check_inline_settings(argv[i + 1])


def _check_inline_settings(text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError("inline --settings is not valid JSON") from e
    for key in REFUSED_SETTINGS_KEYS:
        if key in data:
            raise ValueError(f"refused settings key {key!r}")
    mode = (data.get("permissions") or {}).get("defaultMode") if isinstance(data, dict) else None
    if mode is not None and mode not in ALLOWED_MODES:
        raise ValueError(f"refused permissions.defaultMode {mode!r}")


LAUNCH_EXTRA_FLAGS = ("--model", "--effort")  # the per-project launch flags a session may carry


def _base_command(
    settings_path: Path | None,
    permission_mode: str | None,
    *,
    allow_bypass: bool = False,
    system_prompt: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    argv: list[str] = []
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if extra_args:
        pairs = list(extra_args)
        if len(pairs) % 2 or any(flag not in LAUNCH_EXTRA_FLAGS for flag in pairs[::2]):
            raise ValueError(f"extra launch args must be pairs of {LAUNCH_EXTRA_FLAGS}")
        argv += pairs
    if settings_path is not None:
        if not isinstance(settings_path, Path):
            # Guards against the older architecture signature (session_id, permission_mode):
            # a mode string must never be mistaken for a settings file.
            raise TypeError("settings_path must be a pathlib.Path (or None)")
        argv += ["--settings", str(settings_path)]
    if permission_mode:
        argv += ["--permission-mode", normalize_mode(permission_mode, allow_bypass=allow_bypass)]
    return argv


def resume_command(
    session_id: str,
    settings_path: Path | None = None,
    permission_mode: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    allow_bypass: bool = False,
    system_prompt: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """``env -u ... claude --resume <id> [--settings <file>] [--permission-mode <mode>]``.

    Never contains a bypass flag; the bypass *mode* only with ``allow_bypass`` (a
    project configured for it). Without ``permission_mode`` Claude Code restores
    the session's stored mode (or its own built-in default, which is ``auto`` in
    2.1.x), so the manager always passes one. The ``env -u`` prefix comes from
    ``env_scrub_prefix``.
    """
    if not UUID_RE.match(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    argv = ["claude", "--resume", session_id] + _base_command(
        settings_path, permission_mode, allow_bypass=allow_bypass, system_prompt=system_prompt, extra_args=extra_args
    )
    argv = env_scrub_prefix(environ) + argv
    validate_command(argv, allow_bypass=allow_bypass)
    return argv


def new_session_command(
    session_id: str,
    settings_path: Path | None = None,
    permission_mode: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    allow_bypass: bool = False,
    system_prompt: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """``env -u ... claude --session-id <uuid> [...]`` so the id is known before the first record.

    ``system_prompt`` goes to ``--append-system-prompt`` (the voice-mode text, decision 0019)."""
    if not UUID_RE.match(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    argv = ["claude", "--session-id", session_id] + _base_command(
        settings_path, permission_mode, allow_bypass=allow_bypass, system_prompt=system_prompt, extra_args=extra_args
    )
    argv = env_scrub_prefix(environ) + argv
    validate_command(argv, allow_bypass=allow_bypass)
    return argv


# ---- hook settings ----------------------------------------------------------------------------


def validate_hook_secret(secret: str) -> str:
    if not isinstance(secret, str) or not _SECRET_RE.match(secret):
        raise ValueError("hook secret must be 16+ URL-safe characters")
    return secret


def hook_host(bind: str | None) -> str:
    """The host the pane's curl must POST to for the server bound at ``bind``.

    A wildcard bind (``0.0.0.0``, ``::``, empty) listens on loopback too, so
    loopback is used; any other address is used as is (a server bound only to a
    Tailscale or LAN address does not listen on 127.0.0.1). IPv6 is bracketed.
    """
    host = (bind or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]", "*"):
        return HOOK_DEFAULT_HOST
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if not _HOST_RE.match(host):
        raise ValueError(f"not a usable hook host: {bind!r}")
    return host


def hook_curl_config_text(secret: str) -> str:
    """Body of the 0600 curl config file that carries the secret header."""
    validate_hook_secret(secret)
    return f'header = "{HOOK_SECRET_HEADER}: {secret}"\n'


def hook_command(port: int, curl_config: Path | str, host: str = HOOK_DEFAULT_HOST) -> str:
    """The curl handler: always exits 0 and prints nothing, so it can never block Claude Code.

    The secret is read from ``curl_config`` (``curl -K``), never placed on the
    command line.
    """
    if not (1 <= int(port) <= 65535):
        raise ValueError("port out of range")
    host = hook_host(host)
    path = str(curl_config)
    if not path or "\n" in path:
        raise ValueError("curl config path must be a single non-empty line")
    return (
        "curl -s -m 2 -X POST -H 'Content-Type: application/json' "
        f"-K {shlex.quote(path)} --data-binary @- "
        f"http://{host}:{int(port)}/hooks/claude >/dev/null 2>&1 || true"
    )


SCOPE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
SCOPE_DENY_UNREACHABLE = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Zordon could not be reached to check this file is inside the project; edit refused",
        }
    }
)


def scope_hook_command(port: int, curl_config: Path | str, host: str = HOOK_DEFAULT_HOST) -> str:
    """The PreToolUse handler for scoped projects: synchronous, prints Zordon's decision.

    Unlike the signal hooks it must be heard: its stdout is the allow/deny answer.
    When Zordon cannot be reached it prints a deny (fail closed): a scoped project,
    above all one in bypass mode, must not edit outside its folder because the
    guard is down. The settings file only exists for sessions Zordon launched.
    """
    if not (1 <= int(port) <= 65535):
        raise ValueError("port out of range")
    host = hook_host(host)
    path = str(curl_config)
    if not path or "\n" in path:
        raise ValueError("curl config path must be a single non-empty line")
    return (
        "curl -s -f -m 3 -X POST -H 'Content-Type: application/json' "
        f"-K {shlex.quote(path)} --data-binary @- "
        f"http://{host}:{int(port)}/hooks/scope 2>/dev/null || printf '%s' {shlex.quote(SCOPE_DENY_UNREACHABLE)}"
    )


PERMISSION_HOOK_TIMEOUT_S = 900  # Claude Code waits this long for the user's spoken answer
PERMISSION_NO_OPINION = "{}"  # printed when Zordon is unreachable: Claude Code shows its own dialog


def permission_hook_command(port: int, curl_config: Path | str, host: str = HOOK_DEFAULT_HOST) -> str:
    """The PermissionRequest handler (decision 0019): synchronous, prints Zordon's decision.

    Its stdout is the answer: allow, deny with a message, or ``{}`` for "no opinion"
    (Claude Code then draws its dialog and the screen reader handles it). The curl
    waits as long as the hook timeout so the user can answer by voice; when Zordon
    cannot be reached it prints ``{}`` so nothing is ever decided by accident.
    """
    if not (1 <= int(port) <= 65535):
        raise ValueError("port out of range")
    host = hook_host(host)
    path = str(curl_config)
    if not path or "\n" in path:
        raise ValueError("curl config path must be a single non-empty line")
    return (
        f"curl -s -f -m {PERMISSION_HOOK_TIMEOUT_S - 10} -X POST -H 'Content-Type: application/json' "
        f"-K {shlex.quote(path)} --data-binary @- "
        f"http://{host}:{int(port)}/hooks/permission 2>/dev/null || printf '%s' {shlex.quote(PERMISSION_NO_OPINION)}"
    )


def hook_settings_json(
    port: int,
    curl_config: Path | str,
    events: Sequence[str] = HOOK_EVENTS,
    host: str = HOOK_DEFAULT_HOST,
    *,
    scope: bool = False,
    permission: bool = True,
) -> dict[str, Any]:
    """Settings for ``--settings``: command hooks that POST each event to Zordon.

    ``Notification`` is filtered to the prompt-related matchers; the other events
    have no matcher. With ``permission`` (the default, decision 0019) a synchronous
    ``PermissionRequest`` hook hands every permission, question and plan approval to
    Zordon, which answers only with the user's explicit decision. With ``scope`` a
    synchronous ``PreToolUse`` hook on the file editing tools asks Zordon whether
    the file is inside the project (decision 0018); it can only deny. The JSON
    contains no secret.
    """
    command = hook_command(port, curl_config, host)
    handler = {"type": "command", "command": command, "timeout": 5, "async": True}
    hooks: dict[str, Any] = {}
    for event in events:
        if event == "PermissionRequest":
            raise ValueError("PermissionRequest is installed through the `permission` flag, not the event list")
        entry: dict[str, Any] = {"hooks": [dict(handler)]}
        if event == "Notification":
            entry = {"matcher": HOOK_MATCHER, "hooks": [dict(handler)]}
        hooks[event] = [entry]
    if permission:
        hooks["PermissionRequest"] = [
            {"hooks": [{"type": "command", "command": permission_hook_command(port, curl_config, host), "timeout": PERMISSION_HOOK_TIMEOUT_S}]}
        ]
    if scope:
        hooks["PreToolUse"] = [
            {
                "matcher": SCOPE_MATCHER,
                "hooks": [{"type": "command", "command": scope_hook_command(port, curl_config, host), "timeout": 10}],
            }
        ]
    return {"hooks": hooks}


def hook_settings_path(zordon_home: Path, session_id: str) -> Path:
    return Path(zordon_home) / "hooks" / f"{session_id[:8]}.json"


def hook_curl_config_path(settings_path: Path) -> Path:
    """The curl config file that belongs to ``settings_path`` (same directory, ``.curlrc``)."""
    return Path(settings_path).with_suffix(".curlrc")


def write_hook_settings(
    path: Path,
    port: int,
    secret: str,
    events: Sequence[str] = HOOK_EVENTS,
    host: str = HOOK_DEFAULT_HOST,
    *,
    scope: bool = False,
) -> Path:
    """Write the hooks JSON and its curl config, both mode 0600; returns the JSON path.

    The secret lives only in the curl config (``hook_curl_config_path(path)``);
    the JSON references it by path, so neither the settings file nor ``ps`` shows it.
    """
    path = Path(path)
    curl_config = hook_curl_config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _write_private(curl_config, hook_curl_config_text(secret))
    data = json.dumps(hook_settings_json(port, curl_config, events, host, scope=scope), indent=2) + "\n"
    _write_private(path, data)
    return path


def remove_hook_settings(path: Path | None) -> None:
    """Delete the hooks JSON and its curl config (missing files are fine)."""
    if path is None:
        return
    for p in (Path(path), hook_curl_config_path(path)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _write_private(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---- helpers ------------------------------------------------------------------------------------


def _loads(raw: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _epoch(ts: Any) -> float:
    """ISO-8601 string (``Z`` or offset) or epoch ms/seconds -> epoch seconds; 0.0 on failure."""
    if ts is None:
        return 0.0
    if isinstance(ts, int | float):
        return float(ts) / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return 0.0


def clear_cache() -> None:
    _cache.clear()
