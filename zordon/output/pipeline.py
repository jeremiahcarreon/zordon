"""PipelineThread: ``bus.pane_lines`` -> pre-pass -> verbosity filter -> sentences
-> normalizer -> transcript -> TTS -> ``bus.playback``.

Two threads live here. The pipeline thread (this class) consumes pane lines,
classifies them, assembles sentences and submits normalizer calls to a small
worker pool. A speaker thread drains the resulting jobs in order, waits for
each normalization (falling back to the pre-passed text on error or timeout),
writes the transcript row and streams PCM to ``bus.playback``.

Turn structure:

* The first prose sentence of a turn is INTENT (spoken at every level).
* Trailing prose is held (at most two sentences) until the turn ends or a lull
  passes, so the last one or two sentences can be tagged SUMMARY (never
  filtered). At ``minimal`` that is all a listener hears besides prompts and
  errors.
* The first ``voice.prebuffer_sentences`` normalized sentences of a turn are
  held before audio is released, so a slow normalizer does not leave a gap
  mid-speech. Turn end or a priority item releases early.

Priority items (permission prompts, plan approvals, questions, errors, notices)
arrive through ``speak_now`` from the session manager or dispatcher. They skip
filtering, the sentence buffer, the normalizer and the prebuffer, and go to the
head of the speaker queue. Muted still writes transcript rows; only TTS is
skipped. Redaction happens before anything else sees a line.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from zordon.bus import Bus, LineKind, PaneLine, Sentence, SpeechChunk, TranscriptRow
from zordon.config import Config
from zordon.output.prepass import (
    PrepassState,
    Tagged,
    flush_prepass,
    prepass_markdown,
    prepass_pane_line,
)
from zordon.output.sentences import SentenceBuffer
from zordon.output.tooldesc import describe_tool_result, describe_tool_use
from zordon.output.verbosity import keep
from zordon.providers import Normalizer, ProviderError, TTSProvider
from zordon.transcript.redaction import redact
from zordon.transcript.store import TranscriptStore

log = logging.getLogger("zordon.output.pipeline")

PRIORITY_KINDS = {LineKind.PERMISSION_PROMPT, LineKind.PLAN, LineKind.QUESTION, LineKind.ERROR}
_PROSE_KINDS = {LineKind.PROSE, LineKind.INTENT, LineKind.SUMMARY}
_SUMMARY_PAIR_MAX_CHARS = 160
_SUMMARY_SHORT_LAST = 40
_MAX_SENTENCES_QUEUE = 1000
_SPOKEN_BLOCKS = {"", "text", "tool_use", "tool_result"}


@dataclass(slots=True)
class _Pending:
    """A complete sentence waiting to learn whether it is the turn's summary."""

    text: str
    raw_ids: list[int]
    raw_texts: list[str]
    paragraph: int = 0


@dataclass(slots=True)
class _Job:
    sentence: Sentence
    raw_texts: list[str]
    generation: int
    enqueued_at: float
    future: Future[str] | None = None  # None: speak the text as is
    submitted_at: float = 0.0
    turn_end: bool = False  # marker: re-arm the prebuffer for this session


@dataclass
class _SessionCtx:
    session_id: str
    prepass: PrepassState = field(default_factory=PrepassState)
    sentences: SentenceBuffer = field(default_factory=SentenceBuffer)
    pending_raw_ids: list[int] = field(default_factory=list)
    pending_raw_texts: list[str] = field(default_factory=list)
    block_raw_ids: list[int] = field(default_factory=list)
    block_raw_texts: list[str] = field(default_factory=list)
    trailing: deque[_Pending] = field(default_factory=lambda: deque(maxlen=3))
    last_released: list[tuple[_Pending, bool]] = field(default_factory=list)
    intent_pending: bool = True
    paragraph: int = 0
    last_line_ts: float = field(default_factory=time.monotonic)
    context: deque[_Job] = field(default_factory=lambda: deque(maxlen=2))
    turn_open: bool = False


