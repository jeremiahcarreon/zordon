"""DispatcherThread: utterances -> pane keystrokes, transcript answer, or shim command.

Runs on its own thread, reading ``bus.utterances`` (speech and typed text take the
same path). For every utterance it looks at the focused session's state first:

* AWAITING_PERMISSION (permission or trust prompt): strict yes/no only, at
  ``voice.yes_no_confidence`` or higher. "Always allow" wording is refused.
* AWAITING_PLAN_APPROVAL: approve / revise / deny; revise asks "what should
  change?" and forwards the next utterance verbatim.
* AWAITING_QUESTION: the utterance is matched against the option labels.
* otherwise the router decides between Claude Code, a transcript answer and a
  shim command; ``unclear`` or low confidence goes to Claude Code.

A transcript query never touches the pane. It is answered over the recent *raw*
transcript lines (``store.raw_tail``, which include the tool-call descriptions
the verbosity filter may have kept from being spoken) merged with the spoken
tail, so "what file did it just change?" works at minimal verbosity. Shim
commands are looked up in the closed set ``commands.BY_NAME``; anything else is
refused. Destructive commands
(delete) need a strict spoken yes on the next utterance. An exception while
handling one utterance is logged and spoken; it never kills the thread.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from zordon.bus import (
    Bus,
    Draft,
    Flush,
    LineKind,
    Notice,
    PromptKind,
    SessionState,
    TranscriptRow,
    Utterance,
)
from zordon.config import VERBOSITY_LEVELS, Config
from zordon.routing import commands
from zordon.routing.base import (
    RouteContext,
    Router,
    effective_destination,
    forbidden_permission_phrase,
    fuzzy_match_sessions,
    normalize_utterance,
    words,
)
from zordon.routing.keyword import KeywordRouter
from zordon.routing.transcript_query import TranscriptAnswerer
from zordon.transcript.store import TranscriptStore

log = logging.getLogger("zordon.routing.dispatcher")

# speak(text, session_id, kind, *, bypass_mute=False): the pipeline's ``speak_now``. Its
# ``kind`` is a LineKind; the dispatcher's own vocabulary maps onto it here. The keyword is
# only passed for the "Muted." acknowledgement.
SpeakFn = Callable[..., Any]
SPEAK_KINDS: dict[str, LineKind] = {
    "ack": LineKind.SUMMARY,  # "Muted.", "Approved.", "Switched to api."
    "answer": LineKind.PROSE,  # transcript answers, status, session list
    "repeat": LineKind.PROSE,
    "question": LineKind.QUESTION,  # "Yes or no?", "Which one?"
    "error": LineKind.ERROR,
}

# Shim commands that may run while a prompt is waiting: none of them moves the session.
SAFE_IN_PROMPT = frozenset(
    {"mute", "unmute", "repeat", "status", "list_sessions", "set_verbosity", "set_tool_chatter", "scratch", "hush"}
)
# Shim commands that need a focused session.
NEEDS_SESSION = frozenset({"stop", "repeat", "status", "set_permission_mode", "delete", "detach"})
# Permission modes voice may switch to when session.permissions is not importable.
_DEFAULT_VOICE_SWITCHABLE = ("default", "acceptEdits", "plan")
# Transcript-query context: raw transcript lines (every pre-passed line, kept or not).
RAW_TAIL_LINES = 40

_REVISE = re.compile(
    r"\b(change|changes|revise|revision|instead|modify|different|tweak|adjust|rather|what about|"
    r"edit the plan|update the plan|rework|redo the plan|but)\b"
)
_ORDINALS = {
    "1": 1, "one": 1, "first": 1,
    "2": 2, "two": 2, "second": 2,
    "3": 3, "three": 3, "third": 3,
    "4": 4, "four": 4, "fourth": 4,
    "5": 5, "five": 5, "fifth": 5,
    "6": 6, "six": 6, "sixth": 6,
    "7": 7, "seven": 7, "seventh": 7,
    "8": 8, "eight": 8, "eighth": 8,
    "9": 9, "nine": 9, "ninth": 9,
}
_ORDINAL_RE = re.compile(r"^(?:option |number |the |pick |choose |select |go with )*(\w+)(?: one| option)?$")
_OPTION_STOP = frozenset({"the", "a", "an", "one", "option", "pick", "choose", "select", "with", "go", "and"})

_STATE_PHRASES = {
    SessionState.IDLE.value: "idle, waiting for your next instruction",
    SessionState.WORKING.value: "working",
    SessionState.AWAITING_PERMISSION.value: "waiting on a permission prompt",
    SessionState.AWAITING_PLAN_APPROVAL.value: "waiting for you to approve a plan",
    SessionState.AWAITING_QUESTION.value: "asking you a question",
    SessionState.STALLED.value: "stalled, with no output for a while",
    SessionState.DETACHED.value: "detached; its pane is gone",
}

_MODE_SPOKEN = {
    "default": "default permissions",
    "acceptEdits": "accept edits mode",
    "plan": "plan mode",
    "auto": "auto mode",
    "dontAsk": "don't-ask mode",
    "bypass": "bypass permissions",
}


@dataclass(slots=True)
class _Draft:
    session_id: str
    parts: list[str]
    deadline: float  # time.monotonic() after which it is submitted


@dataclass(slots=True)
class _Pending:
    kind: str  # delete | plan_revise | focus
    session_id: str | None
    title: str = ""
    candidates: list[Any] = field(default_factory=list)


def match_option(text: str, options: Sequence[str]) -> int | None:
    """Pure: resolve a spoken answer to a 1-based option index, or None."""
    n = normalize_utterance(text)
    if not n or not options:
        return None
    m = _ORDINAL_RE.match(n)
    if m and m.group(1) in _ORDINALS:
        idx = _ORDINALS[m.group(1)]
        return idx if 1 <= idx <= len(options) else None
    labels = [normalize_utterance(o) for o in options]
    for i, label in enumerate(labels, 1):
        if label and label == n:
            return i
    n_tokens = {t for t in n.split() if t not in _OPTION_STOP}
    contains = [i for i, label in enumerate(labels, 1) if label and (n in label or label in n) and len(n) >= 3]
    if len(contains) == 1:
        return contains[0]
    scored: list[tuple[float, int]] = []
    for i, label in enumerate(labels, 1):
        lt = {t for t in label.split() if t not in _OPTION_STOP}
        if not lt or not n_tokens:
            continue
        overlap = len(lt & n_tokens) / len(lt)
        if overlap >= 0.5:
            scored.append((overlap, i))
    if scored:
        scored.sort(reverse=True)
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            return scored[0][1]
    return None


def resolve_verbosity(argument: str | None, current: str) -> str | None:
    """Pure: absolute level, or ``more``/``less`` relative to ``current``."""
    if not argument:
        return None
    a = argument.strip().lower()
    if a in VERBOSITY_LEVELS:
        return a
    levels = list(VERBOSITY_LEVELS)
    cur = levels.index(current) if current in levels else 0
    if a == "more":
        return levels[min(cur + 1, len(levels) - 1)]
    if a == "less":
        return levels[max(cur - 1, 0)]
    return None


def voice_switchable_modes() -> tuple[str, ...]:
    try:
        from zordon.session import permissions  # noqa: PLC0415

        modes = getattr(permissions, "VOICE_SWITCHABLE", None)
        if modes:
            return tuple(str(m) for m in modes)
    except Exception:  # noqa: BLE001 - module may not exist yet
        pass
    return _DEFAULT_VOICE_SWITCHABLE


class DispatcherThread(threading.Thread):
    def __init__(
        self,
        bus: Bus,
        config: Config,
        router: Router,
        sessions: Any,
        store: TranscriptStore,
        speak: SpeakFn,
        settings: Any,
        *,
        normalizer: Any | None = None,
        answerer: TranscriptAnswerer | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        super().__init__(name="zordon-dispatcher", daemon=True)
        self.bus = bus
        self.config = config
        self.router = router
        self.sessions = sessions
        self.store = store
        self.speak = speak
        self.settings = settings
        self.poll_interval = poll_interval
        self._keyword = router if isinstance(router, KeywordRouter) else KeywordRouter()
        self._answerer = answerer or TranscriptAnswerer(
            config.providers.key("anthropic"),
            model=config.providers.normalizer_model or "claude-haiku-4-5",
            timeout=config.providers.normalizer_timeout_seconds,
            normalizer=normalizer,
        )
        self._stop_event = threading.Event()
        self._pending: _Pending | None = None
        self.handled = 0
        # Deferred submit (decision 0019): what has been typed into the focused session's
        # input box by voice and not yet sent, and when to send it.
        self._draft: _Draft | None = None
        self._source = "voice"

    # ---- thread ------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("dispatcher started with router %s", getattr(self.router, "name", "?"))
        while not (self._stop_event.is_set() or self.bus.stop.is_set()):
            try:
                u = self.bus.utterances.get(timeout=self.poll_interval)
            except queue.Empty:
                self.check_submit()
                continue
            self.handle(u)
            self.check_submit()
        log.info("dispatcher stopped")

    def handle(self, u: Utterance) -> None:
        """Process one utterance. Never raises."""
        try:
            self._handle(u)
        except Exception:  # noqa: BLE001 - one bad utterance must not kill the thread
            log.exception("dispatcher: failed to handle utterance")
            self._speak("Something went wrong handling that.", self._focused(), "error")
        finally:
            self.handled += 1

    # ---- main path ---------------------------------------------------------------------

    def _handle(self, u: Utterance) -> None:
        text = (u.text or "").strip()
        if u.source in ("draft", "draft_send"):
            self._edit_draft(text, send=u.source == "draft_send")
            return
        if not text:
            return
        self._source = u.source or "voice"
        if self._submit_mode() != "immediate":
            # "... add the tests, send it Zordon": the tail is the send cue; what is in front
            # of it is the last piece of the draft. Typed text only counts when it is nothing
            # but the cue (the page's Send button sends "send it").
            rest, cue = strip_send_cue(text)
            if cue and self._source != "voice" and rest:
                cue = False
            if cue:
                sid = self._focused()
                if rest and sid is not None and not self._state(sid).startswith("awaiting_"):
                    self._to_claude(rest, sid, self._state(sid), source="voice")
                self._cmd_send(None, text, sid)
                return
        sid = self._focused()
        if self._pending is not None and self._handle_pending(text, sid):
            return
        if sid is None:
            self._no_session(text)
            return
        state = self._state(sid)
        ctx = RouteContext(
            session_state=state,
            transcript_tail=self.store.spoken_tail_text(sid, 12),
            focused_session=sid,
            session_names=self._session_names(),
            commands=list(commands.COMMAND_NAMES),
            project_names=self._project_names(),
        )
        if state == SessionState.AWAITING_PERMISSION.value:
            if not self._prompt_safe_shim(text, sid, ctx):
                self._permission(text, sid)
            return
        if state == SessionState.AWAITING_PLAN_APPROVAL.value:
            if not self._prompt_safe_shim(text, sid, ctx):
                self._plan(text, sid)
            return
        if state == SessionState.AWAITING_QUESTION.value:
            if not self._prompt_safe_shim(text, sid, ctx):
                self._question(text, sid)
            return
        self._route(text, sid, ctx)

    def _route(self, text: str, sid: str, ctx: RouteContext) -> None:
        r = self.router.route(text, ctx)
        dest = effective_destination(r, self.config.voice.router_confidence)
        log.info(
            "route %r -> %s (router said %s %.2f%s)",
            text[:60],
            dest,
            r.destination,
            r.confidence,
            f" {r.command}" if r.command else "",
        )
        if dest == "transcript_query":
            self._transcript(text, sid, ctx.transcript_tail, self._raw_tail(sid))
        elif dest == "shim_command":
            self._execute(r.command, r.argument, text, sid, ctx)
        else:
            self._to_claude(text, sid, ctx.session_state, source=self._source)

    def _to_claude(self, text: str, sid: str, state: str, *, source: str = "voice") -> None:
        if state == SessionState.DETACHED.value:
            self._speak("That session is detached. Resume it from the app first.", sid, "error")
            return
        mode = self._submit_mode()
        compose = getattr(self.sessions, "compose", None)
        if source == "voice" and mode != "immediate" and callable(compose):
            # Deferred submit: type now; send on the cue ("send it", "go ahead"), and in
            # quiet mode also after submit_quiet_ms with nothing more said.
            if self._draft is not None and self._draft.session_id != sid:
                self._flush_draft()
            compose(sid, text)
            now = time.monotonic()
            quiet_s = int(getattr(self.config.voice, "submit_quiet_ms", 0) or 0) / 1000.0
            deadline = now + quiet_s if mode == "quiet" and quiet_s > 0 else float("inf")
            if self._draft is None:
                self._draft = _Draft(sid, [text], deadline)
            else:
                self._draft.parts.append(text)
                self._draft.deadline = deadline
            self._publish_draft("composing")
            return
        self.sessions.send_text(sid, text)
        self._user_row(sid, text)

    # ---- deferred submit -----------------------------------------------------------------

    def _edit_draft(self, text: str, *, send: bool) -> None:
        """The draft box was edited (or its Send pressed): what is in the box is the draft.
        The agent's input is cleared and retyped so the pane matches the page."""
        sid = self._draft.session_id if self._draft is not None else self._focused()
        if sid is None:
            self._speak("No project is open.", None, "error")
            return
        clear = getattr(self.sessions, "clear_input", None)
        compose = getattr(self.sessions, "compose", None)
        if self._draft is not None and callable(clear):
            try:
                clear(sid)
            except Exception:  # noqa: BLE001
                log.exception("clear_input failed")
        self._draft = None
        if not text:
            try:
                self.bus.publish(Draft(session_id=sid, text="", state="cleared"))
            except Exception:  # noqa: BLE001
                pass
            return
        if callable(compose):
            compose(sid, text)
        self._draft = _Draft(sid, [text], float("inf"))
        if send:
            self._flush_draft()
        else:
            self._publish_draft("composing")

    def _submit_mode(self) -> str:
        mode = str(getattr(self.config.voice, "submit_mode", "") or "")
        if mode in ("keyphrase", "quiet", "immediate"):
            return mode
        # Older configs: submit_quiet_ms alone decided.
        return "quiet" if int(getattr(self.config.voice, "submit_quiet_ms", 0) or 0) > 0 else "immediate"

    def _publish_draft(self, state: str, draft: _Draft | None = None) -> None:
        d = draft or self._draft
        if d is None:
            return
        try:
            self.bus.publish(Draft(session_id=d.session_id, text=" ".join(p.strip() for p in d.parts), state=state))
        except Exception:  # noqa: BLE001
            pass

    def check_submit(self, now: float | None = None) -> bool:
        """Send the draft when its quiet period has passed. Not while a prompt is up: an
        Enter then would answer nothing and queue the text behind the prompt."""
        d = self._draft
        if d is None:
            return False
        now = time.monotonic() if now is None else now
        if now < d.deadline:
            return False
        state = self._state(d.session_id)
        if state.startswith("awaiting_"):
            d.deadline = now + 1.0  # look again once the prompt is answered
            return False
        return self._flush_draft()

    def _flush_draft(self) -> bool:
        d, self._draft = self._draft, None
        if d is None:
            return False
        text = " ".join(p.strip() for p in d.parts if p.strip())
        try:
            sent = self.sessions.submit(d.session_id)
        except Exception:  # noqa: BLE001
            log.exception("submit failed")
            sent = False
        if sent:
            self._user_row(d.session_id, text)
            log.info("submitted %d part(s) to %s", len(d.parts), d.session_id[:8])
            self._publish_draft("sent", d)
        return bool(sent)

    def _cmd_send(self, argument: str | None, text: str, sid: str | None) -> None:
        if self._draft is None:
            self._speak("Nothing is waiting to be sent.", sid, "ack")
            return
        if not self._flush_draft():
            self._speak("I couldn't send that.", sid, "error")
        # A successful send says nothing: the draft box clears and the agent starts working,
        # and the page plays its "working" cue. "Sent." after every message was noise.

    def _cmd_scratch(self, argument: str | None, text: str, sid: str | None) -> None:
        d, self._draft = self._draft, None
        target = d.session_id if d is not None else sid
        cleared = False
        if target:
            try:
                cleared = bool(self.sessions.clear_input(target))
            except Exception:  # noqa: BLE001
                log.exception("clear_input failed")
        if d is not None:
            self._publish_draft("cleared", d)
        self._speak("Cleared." if cleared or d is not None else "Nothing to clear.", sid, "ack")

    def _user_row(self, sid: str, text: str) -> None:
        """Record what the user sent and show it on the page: the transcript's "you" side.
        (The audio thread used to write a row per utterance; since the draft, the row is
        written when the text is actually sent.)"""
        try:
            event_id = self.store.add_event(sid, "user", text)
        except Exception:  # noqa: BLE001
            log.exception("transcript add_event failed")
            event_id = 0
        try:
            self.bus.publish(TranscriptRow(row_id=-int(event_id or 0), session_id=sid, kind="user", text=text, raw_lines=[], ts=time.time()))
        except Exception:  # noqa: BLE001
            log.debug("could not publish the user row", exc_info=True)

    def _transcript(self, text: str, sid: str, tail: list[str], raw: list[str] | None = None) -> None:
        answer = self._answerer.answer(text, tail, raw=raw)
        self._user_row(sid, text)
        self.store.add_event(sid, "notice", answer)
        self._speak(answer, sid, "answer")

    # ---- prompt states -----------------------------------------------------------------

    def _prompt_safe_shim(self, text: str, sid: str, ctx: RouteContext) -> bool:
        kw = self._keyword.route(text, ctx)
        if kw.destination == "shim_command" and kw.confidence >= 0.95 and kw.command in SAFE_IN_PROMPT:
            self._execute(kw.command, kw.argument, text, sid, ctx)
            return True
        return False

    def _permission(self, text: str, sid: str) -> None:
        if forbidden_permission_phrase(text):
            self._speak("I can't grant always-allow by voice. Yes or no?", sid, "question")
            return
        r = self.router.yes_no(text)
        threshold = self.config.voice.yes_no_confidence
        prompt = self._current_prompt(sid)
        trust = prompt is not None and _prompt_kind(prompt) == PromptKind.TRUST.value
        if r.answer == "yes" and r.confidence >= threshold:
            ok = self.sessions.accept_trust(sid) if trust else self.sessions.approve(sid)
            self._speak("Approved." if ok else "I couldn't approve that.", sid, "ack")
        elif r.answer == "no" and r.confidence >= threshold:
            ok = self.sessions.decline_trust(sid) if trust else self.sessions.deny(sid)
            self._speak("Denied." if ok else "I couldn't deny that.", sid, "ack")
        else:
            self._speak(f"I heard: {text}. Yes or no?", sid, "question")

    def _plan(self, text: str, sid: str) -> None:
        n = normalize_utterance(text)
        if forbidden_permission_phrase(text) or "auto mode" in n:
            self._speak(
                "I can only approve the plan with manual edits by voice. Approve, revise, or deny?",
                sid,
                "question",
            )
            return
        if _REVISE.search(n):
            self._pending = _Pending("plan_revise", sid)
            self._speak("What should change?", sid, "question")
            return
        r = self.router.yes_no(text)
        threshold = self.config.voice.yes_no_confidence
        if r.answer == "yes" and r.confidence >= threshold:
            ok = self.sessions.plan_approve(sid)
            self._speak(
                "Plan approved. Claude Code will ask before each edit." if ok else "I couldn't approve the plan.",
                sid,
                "ack",
            )
        elif r.answer == "no" and r.confidence >= threshold:
            ok = self.sessions.plan_deny(sid)
            self._speak("Plan rejected." if ok else "I couldn't reject the plan.", sid, "ack")
        else:
            self._speak("Approve, revise, or deny the plan?", sid, "question")

    def _question(self, text: str, sid: str) -> None:
        prompt = self._current_prompt(sid)
        options = [_option_label(o) for o in (getattr(prompt, "options", None) or [])] if prompt is not None else []
        if not options:
            self._speak("Claude Code is asking a question, but I couldn't read the options.", sid, "error")
            return
        idx = match_option(text, options)
        if idx is None:
            listing = "; ".join(f"{i}, {o}" for i, o in enumerate(options, 1))
            self._speak(f"The options are: {listing}. Which one?", sid, "question")
            return
        ok = self.sessions.answer_question(sid, idx)
        self._speak(f"Picked {options[idx - 1]}." if ok else "I couldn't answer that.", sid, "ack")

    # ---- pending confirmations ---------------------------------------------------------

    def _handle_pending(self, text: str, sid: str | None) -> bool:
        pending, self._pending = self._pending, None
        if pending is None:
            return False
        if pending.kind == "delete":
            if pending.session_id != sid or sid is None:
                return False
            r = self._keyword.yes_no(text)
            if r.answer == "yes" and r.confidence >= self.config.voice.yes_no_confidence:
                self.sessions.delete(sid)
                self._speak(f"Deleted {pending.title}.", sid, "ack")
            else:
                self._speak("Okay, not deleting.", sid, "ack")
            return True
        if pending.kind == "plan_revise":
            if pending.session_id != sid or sid is None:
                return False
            if self._state(sid) != SessionState.AWAITING_PLAN_APPROVAL.value:
                return False
            ok = self.sessions.plan_revise(sid, text)
            self._speak("Sent your changes to Claude Code." if ok else "I couldn't send that.", sid, "ack")
            return True
        if pending.kind == "focus":
            r = self._keyword.yes_no(text)
            if r.answer == "no":
                self._speak("Okay.", sid, "ack")
                return True
            hits = _resolve_sessions(text, pending.candidates, None)
            if len(hits) == 1:
                self._focus_on(hits[0])
                return True
            return False
        return False

    # ---- shim commands -----------------------------------------------------------------

    def _no_session(self, text: str) -> None:
        kw = self._keyword.route(
            text, RouteContext(session_state="none", session_names=self._session_names(), project_names=self._project_names())
        )
        if kw.destination == "shim_command" and kw.command:
            # Commands that need a session are refused inside _execute.
            self._execute(kw.command, kw.argument, text, None, None)
            return
        if self._project_names():
            self._speak("No project is open. Say open project and its name, or list projects.", None, "error")
            return
        self._speak("No project is open. Tap Start a new project in the app.", None, "error")

    def _execute(
        self, command: str | None, argument: str | None, text: str, sid: str | None, ctx: RouteContext | None
    ) -> None:
        cmd = commands.BY_NAME.get(command or "")
        if cmd is None:
            log.warning("router selected unknown command %r", command)
            self._speak("I don't know that command.", sid, "error")
            return
        if cmd.name in NEEDS_SESSION and sid is None:
            self._speak("No session is focused.", None, "error")
            return
        handler = getattr(self, f"_cmd_{cmd.name}")
        handler(argument, text, sid)

    def _cmd_mute(self, argument: str | None, text: str, sid: str | None) -> None:
        # speak() only queues; the mute below takes effect first, so the ack has to be
        # allowed through it or the user never hears that mute worked.
        self._speak("Muted.", sid, "ack", bypass_mute=True)
        self.settings.set_muted(True)

    def _cmd_unmute(self, argument: str | None, text: str, sid: str | None) -> None:
        self.settings.set_muted(False)
        self._speak("Unmuted.", sid, "ack")

    def _cmd_hush(self, argument: str | None, text: str, sid: str | None) -> None:
        """Stop the voice, not the agent: the rest of the answer is dropped, nothing is typed."""
        hush = getattr(self.settings, "hush", None)
        if callable(hush):
            hush()
            return
        generation = self.bus.next_generation()
        self.bus.publish(Flush(generation=generation))

    def _cmd_stop(self, argument: str | None, text: str, sid: str | None) -> None:
        self.sessions.send_escape(sid)
        self._speak("Stopped.", sid, "ack")

    def _cmd_repeat(self, argument: str | None, text: str, sid: str | None) -> None:
        last = self.store.last_spoken(sid) if sid else None
        self._speak(last or "Nothing to repeat yet.", sid, "repeat")

    def _cmd_status(self, argument: str | None, text: str, sid: str | None) -> None:
        state = self._state(sid)
        phrase = _STATE_PHRASES.get(state, state.replace("_", " "))
        title = self._title_of(sid)
        msg = f"{title} is {phrase}."
        try:
            lines = [ln for ln in self.sessions.last_pane_lines(sid, 5) if ln and ln.strip()]
        except Exception:  # noqa: BLE001
            lines = []
        if lines:
            msg += f" Last line: {lines[-1].strip()}"
        health = getattr(self.settings, "health_summary_sentence", None)
        if callable(health):
            try:
                extra = str(health() or "").strip()
            except Exception:  # noqa: BLE001 - a health probe must not break "status"
                extra = ""
            if extra:
                msg += " " + extra
        self._speak(msg, sid, "answer")

    def _cmd_set_verbosity(self, argument: str | None, text: str, sid: str | None) -> None:
        current = str(self._current_setting("verbosity", self.config.voice.verbosity))
        level = resolve_verbosity(argument, current)
        if level is None:
            self._speak("Verbosity can be minimal, normal or technical.", sid, "question")
            return
        self.settings.set_verbosity(level)
        self._speak(f"Verbosity set to {level}.", sid, "ack")

    def _cmd_set_tool_chatter(self, argument: str | None, text: str, sid: str | None) -> None:
        if argument in ("on", "off"):
            enabled = argument == "on"
        else:
            enabled = not bool(self._current_setting("tool_chatter", self.config.voice.tool_chatter))
        self.settings.set_tool_chatter(enabled)
        self._speak("Tool chatter on." if enabled else "Tool chatter off.", sid, "ack")

    def _cmd_focus(self, argument: str | None, text: str, sid: str | None) -> None:
        summaries = self._sessions_list()
        if not summaries:
            self._speak("I don't see any sessions.", sid, "error")
            return
        if not argument:
            self._speak(f"Which session? {self._names_sentence(summaries)}", sid, "question")
            return
        hits = _resolve_sessions(argument, summaries, sid)
        if len(hits) == 1:
            self._focus_on(hits[0])
            return
        if len(hits) > 1:
            self._pending = _Pending("focus", sid, candidates=hits)
            names = " or ".join(_title(s) for s in hits)
            self._speak(f"Which one: {names}?", sid, "question")
            return
        self._speak(
            f"I don't see a session called {argument}. {self._names_sentence(summaries)}", sid, "error"
        )

    # ---- projects (decision 0018) ------------------------------------------------------

    def _projects_list(self) -> list[dict[str, Any]]:
        lister = getattr(self.sessions, "list_projects", None)
        if not callable(lister):
            return []
        try:
            return [dict(p) for p in lister() or []]
        except Exception:  # noqa: BLE001
            log.exception("sessions.list_projects failed")
            return []

    def _project_names(self) -> list[str]:
        return [str(p.get("name") or "") for p in self._projects_list() if p.get("name")]

    def _cmd_open_project(self, argument: str | None, text: str, sid: str | None) -> None:
        projects = self._projects_list()
        if not projects:
            self._speak("There are no saved projects yet. Tap Start a new project in the app.", sid, "error")
            return
        names = [str(p["name"]) for p in projects]
        if not argument:
            self._speak(f"Which project? {_names_sentence_from(names)}", sid, "question")
            return
        hits = fuzzy_match_sessions(argument, names)
        if len(hits) != 1:
            if len(hits) > 1:
                self._speak(f"Which one: {' or '.join(hits)}?", sid, "question")
            else:
                self._speak(f"I don't have a project called {argument}. {_names_sentence_from(names)}", sid, "error")
            return
        project = next(p for p in projects if p["name"] == hits[0])
        try:
            row = self.sessions.open_project(str(project["id"]))
        except Exception as e:  # noqa: BLE001
            self._speak(f"I couldn't open {hits[0]}: {e}", sid, "error")
            return
        new_sid = row.get("session_id") if isinstance(row, dict) else None
        self._speak(f"Opened {hits[0]}.", new_sid, "ack")

    def _cmd_list_projects(self, argument: str | None, text: str, sid: str | None) -> None:
        projects = self._projects_list()
        if not projects:
            self._speak("There are no saved projects. Tap Start a new project in the app.", sid, "answer")
            return
        parts = [f"{p['name']}, running" if p.get("running") else str(p["name"]) for p in projects]
        count = len(projects)
        noun = "project" if count == 1 else "projects"
        self._speak(f"There {'is' if count == 1 else 'are'} {count} {noun}: " + "; ".join(parts) + ".", sid, "answer")

    def _cmd_new_project(self, argument: str | None, text: str, sid: str | None) -> None:
        # The walkthrough (folder, agent, how much to ask) is visual; voice only points at it.
        self._speak("Tap Start a new project in the app and I'll walk you through it.", sid, "ack")
        try:
            self.bus.publish(Notice(text="Start a new project: tap the button in Projects.", level="info", session_id=sid or ""))
        except Exception:  # noqa: BLE001
            pass

    def _cmd_admin(self, argument: str | None, text: str, sid: str | None) -> None:
        admin = getattr(self.sessions, "admin", None)
        if not callable(admin):
            self._speak("I can't pause here.", sid, "error")
            return
        admin()
        if sid is None:
            self._speak("Nothing is open.", None, "ack")
        else:
            self._speak("Paused. The project keeps running; say open project and its name to come back.", None, "ack")

    def _cmd_list_sessions(self, argument: str | None, text: str, sid: str | None) -> None:
        summaries = self._sessions_list()
        if not summaries:
            self._speak("There are no sessions.", sid, "answer")
            return
        parts = []
        for s in summaries:
            state = _summary_state(s)
            parts.append(f"{_title(s)}, {state.replace('_', ' ')}" if state else _title(s))
        count = len(summaries)
        noun = "session" if count == 1 else "sessions"
        self._speak(f"There {'is' if count == 1 else 'are'} {count} {noun}: " + "; ".join(parts) + ".", sid, "answer")

    def _cmd_set_permission_mode(self, argument: str | None, text: str, sid: str | None) -> None:
        if not argument:
            self._speak(self._permission_summary(sid), sid, "answer")
            return
        mode = argument
        if mode not in voice_switchable_modes():
            spoken = _MODE_SPOKEN.get(mode, mode)
            self._speak(
                f"I can't switch to {spoken} by voice. Use the app or Claude Code's settings.", sid, "error"
            )
            return
        ok = self.sessions.set_permission_mode(sid, mode)
        if ok:
            self._speak(self._permission_summary(sid) or f"Switched to {_MODE_SPOKEN.get(mode, mode)}.", sid, "ack")
        else:
            self._speak("I couldn't switch the permission mode.", sid, "error")

    def _cmd_delete(self, argument: str | None, text: str, sid: str | None) -> None:
        title = self._title_of(sid)
        self._pending = _Pending("delete", sid, title=title)
        self._speak(f"Delete the session {title}? Say yes to confirm.", sid, "question")

    def _cmd_detach(self, argument: str | None, text: str, sid: str | None) -> None:
        title = self._title_of(sid)
        self.sessions.detach(sid)
        self._speak(f"Detached from {title}. It keeps running.", sid, "ack")

    # ---- helpers -----------------------------------------------------------------------

    def _speak(self, text: str, sid: str | None, kind: str, *, bypass_mute: bool = False) -> None:
        try:
            if bypass_mute:
                self.speak(text, sid or "", SPEAK_KINDS.get(kind, LineKind.PROSE), bypass_mute=True)
            else:
                self.speak(text, sid or "", SPEAK_KINDS.get(kind, LineKind.PROSE))
        except Exception:  # noqa: BLE001
            log.exception("speak failed")

    def _raw_tail(self, sid: str) -> list[str]:
        try:
            return self.store.raw_tail(sid, RAW_TAIL_LINES)
        except Exception:  # noqa: BLE001
            log.exception("store.raw_tail failed")
            return []

    def _focused(self) -> str | None:
        try:
            return self.sessions.focused()
        except Exception:  # noqa: BLE001
            log.exception("sessions.focused failed")
            return None

    def _state(self, sid: str | None) -> str:
        if sid is None:
            return "none"
        s = self.sessions.state_of(sid)
        return str(getattr(s, "value", s))

    def _current_prompt(self, sid: str) -> Any | None:
        try:
            return self.sessions.current_prompt(sid)
        except Exception:  # noqa: BLE001
            return None

    def _sessions_list(self) -> list[Any]:
        try:
            return list(self.sessions.list_sessions() or [])
        except Exception:  # noqa: BLE001
            log.exception("sessions.list_sessions failed")
            return []

    def _session_names(self) -> list[str]:
        names: list[str] = []
        for s in self._sessions_list():
            for n in (_title(s), _basename(_directory(s))):
                if n and n not in names:
                    names.append(n)
        return names

    def _title_of(self, sid: str | None) -> str:
        for s in self._sessions_list():
            if _session_id(s) == sid:
                return _title(s)
        return "this session"

    def _names_sentence(self, summaries: Sequence[Any]) -> str:
        return "The sessions are: " + ", ".join(_title(s) for s in summaries) + "."

    def _focus_on(self, summary: Any) -> None:
        target = _session_id(summary)
        self.sessions.focus(target)
        self._speak(f"Switched to {_title(summary)}.", target, "ack")

    def _permission_summary(self, sid: str | None) -> str:
        try:
            return str(self.sessions.permission_summary(sid) or "")
        except Exception:  # noqa: BLE001
            return ""

    def _current_setting(self, key: str, default: Any) -> Any:
        getter = getattr(self.settings, "settings", None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, dict) and key in value:
                    return value[key]
            except Exception:  # noqa: BLE001
                pass
        return getattr(self.settings, key, default)


