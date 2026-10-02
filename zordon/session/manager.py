"""SessionThread: owns the tmux panes, the jsonl tails and the session state.

One ``SessionManager`` thread polls every attached session each
``config.output.poll_interval_ms``:

1. ``tmux.pane_exists`` / ``alternate_on``: a missing pane is DETACHED; the
   normal screen is only looked at for the trust dialog and for signs that Claude
   Code is gone (the exit line, a shell prompt, or having left the alternate
   screen), which make the session DETACHED after one confirming poll;
2. ``capture`` -> ``screen.parse_screen`` -> ``screen.diff_screens``: new content
   lines go to ``bus.pane_lines`` with ``source="pane"`` when the session has no
   jsonl (or ``output.source == "pane"``);
3. ``prompts.detect_prompt`` on the parsed screen: ``PromptDetected`` once per
   distinct prompt, ``PromptCleared`` when it leaves the screen;
4. ``jsonl.poll()`` -> ``to_pane_lines`` -> ``bus.pane_lines`` with
   ``source="jsonl"`` in ``auto`` / ``jsonl`` mode;
5. ``state.next_state`` over an ``Observation`` built from the above, plus the
   second (Notification hook) and third (registry ``status``) prompt signals;
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
ever uses ``Up``/``Down``/``Enter``/``Escape``/``BTab`` from the allowlist. No
keystroke is sent while the pane is off the alternate screen (a shell would run
the text as a command) unless the prompt on screen is the trust dialog.
``approve`` selects only the option labelled exactly ``Yes``; ``plan_approve``
never selects the auto-mode option; ``set_permission_mode`` refuses
``bypassPermissions``, steps past it a bounded number of times if it shows, and
returns to the starting mode when the target is never reached.

Launch: ``start``/``resume`` without an explicit mode pass ``--permission-mode
default`` so a pane never comes up in Claude Code's built-in default (``auto`` in
2.1.x). ``permission_summary`` speaks the mode that was actually observed (status
row or jsonl record), waiting briefly for it instead of assuming one.
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
from zordon.session import discovery, hooks, permissions, prompts
from zordon.session.jsonl import JsonlTail, to_pane_lines
from zordon.session.prompts import PromptMatch
from zordon.session.screen import Screen, diff_screens, held_tail, parse_screen
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
PANE_TAIL_LINES = 200
MAX_BTAB_PRESSES = 6
MAX_BYPASS_STEPS = 2  # how often bypass may show in one switch before giving up
MAX_STUCK_READS = 2  # status row unchanged after this many presses: the TUI is not reacting
BTAB_SETTLE = 0.25  # seconds for the status row to redraw after Shift+Tab
MENU_SETTLE = 0.3  # seconds between selecting a plan option and typing feedback
DEFAULT_LAUNCH_MODE = "default"  # start/resume without an explicit mode never inherit auto
MODE_OBSERVE_TIMEOUT = 2.0  # seconds permission_summary waits for the status row / jsonl record
EXIT_CONFIRM_POLLS = 2  # normal-screen exit signs must hold for this many consecutive polls
QUIET_IDLE_POLLS = 30  # 100 ms polls: three seconds of a quiet input box counts as idle
STALL_TEXT = "Claude Code looks like it is waiting on something. The last lines were: "
NO_CHANGE_TEXT = "I sent that, but nothing changed on screen."
NO_TUI_TEXT = (
    "Claude Code isn't on screen in that pane, so I won't type into it. Resume the session first."
)
TIMEOUT_DROPPED_TEXT = "The session thread was busy and that command was dropped; say it again."
TIMEOUT_RUNNING_TEXT = (
    "That is taking longer than usual; Claude Code may still carry it out, so wait before repeating it."
)
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


@dataclass
class Session:
    session_id: str
    cwd: str
    target: str
    jsonl: JsonlTail | None = None
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
    owned: bool = True  # Zordon opened the window (may kill it); False for registry panes
    settings_path: Path | None = None
    jsonl_path: Path | None = None
    jsonl_from_start: bool = True
    started_at: float = 0.0
    last_active: float = 0.0
    pane_tail: deque[str] = field(default_factory=lambda: deque(maxlen=PANE_TAIL_LINES))
    prompt_key: tuple[Any, ...] | None = None
    stall_notified: bool = False
    exit_notified: bool = False
    hook_hint: hooks.HookHint | None = None
    believed_prompt: PromptKind | None = None  # second/third-signal prompt the regex cannot see
    quiet_polls: int = 0  # consecutive polls with a quiet input box and unchanged content
    echo_deadline: float | None = None
    echo_signature: tuple[Any, ...] | None = None
    registry_checked: float = 0.0
    spinner_tokens: str | None = None
    held_flushed: str | None = None
    seen_alternate: bool = False
    normal_signature: int | None = None
    exit_polls: int = 0  # consecutive normal-screen polls that looked like Claude Code is gone


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
        self.sessions: dict[str, Session] = {}
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

        self._locate_jsonl(s)
        jsonl_lines = self._poll_jsonl(s)
        alt = self.tmux.alternate_on(s.target)
        lines = self.tmux.capture(s.target)
        screen = parse_screen(lines)

        if not alt:
            self._poll_normal_screen(s, screen, jsonl_lines, now)
            return

        s.seen_alternate = True
        s.exit_polls = 0
        prev = s.prev_screen
        new_lines = diff_screens(prev, screen)
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
        mode = prompts.permission_mode_from_screen(screen)
        if mode:
            s.permission_mode = mode

        output_advanced = content_changed or token_progress or bool(jsonl_lines)
        if output_advanced:
            s.last_output_ts = now
        since = max(0.0, now - s.last_output_ts)
        match = prompts.detect_prompt(screen)
        self._check_echo(s, screen, now)

        watchdog = float(self.config.voice.idle_watchdog_seconds)
        idle = prompts.is_idle_prompt(screen)
        # Stability rule: a quiet input box with unchanged content for a few polls is idle
        # even when no completion row is visible (showTurnDuration off, unknown wording).
        if prompts.input_quiet(screen) and not content_changed and not jsonl_lines:
            s.quiet_polls += 1
        else:
            s.quiet_polls = 0
        if not idle and s.quiet_polls >= QUIET_IDLE_POLLS and match is None and s.believed_prompt is None:
            idle = True
        spinning = prompts.is_working(screen)
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

        Claude Code is treated as gone when the resume line is on screen, or (after
        ``EXIT_CONFIRM_POLLS`` consecutive polls) when the pane shows a shell prompt
        or has left the alternate screen it was on before, with no trust dialog up.
        Without this a crashed or never-started ``claude`` leaves a bare shell that
        would receive voice text as commands.
        """
        sig = hash(tuple(screen.lines))
        changed = sig != s.normal_signature
        s.normal_signature = sig
        if changed or jsonl_lines:
            s.last_output_ts = now
        self._check_echo(s, screen, now)
        s.prev_screen = screen
        if screen.exited:
            self._mark_exited(s, now)
            return
        match = prompts.detect_prompt(screen)
        if match is not None and match.kind is not PromptKind.TRUST:
            match = None  # only the trust dialog lives on the normal screen
        shell = bool(getattr(screen, "shell_prompt", False)) or looks_like_shell_prompt(screen.lines)
        if match is None and (shell or s.seen_alternate):
            s.exit_polls += 1
            if s.exit_polls >= EXIT_CONFIRM_POLLS:
                log.info(
                    "%s: Claude Code is gone (%s)", s.session_id[:8],
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

    def _mark_exited(self, s: Session, now: float) -> None:
        with self._lock:
            self._clear_prompt(s)
            changed = s.state is not SessionState.DETACHED
            s.state = SessionState.DETACHED
            s.detail = "Claude Code exited; resume to start it again"
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
            self.bus.publish(
                Notice(
                    text="Claude Code exited. Say resume, or pick the session again, to start it back up.",
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
                score = max(score, hooks.HINT_PROMPT_SCORE)
                override = True
            if not hint.active:
                s.hook_hint = None
        if now - s.registry_checked >= REGISTRY_INTERVAL:
            s.registry_checked = now
            s.registry_status = self._registry_status(s.session_id)
        if match is None and s.registry_status == "waiting":
            score = max(score, REGISTRY_WAITING_SCORE)
        return score, override

    def _registry_status(self, session_id: str) -> str | None:
        try:
            entry = discovery.load_registry(self.claude_home).get(session_id)
        except Exception as e:  # noqa: BLE001
            log.debug("registry read failed: %s", e)
            return None
        if not entry:
            return None
        status = entry.get("status")
        return status if isinstance(status, str) else None

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
        lead = (hint.message if hint and hint.message else "Claude Code looks like it is waiting for permission")
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
                text=STALL_TEXT + (" ".join(tail) if tail else "nothing readable"),
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
        return src == "pane" or (src == "auto" and s.jsonl is None)

    def _jsonl_source_active(self, s: Session) -> bool:
        return self.config.output.source in ("auto", "jsonl") and s.jsonl is not None

    def _locate_jsonl(self, s: Session) -> None:
        if s.jsonl is not None:
            return
        path = s.jsonl_path or discovery.jsonl_path_for(s.cwd, s.session_id, self.claude_home)
        s.jsonl_path = path
        if not path.is_file():
            return
        if s.jsonl_from_start:
            s.jsonl = JsonlTail(path, offset=0, session_id=s.session_id)
        else:
            s.jsonl = JsonlTail(path, start_at_end=True, session_id=s.session_id)
        log.info("%s: following %s", s.session_id[:8], path.name)

    def _poll_jsonl(self, s: Session) -> int:
        """Publish new jsonl events as PaneLines; returns how many events were seen."""
        if s.jsonl is None:
            return 0
        events = s.jsonl.poll()
        if not events:
            return 0
        publish = self._jsonl_source_active(s)
        for ev in events:
            if ev.kind == "permission_mode":
                s.permission_mode = ev.text
            if publish:
                for line in to_pane_lines(ev, s.session_id):
                    self.bus.pane_lines.put(line)
        return len(events)

    def _remember_tail(self, s: Session, lines: list[str]) -> None:
        for line in lines:
            if line.strip():
                s.pane_tail.append(line)

    # ---- SessionControl: reads -----------------------------------------------------------

    def list_sessions(self) -> list[SessionSummary]:
        with self._lock:
            live = {sid: self._summary(s) for sid, s in self.sessions.items()}
        rows: list[SessionSummary] = list(live.values())
        try:
            found = discovery.list_sessions(self.claude_home, self.tmux)
        except Exception:  # noqa: BLE001 - a broken store must not break the picker
            log.exception("session discovery failed")
            found = []
        for info in found:
            if info.session_id in live:
                row = live[info.session_id]
                if row.permission_mode is None:
                    row.permission_mode = info.permission_mode
                continue
            rows.append(
                SessionSummary(
                    session_id=info.session_id,
                    directory=info.directory,
                    title=info.display_title,
                    last_active=info.last_active_ts or None,
                    attached=False,
                    running=info.running,
                    state=SessionState.DETACHED,
                    permission_mode=info.permission_mode,
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

        The mode comes from the status row or the jsonl ``permission-mode`` record.
        When neither has been seen yet the call waits up to ``MODE_OBSERVE_TIMEOUT``
        for the poll loop to observe one (never when called from the session
        thread itself); if it still is not known the sentence says so rather than
        naming Claude Code's built-in default.
        """
        with self._lock:
            s = self.sessions.get(session_id)
            cwd = s.cwd if s else None
            active = s.permission_mode if s else None
        if active is None and s is not None and s.attached:
            active = self._wait_for_mode(s)
        summary = permissions.read_settings(self.claude_home, cwd)
        return permissions.summary_sentence(summary, active)

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

    def start(self, directory: str | None = None, permission_mode: str | None = None) -> str | None:  # type: ignore[override]
        """``start(directory, mode)`` opens a new Claude Code session and returns its id.

        Without arguments it starts the thread itself, so an agent that calls
        ``.start()`` on every worker thread keeps working; ``start_thread`` is
        the explicit spelling.
        """
        if directory is None:
            self.start_thread()
            return None
        return self._call(self._do_start, directory, permission_mode, timeout=10.0)

    def _do_start(self, directory: str, permission_mode: str | None) -> str:
        cwd = str(Path(directory).expanduser())
        if not os.path.isdir(cwd):
            raise SessionError(f"{directory} is not a directory")
        sid = str(uuid.uuid4())
        settings_path = self._write_settings(sid)
        command = discovery.new_session_command(sid, settings_path, permission_mode or DEFAULT_LAUNCH_MODE)
        target = self._open_pane(cwd, sid, command)
        self._register(sid, cwd, target, settings_path, owned=True, from_start=True, detail="starting")
        log.info("started session %s in %s (%s)", sid[:8], cwd, target)
        return sid

    def resume(self, session_id: str, permission_mode: str | None = None) -> None:
        self._call(self._do_resume, session_id, permission_mode, timeout=10.0)

    def _do_resume(self, session_id: str, permission_mode: str | None) -> None:
        with self._lock:
            existing = self.sessions.get(session_id)
            if existing is not None and existing.attached and existing.state is not SessionState.DETACHED:
                self._focused = session_id
                return
        info = discovery.find_session(session_id, self.claude_home, self.tmux)
        if info is not None and info.running:
            target = info.tmux_target
            if target and self.tmux.pane_exists(target):
                self._register(
                    session_id,
                    info.cwd or (existing.cwd if existing else ""),
                    target,
                    None,
                    owned=False,
                    from_start=False,
                    detail="attached to a running pane",
                    jsonl_path=info.jsonl_path,
                )
                log.info("attached to running session %s at %s", session_id[:8], target)
                return
            raise SessionBusy(
                "That session is already running in another terminal. Close it there first, "
                "or start a new session."
            )
        cwd = (info.cwd if info and info.cwd else None) or (existing.cwd if existing else None)
        if not cwd or not os.path.isdir(cwd):
            raise UnknownSession("I can't find that session's project directory.")
        if existing is not None and existing.owned and existing.target:
            try:
                if self.tmux.pane_exists(existing.target):
                    self.tmux.kill_window(existing.target)
            except TmuxError as e:
                log.debug("old pane for %s not killed: %s", session_id[:8], e)
        settings_path = self._write_settings(session_id)
        command = discovery.resume_command(session_id, settings_path, permission_mode or DEFAULT_LAUNCH_MODE)
        target = self._open_pane(cwd, session_id, command)
        self._register(
            session_id,
            cwd,
            target,
            settings_path,
            owned=True,
            from_start=False,
            detail="resuming",
            jsonl_path=info.jsonl_path if info else None,
        )
        log.info("resumed session %s in %s (%s)", session_id[:8], cwd, target)

    def _open_pane(self, cwd: str, sid: str, command: list[str]) -> str:
        """Open the pane in the ``zordon`` tmux session. ``command`` comes from the
        discovery builders, which are the only place a ``claude`` argv is made.

        ``Tmux.new_window`` creates the session when it does not exist, with this
        window as its first, so no idle shell window is left behind; it also scrubs
        the session environment of provider keys and Claude Code's nesting markers.
        """
        name = (os.path.basename(cwd.rstrip("/")) or sid[:8])[:20]
        return self.tmux.new_window(self.tmux_session, name, cwd, command, PANE_WIDTH, PANE_HEIGHT)

    def _register(
        self,
        sid: str,
        cwd: str,
        target: str,
        settings_path: Path | None,
        *,
        owned: bool,
        from_start: bool,
        detail: str,
        jsonl_path: Path | None = None,
    ) -> None:
        now = self.clock()
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                s = Session(session_id=sid, cwd=cwd, target=target)
                self.sessions[sid] = s
            else:
                s.cwd = cwd or s.cwd
                s.target = target
                s.jsonl = None
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
            s.settings_path = settings_path
            s.jsonl_path = jsonl_path
            s.jsonl_from_start = from_start
            s.started_at = s.started_at or time.time()
            s.last_active = time.time()
            s.last_output_ts = now
            s.title = os.path.basename(cwd.rstrip("/")) if cwd else sid[:8]
            if self._focused is None or self._focused not in self.sessions:
                self._set_focus(sid)
            else:
                s.focused = sid == self._focused
        self.bus.publish(StateChanged(sid, SessionState.WORKING, detail))

    def _write_settings(self, sid: str) -> Path | None:
        if not self.hook_port or not self.hook_secret:
            return None
        path = discovery.hook_settings_path(self.zordon_home, sid)
        try:
            host = discovery.hook_host(self.config.server.bind)
            return discovery.write_hook_settings(path, self.hook_port, self.hook_secret, host=host)
        except (OSError, ValueError) as e:
            log.warning("hook settings not written (%s); launching without hooks", e)
            return None

    def _remove_settings(self, s: Session) -> None:
        if s.settings_path is None:
            return
        discovery.remove_hook_settings(s.settings_path)
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
        """Refuse keystrokes unless Claude Code's TUI (alternate screen) is on the pane.

        The trust dialog is the one prompt drawn on the normal screen, so it is
        allowed when it is the current prompt. Anything else off the alternate
        screen is a shell or a dead ``claude``, where text would run as a command.
        """
        try:
            if self.tmux.alternate_on(s.target):
                return
        except TmuxError as e:
            raise SessionError("I can't reach that pane right now.") from e
        current = m if m is not None else s.current_match
        if current is not None and current.kind is PromptKind.TRUST:
            return
        log.warning("%s: refusing keystrokes; pane is not on the alternate screen", s.session_id[:8])
        self.bus.publish(Notice(text=NO_TUI_TEXT, level="warning", session_id=s.session_id, speak=True))
        raise SessionError(NO_TUI_TEXT)

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
        yes = prompts.yes_option(m)
        if yes is None:
            log.warning("%s: no plain Yes option; not approving", session_id[:8])
            return False
        chosen = m.option(yes)
        if chosen is None or chosen.unsafe or chosen.label != "Yes":
            return False
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
        no = prompts.no_option(m)
        if no is None:
            self._require_tui(s, m)
            self.tmux.send_key(s.target, "Escape")
            return True
        self._select(s, m, no)
        return True

    def plan_approve(self, session_id: str) -> bool:
        return self._call(self._do_plan_approve, session_id)

    def _do_plan_approve(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.PLAN)
        if m is None:
            return False
        manual = prompts.plan_manual_option(m)
        if manual is None or manual == prompts.plan_auto_option(m):
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
        revise = prompts.plan_revise_option(m)
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
        self._require_tui(s, m)
        self.tmux.send_key(s.target, "Escape")
        return True

    def answer_question(self, session_id: str, option: int | str) -> bool:
        return self._call(self._do_answer_question, session_id, option)

    def _do_answer_question(self, session_id: str, option: int | str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.QUESTION)
        if m is None:
            return False
        idx = prompts.question_option(m, option)
        if idx is None:
            return False
        self._select(s, m, idx)
        return True

    def accept_trust(self, session_id: str) -> bool:
        return self._call(self._do_accept_trust, session_id)

    def _do_accept_trust(self, session_id: str) -> bool:
        s, m = self._prompt_for(session_id, PromptKind.TRUST)
        if m is None:
            return False
        yes = next((o.index for o in m.options if o.label == "Yes, I trust this folder"), None)
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
        no = next((o.index for o in m.options if o.label == "No, exit"), None)
        if no is None:
            self._require_tui(s, m)
            self.tmux.send_key(s.target, "Escape")
        else:
            self._select(s, m, no)
        with self._lock:
            events = self._clear_prompt(s)
            s.state = SessionState.DETACHED
            s.detail = "trust declined; Claude Code exited"
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
        """Move the pointer from the selected option to ``index`` and press Enter."""
        self._require_tui(s, m)
        selected = m.selected.index if m.selected else 1
        steps = index - selected
        key = "Down" if steps > 0 else "Up"
        for _ in range(abs(steps)):
            self.tmux.send_key(s.target, key)
        self.tmux.send_key(s.target, "Enter")
        log.info("%s: selected option %d (%s)", s.session_id[:8], index, _label(m, index))

    # ---- SessionControl: permission mode --------------------------------------------------------

    def set_permission_mode(self, session_id: str, mode: str) -> bool:
        target = permissions.normalize_target_mode(mode, by_voice=False)
        return self._call(self._do_set_permission_mode, session_id, target, timeout=10.0)

    def _do_set_permission_mode(self, session_id: str, target: str) -> bool:
        s = self._live(session_id)
        screen = parse_screen(self.tmux.capture(s.target))
        if prompts.detect_prompt(screen) is not None or screen.input_box is None:
            log.info("%s: not switching mode while a prompt is up", session_id[:8])
            return False
        self._require_tui(s)
        start = prompts.permission_mode_from_screen(screen)
        if start is None:
            return False
        current = start
        bypass_steps = 0
        stuck = 0
        reason = "the cycle never reached it"
        for _ in range(MAX_BTAB_PRESSES):
            if current == target:
                s.permission_mode = current
                return True
            previous = current
            current = self._press_btab(s)
            if current in permissions.FORBIDDEN_TARGET_MODES:
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
            current = self._return_to(s, start, current)
        if current is not None:
            s.permission_mode = current
        where = (
            f"the pane is back in {permissions.mode_label(current)} mode"
            if current == start
            else f"the pane shows {permissions.mode_label(current) if current else 'an unknown'} mode now"
        )
        self.bus.publish(
            Notice(
                text=f"I couldn't switch to {permissions.mode_label(target)} mode from here: {reason}; {where}.",
                level="warning",
                session_id=session_id,
                speak=True,
            )
        )
        return False

    def _press_btab(self, s: Session) -> str | None:
        """One Shift+Tab, then the mode the status row shows after it settles."""
        self.tmux.send_key(s.target, "BTab")
        time.sleep(BTAB_SETTLE)
        return prompts.permission_mode_from_screen(parse_screen(self.tmux.capture(s.target)))

    def _return_to(self, s: Session, start: str, current: str | None) -> str | None:
        """Press BTab (bounded) until the status row shows ``start`` again, skipping bypass."""
        for _ in range(MAX_BTAB_PRESSES):
            if current == start:
                return current
            previous = current
            current = self._press_btab(s)
            if current is None or (current == previous and current not in permissions.FORBIDDEN_TARGET_MODES):
                break  # not reacting: stop pressing keys into a pane we cannot read
        if current != start:
            log.warning("%s: could not return the mode to %s (now %s)", s.session_id[:8], start, current)
        return current

    # ---- SessionControl: hooks --------------------------------------------------------------------

    def hook_event(self, payload: dict[str, Any]) -> None:
        if not hooks.payload_shape_ok(payload):
            log.debug("ignoring malformed hook payload")
            return
        sid = str(payload["session_id"])
        hint = hooks.hint_for(payload)
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                log.debug("hook event for unknown session %s", sid[:8])
                return
            s.hook_hint = hint
        log.info("%s: hook %s/%s", sid[:8], payload.get("hook_event_name"), hint.notification_type)
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
