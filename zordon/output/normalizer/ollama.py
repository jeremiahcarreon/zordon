"""Normalizer backed by a local Ollama server.

Small instruct models rewrite one sentence in 150-500 ms on a desktop GPU,
well inside the 1.5 s per-sentence budget, with no key and no quota, so this
is the preferred zero-key path when an Ollama server is reachable.

Small models like to pad ("...so that if something goes wrong it will..."),
which for a coding agent's output means inventing intent. Two leashes:

* the prompt demands the same facts, the same order and about the same length;
* a length guard: output longer than ``max_growth`` times the input (in words)
  is retried once; if still too long the pre-passed sentence is spoken as is.

Measured 2026-10-02 on an RTX 4090, Ollama 0.31.1: qwen2.5:1.5b 160-190 ms
(ignores formatting rules), qwen2.5:3b 170-310 ms (good, pads sometimes),
qwen2.5:14b 300-550 ms (best). See docs/decisions/0011-ollama-normalizer.md.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence
from typing import Any

import httpx

from zordon.output.normalizer.base import clean_spoken, first_line
from zordon.providers import ProviderError, ProviderNotConfigured

log = logging.getLogger("zordon.output.normalizer.ollama")

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b-instruct"
DEFAULT_TIMEOUT_S = 1.5
# Keep the model resident for as long as the Ollama server runs. Its default is to
# unload after five idle minutes, and a cold load takes 10-20 s: every sentence of
# the first answer after a pause would then miss the normalizer deadline and be
# spoken raw. ``warm()`` loads it at start so the first answer is covered too.
KEEP_ALIVE = -1
WARM_TIMEOUT_S = 90.0
MAX_GROWTH = 1.6  # output words / input words above this is padding
MIN_WORDS_FOR_GUARD = 4  # very short inputs legitimately grow ("Done." -> "I am done.")

SYSTEM_PROMPT = (
    "You rewrite one sentence of a coding assistant's terminal output into fluent spoken "
    "English for text-to-speech.\n"
    "Rules:\n"
    "- Keep every fact and the order. Do not add reasons, opinions, consequences or details "
    "that are not in the sentence. Keep it about the same length.\n"
    "- Output exactly one sentence of plain prose. No markdown, no backticks, no bullet "
    "points, no quotes, no preamble.\n"
    "- Expand shorthand and terse commit-message English into a full sentence with articles "
    "and verbs.\n"
    "- Say file names as words: auth.py becomes auth dot py; handler.ts becomes handler dot ts.\n"
    "- Numbers as words where natural (3 files becomes three files); keep exact large numbers "
    "as digits.\n"
    "- Expand abbreviations: repo is repository, config is configuration, deps is "
    "dependencies, perm is permission, PR is pull request, CI is C I.\n"
    "- Shell commands become what they do: rm -rf build/ becomes delete the build directory.\n"
    "- Symbols are spoken: < is less than, <= is less than or equal to, -> is to, && is and.\n"
    "- If the sentence is already fluent, return it unchanged.\n"
    "Examples:\n"
    "Edited `auth.py`, 8 lines changed. -> I edited auth dot py, changing eight lines.\n"
    "tests pass. 42/42. -> All forty-two tests pass.\n"
    "Fix lint. Bump deps. Done. -> I fixed the lint errors, updated the dependencies, and finished.\n"
    "Need perm to run rm -rf build/ -> I need permission to delete the build directory."
)

ANSWER_PROMPT = (
    "You answer a question about what a coding assistant recently said and did, using only "
    "the transcript given. Answer in one or two spoken sentences, plain prose, no markdown. "
    "If the transcript does not contain the answer, reply exactly: I don't see that in the transcript."
)
TRANSCRIPT_ABSENT = "I don't see that in the transcript."

_WORDS = re.compile(r"[A-Za-z0-9']+")


def word_count(text: str) -> int:
    return len(_WORDS.findall(text or ""))


def too_long(source: str, output: str, max_growth: float = MAX_GROWTH) -> bool:
    n_in = word_count(source)
    if n_in < MIN_WORDS_FOR_GUARD:
        return False
    return word_count(output) > max_growth * n_in + 2


def server_models(url: str = DEFAULT_URL, timeout: float = 1.0, client: httpx.Client | None = None) -> list[str]:
    """Names of the models the server has pulled; raises ProviderNotConfigured when unreachable."""
    try:
        c = client or httpx.Client(timeout=timeout)
        resp = c.get(url.rstrip("/") + "/api/tags")
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ProviderNotConfigured(f"ollama: no server at {url} ({type(e).__name__})") from e
    return [str(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)]


def has_model(models: Sequence[str], model: str) -> bool:
    want = model if ":" in model else model + ":latest"
    return any(m == want or m == model for m in models)


class OllamaNormalizer:
    """``Normalizer`` over Ollama's ``/api/chat``. Per-sentence granularity."""

    name = "ollama"
    granularity = "sentence"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_growth: float = MAX_GROWTH,
        client: httpx.Client | None = None,
        check: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.max_growth = max_growth
        self.client = client or httpx.Client(timeout=httpx.Timeout(self.timeout, connect=0.5))
        self.requests = 0
        self.failures = 0
        self.guarded = 0
        if check:
            models = server_models(self.url, client=self.client)
            if not has_model(models, model):
                raise ProviderNotConfigured(
                    f"ollama: model {model!r} is not pulled; run `ollama pull {model}`"
                )

    # ---- model residency -----------------------------------------------------------

    def warm(self, *, background: bool = True) -> None:
        """Load the model into memory now (``/api/generate`` with no prompt) and pin it
        there with ``KEEP_ALIVE``. In the background by default: a cold load can take
        10-20 s and must not hold up start-up; sentences that arrive meanwhile fall
        back to pre-passed text as they would anyway."""
        if background:
            threading.Thread(target=self._warm, name="ollama-warm", daemon=True).start()
        else:
            self._warm()

    def _warm(self) -> None:
        body = {"model": self.model, "keep_alive": KEEP_ALIVE}
        try:
            resp = self.client.post(self.url + "/api/generate", json=body, timeout=WARM_TIMEOUT_S)
            resp.raise_for_status()
            log.info("ollama: %s loaded and pinned", self.model)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("ollama: could not preload %s: %s", self.model, e)

    # ---- chat ---------------------------------------------------------------------

    def _chat(self, system: str, user: str, *, temperature: float = 0.0, num_predict: int = 160) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": temperature, "num_predict": num_predict},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = self.client.post(self.url + "/api/chat", json=body, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as e:
            self.failures += 1
            raise ProviderError(f"ollama: timed out after {self.timeout:.1f}s") from e
        except (httpx.HTTPError, ValueError) as e:
            self.failures += 1
            raise ProviderError(f"ollama: {type(e).__name__}: {e}") from e
        self.requests += 1
        msg = data.get("message") or {}
        return str(msg.get("content") or "")

    # ---- Normalizer ---------------------------------------------------------------

    def normalize(self, sentence: str, context: list[str]) -> str:
        sentence = (sentence or "").strip()
        if not sentence:
            return sentence
        user = sentence
        if context:
            ctx = " ".join(c.strip() for c in context[-2:] if c and c.strip())
            if ctx:
                user = f"Previous sentences, for reference only (do not rewrite them): {ctx}\n\nSentence: {sentence}"
        out = clean_spoken(first_line(self._chat(SYSTEM_PROMPT, user)))
        if not out:
            raise ProviderError("ollama: empty rewrite")
        if too_long(sentence, out, self.max_growth):
            self.guarded += 1
            retry = clean_spoken(
                first_line(self._chat(SYSTEM_PROMPT + "\nThe previous answer was too long. Be brief: same facts only.", user))
            )
            if retry and not too_long(sentence, retry, self.max_growth):
                return retry
            log.info("ollama: rewrite padded (%d -> %d words); speaking pre-passed text", word_count(sentence), word_count(out))
            raise ProviderError("ollama: rewrite grew beyond the length guard")
        return out

    def answer_transcript_query(self, question: str, transcript_tail: Sequence[str]) -> str:
        lines = [t for t in (transcript_tail or []) if (t or "").strip()]
        if not lines:
            return TRANSCRIPT_ABSENT
        transcript = "\n".join(lines[-40:])
        user = f"Transcript (oldest first):\n{transcript}\n\nQuestion: {question.strip()}"
        out = clean_spoken(self._chat(ANSWER_PROMPT, user, num_predict=120))
        return out or TRANSCRIPT_ABSENT

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
