"""Normalizer that uses Claude Code itself, headless, through the user's own login.

No API key: ``claude -p`` runs under the subscription the user already has.
Each request is its own short-lived process, so nothing from one request is in
the context of the next (there is no "clear" in print mode; a fresh process is
the clear). Process start-up costs a few seconds, so one process is always
kept warm and waiting on stdin; a request uses it and spawns the next one in
the background.

Measured on Claude Code 2.1.287: 4-7 s per request with claude-haiku-4-5, about
1.5 s API time with claude-sonnet-5 (plus start-up). Far above the 1.5 s per-sentence
budget of the API normalizer, so this provider declares
``granularity = "turn"``: the pipeline batches a whole turn's prose and
normalizes it in one request once the turn is over.

The same warm process answers transcript questions.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from zordon.output.normalizer.base import clean_spoken
from zordon.providers import ProviderError, ProviderNotConfigured

log = logging.getLogger("zordon.output.normalizer.claude_cli")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 30.0
MAX_INPUT_CHARS = 12_000

SYSTEM_PROMPT = (
    "You are a text post-processor for a voice interface over a coding assistant. "
    "Each message gives you one instruction and one piece of text. Follow the instruction "
    "exactly and output only the result as plain prose: no markdown, no headings, no code, "
    "no bullet points, no preamble, no quotes around the output. Never add information."
)

REWRITE_INSTRUCTION = (
    "Rewrite the text below as fluent spoken English for text-to-speech. Keep every fact, "
    "keep the order, keep it about the same length. Expand shorthand and terse "
    "commit-message English into full sentences with articles and verbs. Say file names as "
    "words (auth.py becomes auth dot py). Numbers as words where natural; keep exact large "
    "numbers as digits. Expand abbreviations (repo, config, deps, PR, CI). Code blocks, "
    "tables and paths have already been replaced by short descriptions; keep those as they are."
)

ANSWER_INSTRUCTION = (
    "Below is the recent transcript of what the coding assistant said and did, oldest first, "
    "followed by a question from the user. Answer the question in one or two spoken "
    "sentences using only the transcript. If the transcript does not contain the answer, "
    "say exactly: I don't see that in the transcript."
)

TRANSCRIPT_ABSENT = "I don't see that in the transcript."

# Nesting markers and session identity of a parent Claude Code must not leak into the
# headless child; everything else (including the user's login) is inherited.
SCRUB_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_JOB_")
KEEP = {"CLAUDE_CONFIG_DIR"}


def claude_binary(binary: str = "claude") -> str | None:
    if os.sep in binary:
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for k in list(env):
        if k in KEEP:
            continue
        if k.startswith(SCRUB_PREFIXES):
            env.pop(k, None)
    return env


def build_command(binary: str, model: str, system_prompt: str = SYSTEM_PROMPT) -> list[str]:
    return [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--max-turns",
        "1",
        "--system-prompt",
        system_prompt,
    ]


class _Proc:
    """One headless Claude Code process: spawned idle, used for exactly one request."""

    def __init__(self, cmd: list[str], env: dict[str, str], cwd: Path) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.started = time.monotonic()
        self._reader = threading.Thread(target=self._read, name="zordon-claude-cli-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.lines.put(None)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def request(self, content: str, timeout: float) -> str:
        assert self.proc.stdin is not None
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise ProviderError(f"claude-cli: cannot write to the process: {e}") from e
        deadline = time.monotonic() + timeout
        text_parts: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(f"claude-cli: no result within {timeout:.0f}s")
            try:
                line = self.lines.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
                err = self._stderr_tail()
                raise ProviderError(f"claude-cli: process exited before answering{err}")
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            if kind == "assistant":
                content_blocks = (rec.get("message") or {}).get("content") or []
                for b in content_blocks:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text_parts.append(str(b.get("text") or ""))
            elif kind == "result":
                if rec.get("is_error") or rec.get("subtype", "success") != "success":
                    raise ProviderError(f"claude-cli: {rec.get('subtype')}: {str(rec.get('result'))[:200]}")
                result = rec.get("result")
                if isinstance(result, str) and result.strip():
                    return result
                return "".join(text_parts)

    def _stderr_tail(self) -> str:
        try:
            assert self.proc.stderr is not None
            data = self.proc.stderr.read() or ""
        except (OSError, ValueError):
            return ""
        tail = data.strip().splitlines()[-3:]
        return (": " + " | ".join(tail)) if tail else ""

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except OSError:
                    pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class ClaudeCliNormalizer:
    """``Normalizer`` backed by headless Claude Code under the user's login.

    ``granularity`` tells the pipeline to batch a whole turn into one request.
    """

    name = "claude-cli"
    granularity = "turn"

    def __init__(
        self,
        binary: str = "claude",
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_S,
        warm: bool = True,
        cwd: Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        path = claude_binary(binary)
        if not path:
            raise ProviderNotConfigured(
                f"claude-cli normalizer: {binary!r} is not on PATH; install Claude Code and log in"
            )
        self.binary = path
        self.model = (model or DEFAULT_MODEL).strip()
        self.timeout = float(timeout)
        self.warm = warm
        self.cwd = cwd or Path(tempfile.mkdtemp(prefix="zordon-cli-"))
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._env = child_env(environ)
        self._lock = threading.Lock()
        self._spare: _Proc | None = None
        self._spawn_lock = threading.Lock()
        self._closed = False
        self.requests = 0
        self.failures = 0
        self.last_latency_s: float | None = None
        if warm:
            self._spawn_in_background()

    # ---- process pool of one ------------------------------------------------------

    def _spawn(self) -> _Proc:
        return _Proc(build_command(self.binary, self.model), self._env, self.cwd)

    def _spawn_in_background(self) -> None:
        def _do() -> None:
            with self._spawn_lock:
                if self._closed or self._spare is not None:
                    return
                try:
                    self._spare = self._spawn()
                except OSError as e:
                    log.warning("claude-cli: cannot start a warm process: %s", e)

        threading.Thread(target=_do, name="zordon-claude-cli-warm", daemon=True).start()

    def _take(self) -> _Proc:
        with self._spawn_lock:
            proc = self._spare
            self._spare = None
        if proc is not None and proc.alive():
            return proc
        if proc is not None:
            proc.kill()
        return self._spawn()

    def _request(self, content: str) -> str:
        if self._closed:
            raise ProviderError("claude-cli: normalizer is closed")
        if len(content) > MAX_INPUT_CHARS:
            content = content[:MAX_INPUT_CHARS]
        with self._lock:
            proc = self._take()
            t0 = time.monotonic()
            try:
                out = proc.request(content, self.timeout)
            except ProviderError:
                self.failures += 1
                raise
            finally:
                proc.kill()  # one request per process: the next one starts clean
                if self.warm and not self._closed:
                    self._spawn_in_background()
            self.requests += 1
            self.last_latency_s = time.monotonic() - t0
            log.info("claude-cli: %s answered in %.1fs", self.model, self.last_latency_s)
            return out

    # ---- Normalizer ---------------------------------------------------------------

    def normalize(self, sentence: str, context: list[str]) -> str:
        """Per-sentence form (used only when the pipeline is not in turn mode)."""
        return self.normalize_turn(sentence)

    def normalize_turn(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return text
        out = self._request(f"{REWRITE_INSTRUCTION}\n\nText:\n{text}")
        cleaned = clean_spoken(out)
        if not cleaned:
            raise ProviderError("claude-cli: empty rewrite")
        return cleaned

    def answer_transcript_query(self, question: str, transcript_tail: Sequence[str]) -> str:
        lines = [t for t in (transcript_tail or []) if (t or "").strip()]
        if not lines:
            return TRANSCRIPT_ABSENT
        transcript = "\n".join(lines[-40:])
        out = self._request(f"{ANSWER_INSTRUCTION}\n\nTranscript:\n{transcript}\n\nQuestion: {question.strip()}")
        return clean_spoken(out) or TRANSCRIPT_ABSENT

    def close(self) -> None:
        self._closed = True
        with self._spawn_lock:
            spare, self._spare = self._spare, None
        if spare is not None:
            spare.kill()
