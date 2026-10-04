"""SessionThread: owns the tmux panes, the transcript tails and the session state.

Everything that is specific to one coding agent (how to launch it, what its
prompts and screen look like, where its transcript lives, its permission modes)
is behind ``zordon.agents.AgentAdapter``; each ``Session`` carries the adapter
it was opened with. One ``SessionManager`` thread polls every attached session
each ``config.output.poll_interval_ms``:

1. ``tmux.pane_exists`` / ``alternate_on``: a missing pane is DETACHED; for an
   agent that draws on the alternate screen the normal screen is only looked at
   for the trust dialog and for signs that the agent is gone (the exit line, a
   shell prompt, or having left the alternate screen), which make the session
   DETACHED after ``EXIT_CONFIRM_POLLS`` confirming polls; a plain-screen agent
   (the generic adapter) is polled on the normal screen and is gone when
   ``adapter.exited`` holds for the same number of polls;
2. ``capture`` -> ``adapter.parse`` -> ``screen.diff_screens``: new content
   lines go to ``bus.pane_lines`` with ``source="pane"`` when the session has no
   transcript source (or ``output.source == "pane"``);
3. ``adapter.detect_prompt`` on the parsed screen: ``PromptDetected`` once per
   distinct prompt, ``PromptCleared`` when it leaves the screen;
4. ``adapter.transcript_source(...).poll()`` -> ``bus.pane_lines`` with
   ``source="jsonl"`` in ``auto`` / ``jsonl`` mode;
5. ``state.next_state`` over an ``Observation`` built from the above, plus the
   second (``adapter.hook_hint``) and third (``adapter.status_hint``) prompt signals;
6. the idle watchdog: WORKING with no output for ``voice.idle_watchdog_seconds``
   and no prompt becomes STALLED, with one spoken ``Notice`` carrying the last
   pane lines.

State is derived from the pane, never from what was sent (design). The control
methods (``SessionControl`` in ``docs/architecture.md``) are thread-safe: they
enqueue a command the thread runs on its next tick and wait on a future with a
short timeout; when the thread is not running (unit tests) or the caller is the
thread itself, the command runs inline.

Keystrokes: literal text goes through ``Tmux.send_literal`` (control characters
stripped, ``send-keys -l``) and Enter is a separate call. Menu navigation only
ever uses ``Up``/``Down``/``Enter``/``Escape``/``BTab`` from the allowlist. For
an alternate-screen agent no keystroke is sent while the pane is off that screen
(a shell would run the text as a command) unless the prompt on screen is the
trust dialog; for a plain-screen agent keystrokes are refused once
``adapter.exited`` says the agent is gone. ``approve`` selects only the
adapter's plain yes option (an inline ``(y/n)`` prompt gets a literal ``y`` and
Enter); ``plan_approve`` never selects the auto-mode option;
``set_permission_mode`` refuses the adapter's forbidden modes, steps past them a
bounded number of times if they show, and returns to the starting mode when the
target is never reached.

Launch: ``start``/``resume`` without an explicit mode pass the adapter's
``default_launch_mode`` so a pane never comes up in the agent's built-in default.
``permission_summary`` speaks the mode that was actually observed (status row or
transcript record), waiting briefly for it instead of assuming one.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zordon import paths
from zordon.agents import (
    ADAPTERS,
    DEFAULT_AGENT,
    AgentAdapter,
    HookRequest,
    LaunchSpec,
    get_adapter,
)
from zordon.agents import SessionInfo as AgentSessionInfo
from zordon.agents.base import TranscriptSource
from zordon.bus import (
    Bus,
    Notice,
    PaneLine,
    PromptCleared,
    PromptDetected,
    PromptKind,
    SessionState,
    StateChanged,
)
from zordon.config import Config
from zordon.projects import (
    BYPASS_MODE,
    Project,
    ProjectError,
    ProjectStore,
    create_directory,
    inside_home,
    new_project,
)
from zordon.session import hookprompts
from zordon.session.prompts import PromptMatch
from zordon.session.screen import Screen, diff_screens, held_tail
from zordon.session.state import Observation, next_state
from zordon.session.tmux import Tmux, TmuxError, strip_control
from zordon.transcript.redaction import redact

log = logging.getLogger("zordon.session.manager")

TMUX_SESSION = "zordon"
PANE_WIDTH = 160
PANE_HEIGHT = 45
COMMAND_TIMEOUT = 3.0
ECHO_TIMEOUT = 1.5  # seconds for the screen to change after send_text
REGISTRY_INTERVAL = 1.0  # seconds between registry status reads per session
REGISTRY_WAITING_SCORE = 0.95
HOOK_HINT_SCORE = 0.9  # fallback prompt score for a hook hint that carries none
PANE_TAIL_LINES = 200
MAX_BTAB_PRESSES = 6
MAX_BYPASS_STEPS = 2  # how often bypass may show in one switch before giving up
MAX_STUCK_READS = 2  # status row unchanged after this many presses: the TUI is not reacting
BTAB_SETTLE = 0.25  # seconds for the status row to redraw after Shift+Tab
MENU_SETTLE = 0.3  # seconds between selecting a plan option and typing feedback
MODE_OBSERVE_TIMEOUT = 2.0  # seconds permission_summary waits for the status row / transcript record
EXIT_CONFIRM_POLLS = 2  # exit signs must hold for this many consecutive polls
QUIET_IDLE_POLLS = 30  # 100 ms polls: three seconds of a quiet input box counts as idle
NO_CHANGE_TEXT = "I sent that, but nothing changed on screen."
TIMEOUT_DROPPED_TEXT = "The session thread was busy and that command was dropped; say it again."
TIMEOUT_RUNNING_TEXT = (
    "That is taking longer than usual; the agent may still carry it out, so wait before repeating it."
)


def stall_text(agent_name: str) -> str:
    return f"{agent_name} looks like it is waiting on something. The last lines were: "


def no_tui_text(agent_name: str) -> str:
    return f"{agent_name} isn't on screen in that pane, so I won't type into it. Resume the session first."


def scope_reason(path: str, scope_dir: str, agent_home: Path) -> str | None:
    """None when ``path`` may be edited; else the reason to deny (decision 0018)."""
    if not path:
        return "Zordon could not tell which file this edits; edits are limited to the project folder"
    try:
        raw = os.path.expanduser(path) if path.startswith("~") else path
        if not os.path.isabs(raw):
            raw = os.path.join(scope_dir, raw)  # Claude Code resolves relative paths against the cwd
        candidate = os.path.realpath(raw)
    except (OSError, ValueError):
        return "Zordon could not resolve this path; edits are limited to the project folder"
    allowed = [os.path.realpath(scope_dir), os.path.realpath(str(agent_home))]
    for root in allowed:
        if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
            return None
    return f"This project is limited to {scope_dir}; {path} is outside it. Zordon blocked the edit."


def no_modes_text(agent_name: str) -> str:
    return f"{agent_name} has no permission modes Zordon can switch."


# The Claude Code wording, kept as constants for callers that match on it.
STALL_TEXT = stall_text("Claude Code")
NO_TUI_TEXT = no_tui_text("Claude Code")
# A shell waiting for input: "user@host:~/dir$ ", "host% ", "# ", "(venv) user@host:~$ "...
_SHELL_PROMPT_RE = re.compile(
    r"^(?:\(\S+\)\s*)?(?:\S+@\S+:\S*|\S+:\S+|~\S*|/\S*|[A-Za-z][\w.-]*)?\s*[$#%]\s*$"
)


class SessionError(RuntimeError):
    """A control method could not do what was asked; the message is speakable."""


class SessionBusy(SessionError):
    """The session is running in a terminal Zordon cannot attach to."""


class UnknownSession(SessionError):
    pass


class CommandTimeout(SessionError):
    """The session thread did not run the command in time; ``executed`` says whether it may
    still run (``None``: it had already started and may complete) or was dropped (``False``)."""

    def __init__(self, message: str, *, executed: bool | None) -> None:
        super().__init__(message)
        self.executed = executed


def looks_like_shell_prompt(lines: list[str]) -> bool:
    """Fallback for ``Screen.shell_prompt``: is the last non-blank line a bare shell prompt?"""
    for line in reversed(lines):
        if line.strip():
            return bool(_SHELL_PROMPT_RE.match(line.rstrip()))
    return False


@dataclass(slots=True)
class SessionSummary:
    """One row for the picker (``transport.ws.to_session_summary`` reads these fields)."""

    session_id: str
    directory: str
    title: str
    last_active: float | None
    attached: bool
    running: bool
    state: SessionState
    permission_mode: str | None = None
    focused: bool = False
    agent: str = DEFAULT_AGENT


@dataclass
class Session:
    session_id: str
    cwd: str
    target: str
    adapter: AgentAdapter
    agent: str = DEFAULT_AGENT
    transcript: TranscriptSource | None = None  # the adapter's clean transcript, once found
    state: SessionState = SessionState.WORKING
    detail: str = "starting"
    prev_screen: Screen | None = None
    last_output_ts: float = 0.0
    current_prompt: PromptDetected | None = None
    current_match: PromptMatch | None = None
    permission_mode: str | None = None
    attached: bool = True
    focused: bool = False
    registry_status: str | None = None
    # bookkeeping
    title: str = ""
    owned: bool = True  # Zordon opened the window (may kill it); False for registry / attached panes
    settings_path: Path | None = None  # the first of ``settings_paths`` (the agent's settings file)
    settings_paths: list[Path] = field(default_factory=list)  # removed when the pane goes
    info: AgentSessionInfo | None = None  # discovery record for a resumed/attached session
    started_at: float = 0.0
    last_active: float = 0.0
    pane_tail: deque[str] = field(default_factory=lambda: deque(maxlen=PANE_TAIL_LINES))
    prompt_key: tuple[Any, ...] | None = None
    stall_notified: bool = False
    exit_notified: bool = False
    onboarding_stage: str | None = None  # first-run screen currently showing (theme/login)
    onboarding_notified: bool = False
    theme_enter_sent: bool = False
    hook_hint: Any | None = None  # the adapter's hook hint (``session.hooks.HookHint`` for Claude Code)
    believed_prompt: PromptKind | None = None  # second/third-signal prompt the regex cannot see
    quiet_polls: int = 0  # consecutive polls with a quiet input box and unchanged content
    echo_deadline: float | None = None
    echo_signature: tuple[Any, ...] | None = None
    registry_checked: float = 0.0
    spinner_tokens: str | None = None
    held_flushed: str | None = None
    seen_alternate: bool = False
    normal_signature: int | None = None
    exit_polls: int = 0  # consecutive polls that looked like the agent is gone
    quiet_first_poll: bool = False  # attached to a pane that already has content: do not speak it
    project_id: str | None = None  # the project this session belongs to (decision 0018)
    bypass_allowed: bool = False  # launched for a project created in bypass mode: its warning may be accepted
    scope_dir: str | None = None  # file edits outside this directory (and ~/.claude) are denied by the hook
    hook_prompt: hookprompts.HookPrompt | None = None  # a PermissionRequest waiting for the user (decision 0019)
    composing: bool = False  # text typed into the input box and not yet submitted (deferred submit)

    @property
    def jsonl(self) -> TranscriptSource | None:
        """Older name for ``transcript`` (the Claude Code source is a jsonl tail)."""
        return self.transcript

    @jsonl.setter
    def jsonl(self, value: TranscriptSource | None) -> None:
        self.transcript = value

    @property
    def jsonl_from_start(self) -> bool:
        """A session Zordon started is replayed from the start; a resumed/attached one from the end."""
        return self.info is None

    @property
    def display_name(self) -> str:
        return self.adapter.info.display_name


class SessionManager(threading.Thread):
    """The SessionThread. Implements ``SessionControl``."""

    def __init__(
        self,
        bus: Bus,
        config: Config,
        tmux: Tmux | None = None,
        *,
        claude_home: Path | None = None,
        zordon_home: Path | None = None,
        hook_port: int | None = None,
        hook_secret: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        tmux_session: str = TMUX_SESSION,
        adapters: dict[str, AgentAdapter] | None = None,
        default_agent: str | None = None,
    ) -> None:
        super().__init__(name="SessionThread", daemon=True)
        self.bus = bus
        self.config = config
        self.tmux = tmux or Tmux()
        self.claude_home = Path(claude_home) if claude_home else paths.claude_home()
        self.zordon_home = Path(zordon_home) if zordon_home else paths.zordon_home()
        self.hook_port = hook_port
        self.hook_secret = hook_secret
        self.clock = clock
        self.tmux_session = tmux_session
        self.default_agent = (default_agent or getattr(config.providers, "agent", None) or DEFAULT_AGENT).strip().lower()
        self.adapters: dict[str, AgentAdapter] = dict(adapters) if adapters else self._build_adapters()
        if self.default_agent not in self.adapters:
            raise ValueError(f"default agent {self.default_agent!r} has no adapter; known: {sorted(self.adapters)}")
        for adapter in self.adapters.values():
            bind = getattr(adapter, "bind", None)
            if callable(bind):
                bind(tmux=self.tmux, zordon_home=self.zordon_home, claude_home=self.claude_home)
        self.sessions: dict[str, Session] = {}
        self.projects = ProjectStore(self.zordon_home / "projects.json")
        self._lock = threading.RLock()
        self._commands: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], Future[Any]] | None] = (
            queue.Queue()
        )
        self._stop_event = threading.Event()
        self._focused: str | None = None
        self._refocused = False  # focus moved automatically; publish a Sessions snapshot
        # Called after an automatic refocus (pane died, session deleted or exited) so
        # every client's picker learns the new focus. The default builds the
        # transport's Sessions snapshot; the agent may replace it.
        self.publish_sessions: Callable[[], None] | None = None
        self.polls = 0

    def _build_adapters(self) -> dict[str, AgentAdapter]:
        """Every registered adapter that imports; a missing optional one is skipped."""
        out: dict[str, AgentAdapter] = {}
        for key in ADAPTERS:
            try:
                out[key] = get_adapter(key, self.config)
            except ImportError as e:
                log.debug("agent adapter %s not present: %s", key, e)
            except Exception as e:  # noqa: BLE001 - one broken adapter must not take the others down
                log.warning("agent adapter %s unavailable: %s", key, e)
        return out

    def adapter_for(self, agent: str | None) -> AgentAdapter:
        key = (agent or self.default_agent).strip().lower()
        adapter = self.adapters.get(key)
        if adapter is None:
            raise SessionError(f"I don't know an agent called {agent!r}; known: {', '.join(sorted(self.adapters))}.")
        return adapter

    # ---- thread ---------------------------------------------------------------------

    @property
    def poll_interval(self) -> float:
        return max(0.01, self.config.output.poll_interval_ms / 1000.0)

    def run(self) -> None:
        log.info("session thread started (poll every %d ms)", self.config.output.poll_interval_ms)
        while not self._stop_event.is_set() and not self.bus.stop.is_set():
            deadline = time.monotonic() + self.poll_interval
            self._drain_commands(deadline)
            if self._stop_event.is_set() or self.bus.stop.is_set():
                break
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the poll loop must never die
                log.exception("poll failed")
        self._drain_commands(time.monotonic())
        log.info("session thread stopped")

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop_event.set()
        self._commands.put(None)
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def _drain_commands(self, deadline: float) -> None:
        """Run queued commands; block until the first arrives or ``deadline`` passes."""
        wait = max(0.0, deadline - time.monotonic())
        try:
            item = self._commands.get(timeout=wait)
        except queue.Empty:
            return
        while True:
            if item is not None:
                fn, args, fut = item
                if fut.set_running_or_notify_cancel():  # False: the caller gave up; never run it
                    try:
                        fut.set_result(fn(*args))
                    except BaseException as e:  # noqa: BLE001 - hand every failure to the caller
                        fut.set_exception(e)
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return

    def _call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None) -> Any:
        """Run ``fn`` on the session thread and wait for its result.

        On timeout a command that has not started yet is cancelled, so a retry
        never types the same keystrokes twice; one that is already running cannot
        be stopped, and the ``CommandTimeout`` (and a spoken Notice) say so.
        """
        if not self.is_alive() or threading.current_thread() is self:
            return fn(*args)
        if timeout is None:
            timeout = COMMAND_TIMEOUT
        fut: Future[Any] = Future()
        self._commands.put((fn, args, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            dropped = fut.cancel()
            text = TIMEOUT_DROPPED_TEXT if dropped else TIMEOUT_RUNNING_TEXT
            log.warning("%s timed out after %.1fs (%s)", getattr(fn, "__name__", "command"), timeout,
                        "dropped" if dropped else "still running")
            self.bus.publish(Notice(text=text, level="warning", speak=True))
            raise CommandTimeout(text, executed=False if dropped else None) from None

    def wake(self) -> None:
        """Make the thread poll now instead of at the next tick."""
        self._commands.put((lambda: None, (), Future()))

    # ---- polling ---------------------------------------------------------------------

    def poll_once(self) -> None:
        """One tick: poll every attached session. Public so tests can drive it."""
        self.polls += 1
        with self._lock:
            targets = [s for s in self.sessions.values() if s.attached]
        for s in targets:
            try:
                self._poll_session(s)
            except TmuxError as e:
                log.warning("poll of %s failed: %s", s.session_id[:8], e)
            except Exception:  # noqa: BLE001
                log.exception("poll of %s failed", s.session_id[:8])

    def _poll_session(self, s: Session) -> None:
        now = self.clock()
        sid = s.session_id
        if not self.tmux.pane_exists(s.target):
            self._apply(s, Observation(False, None, False, False, False, 0.0), now, None)
            return

        self._locate_transcript(s)
        jsonl_lines = self._poll_transcript(s)
        alt = self.tmux.alternate_on(s.target)
        lines = self.tmux.capture(s.target)
        adapter = s.adapter
        screen = adapter.parse(lines)

        if adapter.uses_alternate_screen():
            if not alt:
                self._poll_normal_screen(s, screen, jsonl_lines, now)
                return
            s.seen_alternate = True
            s.exit_polls = 0
        elif adapter.exited(screen):
            # A plain-screen agent: a shell prompt at the bottom means it is gone.
            s.exit_polls += 1
            if s.exit_polls >= EXIT_CONFIRM_POLLS:
                log.info("%s: %s is gone (shell prompt)", sid[:8], s.display_name)
                self._mark_exited(s, now)
                return
        else:
            s.exit_polls = 0
        prev = s.prev_screen
        new_lines = diff_screens(prev, screen)
        if s.quiet_first_poll:
            s.quiet_first_poll = False
            if prev is None:
                new_lines = []  # what was on the pane before Zordon arrived is history
        if s.held_flushed is not None:
            if new_lines and new_lines[0] == s.held_flushed:
                new_lines = new_lines[1:]
            s.held_flushed = None
        content_changed = prev is None or prev.content_key != screen.content_key
        tokens = screen.spinner.tokens if screen.spinner else None
        token_progress = tokens is not None and tokens != s.spinner_tokens
        s.spinner_tokens = tokens
        s.prev_screen = screen
        self._remember_tail(s, new_lines)
        if new_lines and self._pane_source_active(s):
            for line in new_lines:
                self.bus.pane_lines.put(PaneLine(session_id=sid, text=line, source="pane"))
        mode = adapter.permission_mode_from_screen(screen)
        if mode:
            s.permission_mode = mode

        output_advanced = content_changed or token_progress or bool(jsonl_lines)
        if output_advanced:
            s.last_output_ts = now
        since = max(0.0, now - s.last_output_ts)
        match = adapter.detect_prompt(screen)
        if match is None and s.hook_prompt is not None and s.hook_prompt.pending:
            # Claude Code draws nothing while its PermissionRequest hook waits on us; the
            # hook payload is the prompt (decision 0019).
            match = s.hook_prompt.match
        self._check_echo(s, screen, now)

        watchdog = float(self.config.voice.idle_watchdog_seconds)
        idle = adapter.is_idle(screen)
        # Stability rule: a quiet input box with unchanged content for a few polls is idle
        # even when no completion row is visible (showTurnDuration off, unknown wording).
        if adapter.input_quiet(screen) and not content_changed and not jsonl_lines:
            s.quiet_polls += 1
        else:
            s.quiet_polls = 0
        if not idle and s.quiet_polls >= QUIET_IDLE_POLLS and match is None and s.believed_prompt is None:
            idle = True
        spinning = adapter.is_working(screen)
        working = spinning and since < watchdog
        score, override = self._second_opinions(s, match, now)
        if s.believed_prompt is not None and (match is not None or idle or spinning or content_changed):
            s.believed_prompt = None  # the screen moved on: the regex is in charge again
        obs = Observation(
            pane_alive=True,
            prompt_kind=match.kind if match else s.believed_prompt,
            idle=idle,
            working=working,
            output_advanced=output_advanced,
            seconds_since_output=since,
            prompt_score=score,
        )
        self._apply(s, obs, now, match, watchdog=0.0 if override else watchdog, screen=screen)

    def _poll_normal_screen(self, s: Session, screen: Screen, jsonl_lines: int, now: float) -> None:
        """The pane is not on the alternate screen: shell, trust dialog or the exit line.

        The agent is treated as gone when its exit line is on screen, or (after
        ``EXIT_CONFIRM_POLLS`` consecutive polls) when the pane shows a shell prompt
        or has left the alternate screen it was on before, with no trust dialog up.
        Without this a crashed or never-started agent leaves a bare shell that
        would receive voice text as commands.
        """
        sig = hash(tuple(screen.lines))
        changed = sig != s.normal_signature
        s.normal_signature = sig
        if changed or jsonl_lines:
            s.last_output_ts = now
        self._check_echo(s, screen, now)
        s.prev_screen = screen
        stage = s.adapter.onboarding(screen)
        if stage is not None:
            self._handle_onboarding(s, stage, now)
            return
        s.onboarding_stage = None
        if s.adapter.exited(screen):
            self._mark_exited(s, now)
            return
        match = s.adapter.detect_prompt(screen)
        if match is not None and match.kind is not PromptKind.TRUST:
            match = None  # only the trust dialog lives on the normal screen
        shell = bool(getattr(screen, "shell_prompt", False)) or looks_like_shell_prompt(screen.lines)
        if match is None and (shell or s.seen_alternate):
            s.exit_polls += 1
            if s.exit_polls >= EXIT_CONFIRM_POLLS:
                log.info(
                    "%s: %s is gone (%s)", s.session_id[:8], s.display_name,
                    "shell prompt" if shell else "left the alternate screen",
                )
                self._mark_exited(s, now)
                return
        else:
            s.exit_polls = 0
        since = max(0.0, now - s.last_output_ts)
        obs = Observation(
            pane_alive=True,
            prompt_kind=match.kind if match else None,
            idle=False,
            working=False,
            output_advanced=changed,
            seconds_since_output=since,
        )
        self._apply(s, obs, now, match, screen=screen)

    def _handle_onboarding(self, s: Session, stage: str, now: float) -> None:
        """The agent's first-run screens live on the normal screen. Theme: accept the default.
        Login: cannot be done for the user; say exactly where to do it and wait."""
        s.exit_polls = 0
        s.last_output_ts = now
        changed = stage != s.onboarding_stage
        s.onboarding_stage = stage
        if stage == "theme":
            if not s.theme_enter_sent:
                s.theme_enter_sent = True
                try:
                    self.tmux.send_enter(s.target)
                    log.info("%s: accepted %s's default text style", s.session_id[:8], s.display_name)
                except TmuxError:
                    log.debug("could not send Enter to the theme picker", exc_info=True)
            return
        detail = f"{s.display_name} needs its first-time login"
        if changed:
            with self._lock:
                s.state = SessionState.STALLED
                s.detail = detail
            self.bus.publish(StateChanged(s.session_id, SessionState.STALLED, detail))
        if not s.onboarding_notified:
            s.onboarding_notified = True
            where = f"tmux attach -t {self.tmux_session}" if s.owned else "the terminal where it runs"
            self.bus.publish(
                Notice(
                    text=(
                        f"{s.display_name} is installed but not logged in. In a terminal run: {where}; "
                        "pick your login method and finish signing in, then press Ctrl-b then d to detach. "
                        "I'll pick up the session as soon as it is ready."
                    ),
                    level="warning",
                    session_id=s.session_id,
                    speak=True,
                )
            )

    def _mark_exited(self, s: Session, now: float) -> None:
        with self._lock:
            self._clear_prompt(s)
            changed = s.state is not SessionState.DETACHED
            s.state = SessionState.DETACHED
            s.detail = (
                f"{s.display_name} exited; resume to start it again"
                if s.adapter.supports_resume()
                else f"{s.display_name} exited; attach again when it is back"
            )
            s.attached = False
            s.exit_polls = 0
            if self._focused == s.session_id:
                self._set_focus(self._next_focus(exclude=s.session_id), auto=True)
        if changed:
            self.bus.publish(StateChanged(s.session_id, SessionState.DETACHED, s.detail))
            self._publish_session_closed(s.session_id)
        self._flush_refocus()
        if not s.exit_notified:
            s.exit_notified = True
            what_next = (
                "Say resume, or pick the session again, to start it back up."
                if s.adapter.supports_resume()
                else "Nothing is running in that pane now. Start the agent there first (for Claude Code: run `claude`), then attach to the pane again."
            )
            self.bus.publish(
                Notice(
                    text=f"{s.display_name} exited. {what_next}",
                    level="warning",
                    session_id=s.session_id,
                    speak=True,
                )
            )
        self._remove_settings(s)

    def _second_opinions(self, s: Session, match: PromptMatch | None, now: float) -> tuple[float, bool]:
        """(prompt_score, watchdog_override) from the hook hint and the registry status."""
        score = 0.0
        override = False
        hint = s.hook_hint
        if hint is not None and hint.active:
            hint.consume()
            if hint.kind == "prompt" and match is None:
                score = max(score, float(getattr(hint, "score", HOOK_HINT_SCORE)))
                override = True
            if not hint.active:
                s.hook_hint = None
        if now - s.registry_checked >= REGISTRY_INTERVAL:
            s.registry_checked = now
            s.registry_status = self._status_hint(s)
        if match is None and s.registry_status == "waiting":
            score = max(score, REGISTRY_WAITING_SCORE)
        return score, override

    def _status_hint(self, s: Session) -> str | None:
        """The adapter's out-of-band status (Claude Code: the registry ``status``)."""
        try:
            return s.adapter.status_hint(s.session_id)
        except Exception as e:  # noqa: BLE001
            log.debug("status hint failed: %s", e)
            return None

    def _apply(
        self,
        s: Session,
        obs: Observation,
        now: float,
        match: PromptMatch | None,
        *,
        watchdog: float | None = None,
        screen: Screen | None = None,
    ) -> None:
        """Feed one observation to the state machine and publish what changed."""
        if watchdog is None:
            watchdog = float(self.config.voice.idle_watchdog_seconds)
        sid = s.session_id
        events: list[Any] = []
        with self._lock:
            new_state, detail = next_state(s.state, obs, watchdog)
            if obs.pane_alive:
                events += self._update_prompt(s, match)
            else:
                events += self._clear_prompt(s)
            if new_state is not s.state:
                log.info("%s: %s -> %s (%s)", sid[:8], s.state.value, new_state.value, detail)
                s.state = new_state
                s.detail = detail
                events.append(StateChanged(sid, new_state, detail))
                if new_state is not SessionState.STALLED:
                    s.stall_notified = False
                if new_state is SessionState.AWAITING_PERMISSION and match is None and s.believed_prompt is None:
                    # Reached through the hook or registry signal: hold the belief until the
                    # screen changes (next_state alone would read "prompt gone" next poll).
                    s.believed_prompt = PromptKind.PERMISSION
                    events.append(self._unreadable_prompt_notice(s))
            if obs.output_advanced or match is not None:
                s.last_active = time.time()
            if new_state is SessionState.STALLED and not s.stall_notified:
                s.stall_notified = True
                events += self._stall_events(s, screen)
            if not obs.pane_alive:
                s.attached = False
                if self._focused == sid:
                    self._set_focus(self._next_focus(exclude=sid), auto=True)
                self._remove_settings(s)
        for ev in events:
            if isinstance(ev, PaneLine):
                self.bus.pane_lines.put(ev)
            else:
                self.bus.publish(ev)
        if not obs.pane_alive:
            self._publish_session_closed(sid)
            self._flush_refocus()

    def _unreadable_prompt_notice(self, s: Session) -> Notice:
        hint = s.hook_hint
        lead = (hint.message if hint and hint.message else f"{s.display_name} looks like it is waiting for permission")
        tail = " ".join(self.last_pane_lines(s.session_id, 10)) or "nothing readable"
        return Notice(
            text=f"{lead}, but I can't read the prompt. The last lines were: {tail}",
            level="warning",
            session_id=s.session_id,
            speak=True,
        )

    def _stall_events(self, s: Session, screen: Screen | None) -> list[Any]:
        events: list[Any] = []
        if screen is not None and self._pane_source_active(s):
            for line in held_tail(screen):
                s.held_flushed = line
                self._remember_tail(s, [line])
                events.append(PaneLine(session_id=s.session_id, text=line, source="pane"))
        tail = self.last_pane_lines(s.session_id, 10)
        events.append(
            Notice(
                text=stall_text(s.display_name) + (" ".join(tail) if tail else "nothing readable"),
                level="warning",
                session_id=s.session_id,
                speak=True,
            )
        )
        return events

    def _update_prompt(self, s: Session, match: PromptMatch | None) -> list[Any]:
        """PromptDetected / PromptCleared events for this poll (caller holds the lock)."""
        if match is None:
            return self._clear_prompt(s)
        key = (match.kind, match.title, match.question, tuple(match.labels))
        s.current_match = match
        if s.prompt_key == key and s.current_prompt is not None:
            return []
        events = self._clear_prompt(s)
        s.current_match = match
        s.prompt_key = key
        s.current_prompt = PromptDetected(
            session_id=s.session_id,
            kind=match.kind,
            title=match.title,
            options=list(match.labels),
            raw_lines=list(match.raw_lines),
        )
        events.append(s.current_prompt)
        log.info("%s: %s prompt: %s", s.session_id[:8], match.kind.value, redact(match.title)[0])
        return events

    def _clear_prompt(self, s: Session) -> list[Any]:
        if s.current_prompt is None:
            s.current_match = None
            s.prompt_key = None
            return []
        ev = PromptCleared(session_id=s.session_id, prompt_id=s.current_prompt.prompt_id)
        s.current_prompt = None
        s.current_match = None
        s.prompt_key = None
        return [ev]

    def _check_echo(self, s: Session, screen: Screen, now: float) -> None:
        """After send_text: did the screen change within ECHO_TIMEOUT?"""
        if s.echo_deadline is None:
            return
        if _signature(screen) != s.echo_signature:
            s.echo_deadline = None
            s.echo_signature = None
            return
        if now >= s.echo_deadline:
            s.echo_deadline = None
            s.echo_signature = None
            self.bus.publish(Notice(text=NO_CHANGE_TEXT, level="warning", session_id=s.session_id, speak=True))

    def _pane_source_active(self, s: Session) -> bool:
        src = self.config.output.source
        return src == "pane" or (src == "auto" and s.transcript is None)

    def _transcript_source_active(self, s: Session) -> bool:
        return self.config.output.source in ("auto", "jsonl") and s.transcript is not None

    def _locate_transcript(self, s: Session) -> None:
        """Ask the adapter for its clean transcript until it has one (the file may not exist yet)."""
        if s.transcript is not None:
            return
        try:
            src = s.adapter.transcript_source(s.session_id, s.cwd, s.info)
        except Exception as e:  # noqa: BLE001 - a broken transcript must not stop the pane feed
            log.debug("%s: transcript source failed: %s", s.session_id[:8], e)
            return
        if src is None:
            return
        s.transcript = src
        log.info("%s: following %s", s.session_id[:8], getattr(src, "path", type(src).__name__))

    def _poll_transcript(self, s: Session) -> int:
        """Publish new transcript events as PaneLines; returns how many were seen."""
        if s.transcript is None:
            return 0
        lines = s.transcript.poll()
        if not lines:
            return 0
        publish = self._transcript_source_active(s)
        for line in lines:
            if line.block == "permission_mode" and line.text:
                s.permission_mode = line.text
            if publish:
                self.bus.pane_lines.put(line)
        return len(lines)

    def _remember_tail(self, s: Session, lines: list[str]) -> None:
        for line in lines:
            if line.strip():
                s.pane_tail.append(line)

    # ---- SessionControl: reads -----------------------------------------------------------

    def list_sessions(self) -> list[SessionSummary]:
        """Live sessions (started, resumed or attached) plus every adapter's resumable ones."""
        with self._lock:
            live = {sid: self._summary(s) for sid, s in self.sessions.items()}
        rows: list[SessionSummary] = list(live.values())
        for key, adapter in self.adapters.items():
            try:
                if adapter.available() is None:
                    continue  # not installed: nothing on disk to list
                found = adapter.list_sessions()
            except Exception:  # noqa: BLE001 - a broken store must not break the picker
                log.exception("session discovery failed for %s", key)
                continue
            for info in found:
                if info.session_id in live:
                    row = live[info.session_id]
                    if row.permission_mode is None:
                        row.permission_mode = info.permission_mode
                    continue
                rows.append(
                    SessionSummary(
                        session_id=info.session_id,
                        directory=info.cwd,
                        title=info.title or info.session_id[:8],
                        last_active=info.last_active or None,
                        attached=False,
                        running=bool(info.extra.get("running")) or info.running_pid is not None,
                        state=SessionState.DETACHED,
                        permission_mode=info.permission_mode,
                        agent=info.agent or key,
                    )
                )
        rows.sort(key=lambda r: (r.attached, r.last_active or 0.0), reverse=True)
        return rows

    def _summary(self, s: Session) -> SessionSummary:
        return SessionSummary(
            session_id=s.session_id,
            directory=s.cwd,
            title=s.title or os.path.basename(s.cwd.rstrip("/")) or s.session_id[:8],
            last_active=s.last_active or s.started_at or None,
            attached=s.attached,
            running=s.attached and s.state is not SessionState.DETACHED,
            state=s.state,
            permission_mode=s.permission_mode,
            focused=s.session_id == self._focused,
            agent=s.agent,
        )

    def focused(self) -> str | None:
        with self._lock:
            return self._focused

    def state_of(self, session_id: str) -> SessionState:
        with self._lock:
            s = self.sessions.get(session_id)
            return s.state if s else SessionState.DETACHED

    def current_prompt(self, session_id: str) -> PromptDetected | None:
        with self._lock:
            s = self.sessions.get(session_id)
            return s.current_prompt if s else None

    def current_match(self, session_id: str) -> PromptMatch | None:
        """The parsed prompt (with ``unsafe`` flags), for cards that need more than labels."""
        with self._lock:
            s = self.sessions.get(session_id)
            return s.current_match if s else None

    def last_pane_lines(self, session_id: str, n: int = 10) -> list[str]:
        with self._lock:
            s = self.sessions.get(session_id)
            if s is None:
                return []
            source = [line for line in (s.prev_screen.content if s.prev_screen else []) if line.strip()]
            if not source:
                source = list(s.pane_tail)
            return [line.strip() for line in source[-n:]] if n > 0 else []

    def permission_summary(self, session_id: str) -> str:
        """One sentence about the mode the pane is really in and the configured rules.

        The mode comes from the status row or the transcript's permission-mode
        record. When neither has been seen yet the call waits up to
        ``MODE_OBSERVE_TIMEOUT`` for the poll loop to observe one (never when
        called from the session thread itself); if it still is not known the
        sentence says so rather than naming the agent's built-in default.
        """
        with self._lock:
            s = self.sessions.get(session_id)
            cwd = s.cwd if s else ""
            active = s.permission_mode if s else None
            adapter = s.adapter if s else self.adapter_for(None)
        if active is None and s is not None and s.attached:
            active = self._wait_for_mode(s)
        if active is None and s is not None and s.bypass_allowed:
            # Launched for a bypass project: the mode is known from the launch, whatever
            # the status row shows (Claude Code's bypass dialog may still be up).
            active = BYPASS_MODE
        return adapter.permission_summary(cwd, active)

    def _wait_for_mode(self, s: Session) -> str | None:
        if not self.is_alive() or threading.current_thread() is self:
            return s.permission_mode
        deadline = time.monotonic() + MODE_OBSERVE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self._lock:
                if s.permission_mode is not None or not s.attached:
                    return s.permission_mode
        return s.permission_mode

    # ---- SessionControl: lifecycle --------------------------------------------------------

    def focus(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self.sessions:
                raise UnknownSession(f"no session {session_id[:8]}")
            self._set_focus(session_id)

    def start_thread(self) -> None:
        """Start the poll loop (``threading.Thread.start``)."""
        threading.Thread.start(self)

    def start(
        self,
        directory: str | None = None,
        permission_mode: str | None = None,
        agent: str | None = None,
    ) -> str | None:  # type: ignore[override]
        """``start(directory, mode, agent)`` opens a new session and returns its id.

        Without arguments it starts the thread itself, so an agent that calls
        ``.start()`` on every worker thread keeps working; ``start_thread`` is
        the explicit spelling.
        """
        if directory is None:
            self.start_thread()
            return None
        return self._call(self._do_start, directory, permission_mode, agent, timeout=10.0)

    def _do_start(self, directory: str, permission_mode: str | None, agent: str | None = None, project: Project | None = None) -> str:
        adapter = self.adapter_for(agent)
        cwd = str(Path(directory).expanduser())
        if not os.path.isdir(cwd):
            raise SessionError(f"{directory} is not a directory")
        bypass = project is not None and project.bypass
        if bypass:
            permission_mode = BYPASS_MODE
        else:
            permission_mode = adapter.normalize_mode(permission_mode) if permission_mode else self._launch_mode(adapter)
        sid = str(uuid.uuid4())
        scope = project is not None and project.scope_edits
        try:
            spec = adapter.new_session(sid, cwd, permission_mode, self._hook_request(sid, scope=scope), allow_bypass=bypass)
        except (NotImplementedError, ValueError) as e:
            raise SessionError(str(e)) from e
        target = self._open_pane(cwd, sid, spec)
        self._register(sid, cwd, target, spec, adapter, owned=True, info=None, detail="starting", project=project)
        log.info("started %s session %s in %s (%s)", adapter.info.key, sid[:8], cwd, target)
        return sid

    def _launch_mode(self, adapter: AgentAdapter) -> str | None:
        """The mode for a session the client starts without naming one: the configured
        ``sessions.permission_mode`` when this agent has it, else the adapter's own."""
        want = str(getattr(getattr(self.config, "sessions", None), "permission_mode", "") or "")
        if want and want in adapter.allowed_modes():
            return want
        return adapter.default_launch_mode()

    def resume(self, session_id: str, permission_mode: str | None = None, agent: str | None = None) -> None:
        self._call(self._do_resume, session_id, permission_mode, agent, timeout=10.0)

    def _do_resume(self, session_id: str, permission_mode: str | None, agent: str | None = None, project: Project | None = None) -> None:
        with self._lock:
            existing = self.sessions.get(session_id)
            if existing is not None and existing.attached and existing.state is not SessionState.DETACHED:
                self._set_focus(session_id)
                if project is not None:
                    existing.project_id = project.id
                return
        adapter = existing.adapter if existing is not None and agent is None else self.adapter_for(agent)
        info = adapter.find_session(session_id)
        if info is None and agent is None and existing is None:
            # The id may belong to another installed agent's store.
            for other in self.adapters.values():
                if other is adapter or other.available() is None:
                    continue
                info = other.find_session(session_id)
                if info is not None:
                    adapter = other
                    break
        if info is not None and (info.running_pid is not None or info.extra.get("running")):
            target = info.tmux_target
            if target and self.tmux.pane_exists(target):
                self._register(
                    session_id,
                    info.cwd or (existing.cwd if existing else ""),
                    target,
                    None,
                    adapter,
                    owned=False,
                    info=info,
                    detail="attached to a running pane",
                    project=project,
                )
                log.info("attached to running session %s at %s", session_id[:8], target)
                return
            raise SessionBusy(
                "That session is already running in another terminal. Close it there first, "
                "or start a new session."
            )
        if not adapter.supports_resume():
            raise SessionError(f"{adapter.info.display_name} sessions can't be resumed; attach to its pane instead.")
        cwd = (info.cwd if info and info.cwd else None) or (existing.cwd if existing else None)
        if not cwd or not os.path.isdir(cwd):
            raise UnknownSession("I can't find that session's project directory.")
        bypass = project is not None and project.bypass
        if bypass:
            permission_mode = BYPASS_MODE
        else:
            permission_mode = adapter.normalize_mode(permission_mode) if permission_mode else self._launch_mode(adapter)
        scope = project is not None and project.scope_edits
        if existing is not None and existing.owned and existing.target:
            try:
                if self.tmux.pane_exists(existing.target):
                    self.tmux.kill_window(existing.target)
            except TmuxError as e:
                log.debug("old pane for %s not killed: %s", session_id[:8], e)
        try:
            spec = adapter.resume_session(
                session_id, cwd, permission_mode, self._hook_request(session_id, scope=scope), allow_bypass=bypass
            )
        except (NotImplementedError, ValueError) as e:
            raise SessionError(str(e)) from e
        if info is None:
            info = AgentSessionInfo(agent=adapter.info.key, session_id=session_id, cwd=cwd)
        target = self._open_pane(cwd, session_id, spec)
        self._register(session_id, cwd, target, spec, adapter, owned=True, info=info, detail="resuming", project=project)
        log.info("resumed %s session %s in %s (%s)", adapter.info.key, session_id[:8], cwd, target)

    def attach(self, target: str, agent: str = "generic", cwd: str | None = None) -> str:
        """Follow an existing tmux pane with ``agent``'s adapter; returns the new session id."""
        return self._call(self._do_attach, target, agent, cwd, timeout=10.0)

    def _do_attach(self, target: str, agent: str | None, cwd: str | None) -> str:
        adapter = self.adapter_for(agent or "generic")
        target = (target or "").strip()
        if not target:
            raise SessionError("Which pane? Give a tmux target like session:window.pane.")
        try:
            if not self.tmux.pane_exists(target):
                raise UnknownSession(f"There is no tmux pane {target}.")
        except TmuxError as e:
            raise SessionError(f"I can't reach tmux: {e}") from e
        with self._lock:
            for s in self.sessions.values():
                if s.target == target and s.attached:
                    self._set_focus(s.session_id)
                    return s.session_id
        if not cwd:
            cwd = self._pane_cwd(target) or ""
        cwd = str(Path(cwd).expanduser()) if cwd else ""
        sid = str(uuid.uuid4())
        info = AgentSessionInfo(agent=adapter.info.key, session_id=sid, cwd=cwd, tmux_target=target)
        self._register(sid, cwd, target, None, adapter, owned=False, info=info, detail="attached to a pane")
        log.info("attached %s adapter to pane %s as %s", adapter.info.key, target, sid[:8])
        return sid

    def _pane_cwd(self, target: str) -> str | None:
        getter = getattr(self.tmux, "pane_cwd", None)
        if callable(getter):
            try:
                return getter(target) or None
            except TmuxError:
                return None
        try:
            for pane in self.tmux.list_panes():
                if pane.target == target:
                    return pane.cwd or None
        except (TmuxError, AttributeError):
            pass
        return None

    def _hook_request(self, sid: str, *, scope: bool = False) -> HookRequest | None:
        """What an adapter with out-of-band prompt signals needs; None when hooks are off."""
        if not self.hook_port or not self.hook_secret:
            return None
        return HookRequest(
            port=int(self.hook_port),
            secret=self.hook_secret,
            host=self.config.server.bind,
            zordon_home=self.zordon_home,
            session_id=sid,
            scope=scope,
        )

    def _open_pane(self, cwd: str, sid: str, spec: LaunchSpec) -> str:
        """Open the pane in the ``zordon`` tmux session. ``spec.command`` comes from the
        adapter, which is the only place an agent's argv is made.

        ``Tmux.new_window`` creates the session when it does not exist, with this
        window as its first, so no idle shell window is left behind; it also scrubs
        the session environment of provider keys and the agent's nesting markers.
        """
        name = (os.path.basename(cwd.rstrip("/")) or sid[:8])[:20]
        return self.tmux.new_window(self.tmux_session, name, spec.cwd or cwd, spec.command, PANE_WIDTH, PANE_HEIGHT)

    def _register(
        self,
        sid: str,
        cwd: str,
        target: str,
        spec: LaunchSpec | None,
        adapter: AgentAdapter,
        *,
        owned: bool,
        info: AgentSessionInfo | None,
        detail: str,
        project: Project | None = None,
    ) -> None:
        now = self.clock()
        settings_paths = list(spec.settings_paths) if spec is not None else []
        if project is None:
            project = self.projects.by_session(sid) or (self.projects.by_directory(cwd) if cwd else None)
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                s = Session(session_id=sid, cwd=cwd, target=target, adapter=adapter, agent=adapter.info.key)
                self.sessions[sid] = s
            else:
                s.cwd = cwd or s.cwd
                s.target = target
                s.adapter = adapter
                s.agent = adapter.info.key
                s.transcript = None
                s.prev_screen = None
                s.pane_tail.clear()
                s.seen_alternate = False
                s.normal_signature = None
                s.exit_polls = 0
                s.exit_notified = False
                s.stall_notified = False
                s.hook_hint = None
                s.believed_prompt = None
                s.echo_deadline = None
                self._clear_prompt(s)
            s.state = SessionState.WORKING
            s.detail = detail
            s.attached = True
            s.owned = owned
            s.quiet_first_poll = not owned
            s.settings_paths = settings_paths
            s.settings_path = settings_paths[0] if settings_paths else None
            s.info = info
            s.started_at = s.started_at or time.time()
            s.last_active = time.time()
            s.last_output_ts = now
            s.title = os.path.basename(cwd.rstrip("/")) if cwd else sid[:8]
            if project is not None:
                s.project_id = project.id
                s.bypass_allowed = project.bypass
                s.scope_dir = project.directory if project.scope_edits else None
                s.title = project.name or s.title
            if self._focused is None or self._focused not in self.sessions:
                self._set_focus(sid)
            else:
                s.focused = sid == self._focused
        if project is not None:
            try:
                self.projects.update(project, session_id=sid, tmux_target=target, last_used_at=time.time())
            except OSError as e:
                log.warning("could not save projects: %s", e)
        self.bus.publish(StateChanged(sid, SessionState.WORKING, detail))

    def _remove_settings(self, s: Session) -> None:
        """Delete the per-session files the adapter's LaunchSpec asked to clean up."""
        for path in s.settings_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as e:
                log.debug("could not remove %s: %s", path, e)
        s.settings_paths = []
        s.settings_path = None

    def detach(self, session_id: str) -> None:
        self._call(self._do_detach, session_id)

    def _do_detach(self, session_id: str) -> None:
        with self._lock:
            s = self._get(session_id)
            events = self._clear_prompt(s)
            s.attached = False
            s.state = SessionState.DETACHED
            s.detail = "detached; the pane keeps running"
            s.exit_polls = 0
            if self._focused == session_id:
                self._set_focus(self._next_focus(exclude=session_id), auto=True)
        for ev in events:
            self.bus.publish(ev)
        self.bus.publish(StateChanged(session_id, SessionState.DETACHED, s.detail))
        self._publish_session_closed(session_id)
        self._flush_refocus()

    def delete(self, session_id: str) -> None:
        self._call(self._do_delete, session_id)

    def _do_delete(self, session_id: str) -> None:
        with self._lock:
            s = self._get(session_id)
            events = self._clear_prompt(s)
            del self.sessions[session_id]
            if self._focused == session_id:
                self._set_focus(self._next_focus(exclude=session_id), auto=True)
        try:
            if self.tmux.pane_exists(s.target):
                self.tmux.kill_window(s.target)
        except TmuxError as e:
            log.warning("could not kill pane %s: %s", s.target, e)
        self._remove_settings(s)
        for ev in events:
            self.bus.publish(ev)
        self.bus.publish(StateChanged(session_id, SessionState.DETACHED, "pane killed"))
        self._publish_session_closed(session_id)
        self._flush_refocus()
        log.info("deleted session %s (%s)", session_id[:8], s.target)

    def _next_focus(self, exclude: str) -> str | None:
        for sid, s in self.sessions.items():
            if sid != exclude and s.attached:
                return sid
        return None

    def _set_focus(self, session_id: str | None, *, auto: bool = False) -> None:
        """Move focus (caller holds the lock). ``auto`` marks a refocus the user did not ask
        for, which ``_flush_refocus`` turns into a Sessions snapshot once the lock is released."""
        if auto and session_id != self._focused:
            self._refocused = True
        self._focused = session_id
        for sid, s in self.sessions.items():
            s.focused = sid == session_id

    def _flush_refocus(self) -> None:
        """Publish a Sessions snapshot after an automatic refocus (never under the lock)."""
        with self._lock:
            if not self._refocused:
                return
            self._refocused = False
        try:
            if self.publish_sessions is not None:
                self.publish_sessions()
            else:
                self._publish_sessions_snapshot()
        except Exception:  # noqa: BLE001 - a picker refresh must never break the poll loop
            log.exception("could not publish the sessions snapshot")

    def _publish_sessions_snapshot(self) -> None:
        """Default ``publish_sessions``: the transport's ``Sessions`` message with the new focus."""
        # Imported here so the session package never depends on the transport at import time.
        from zordon.transport.protocol import Sessions
        from zordon.transport.ws import to_session_summary

        focused = self.focused()
        rows = [to_session_summary(r, focused) for r in self.list_sessions()]
        self.bus.publish(Sessions(sessions=rows))

    def _publish_session_closed(self, session_id: str) -> None:
        """Tell the pipeline the session's output stream ended (detach, delete, exit).

        A ``turn_end`` marker makes the pipeline flush its per-session buffers now,
        so a trailing sentence from before the detach is never spoken later as the
        summary of a turn in a resumed session.
        """
        self.bus.pane_lines.put(
            PaneLine(
                session_id=session_id,
                text="",
                source="jsonl",
                block="turn_end",
                meta={"reason": "session_closed"},
            )
        )

    def _get(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise UnknownSession(f"no session {session_id[:8]}")
        return s

    def _live(self, session_id: str) -> Session:
        s = self._get(session_id)
        if not s.attached or s.state is SessionState.DETACHED:
            raise SessionError("That session is detached. Resume it first.")
        return s

    def _require_tui(self, s: Session, m: PromptMatch | None = None) -> None:
        """Refuse keystrokes unless the agent is really on the pane.

        For an alternate-screen agent (Claude Code) that means the alternate
        screen is on; the trust dialog is the one prompt drawn on the normal
        screen, so it is allowed when it is the current prompt. Anything else off
        the alternate screen is a shell or a dead agent, where text would run as
        a command. A plain-screen agent is checked with ``adapter.exited`` on a
        fresh capture instead.
        """
        if not s.adapter.uses_alternate_screen():
            try:
                screen = s.adapter.parse(self.tmux.capture(s.target))
            except TmuxError as e:
                raise SessionError("I can't reach that pane right now.") from e
            if not s.adapter.exited(screen):
                return
        else:
            try:
                if self.tmux.alternate_on(s.target):
                    return
            except TmuxError as e:
                raise SessionError("I can't reach that pane right now.") from e
            current = m if m is not None else s.current_match
            if current is not None and current.kind is PromptKind.TRUST:
                return
        if s.onboarding_stage in ("login", "login_browser"):
            text = (
                f"{s.display_name} is waiting for you to log in before it can take requests. "
                f"Run `tmux attach -t {self.tmux_session}` in a terminal and finish signing in."
            )
        else:
            text = no_tui_text(s.display_name)
        log.warning("%s: refusing keystrokes; %s is not on the pane", s.session_id[:8], s.display_name)
        self.bus.publish(Notice(text=text, level="warning", session_id=s.session_id, speak=True))
        raise SessionError(text)

    # ---- SessionControl: keystrokes -------------------------------------------------------

    def send_text(self, session_id: str, text: str) -> None:
        self._call(self._do_send_text, session_id, text)

    def _do_send_text(self, session_id: str, text: str) -> None:
        s = self._live(session_id)
        clean = strip_control(text).strip()
        if not clean:
            self.bus.publish(
                Notice(text="There was nothing to send.", level="warning", session_id=session_id, speak=True)
            )
            return
        self._require_tui(s)
        screen = s.prev_screen
        s.echo_signature = _signature(screen) if screen is not None else None
        s.echo_deadline = self.clock() + ECHO_TIMEOUT
        self.tmux.send_literal(s.target, clean)
        self.tmux.send_enter(s.target)
        log.debug("%s: sent %d characters", session_id[:8], len(clean))

    # ---- deferred submit (decision 0019) ----------------------------------------------------

    def compose(self, session_id: str, text: str) -> None:
        """Type ``text`` into the input box without Enter; ``submit`` sends it later."""
        self._call(self._do_compose, session_id, text)

    def _do_compose(self, session_id: str, text: str) -> None:
        s = self._live(session_id)
        clean = strip_control(text).strip()
        if not clean:
            return
        self._require_tui(s)
        self.tmux.send_literal(s.target, (" " if s.composing else "") + clean)
        s.composing = True
        log.debug("%s: composed %d characters", session_id[:8], len(clean))

    def submit(self, session_id: str) -> bool:
        """Press Enter on what ``compose`` typed. False when nothing was composed."""
        return self._call(self._do_submit, session_id)

    def _do_submit(self, session_id: str) -> bool:
        s = self._live(session_id)
        if not s.composing:
            return False
        self._require_tui(s)
        screen = s.prev_screen
        s.echo_signature = _signature(screen) if screen is not None else None
        s.echo_deadline = self.clock() + ECHO_TIMEOUT
        self.tmux.send_enter(s.target)
        s.composing = False
        return True

    def clear_input(self, session_id: str) -> bool:
        """Erase what ``compose`` typed (Ctrl-U clears Claude Code's input box)."""
        return self._call(self._do_clear_input, session_id)

    def _do_clear_input(self, session_id: str) -> bool:
        s = self._live(session_id)
        if not s.composing:
            return False
        self._require_tui(s)
        self.tmux.send_key(s.target, "C-u")
        s.composing = False
        return True

    def is_composing(self, session_id: str) -> bool:
        with self._lock:
            s = self.sessions.get(session_id)
        return bool(s and s.composing)

    def send_escape(self, session_id: str) -> None:
        self._call(self._do_send_key, session_id, "Escape")

    def _do_send_key(self, session_id: str, key: str) -> None:
        s = self._live(session_id)
        self._require_tui(s)
        self.tmux.send_key(s.target, key)

    def approve(self, session_id: str) -> bool:
        return self._call(self._do_approve, session_id)

    def _do_approve(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PERMISSION, PromptKind.TRUST)
        if m is None:
            return False
        if m.kind is PromptKind.TRUST:
            return self._do_accept_trust(session_id)
        if hookprompts.is_hook(m):
            self._resolve_hook(s, hookprompts.allow(), "allowed by voice")
            return True
        yes = s.adapter.yes_option(m)
        if yes is None:
            log.warning("%s: no plain Yes option; not approving", session_id[:8])
            return False
        chosen = m.option(yes)
        if chosen is None or chosen.unsafe or _widens(chosen.label):
            return False
        if m.extra.get("inline_yn"):
            self._answer_inline(s, m, "y")
            return True
        self._select(s, m, yes)
        return True

    def deny(self, session_id: str) -> bool:
        return self._call(self._do_deny, session_id)

    def _do_deny(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PERMISSION, PromptKind.TRUST)
        if m is None:
            return False
        if m.kind is PromptKind.TRUST:
            return self._do_decline_trust(session_id)
        if hookprompts.is_hook(m):
            self._resolve_hook(s, hookprompts.deny("The user said no."), "denied by voice")
            return True
        no = s.adapter.no_option(m)
        if no is None:
            self._require_tui(s, m)
            self.tmux.send_key(s.target, "Escape")
            return True
        if m.extra.get("inline_yn"):
            self._answer_inline(s, m, "n")
            return True
        self._select(s, m, no)
        return True

    def plan_approve(self, session_id: str) -> bool:
        return self._call(self._do_plan_approve, session_id)

    def _do_plan_approve(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PLAN)
        if m is None:
            return False
        if hookprompts.is_hook(m):
            self._resolve_hook(s, hookprompts.allow(), "plan approved by voice")
            return True
        manual = s.adapter.plan_approve_option(m)
        if manual is None:
            return False
        chosen = m.option(manual)
        if chosen is None or chosen.unsafe:
            return False
        self._select(s, m, manual)
        return True

    def plan_revise(self, session_id: str, feedback: str) -> bool:
        return self._call(self._do_plan_revise, session_id, feedback, timeout=COMMAND_TIMEOUT + MENU_SETTLE)

    def _do_plan_revise(self, session_id: str, feedback: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PLAN)
        if m is None:
            return False
        if hookprompts.is_hook(m):
            note = strip_control(feedback).strip() or "Please revise the plan."
            self._resolve_hook(s, hookprompts.deny(f"The user wants changes to the plan: {note}"), "plan sent back by voice")
            return True
        revise = s.adapter.plan_revise_option(m)
        if revise is None:
            return False
        self._select(s, m, revise)
        if strip_control(feedback).strip():
            time.sleep(MENU_SETTLE)
            self._do_send_text(session_id, feedback)
        return True

    def plan_deny(self, session_id: str) -> bool:
        return self._call(self._do_plan_deny, session_id)

    def _do_plan_deny(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PLAN)
        if m is None:
            return False
        if hookprompts.is_hook(m):
            self._resolve_hook(s, hookprompts.deny("The user rejected the plan."), "plan rejected by voice")
            return True
        self._require_tui(s, m)
        self.tmux.send_key(s.target, "Escape")
        return True

    def answer_question(self, session_id: str, option: int | str) -> bool:
        return self._call(self._do_answer_question, session_id, option)

    def _do_answer_question(self, session_id: str, option: int | str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.QUESTION)
        if m is None:
            return False
        idx = s.adapter.question_option(m, option)
        if idx is None:
            return False
        if hookprompts.is_hook(m):
            return self._answer_hook_question(s, m, idx)
        self._select(s, m, idx)
        return True

    def _answer_hook_question(self, s: Session, m: PromptMatch, idx: int) -> bool:
        """Record the chosen option; move to the next question or answer the hook."""
        hp = s.hook_prompt
        if hp is None or not hp.pending:
            return False
        chosen = m.option(idx)
        if chosen is None:
            return False
        hp.answers[m.question] = chosen.label
        hp.question_index += 1
        if hp.question_index < len(hp.questions):
            hp.match = hookprompts.question_match(hp.questions[hp.question_index], hp.question_index + 1, len(hp.questions))
            with self._lock:
                events = self._update_prompt(s, hp.match)
            for ev in events:
                self.bus.publish(ev)
            return True
        self._resolve_hook(s, hookprompts.allow(hookprompts.answers_input(hp)), "answered by voice")
        return True

    def accept_trust(self, session_id: str) -> bool:
        return self._call(self._do_accept_trust, session_id)

    def _do_accept_trust(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.TRUST)
        if m is None:
            return False
        if m.extra.get("dialog") == "bypass" and not s.bypass_allowed:
            # The warning appeared in a session that was not launched for a bypass project
            # (someone started claude that way in an attached pane). Zordon never accepts
            # it on the user's behalf there; the Escape path below leaves the dialog alone.
            self.bus.publish(
                Notice(
                    text=(
                        f"{s.display_name} is asking to run without permission checks, but this project was not set up "
                        "for that. Say no to exit, or create the project in bypass mode."
                    ),
                    level="warning",
                    session_id=session_id,
                    speak=True,
                )
            )
            return False
        yes = s.adapter.trust_accept_option(m)
        if yes is None:
            return False
        self._select(s, m, yes)
        return True

    def decline_trust(self, session_id: str) -> bool:
        return self._call(self._do_decline_trust, session_id)

    def _do_decline_trust(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.TRUST)
        if m is None:
            return False
        no = s.adapter.trust_decline_option(m)
        if no is None:
            self._require_tui(s, m)
            self.tmux.send_key(s.target, "Escape")
        else:
            self._select(s, m, no)
        with self._lock:
            events = self._clear_prompt(s)
            s.state = SessionState.DETACHED
            s.detail = f"trust declined; {s.display_name} exited"
        for ev in events:
            self.bus.publish(ev)
        self.bus.publish(StateChanged(session_id, SessionState.DETACHED, s.detail))
        self._publish_session_closed(session_id)
        return True

    def _prompt_for(self, session_id: str, *kinds: PromptKind) -> tuple[Session, PromptMatch | None]:
        with self._lock:
            s = self._live(session_id)
            m = s.current_match
        if m is None or m.kind not in kinds:
            return s, None
        return s, m

    def _select(self, s: Session, m: PromptMatch, index: int) -> None:
        """Move the pointer from the selected option to ``index`` and press Enter.

        A lettered menu (``PromptMatch.extra["key<n>"]``, generic adapter) gets
        the letter typed instead, with no Enter: such tools act on the key.
        """
        self._require_tui(s, m)
        letter = m.extra.get(f"key{index}")
        if letter:
            self.tmux.send_literal(s.target, letter)
            log.info("%s: typed %r for option %d (%s)", s.session_id[:8], letter, index, _label(m, index))
            return
        selected = m.selected.index if m.selected else 1
        steps = index - selected
        key = "Down" if steps > 0 else "Up"
        for _ in range(abs(steps)):
            self.tmux.send_key(s.target, key)
        self.tmux.send_key(s.target, "Enter")
        log.info("%s: selected option %d (%s)", s.session_id[:8], index, _label(m, index))

    def _answer_inline(self, s: Session, m: PromptMatch, answer: str) -> None:
        """An inline ``(y/n)`` prompt: type the single letter, then Enter."""
        self._require_tui(s, m)
        self.tmux.send_literal(s.target, answer)
        self.tmux.send_enter(s.target)
        log.info("%s: answered inline prompt with %r", s.session_id[:8], answer)

    # ---- PermissionRequest hook (decision 0019) -------------------------------------------------

    def permission_request(self, payload: dict[str, Any], *, timeout_s: float = 870.0) -> dict[str, Any]:
        """Called from the transport's executor thread with the hook payload; blocks until the
        user answers (through approve/deny/answer_question/plan_*) or ``timeout_s`` passes.
        Returns the decision for the hook to print, or ``{}`` for "no opinion"."""
        hp = self._call(self._do_hook_prompt, payload, timeout=10.0)
        if hp is None:
            return hookprompts.NO_OPINION
        hp.event.wait(timeout_s)
        if hp.pending:
            self._call(self._do_hook_timeout, hp, timeout=10.0)
            return hookprompts.NO_OPINION
        return hp.decision or hookprompts.NO_OPINION

    def _hook_session(self, payload: dict[str, Any]) -> Session | None:
        sid = str(payload.get("session_id") or "")
        cwd = str(payload.get("cwd") or "")
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                for cand in self.sessions.values():
                    info_sid = getattr(cand.info, "session_id", None) if cand.info is not None else None
                    if info_sid == sid:
                        s = cand
                        break
            if s is None and cwd:
                s = next((c for c in self.sessions.values() if c.attached and c.cwd and os.path.realpath(c.cwd) == os.path.realpath(cwd)), None)
        return s if s is not None and s.attached else None

    def _do_hook_prompt(self, payload: dict[str, Any]) -> hookprompts.HookPrompt | None:
        s = self._hook_session(payload)
        if s is None:
            log.info("permission hook for an unknown session %s; no opinion", str(payload.get("session_id") or "?")[:8])
            return None
        hp = hookprompts.build(payload)
        if hp is None:
            return None
        if s.hook_prompt is not None and s.hook_prompt.pending:
            # One at a time: a second request while the first waits gets no opinion.
            log.warning("%s: a second permission request arrived while one is pending; no opinion", s.session_id[:8])
            return None
        mode = payload.get("permission_mode")
        if isinstance(mode, str) and mode:
            s.permission_mode = mode
        state = {
            PromptKind.PERMISSION: SessionState.AWAITING_PERMISSION,
            PromptKind.QUESTION: SessionState.AWAITING_QUESTION,
            PromptKind.PLAN: SessionState.AWAITING_PLAN_APPROVAL,
        }[hp.match.kind]
        with self._lock:
            events = self._update_prompt(s, hp.match)
            s.believed_prompt = None
            if s.state is not state:
                s.state = state
                s.detail = f"{hp.match.kind.value} request through the hook"
                events.append(StateChanged(s.session_id, state, s.detail))
            s.last_active = time.time()
            s.hook_prompt = hp  # last: a lock-free reader that sees it also sees the state
        log.info("%s: hook %s for %s", s.session_id[:8], hp.match.kind.value, hp.tool_name)
        for ev in events:
            self.bus.publish(ev)
        return hp

    def _resolve_hook(self, s: Session, decision: dict[str, Any], detail: str) -> None:
        hp = s.hook_prompt
        if hp is None or not hp.pending:
            return
        hp.decision = decision
        with self._lock:
            s.hook_prompt = None
            events = self._clear_prompt(s)
            s.state = SessionState.WORKING
            s.detail = detail
            s.last_active = time.time()
            s.last_output_ts = self.clock()
            events.append(StateChanged(s.session_id, SessionState.WORKING, detail))
        hp.event.set()
        log.info("%s: hook %s -> %s", s.session_id[:8], hp.tool_name, detail)
        for ev in events:
            self.bus.publish(ev)

    def _do_hook_timeout(self, hp: hookprompts.HookPrompt) -> None:
        """The user never answered: drop the prompt; Claude Code draws its dialog next."""
        with self._lock:
            s = next((c for c in self.sessions.values() if c.hook_prompt is hp), None)
            if s is None:
                return
            s.hook_prompt = None
            events = self._clear_prompt(s)
        hp.event.set()
        for ev in events:
            self.bus.publish(ev)

    # ---- projects (decision 0018) ------------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        """Every project with its live state, most recently used first."""
        return self._call(self._do_list_projects, timeout=5.0)

    def _do_list_projects(self) -> list[dict[str, Any]]:
        out = []
        with self._lock:
            live = {s.project_id: s for s in self.sessions.values() if s.project_id and s.attached}
        for p in self.projects.list():
            s = live.get(p.id)
            running = s is not None and s.state is not SessionState.DETACHED
            if not running and p.tmux_target:
                try:
                    running = self.tmux.pane_exists(p.tmux_target)
                except TmuxError:
                    running = False
            out.append(self._project_row(p, s, running))
        return out

    def _project_row(self, p: Project, s: Session | None, running: bool) -> dict[str, Any]:
        return {
            "id": p.id,
            "name": p.name,
            "directory": p.directory,
            "agent": p.agent,
            "permission_mode": p.permission_mode,
            "scope_edits": p.scope_edits,
            "running": running,
            "session_id": s.session_id if s is not None else None,
            "focused": s is not None and s.focused,
            "state": s.state.value if s is not None else None,
            "last_used": p.last_used_at,
            "exists": os.path.isdir(p.directory),
        }

    def create_project(
        self,
        parent: str,
        name: str,
        *,
        agent: str | None = None,
        permission_mode: str = "default",
        scope_edits: bool = True,
        existing: bool = False,
    ) -> dict[str, Any]:
        """Make the folder (or take an existing one), record the project, open it, focus it."""
        return self._call(self._do_create_project, parent, name, agent, permission_mode, scope_edits, existing, timeout=15.0)

    def _do_create_project(
        self, parent: str, name: str, agent: str | None, permission_mode: str, scope_edits: bool, existing: bool
    ) -> dict[str, Any]:
        agent_key = (agent or self.default_agent).strip().lower()
        if agent_key not in self.adapters:
            raise SessionError(f"unknown agent {agent!r}")
        if existing:
            directory = str(Path(parent).expanduser().resolve())
            if not inside_home(directory):
                raise SessionError("Projects live inside your home directory.")
            if not os.path.isdir(directory):
                raise SessionError(f"{directory} is not a folder.")
            if self.projects.by_directory(directory) is not None:
                raise SessionError("That folder is already a project; continue it instead.")
            display = (name or "").strip() or os.path.basename(directory.rstrip("/"))
        else:
            try:
                directory = create_directory(parent, name)
            except ProjectError as e:
                raise SessionError(str(e)) from e
            display = name.strip()
        if permission_mode == BYPASS_MODE and agent_key != "claude-code":
            raise SessionError(f"{self.adapters[agent_key].info.display_name} projects cannot run without approvals yet.")
        try:
            project = new_project(directory, name=display, agent=agent_key, permission_mode=permission_mode, scope_edits=scope_edits)
        except ProjectError as e:
            raise SessionError(str(e)) from e
        self.projects.add(project)
        log.info("project %s created at %s (%s, %s)", project.name, directory, agent_key, permission_mode)
        try:
            self._do_open_project(project.id)
        except SessionError:
            self.projects.remove(project.id)
            raise
        s = self.sessions.get(project.session_id or "")
        return self._project_row(project, s, s is not None)

    def open_project(self, project_id: str) -> dict[str, Any]:
        """Continue a project: reconnect to its pane when it still runs, else resume its last
        session, else start a fresh one in its folder. Focuses it."""
        return self._call(self._do_open_project, project_id, timeout=15.0)

    def _do_open_project(self, project_id: str) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            raise UnknownSession("I don't know that project.")
        if not os.path.isdir(project.directory):
            raise SessionError(f"The folder {project.directory} is gone. Forget the project or restore the folder.")
        adapter = self.adapters.get(project.agent) or self.adapter_for(None)
        # 1. a session of ours that is still attached
        with self._lock:
            for s in self.sessions.values():
                if s.project_id == project.id and s.attached and s.state is not SessionState.DETACHED:
                    self._set_focus(s.session_id)
                    self.projects.touch(project)
                    return self._project_row(project, s, True)
        # 2. the pane it last ran in, still alive and still running the agent
        if project.tmux_target:
            alive = False
            try:
                alive = self.tmux.pane_exists(project.tmux_target)
                if alive and self._pane_is_bare_shell(adapter, project.tmux_target):
                    alive = False
                    if project.tmux_target.startswith(f"{self.tmux_session}:"):
                        # Our own window with nothing but a shell in it: close it rather than
                        # leave a dead window per restart.
                        self.tmux.kill_window(project.tmux_target)
            except TmuxError:
                alive = False
            if alive:
                sid = project.session_id or str(uuid.uuid4())
                info = AgentSessionInfo(agent=adapter.info.key, session_id=sid, cwd=project.directory, tmux_target=project.tmux_target)
                self._register(sid, project.directory, project.tmux_target, None, adapter, owned=False, info=info, detail="reconnected", project=project)
                log.info("project %s: reconnected to %s", project.name, project.tmux_target)
                return self._project_row(project, self.sessions[sid], True)
        # 3. resume the last conversation, if the agent still has it
        if project.session_id and adapter.supports_resume() and adapter.find_session(project.session_id) is not None:
            try:
                self._do_resume(project.session_id, project.permission_mode, project.agent, project=project)
                s = self.sessions.get(project.session_id)
                return self._project_row(project, s, s is not None)
            except (SessionBusy, UnknownSession, SessionError) as e:
                log.info("project %s: resume failed (%s); starting fresh", project.name, e)
        # 4. a new conversation in the folder
        sid = self._do_start(project.directory, project.permission_mode, project.agent, project=project)
        return self._project_row(project, self.sessions.get(sid), True)

    def _pane_is_bare_shell(self, adapter: AgentAdapter, target: str) -> bool:
        """True when the pane shows a shell prompt or the agent's exit line (the agent is gone)."""
        try:
            lines = self.tmux.capture(target)
        except TmuxError:
            return True
        screen = adapter.parse(lines)
        if adapter.exited(screen):
            return True
        if adapter.uses_alternate_screen():
            try:
                if self.tmux.alternate_on(target):
                    return False
            except TmuxError:
                return True
            return looks_like_shell_prompt(screen.lines) or bool(getattr(screen, "shell_prompt", False))
        return False

    def admin(self) -> None:
        """Leave work mode: nothing focused, every pane keeps running (decision 0018)."""
        self._call(self._do_admin, timeout=5.0)

    def _do_admin(self) -> None:
        with self._lock:
            self._set_focus(None, auto=True)
        self._flush_refocus()

    def forget_project(self, project_id: str) -> bool:
        """Drop the project record. Files and any running pane are left alone."""
        return self._call(self._do_forget_project, project_id, timeout=5.0)

    def _do_forget_project(self, project_id: str) -> bool:
        with self._lock:
            for s in self.sessions.values():
                if s.project_id == project_id:
                    s.project_id = None
        return self.projects.remove(project_id)

    def project_for_session(self, session_id: str) -> Project | None:
        with self._lock:
            s = self.sessions.get(session_id)
        if s is None or not s.project_id:
            return None
        return self.projects.get(s.project_id)

    def scope_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Answer a PreToolUse scope hook: deny a file edit outside the project directory.

        The payload is Claude Code's hook input (``session_id``, ``tool_name``,
        ``tool_input``). Allowed: anything under the project directory, and the
        agent's own home (``~/.claude``, where its scratchpad lives). Everything
        else, including a path the hook cannot interpret, is denied: this hook only
        ever narrows. An empty answer means "no opinion" (Claude Code decides as usual).
        """
        sid = str(payload.get("session_id") or "")
        with self._lock:
            s = self.sessions.get(sid)
            scope = s.scope_dir if s is not None else None
        if s is None:
            # Unknown session: find the project by the pane's cwd if Claude Code sent it.
            cwd = str(payload.get("cwd") or "")
            p = self.projects.by_directory(cwd) if cwd else None
            scope = p.directory if p is not None and p.scope_edits else None
        if not scope:
            return {}
        tool_input = payload.get("tool_input") or {}
        path = ""
        if isinstance(tool_input, dict):
            path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
        reason = scope_reason(path, scope, self.claude_home)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    # ---- SessionControl: permission mode --------------------------------------------------------

    def set_permission_mode(self, session_id: str, mode: str) -> bool:
        with self._lock:
            s = self.sessions.get(session_id)
            adapter = s.adapter if s is not None else self.adapter_for(None)
        if adapter.mode_cycle_key() is None:
            self.bus.publish(
                Notice(text=no_modes_text(adapter.info.display_name), level="warning", session_id=session_id, speak=True)
            )
            return False
        target = adapter.normalize_mode(mode)
        return self._call(self._do_set_permission_mode, session_id, target, timeout=10.0)

    def _do_set_permission_mode(self, session_id: str, target: str) -> bool:
        s = self._live(session_id)
        adapter = s.adapter
        key = adapter.mode_cycle_key()
        if key is None:
            return False
        screen = adapter.parse(self.tmux.capture(s.target))
        if adapter.detect_prompt(screen) is not None or screen.input_box is None:
            log.info("%s: not switching mode while a prompt is up", session_id[:8])
            return False
        self._require_tui(s)
        start = adapter.permission_mode_from_screen(screen)
        if start is None:
            return False
        forbidden = adapter.forbidden_modes()
        current = start
        bypass_steps = 0
        stuck = 0
        reason = "the cycle never reached it"
        for _ in range(MAX_BTAB_PRESSES):
            if current == target:
                s.permission_mode = current
                return True
            previous = current
            current = self._press_cycle_key(s, key)
            if current in forbidden:
                bypass_steps += 1
                if bypass_steps > MAX_BYPASS_STEPS:
                    reason = "bypass permissions keeps showing"
                    break
                log.warning("%s: bypass mode showed in the cycle; stepping past it", session_id[:8])
                continue
            if current is None or current == previous:
                stuck += 1
                if stuck >= MAX_STUCK_READS:
                    reason = "the status row did not change"
                    break
                continue
            stuck = 0
            if current == start and current != target:
                break  # a full cycle without the target: it is not reachable from here
        if current == target:
            s.permission_mode = current
            return True
        if stuck < MAX_STUCK_READS:
            current = self._return_to(s, key, start, current)
        if current is not None:
            s.permission_mode = current
        label = adapter.mode_label
        where = (
            f"the pane is back in {label(current)} mode"
            if current == start
            else f"the pane shows {label(current) if current else 'an unknown'} mode now"
        )
        self.bus.publish(
            Notice(
                text=f"I couldn't switch to {label(target)} mode from here: {reason}; {where}.",
                level="warning",
                session_id=session_id,
                speak=True,
            )
        )
        return False

    def _press_cycle_key(self, s: Session, key: str) -> str | None:
        """One press of the adapter's mode key, then the mode the status row shows after it settles."""
        self.tmux.send_key(s.target, key)
        time.sleep(BTAB_SETTLE)
        return s.adapter.permission_mode_from_screen(s.adapter.parse(self.tmux.capture(s.target)))

    def _return_to(self, s: Session, key: str, start: str, current: str | None) -> str | None:
        """Press the mode key (bounded) until the status row shows ``start`` again, skipping forbidden modes."""
        forbidden = s.adapter.forbidden_modes()
        for _ in range(MAX_BTAB_PRESSES):
            if current == start:
                return current
            previous = current
            current = self._press_cycle_key(s, key)
            if current is None or (current == previous and current not in forbidden):
                break  # not reacting: stop pressing keys into a pane we cannot read
        if current != start:
            log.warning("%s: could not return the mode to %s (now %s)", s.session_id[:8], start, current)
        return current

    # ---- SessionControl: hooks --------------------------------------------------------------------

    def hook_event(self, payload: dict[str, Any]) -> None:
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(sid, str) or not sid:
            log.debug("ignoring malformed hook payload")
            return
        with self._lock:
            s = self.sessions.get(sid)
        if s is None:
            log.debug("hook event for unknown session %s", sid[:8])
            return
        hint = s.adapter.hook_hint(payload)
        if hint is None:
            log.debug("%s: hook payload ignored by the %s adapter", sid[:8], s.agent)
            return
        with self._lock:
            s.hook_hint = hint
        log.info("%s: hook %s/%s", sid[:8], payload.get("hook_event_name"), getattr(hint, "notification_type", ""))
        self.wake()


# ---- helpers ------------------------------------------------------------------------------------


def _signature(screen: Screen | None) -> tuple[Any, ...]:
    if screen is None:
        return ()
    return (
        screen.content_key,
        screen.input_box.text if screen.input_box else None,
        len(screen.prompt_block),
        screen.spinner is not None,
    )


def _label(m: PromptMatch, index: int) -> str:
    o = m.option(index)
    return o.label if o else "?"


_WIDENING = re.compile(r"always|don'?t ask|do not ask|for this session|auto[- ]?mode|switch to|all future|yolo", re.I)


def _widens(label: str) -> bool:
    """A yes-option label that widens permissions; never selected by ``approve``."""
    return bool(_WIDENING.search(label))