# ---- session summary accessors (summaries may be dataclasses, pydantic models or dicts) ----


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _session_id(s: Any) -> str:
    return str(_get(s, "session_id") or _get(s, "id") or "")


def _directory(s: Any) -> str:
    return str(_get(s, "directory") or _get(s, "cwd") or "")


def _basename(path: str) -> str:
    return PurePath(path).name if path else ""


def _title(s: Any) -> str:
    t = _get(s, "title")
    if t:
        return str(t)
    base = _basename(_directory(s))
    if base:
        return base
    return _session_id(s)[:8] or "unnamed"


def _summary_state(s: Any) -> str:
    st = _get(s, "state")
    return str(getattr(st, "value", st) or "")


def _option_label(option: Any) -> str:
    if isinstance(option, str):
        return option
    label = _get(option, "label") or _get(option, "text")
    return str(label) if label else str(option)


def _prompt_kind(prompt: Any) -> str:
    k = _get(prompt, "kind")
    return str(getattr(k, "value", k) or "")


# The send cue at the end of an utterance (decision 0019). Speech recognition spells
# "Zordon" many ways, so the name is matched loosely; the cue may stand alone or close a
# longer sentence ("add the tests, send it Zordon").
_ZORDON = r"z[oa]r+[aeiou]?[-' ]?d[aoe]+n?e?"
_SEND_CUE = re.compile(
    r"(?:^|[,.;!?\s])(?:(?:ok(?:ay)?|alright|please)[,\s]+)?"
    r"(?:(?:" + _ZORDON + r")[,\s]+)?"
    r"(?:send(?: it| that| this| the message)?|go ahead(?: and send(?: it)?)?|ship it|submit(?: it| that)?|that'?s (?:all|it)|done talking|end of message)"
    r"(?:[,\s]+" + _ZORDON + r")?[\s.!?,]*$",
    re.IGNORECASE,
)