class PipelineThread(threading.Thread):
    """Owns the normalizer and TTS providers. See the module docstring."""

    def __init__(
        self,
        bus: Bus,
        config: Config,
        normalizer: Normalizer,
        tts: TTSProvider,
        store: TranscriptStore | None = None,
        muted: Callable[[], bool] | threading.Event | None = None,
        *,
        transcript_store: TranscriptStore | None = None,
        lull_seconds: float = 1.5,
        prebuffer_max_wait: float = 2.5,
        workers: int = 2,
        poll_timeout: float = 0.1,
    ) -> None:
        super().__init__(name="zordon-pipeline", daemon=True)
        self.bus = bus
        self.config = config
        self.normalizer = normalizer
        self.tts = tts
        if store is None and transcript_store is None:
            raise TypeError("PipelineThread needs a TranscriptStore (store= or transcript_store=)")
        self.store = store if store is not None else transcript_store
        self._muted = muted
        self.lull_seconds = lull_seconds
        self.prebuffer_max_wait = prebuffer_max_wait
        self.poll_timeout = poll_timeout
        self._stopping = threading.Event()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="zordon-normalize"
        )
        self._cv = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._prebuffering: dict[str, bool] = {}
        self._sessions: dict[str, _SessionCtx] = {}
        self._speaker = threading.Thread(
            target=self._speak_loop, name="zordon-speaker", daemon=True
        )
        self._control: queue.Queue[Callable[[], None]] = queue.Queue()
        self._stats_lock = threading.Lock()
        self._stats: dict[str, Any] = {
            "lines_in": 0,
            "tagged": 0,
            "kept": 0,
            "dropped": 0,
            "sentences": 0,
            "normalized": 0,
            "normalizer_failures": 0,
            "literal": 0,
            "synthesized": 0,
            "chunks": 0,
            "tts_failures": 0,
            "priority": 0,
            "dropped_by_generation": 0,
            "muted_skips": 0,
            "turns": 0,
            "kinds": {},
        }

    # ---- public ----------------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()
        with self._cv:
            self._cv.notify_all()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def speak_now(
        self, text: str, session_id: str, kind: LineKind = LineKind.PERMISSION_PROMPT
    ) -> int:
        """Speak ``text`` immediately: no filter, no sentence buffer, no prebuffer.
        Used for permission prompts, plan approvals, questions, errors and
        notices. Returns the sentence id."""
        masked, _ = redact(text)
        masked = masked.strip()
        sentence = Sentence(
            session_id=session_id,
            text=masked,
            raw_text=masked,
            raw_line_ids=[],
            kind=kind,
            priority=True,
        )
        job = _Job(
            sentence=sentence,
            raw_texts=[],
            generation=self.bus.generation,
            enqueued_at=time.monotonic(),
        )
        with self._cv:
            # Ahead of every ordinary job, behind priority jobs already waiting.
            pos = 0
            while pos < len(self._jobs) and self._jobs[pos].sentence.priority:
                pos += 1
            self._jobs.insert(pos, job)
            self._cv.notify_all()
        self._bump("priority")
        return sentence.sentence_id

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            out = dict(self._stats)
            out["kinds"] = dict(self._stats["kinds"])
        with self._cv:
            out["queued"] = len(self._jobs)
        out["sessions"] = len(self._sessions)
        out["verbosity"] = self.config.voice.verbosity
        out["tool_chatter"] = self.config.voice.tool_chatter
        out["muted"] = self._is_muted()
        return out

    def flush(self, session_id: str | None = None) -> None:
        """Release held prose for one or every session. Runs on the pipeline thread."""

        def _do() -> None:
            for ctx in list(self._sessions.values()):
                if session_id is None or ctx.session_id == session_id:
                    self._release_prose(ctx)

        self._control.put(_do)

    # ---- thread body -----------------------------------------------------------

    def run(self) -> None:
        self._speaker.start()
        try:
            while not self._stopping.is_set() and not self.bus.stop.is_set():
                try:
                    line = self.bus.pane_lines.get(timeout=self.poll_timeout)
                except queue.Empty:
                    line = None
                if line is not None:
                    try:
                        self._handle_line(line)
                    except Exception:  # noqa: BLE001
                        log.exception("pipeline failed on a line from session %s", line.session_id)
                self._run_control()
                self._check_lulls()
        finally:
            self._stopping.set()
            with self._cv:
                self._cv.notify_all()
            self._speaker.join(timeout=2.0)
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _run_control(self) -> None:
        while True:
            try:
                fn = self._control.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("pipeline control request failed")

    # ---- line handling -----------------------------------------------------------

    def _ctx(self, session_id: str) -> _SessionCtx:
        ctx = self._sessions.get(session_id)
        if ctx is None:
            ctx = _SessionCtx(session_id=session_id)
            self._sessions[session_id] = ctx
            self._prebuffering.setdefault(session_id, True)
        return ctx

    def _handle_line(self, line: PaneLine) -> None:
        self._bump("lines_in")
        ctx = self._ctx(line.session_id)
        ctx.last_line_ts = time.monotonic()
        masked, _ = redact(line.text or "")
        raw_id = self.store.add_raw(line.session_id, line.text or "", line.ts, line.source)

        block = line.block or ""
        if block == "turn_end":
            self._turn_end(ctx)
            return
        if block in ("turn_start", "user_prompt"):
            self._turn_start(ctx)
            return
        if line.source == "jsonl" and block not in _SPOKEN_BLOCKS:
            # thinking, permission_mode, title and anything new: transcript only.
            return

        complete = False
        if block == "tool_use":
            name = str(line.meta.get("name") or line.meta.get("tool_name") or masked or "tool")
            inp = line.meta.get("input") or line.meta.get("tool_input") or {}
            tagged = [describe_tool_use(name, inp if isinstance(inp, dict) else {})]
            complete = True
        elif block == "tool_result":
            tagged = [
                describe_tool_result(
                    masked,
                    is_error=bool(line.meta.get("is_error")),
                    is_rejection=bool(line.meta.get("rejected") or line.meta.get("is_rejection")),
                )
            ]
            complete = True
        elif line.source == "jsonl":
            tagged = prepass_markdown(masked, ctx.prepass)
            complete = True
        else:
            tagged = prepass_pane_line(masked, ctx.prepass)
            if not tagged:
                # Absorbed into an open code/diff/table block; link it when the block closes.
                ctx.block_raw_ids.append(raw_id)
                ctx.block_raw_texts.append(masked)

        for t in tagged:
            self._handle_tagged(ctx, t, raw_id, masked, complete)

    def _handle_tagged(
        self, ctx: _SessionCtx, t: Tagged, raw_id: int, raw_text: str, complete: bool
    ) -> None:
        self._bump("tagged")
        self._bump_kind(t.kind)
        kind = t.kind

        if kind is LineKind.SUMMARY and t.meta.get("marker"):
            self._turn_end(ctx)
            return
        if kind is LineKind.UI and t.meta.get("turn_start"):
            self._turn_start(ctx)
            return
        if kind is LineKind.BLANK:
            # Paragraph boundary: whatever the sentence buffer holds is complete.
            for s in ctx.sentences.flush():
                self._complete_sentence(ctx, s)
            ctx.paragraph += 1
            return
        if kind in (LineKind.UI, LineKind.PROGRESS):
            return

        if kind is LineKind.PROSE:
            if not t.spoken:
                return
            if t.meta.get("list_item") or t.meta.get("paragraph_start"):
                for s in ctx.sentences.flush():
                    self._complete_sentence(ctx, s)
                ctx.paragraph += 1
            ctx.pending_raw_ids.append(raw_id)
            ctx.pending_raw_texts.append(raw_text)
            ctx.turn_open = True
            for s in ctx.sentences.push(t.spoken):
                self._complete_sentence(ctx, s)
            if complete or t.meta.get("complete"):
                for s in ctx.sentences.flush():
                    self._complete_sentence(ctx, s)
            return

        # Everything else is a self-contained item: filter it, keep order.
        raw_ids, raw_texts = [raw_id], [raw_text]
        if kind in (LineKind.CODE, LineKind.DIFF) and ctx.block_raw_ids:
            raw_ids, raw_texts = list(ctx.block_raw_ids), list(ctx.block_raw_texts)
            ctx.block_raw_ids, ctx.block_raw_texts = [], []
        touches = bool(t.meta.get("touches_file"))
        # A block or tool call between two sentences separates them for the summary choice.
        for s in ctx.sentences.flush():
            self._complete_sentence(ctx, s)
        ctx.paragraph += 1
        if not keep(
            kind, self.config.voice.verbosity, self.config.voice.tool_chatter, touches_file=touches
        ):
            self._bump("dropped")
            return
        if t.spoken is None:
            return  # placeholder (AskUserQuestion / ExitPlanMode): the prompt path speaks it
        self._bump("kept")
        self._release_prose(ctx)
        ctx.turn_open = True
        self._enqueue(ctx, t.spoken, kind, raw_ids, raw_texts, literal=bool(t.meta.get("literal")))

    # ---- turn structure ------------------------------------------------------------

    def _turn_start(self, ctx: _SessionCtx) -> None:
        if ctx.turn_open:
            self._turn_end(ctx)
        ctx.intent_pending = True
        ctx.last_released = []
        self._prebuffering[ctx.session_id] = True

    def _turn_end(self, ctx: _SessionCtx) -> None:
        for t in flush_prepass(ctx.prepass):
            last_id = ctx.block_raw_ids[-1] if ctx.block_raw_ids else 0
            self._handle_tagged(ctx, t, last_id, t.raw, True)
        for s in ctx.sentences.flush():
            self._complete_sentence(ctx, s)

        summary: list[_Pending] = list(ctx.trailing)
        ctx.trailing.clear()
        if not summary and ctx.last_released and not any(kept for _, kept in ctx.last_released):
            # Released at a lull but never spoken (minimal verbosity): it is the outcome.
            summary = [p for p, _ in ctx.last_released]
        ctx.last_released = []
        for p in self._choose_summary(summary):
            self._enqueue(ctx, p.text, LineKind.SUMMARY, p.raw_ids, p.raw_texts)
            self._bump("kept")

        if ctx.turn_open:
            self._bump("turns")
        ctx.turn_open = False
        ctx.intent_pending = True
        ctx.pending_raw_ids = []
        ctx.pending_raw_texts = []
        ctx.prepass.turn_started = False
        marker = _Job(
            sentence=Sentence(
                session_id=ctx.session_id,
                text="",
                raw_text="",
                raw_line_ids=[],
                kind=LineKind.SUMMARY,
            ),
            raw_texts=[],
            generation=self.bus.generation,
            enqueued_at=time.monotonic(),
            turn_end=True,
        )
        with self._cv:
            self._jobs.append(marker)
            self._cv.notify_all()

    @staticmethod
    def _choose_summary(items: list[_Pending]) -> list[_Pending]:
        if not items:
            return []
        if len(items) == 1:
            return items
        last, prev = items[-1], items[-2]
        if prev.paragraph != last.paragraph:
            return [last]
        if (
            len(last.text) < _SUMMARY_SHORT_LAST
            or len(last.text) + len(prev.text) <= _SUMMARY_PAIR_MAX_CHARS
        ):
            return [prev, last]
        return [last]

    def _complete_sentence(self, ctx: _SessionCtx, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._bump("sentences")
        raw_ids = list(ctx.pending_raw_ids) or []
        raw_texts = list(ctx.pending_raw_texts)
        # The buffer may still hold the tail of the last line; keep that line linked.
        if ctx.sentences.pending():
            ctx.pending_raw_ids = ctx.pending_raw_ids[-1:]
            ctx.pending_raw_texts = ctx.pending_raw_texts[-1:]
        else:
            ctx.pending_raw_ids = []
            ctx.pending_raw_texts = []
        ctx.last_released = []
        if ctx.intent_pending:
            ctx.intent_pending = False
            self._dispatch_prose(
                ctx, _Pending(text, raw_ids, raw_texts, ctx.paragraph), LineKind.INTENT
            )
            return
        ctx.trailing.append(_Pending(text, raw_ids, raw_texts, ctx.paragraph))
        while len(ctx.trailing) > 2:
            self._dispatch_prose(ctx, ctx.trailing.popleft(), LineKind.PROSE)

    def _dispatch_prose(self, ctx: _SessionCtx, p: _Pending, kind: LineKind) -> bool:
        if keep(kind, self.config.voice.verbosity, self.config.voice.tool_chatter):
            self._bump("kept")
            self._enqueue(ctx, p.text, kind, p.raw_ids, p.raw_texts)
            return True
        self._bump("dropped")
        return False

    def _release_prose(self, ctx: _SessionCtx) -> None:
        """A lull or an interleaved item: held prose is ordinary prose after all."""
        for s in ctx.sentences.flush():
            self._complete_sentence(ctx, s)
        if not ctx.trailing:
            return
        released: list[tuple[_Pending, bool]] = []
        while ctx.trailing:
            p = ctx.trailing.popleft()
            released.append((p, self._dispatch_prose(ctx, p, LineKind.PROSE)))
        ctx.last_released = released

    def _check_lulls(self) -> None:
        now = time.monotonic()
        for ctx in list(self._sessions.values()):
            if (
                ctx.trailing or ctx.sentences.pending()
            ) and now - ctx.last_line_ts >= self.lull_seconds:
                ctx.last_line_ts = now
                self._release_prose(ctx)

    # ---- enqueue -----------------------------------------------------------------------

    def _enqueue(
        self,
        ctx: _SessionCtx,
        text: str,
        kind: LineKind,
        raw_ids: list[int],
        raw_texts: list[str],
        *,
        literal: bool = False,
    ) -> None:
        text = text.strip()
        if not text:
            return
        sentence = Sentence(
            session_id=ctx.session_id,
            text=text,
            raw_text=text,
            raw_line_ids=[r for r in raw_ids if r],
            kind=kind,
            priority=kind in PRIORITY_KINDS,
        )
        job = _Job(
            sentence=sentence,
            raw_texts=list(raw_texts),
            generation=self.bus.generation,
            enqueued_at=time.monotonic(),
        )
        if not literal and kind is not LineKind.ERROR:
            context = [self._job_text(j) for j in ctx.context]
            job.submitted_at = time.monotonic()
            try:
                job.future = self._pool.submit(self._normalize, text, context)
            except RuntimeError:  # pool shut down
                job.future = None
            ctx.context.append(job)
        else:
            self._bump("literal")
        with self._cv:
            self._jobs.append(job)
            self._cv.notify_all()

    def _normalize(self, text: str, context: list[str]) -> str:
        out = self.normalizer.normalize(text, context)
        if not isinstance(out, str) or not out.strip():
            raise ProviderError("normalizer returned empty output")
        with self._cv:
            self._cv.notify_all()  # a prebuffer may now be satisfied
        return out.strip()

    @staticmethod
    def _job_text(job: _Job) -> str:
        f = job.future
        if f is not None and f.done() and not f.cancelled() and f.exception() is None:
            return f.result()
        return job.sentence.raw_text

    # ---- speaker thread --------------------------------------------------------------

    def _speak_loop(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self._stopping.is_set():
                    self._cv.wait(0.1)
                if self._stopping.is_set():
                    return
                job = self._jobs[0]
                if job.turn_end:
                    self._jobs.popleft()
                    self._prebuffering[job.sentence.session_id] = True
                    continue
                if job.sentence.priority:
                    self._prebuffering[job.sentence.session_id] = False
                elif self._prebuffer_holds(job):
                    self._cv.wait(0.05)
                    continue
                self._jobs.popleft()
            try:
                self._process(job)
            except Exception:  # noqa: BLE001
                log.exception("speaker failed on sentence %s", job.sentence.sentence_id)

    def _prebuffer_holds(self, head: _Job) -> bool:
        """Called under the condition lock. True while the head job must wait."""
        sid = head.sentence.session_id
        if not self._prebuffering.get(sid, True):
            return False
        n = int(self.config.voice.prebuffer_sentences or 0)
        if n <= 1:
            self._prebuffering[sid] = False
            return False
        ready = 0
        for j in self._jobs:
            if j.sentence.session_id != sid:
                continue
            if j.turn_end or j.sentence.priority:
                self._prebuffering[sid] = False
                return False
            if j.future is None or j.future.done():
                ready += 1
                if ready >= n:
                    self._prebuffering[sid] = False
                    return False
        if time.monotonic() - head.enqueued_at >= self.prebuffer_max_wait:
            self._prebuffering[sid] = False
            return False
        return True

    def _process(self, job: _Job) -> None:
        sentence = job.sentence
        text = self._await_normalized(job)
        sentence.text = text
        spoken_id = self.store.add_spoken(
            sentence.sentence_id,
            sentence.session_id,
            text,
            sentence.raw_text,
            sentence.kind.value,
            raw_ids=sentence.raw_line_ids,
            ts=sentence.ts,
        )
        self._put_sentence(sentence)
        self.bus.publish(
            TranscriptRow(
                row_id=spoken_id,
                session_id=sentence.session_id,
                kind="spoken",
                text=text,
                raw_lines=list(job.raw_texts),
                ts=sentence.ts,
                sentence_id=sentence.sentence_id,
                spoken=None,
            )
        )
        if self._is_muted():
            self._bump("muted_skips")
            return
        if not sentence.priority and job.generation != self.bus.generation:
            # Barge-in happened after this sentence was queued: the listener moved on.
            self.store.mark_unspoken(sentence.sentence_id)
            self._bump("dropped_by_generation")
            return
        self._synthesize(sentence)

    def _await_normalized(self, job: _Job) -> str:
        sentence = job.sentence
        if job.future is None:
            return sentence.text
        timeout = float(self.config.providers.normalizer_timeout_seconds or 1.5)
        remaining = job.submitted_at + timeout - time.monotonic()
        try:
            text = job.future.result(timeout=max(0.0, remaining) + 0.05)
            self._bump("normalized")
            return text
        except FutureTimeout:
            log.warning("normalizer exceeded %.1fs; speaking pre-passed text", timeout)
            job.future.cancel()
        except (ProviderError, CancelledError) as e:
            log.warning("normalizer failed (%s); speaking pre-passed text", type(e).__name__)
        except Exception:  # noqa: BLE001
            log.exception("normalizer raised; speaking pre-passed text")
        self._bump("normalizer_failures")
        return sentence.raw_text

    def _synthesize(self, sentence: Sentence) -> None:
        gen = self.bus.generation
        seq = 0
        held: bytes | None = None
        rate = int(getattr(self.tts, "sample_rate", 24000))
        try:
            for pcm in self.tts.synthesize(sentence.text):
                if self.bus.generation != gen:
                    self.store.mark_unspoken(sentence.sentence_id)
                    self._bump("dropped_by_generation")
                    return
                if not pcm:
                    continue
                if held is not None:
                    self.bus.playback.put(
                        SpeechChunk(sentence.sentence_id, seq, gen, held, rate, final=False)
                    )
                    seq += 1
                    self._bump("chunks")
                held = bytes(pcm)
        except ProviderError as e:
            log.warning("tts failed on sentence %s: %s", sentence.sentence_id, e)
            self._bump("tts_failures")
            return
        if held is None:
            return
        if self.bus.generation != gen:
            self.store.mark_unspoken(sentence.sentence_id)
            self._bump("dropped_by_generation")
            return
        self.bus.playback.put(SpeechChunk(sentence.sentence_id, seq, gen, held, rate, final=True))
        self._bump("chunks")
        self._bump("synthesized")

    def _put_sentence(self, sentence: Sentence) -> None:
        q = self.bus.sentences
        while q.qsize() >= _MAX_SENTENCES_QUEUE:
            try:
                q.get_nowait()
            except queue.Empty:
                break
        q.put(sentence)

    # ---- misc ------------------------------------------------------------------------------

    def _is_muted(self) -> bool:
        m = self._muted
        if m is None:
            return False
        if isinstance(m, threading.Event):
            return m.is_set()
        try:
            return bool(m())
        except Exception:  # noqa: BLE001
            return False

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def _bump_kind(self, kind: LineKind) -> None:
        with self._stats_lock:
            kinds = self._stats["kinds"]
            kinds[kind.value] = kinds.get(kind.value, 0) + 1
