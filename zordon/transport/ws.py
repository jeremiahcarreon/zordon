"""Per-client WebSocket: bus events out as JSON, JSON in as bus traffic and commands.

Two pieces:

* :class:`Broadcaster` (one per app) drains ``bus.client_events`` on its own
  thread, converts each event to an outbound protocol model once, and fans the
  serialized JSON out to every connection's queue through
  ``loop.call_soon_threadsafe``. No executor thread is ever parked on a queue.
* :class:`ClientConnection` (one per socket) sends ``hello``, ``sessions``, the
  ``health`` report (when the agent has one) and the recent transcript, then runs
  a receive task and a send task concurrently until either finishes.

``health`` and ``update`` are published by the agent as ready protocol models
(``HealthOut`` / ``UpdateOut``); :func:`to_outbound` passes them through like
``Sessions`` and ``SettingsOut``.

The transport never talks to tmux, providers or the session thread directly:
inbound audio goes to ``bus.inbound_audio``, typed text to ``agent.submit_text``,
commands to the ``SessionControl`` surface described in ``docs/architecture.md``.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import itertools
import json
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette import status
from starlette.websockets import WebSocketDisconnected

from zordon.agents import DEFAULT_AGENT, available_agents
from zordon.bus import (
    Bus,
    Flush,
    Notice,
    PromptCleared,
    PromptDetected,
    SpeechChunk,
    StateChanged,
    TranscriptRow,
    drain,
)
from zordon.config import VERBOSITY_LEVELS
from zordon.transport import protocol as P
from zordon.transport.sanitize import sanitize_keystrokes

log = logging.getLogger("zordon.transport.ws")

QUEUE_CAP = 2000
MAX_PROTOCOL_ERRORS = 10
TRANSCRIPT_TAIL = 30
DEFAULT_TTS_SAMPLE_RATE = 24000
UPLOAD_MAX_BYTES = 25 * 1024 * 1024

# Modes a tap may select. Voice is narrower (dispatcher); nothing selects the bypass mode.
ALLOWED_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")
FORBIDDEN_PERMISSION_MODES = ("bypassPermissions",)
PROVIDER_KINDS = ("stt", "tts", "normalizer", "router")
# ``voice`` is not a provider but the TTS voice; it rides on the same command.
PROVIDER_LIKE_KINDS = (*PROVIDER_KINDS, "voice")
# Command handlers call synchronous SessionControl methods that may block (tmux,
# discovery, futures on the session thread). They run on the default executor so
# the event loop keeps reading audio and sending flushes; past this many seconds
# the client gets an error and the handler is left to finish on its thread.
COMMAND_TIMEOUT_S = 12.0
_SECRET_WORDS = ("key", "token", "secret", "password")

# Ephemeral notices get row ids far below anything the transcript store hands out
# (the store uses -event_id for its own event rows).
_NOTICE_BASE = 1_000_000_000
_notice_ids = itertools.count(1)


# ---- event -> outbound conversion (pure) --------------------------------------------


def to_outbound(event: Any) -> list[BaseModel | dict[str, Any]]:
    """Convert one bus event to zero or more outbound messages."""
    if isinstance(event, SpeechChunk):
        return [
            P.SpeechOut(
                sentence_id=event.sentence_id,
                seq=event.seq,
                generation=event.generation,
                sample_rate=event.sample_rate,
                pcm=base64.b64encode(event.pcm).decode("ascii"),
                final=event.final,
            )
        ]
    if isinstance(event, Flush):
        return [P.FlushOut(generation=event.generation, sentence_id=event.interrupted_sentence_id)]
    if isinstance(event, StateChanged):
        return [
            P.StateOut(
                session_id=event.session_id,
                state=_enum_value(event.state),
                detail=event.detail,
                ts=event.ts,
            )
        ]
    if isinstance(event, PromptDetected):
        return [
            P.PromptOut(
                prompt_id=event.prompt_id,
                session_id=event.session_id,
                kind=_enum_value(event.kind),  # type: ignore[arg-type]
                title=event.title,
                options=list(event.options),
                raw_lines=list(event.raw_lines),
            )
        ]
    if isinstance(event, PromptCleared):
        return [
            P.PromptOut(
                prompt_id=event.prompt_id,
                session_id=event.session_id,
                kind="permission",
                title="",
                options=[],
                raw_lines=[],
                cleared=True,
            )
        ]
    if isinstance(event, TranscriptRow):
        return [transcript_out(event)]
    if isinstance(event, Notice):
        out: list[BaseModel | dict[str, Any]] = [
            P.TranscriptOut(
                row_id=-(_NOTICE_BASE + next(_notice_ids)),
                session_id=event.session_id,
                kind="notice",
                text=event.text,
                raw_lines=[],
                ts=event.ts,
            )
        ]
        if event.level == "error":
            out.append(P.ErrorOut(message=event.text, code="notice"))
        return out
    if isinstance(event, BaseModel) and getattr(event, "type", None) in P.OUTBOUND_TYPES:
        return [event]
    if isinstance(event, dict) and event.get("type") in P.OUTBOUND_TYPES:
        return [event]
    log.debug("dropping unknown client event %r", type(event).__name__)
    return []


def transcript_out(row: TranscriptRow) -> P.TranscriptOut:
    kind = row.kind if row.kind in ("spoken", "user", "notice", "raw") else "notice"
    return P.TranscriptOut(
        row_id=row.row_id,
        session_id=row.session_id,
        kind=kind,  # type: ignore[arg-type]
        text=row.text,
        raw_lines=list(row.raw_lines),
        ts=row.ts,
        sentence_id=row.sentence_id,
        spoken=row.spoken,
    )


def serialize(msg: BaseModel | dict[str, Any]) -> tuple[str, int, str]:
    """``(type, generation, json)``; generation is 0 for anything but speech."""
    if isinstance(msg, BaseModel):
        kind = str(getattr(msg, "type", ""))
        gen = int(getattr(msg, "generation", 0)) if kind == "speech" else 0
        return kind, gen, P.dump(msg)
    kind = str(msg.get("type", ""))
    gen = int(msg.get("generation", 0) or 0) if kind == "speech" else 0
    return kind, gen, json.dumps(msg, separators=(",", ":"))


def to_session_summary(obj: Any, focused: str | None) -> P.SessionSummary:
    """Accept a dataclass, a pydantic model or a dict from the session manager."""
    if isinstance(obj, dict):

        def get(k: str, d: Any = None) -> Any:
            return obj.get(k, d)

    else:

        def get(k: str, d: Any = None) -> Any:
            return getattr(obj, k, d)

    sid = str(get("session_id", ""))
    last_active = get("last_active")
    return P.SessionSummary(
        session_id=sid,
        directory=str(get("directory", "") or ""),
        title=str(get("title", "") or ""),
        last_active=float(last_active) if isinstance(last_active, (int, float)) else None,
        attached=bool(get("attached", False)),
        running=bool(get("running", False)),
        state=_enum_value(get("state", "detached")),
        permission_mode=get("permission_mode"),
        focused=bool(sid) and sid == focused,
        agent=str(get("agent", "") or "claude-code"),
    )


def settings_out(settings: dict[str, Any]) -> P.SettingsOut:
    providers = settings.get("providers") or {}
    clean = {
        str(k): str(v)
        for k, v in dict(providers).items()
        if not any(w in str(k).lower() for w in _SECRET_WORDS)
    }
    return P.SettingsOut(
        verbosity=str(settings.get("verbosity", "minimal")),
        tool_chatter=bool(settings.get("tool_chatter", False)),
        muted=bool(settings.get("muted", False)),
        providers=clean,
        permission_mode=settings.get("permission_mode"),
        launch_mode=settings.get("launch_mode"),
    )


def _enum_value(v: Any) -> str:
    return v.value if isinstance(v, Enum) else str(v)


# ---- per-connection outbound queue ---------------------------------------------------


class OutQueue:
    """Bounded FIFO owned by the connection's event loop.

    Speech is the only thing dropped: when the queue is full the oldest speech
    chunk goes first; control messages are never lost. A ``flush`` purges queued
    speech from older generations, since the client would discard it anyway.
    """

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self.cap = cap
        self._dq: collections.deque[tuple[str, int, str]] = collections.deque()
        self._event = asyncio.Event()
        self.dropped = 0

    def push(self, kind: str, generation: int, text: str) -> None:
        if kind == "flush":
            self._purge_speech_older_than(generation)
        if len(self._dq) >= self.cap:
            if not self._drop_oldest_speech():
                if kind == "speech":
                    self.dropped += 1
                    return
        self._dq.append((kind, generation, text))
        self._event.set()

    async def get(self) -> str:
        while not self._dq:
            self._event.clear()
            await self._event.wait()
        return self._dq.popleft()[2]

    def __len__(self) -> int:
        return len(self._dq)

    def _drop_oldest_speech(self) -> bool:
        for i, (kind, _gen, _text) in enumerate(self._dq):
            if kind == "speech":
                del self._dq[i]
                self.dropped += 1
                return True
        return False

    def _purge_speech_older_than(self, generation: int) -> None:
        if generation <= 0:
            return
        keep = [item for item in self._dq if not (item[0] == "speech" and item[1] < generation)]
        if len(keep) != len(self._dq):
            self.dropped += len(self._dq) - len(keep)
            self._dq = collections.deque(keep)


# ---- broadcaster ------------------------------------------------------------------------


class Broadcaster:
    """Drain ``bus.client_events`` on a thread and fan out to every connection."""

    def __init__(self, bus: Bus, poll_s: float = 0.25) -> None:
        self._bus = bus
        self._poll_s = poll_s
        self._conns: set[ClientConnection] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.events_seen = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zordon-broadcast", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def register(self, conn: ClientConnection) -> None:
        with self._lock:
            self._conns.add(conn)

    def unregister(self, conn: ClientConnection) -> None:
        with self._lock:
            self._conns.discard(conn)

    def connection_count(self) -> int:
        with self._lock:
            return len(self._conns)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._bus.client_events.get(timeout=self._poll_s)
            except queue.Empty:
                continue
            self.events_seen += 1
            try:
                payloads = [serialize(m) for m in to_outbound(event)]
            except Exception:  # noqa: BLE001
                log.exception("could not convert client event %r", type(event).__name__)
                continue
            if not payloads:
                continue
            with self._lock:
                conns = list(self._conns)
            for conn in conns:
                conn.deliver_threadsafe(payloads)


# ---- one client -------------------------------------------------------------------------


class ClientConnection:
    """One WebSocket. Created by the ``/ws`` route after the cookie check passed."""

    # Test hook: a small number here overrides the configured idle disconnect.
    IDLE_SECONDS_OVERRIDE: float | None = None

    def __init__(
        self,
        ws: WebSocket,
        agent: Any,
        broadcaster: Broadcaster,
        *,
        idle_seconds: float | None,
        client_id: str | None = None,
    ) -> None:
        self.ws = ws
        self.agent = agent
        self.broadcaster = broadcaster
        self.client_id = client_id or secrets.token_hex(8)
        self.idle_seconds = (
            self.IDLE_SECONDS_OVERRIDE if self.IDLE_SECONDS_OVERRIDE is not None else idle_seconds
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.out = OutQueue()
        self.call_active = False
        self.protocol_errors = 0
        self.audio_frames = 0
        self.audio_dropped = 0
        self.audio_ignored = 0
        self.last_flush_ack = 0
        self._closed = False
        # WEB-6: only non-ping traffic counts as activity for the idle disconnect.
        self._last_activity = time.monotonic()
        self._cmd_lock: asyncio.Lock | None = None
        self._cmd_tasks: set[asyncio.Task[Any]] = set()

    # ---- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        await self.ws.accept()
        self.loop = asyncio.get_running_loop()
        self.broadcaster.register(self)
        log.info("client %s connected", self.client_id)
        try:
            self._cmd_lock = asyncio.Lock()
            await self._send_model(await self._off_loop(self._hello))
            await self._send_model(await self._off_loop(self._sessions))
            health = await self._off_loop(self._health)
            if health is not None:
                await self._send_model(health)
            for row in await self._off_loop(self._transcript_tail):
                await self._send_model(row)
            recv = asyncio.create_task(self._receive_loop(), name=f"ws-recv-{self.client_id}")
            send = asyncio.create_task(self._send_loop(), name=f"ws-send-{self.client_id}")
            tasks = {recv, send}
            for task in tasks:
                task.add_done_callback(_retrieve_exception)
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    # asyncio.wait never re-raises a child's cancellation into us, so an
                    # outer cancellation arriving here keeps its own identity.
                    await asyncio.wait(pending)
                for task in done:
                    exc = None if task.cancelled() else task.exception()
                    if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                        log.warning("client %s task failed: %r", self.client_id, exc)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
        except (WebSocketDisconnect, WebSocketDisconnected):
            pass
        finally:
            for task in list(self._cmd_tasks):
                task.cancel()
            self.broadcaster.unregister(self)
            if self.call_active:
                self.call_active = False
                self._safe_call(self.agent.call_state, self.client_id, "end")
            log.info(
                "client %s disconnected (audio frames=%d dropped=%d, out dropped=%d)",
                self.client_id,
                self.audio_frames,
                self.audio_dropped,
                self.out.dropped,
            )

    def deliver_threadsafe(self, payloads: list[tuple[str, int, str]]) -> None:
        """Called from the broadcaster thread."""
        loop = self.loop
        if loop is None or self._closed:
            return
        try:
            loop.call_soon_threadsafe(self._push_many, payloads)
        except RuntimeError:
            # Loop closed under us: the connection is gone.
            self.broadcaster.unregister(self)

    def _push_many(self, payloads: list[tuple[str, int, str]]) -> None:
        for kind, gen, text in payloads:
            self.out.push(kind, gen, text)

    # ---- send side ------------------------------------------------------------------

    async def _send_loop(self) -> None:
        while True:
            text = await self.out.get()
            await self.ws.send_text(text)

    async def _send_model(self, msg: BaseModel) -> None:
        await self.ws.send_text(P.dump(msg))

    async def _reply(self, msgs: BaseModel | list[BaseModel] | None) -> None:
        if msgs is None:
            return
        if isinstance(msgs, BaseModel):
            msgs = [msgs]
        for m in msgs:
            await self._send_model(m)

    async def _close(self, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(code=code, reason=reason)
        except RuntimeError:
            pass

    # ---- receive side ---------------------------------------------------------------

    async def _receive_loop(self) -> None:
        while not self._closed:
            try:
                if self.idle_seconds:
                    remaining = self.idle_seconds - (time.monotonic() - self._last_activity)
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(self.ws.receive(), timeout=remaining)
                else:
                    message = await self.ws.receive()
            except TimeoutError:
                log.info("client %s idle for %.0fs, closing", self.client_id, self.idle_seconds or 0)
                await self._close(status.WS_1000_NORMAL_CLOSURE, "idle")
                return
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
            raw = message.get("text")
            if raw is None:
                raw = message.get("bytes") or b""
            try:
                msg = P.parse_inbound(raw)
            except P.ProtocolError as e:
                await self._protocol_error(str(e))
                continue
            await self._handle(msg)

    async def _protocol_error(self, detail: str) -> None:
        self.protocol_errors += 1
        await self._reply(P.ErrorOut(message=detail, code="protocol"))
        if self.protocol_errors >= MAX_PROTOCOL_ERRORS:
            log.warning("client %s: too many protocol errors, closing", self.client_id)
            await self._close(status.WS_1008_POLICY_VIOLATION, "protocol errors")

    async def _handle(self, msg: Any) -> None:
        if not isinstance(msg, P.Ping):
            self._last_activity = time.monotonic()
        if isinstance(msg, P.AudioIn):
            self._audio(msg)
        elif isinstance(msg, P.TextIn):
            text = sanitize_keystrokes(msg.text)
            if text.strip():
                self._safe_call(self.agent.submit_text, text, self.client_id)
        elif isinstance(msg, P.CommandIn):
            self._spawn_command(msg)
        elif isinstance(msg, P.CallIn):
            self.call_active = msg.action in ("start", "resume")
            self._safe_call(self.agent.call_state, self.client_id, msg.action)
        elif isinstance(msg, P.Ping):
            await self._reply(P.Pong(ts=msg.ts))
        elif isinstance(msg, P.FlushAck):
            self.last_flush_ack = max(self.last_flush_ack, msg.generation)

    def _audio(self, msg: P.AudioIn) -> None:
        if not self.call_active:
            self.audio_ignored += 1
            return
        samples = msg.samples()
        q = self.agent.bus.inbound_audio
        try:
            q.put_nowait(samples)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            self.audio_dropped += 1
            try:
                q.put_nowait(samples)
            except queue.Full:
                return
        self.audio_frames += 1

    # ---- commands -------------------------------------------------------------------

    def _spawn_command(self, cmd: P.CommandIn) -> None:
        """Run the command on its own task so the receive loop keeps reading audio.

        Commands of one connection still run one at a time (``_cmd_lock``), in order.
        """
        task = asyncio.create_task(self._run_command(cmd), name=f"ws-cmd-{cmd.name}")
        self._cmd_tasks.add(task)
        task.add_done_callback(self._cmd_tasks.discard)
        task.add_done_callback(_retrieve_exception)

    async def _run_command(self, cmd: P.CommandIn) -> None:
        lock = self._cmd_lock
        if lock is None:
            self._cmd_lock = lock = asyncio.Lock()
        async with lock:
            if self._closed:
                return
            reply = await self._command(cmd)
        try:
            await self._reply(reply)
        except (WebSocketDisconnect, WebSocketDisconnected, RuntimeError):
            pass

    async def _command(self, cmd: P.CommandIn) -> BaseModel | list[BaseModel] | None:
        """Run one command handler off the event loop, bounded by ``COMMAND_TIMEOUT_S``."""
        handler = self._handlers().get(cmd.name)
        if handler is None:
            return P.ErrorOut(message=f"command {cmd.name} is not handled", code="command")
        try:
            return await asyncio.wait_for(
                self._off_loop(handler, cmd.args), timeout=COMMAND_TIMEOUT_S
            )
        except TimeoutError:
            log.warning("client %s: command %s timed out", self.client_id, cmd.name)
            return P.ErrorOut(
                message=f"{cmd.name} did not finish within {COMMAND_TIMEOUT_S:.0f}s",
                code="timeout",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("client %s: command %s failed: %r", self.client_id, cmd.name, e)
            return P.ErrorOut(message=f"{cmd.name} failed: {e}", code="command")

    async def _off_loop(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "list_sessions": lambda a: self._sessions(),
            "focus": self._cmd_focus,
            "start": self._cmd_start,
            "resume": self._cmd_resume,
            "attach": self._cmd_attach,
            "detach": self._cmd_detach,
            "delete": self._cmd_delete,
            "send_text": self._cmd_send_text,
            "approve": lambda a: self._prompt_action("approve", a),
            "deny": lambda a: self._prompt_action("deny", a),
            "plan_approve": lambda a: self._prompt_action("plan_approve", a),
            "plan_deny": lambda a: self._prompt_action("plan_deny", a),
            "plan_revise": self._cmd_plan_revise,
            "answer": self._cmd_answer,
            "stop": self._cmd_stop,
            "mute": lambda a: self._set_muted(True),
            "unmute": lambda a: self._set_muted(False),
            "set_verbosity": self._cmd_set_verbosity,
            "set_tool_chatter": self._cmd_set_tool_chatter,
            "set_permission_mode": self._cmd_set_permission_mode,
            "set_provider": self._cmd_set_provider,
            "repeat": self._cmd_repeat,
            "status": self._cmd_status,
            "upload": self._cmd_upload,
        }

    def _focused_or_arg(self, args: dict[str, Any]) -> str | None:
        sid = args.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
        return self.agent.sessions.focused()

    def _need_session(self, args: dict[str, Any]) -> str | P.ErrorOut:
        sid = self._focused_or_arg(args)
        if not sid:
            return P.ErrorOut(message="no session is focused", code="no_session")
        return sid

    def _cmd_focus(self, args: dict[str, Any]) -> BaseModel:
        sid = _str_arg(args, "session_id")
        if sid is None:
            return _bad_argument("session_id")
        self.agent.sessions.focus(sid)
        return self._sessions()

    def _cmd_start(self, args: dict[str, Any]) -> BaseModel:
        directory = _str_arg(args, "directory")
        if directory is None:
            return _bad_argument("directory")
        mode = _str_arg(args, "permission_mode")
        if mode is not None and (err := _check_mode(mode)) is not None:
            return err
        agent = _str_arg(args, "agent")
        if agent is not None:
            self.agent.sessions.start(directory, mode, agent=agent)
        else:
            self.agent.sessions.start(directory, mode)
        return self._sessions()

    def _cmd_resume(self, args: dict[str, Any]) -> BaseModel:
        sid = _str_arg(args, "session_id")
        if sid is None:
            return _bad_argument("session_id")
        mode = _str_arg(args, "permission_mode")
        if mode is not None and (err := _check_mode(mode)) is not None:
            return err
        agent = _str_arg(args, "agent")
        if agent is not None:
            self.agent.sessions.resume(sid, mode, agent=agent)
        else:
            self.agent.sessions.resume(sid, mode)
        return self._sessions()

    def _cmd_attach(self, args: dict[str, Any]) -> BaseModel:
        """Follow an existing tmux pane (``target`` like ``session:window.pane``) with an adapter."""
        target = _str_arg(args, "target")
        if target is None:
            return _bad_argument("target")
        agent = _str_arg(args, "agent") or "generic"
        attach = getattr(self.agent.sessions, "attach", None)
        if not callable(attach):
            return P.ErrorOut(message="attaching to a pane is not supported by this agent", code="unsupported")
        attach(target, agent)
        return self._sessions()

    def _cmd_detach(self, args: dict[str, Any]) -> BaseModel:
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        self.agent.sessions.detach(sid)
        return self._sessions()

    def _cmd_delete(self, args: dict[str, Any]) -> BaseModel:
        sid = _str_arg(args, "session_id")
        if sid is None:
            return _bad_argument("session_id")
        if args.get("confirm") is not True:
            return P.ErrorOut(
                message="delete needs confirm=true; it kills the session's pane",
                code="confirm_required",
            )
        self.agent.sessions.delete(sid)
        return self._sessions()

    def _cmd_send_text(self, args: dict[str, Any]) -> BaseModel | None:
        text = _text_arg(args, "text")
        if text is None:
            return _bad_argument("text")
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        self.agent.sessions.send_text(sid, text)
        return None

    def _prompt_action(self, name: str, args: dict[str, Any]) -> BaseModel | None:
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        ok = getattr(self.agent.sessions, name)(sid)
        if ok is False:
            return P.ErrorOut(message=f"{name}: no prompt is waiting", code="no_prompt")
        return None

    def _cmd_plan_revise(self, args: dict[str, Any]) -> BaseModel | None:
        text = _text_arg(args, "text")
        if text is None:
            return _bad_argument("text")
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        if self.agent.sessions.plan_revise(sid, text) is False:
            return P.ErrorOut(message="plan_revise: no plan is waiting", code="no_prompt")
        return None

    def _cmd_answer(self, args: dict[str, Any]) -> BaseModel | None:
        option = args.get("option")
        if isinstance(option, str):
            option = sanitize_keystrokes(option)
        if not isinstance(option, (int, str)) or isinstance(option, bool) or option == "":
            return _bad_argument("option")
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        if self.agent.sessions.answer_question(sid, option) is False:
            return P.ErrorOut(message="answer: no question is waiting", code="no_prompt")
        return None

    def _cmd_stop(self, args: dict[str, Any]) -> BaseModel | None:
        """Stop: silence speech now (a real Flush with a new generation, so chunks still
        in flight and sentences already synthesized are dropped everywhere) and send
        Escape to the focused pane."""
        self._flush_speech()
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        self.agent.sessions.send_escape(sid)
        return None

    def _flush_speech(self) -> None:
        bus = getattr(self.agent, "bus", None)
        if bus is None or not hasattr(bus, "next_generation"):
            return
        generation = bus.next_generation()
        drain(bus.playback)
        bus.publish(Flush(generation=generation))

    def _set_muted(self, muted: bool) -> BaseModel:
        self.agent.set_muted(muted)
        return self._settings()

    def _cmd_set_verbosity(self, args: dict[str, Any]) -> BaseModel:
        level = _str_arg(args, "level")
        if level not in VERBOSITY_LEVELS:
            return P.ErrorOut(
                message=f"level must be one of {', '.join(VERBOSITY_LEVELS)}", code="bad_argument"
            )
        self.agent.set_verbosity(level)
        return self._settings()

    def _cmd_set_tool_chatter(self, args: dict[str, Any]) -> BaseModel:
        enabled = args.get("enabled")
        if not isinstance(enabled, bool):
            return _bad_argument("enabled")
        self.agent.set_tool_chatter(enabled)
        return self._settings()

    def _cmd_set_permission_mode(self, args: dict[str, Any]) -> BaseModel:
        mode = _str_arg(args, "mode")
        if mode is None:
            return _bad_argument("mode")
        err = _check_mode(mode)
        if err is not None:
            return err
        sid = self._need_session(args)
        if isinstance(sid, P.ErrorOut):
            return sid
        if self.agent.sessions.set_permission_mode(sid, mode) is False:
            return P.ErrorOut(message=f"could not switch to {mode}", code="command")
        return self._settings()

    def _cmd_set_provider(self, args: dict[str, Any]) -> BaseModel:
        kind = _str_arg(args, "kind")
        name = _str_arg(args, "name")
        if kind not in PROVIDER_LIKE_KINDS:
            return P.ErrorOut(
                message=f"kind must be one of {', '.join(PROVIDER_LIKE_KINDS)}",
                code="bad_argument",
            )
        if name is None:
            return _bad_argument("name")
        if kind == "voice":
            return self._set_voice(name)
        self.agent.set_provider(kind, name)
        return self._settings()

    def _set_voice(self, name: str) -> BaseModel:
        """``set_provider {kind: voice}``: the TTS voice. Agents expose it either as
        ``set_voice(name)`` or by accepting kind ``voice`` in ``set_provider``."""
        setter = getattr(self.agent, "set_voice", None)
        if callable(setter):
            setter(name)
            return self._settings()
        try:
            self.agent.set_provider("voice", name)
        except ValueError:
            return P.ErrorOut(
                message="changing the voice is not supported by this agent; set providers.tts_voice in config.toml",
                code="unsupported",
            )
        return self._settings()

    def _cmd_repeat(self, args: dict[str, Any]) -> None:
        self.agent.repeat_last()
        return None

    def _cmd_status(self, args: dict[str, Any]) -> BaseModel | list[BaseModel]:
        sid = self.agent.sessions.focused()
        if not sid:
            return self._sessions()
        state = self.agent.sessions.state_of(sid)
        detail = ""
        try:
            detail = str(self.agent.sessions.permission_summary(sid) or "")
        except Exception:  # noqa: BLE001
            detail = ""
        return [
            P.StateOut(session_id=sid, state=_enum_value(state), detail=detail, ts=_now()),
            self._sessions(),
        ]

    def _cmd_upload(self, args: dict[str, Any]) -> BaseModel | None:
        size = args.get("size")
        if isinstance(size, (int, float)) and size > UPLOAD_MAX_BYTES:
            return P.ErrorOut(
                message=f"file is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB",
                code="too_large",
            )
        return None

    # ---- snapshots ------------------------------------------------------------------

    def _hello(self) -> P.Hello:
        settings = self.agent.settings() or {}
        rate = getattr(self.agent, "tts_sample_rate", None) or settings.get("tts_sample_rate")
        return P.Hello(
            version=str(self.agent.version),
            focused_session=self.agent.sessions.focused(),
            verbosity=str(settings.get("verbosity", "minimal")),
            tool_chatter=bool(settings.get("tool_chatter", False)),
            providers=settings_out(settings).providers,
            tts_sample_rate=int(rate or DEFAULT_TTS_SAMPLE_RATE),
            tunnel_url=getattr(self.agent, "tunnel_url", None),
            muted=bool(settings.get("muted", False)),
            agents=self._agents_installed(),
            default_agent=self._default_agent(),
        )

    def _agents_installed(self) -> dict[str, bool]:
        """Adapter key -> whether its binary is on PATH (the generic adapter always is)."""
        probe = getattr(self.agent, "available_agents", None)
        try:
            found = probe() if callable(probe) else available_agents(getattr(self.agent, "config", None))
        except Exception:  # noqa: BLE001 - a broken probe must not stop hello
            log.exception("agent availability probe failed")
            return {}
        return {str(k): v is not None for k, v in dict(found).items()}

    def _default_agent(self) -> str:
        cfg = getattr(self.agent, "config", None)
        providers = getattr(cfg, "providers", None)
        return str(getattr(providers, "agent", None) or DEFAULT_AGENT)

    def _sessions(self) -> P.Sessions:
        focused = self.agent.sessions.focused()
        rows = [to_session_summary(s, focused) for s in self.agent.sessions.list_sessions()]
        return P.Sessions(sessions=rows)

    def _settings(self) -> P.SettingsOut:
        return settings_out(self.agent.settings() or {})

    def _health(self) -> P.HealthOut | None:
        """The agent's health report as a ``health`` message; None when the agent has none."""
        probe = getattr(self.agent, "health", None)
        if not callable(probe):
            return None
        try:
            report = probe()
        except Exception:  # noqa: BLE001 - a broken probe must not stop the opening burst
            log.exception("health report failed")
            return None
        if isinstance(report, P.HealthOut):
            return report
        to_out = getattr(report, "to_out", None)
        return to_out() if callable(to_out) else None

    def _transcript_tail(self) -> list[P.TranscriptOut]:
        tail = getattr(self.agent, "transcript_tail", None)
        if tail is None:
            return []
        sid = self.agent.sessions.focused()
        if not sid:
            return []
        try:
            rows = tail(sid, TRANSCRIPT_TAIL) or []
        except Exception:  # noqa: BLE001
            log.exception("transcript tail failed")
            return []
        return [transcript_out(r) for r in rows if isinstance(r, TranscriptRow)]

    def _safe_call(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("client %s: %s failed", self.client_id, getattr(fn, "__name__", fn))


# ---- small helpers ----------------------------------------------------------------------


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    """Mark a finished task's exception as seen so asyncio never logs it as unretrieved."""
    if not task.cancelled():
        task.exception()



def _str_arg(args: dict[str, Any], key: str) -> str | None:
    v = args.get(key)
    return v if isinstance(v, str) and v != "" else None


def _text_arg(args: dict[str, Any], key: str) -> str | None:
    """A string argument that will be typed into a pane: control/bidi characters stripped."""
    v = _str_arg(args, key)
    if v is None:
        return None
    v = sanitize_keystrokes(v)
    return v if v.strip() else None


def _bad_argument(name: str) -> P.ErrorOut:
    return P.ErrorOut(message=f"missing or invalid argument: {name}", code="bad_argument")


def _check_mode(mode: str) -> P.ErrorOut | None:
    if mode in FORBIDDEN_PERMISSION_MODES:
        return P.ErrorOut(
            message="Zordon never sets that permission mode; change it in Claude Code's own settings",
            code="forbidden",
        )
    if mode not in ALLOWED_PERMISSION_MODES:
        return P.ErrorOut(
            message=f"mode must be one of {', '.join(ALLOWED_PERMISSION_MODES)}",
            code="bad_argument",
        )
    return None


def _now() -> float:
    return time.time()