def strip_send_cue(text: str) -> tuple[str, bool]:
    """``("add the tests", True)`` for "add the tests, send it Zordon"; ``(text, False)``
    when the utterance does not end with a send cue."""
    m = _SEND_CUE.search(text)
    if not m:
        return text, False
    rest = text[: m.start()].rstrip(" ,.;")
    return rest, True


def _names_sentence_from(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"The only project is {names[0]}."
    return "Projects: " + ", ".join(names[:-1]) + f", and {names[-1]}."


def _resolve_sessions(query: str, summaries: Sequence[Any], focused: str | None) -> list[Any]:
    """Pure: which summaries does a spoken name refer to?"""
    q = normalize_utterance(query)
    others = [s for s in summaries if _session_id(s) != focused]
    if q in ("other", "next", "previous", "last", "the other one"):
        return others if len(others) == 1 else list(others)
    if q == "first" and summaries:
        return [summaries[0]]
    by_name: dict[str, list[Any]] = {}
    for s in summaries:
        for name in (_title(s), _basename(_directory(s)), _directory(s), _session_id(s)):
            if name:
                by_name.setdefault(name, []).append(s)
    hits = fuzzy_match_sessions(q, by_name.keys())
    found: list[Any] = []
    for name in hits:
        for s in by_name[name]:
            if s not in found:
                found.append(s)
    if len(found) > 1 and len(words(q)) == 1:
        # Prefer a title match over a path match when the spoken word is one token.
        titled = [s for s in found if normalize_utterance(_title(s)) == q]
        if len(titled) == 1:
            return titled
    return found
