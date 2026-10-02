"""Answer a question about what Claude Code already said, without touching the pane.

Two paths:

* Anthropic credentials configured: one Haiku call over the last transcript
  lines. ``zordon.output.normalizer.anthropic.answer_transcript_query`` is used
  when it exists (imported lazily); otherwise the same call is made here.
* No credentials, or the call fails: a deterministic fallback that reads back
  the most recent transcript lines sharing content words with the question.

``answer()`` never raises: a transcript question must always produce something
to say, and the pane must never be touched on this path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from zordon.providers import ProviderError
from zordon.routing.base import words

log = logging.getLogger("zordon.routing.transcript_query")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT = 1.5
MAX_TOKENS = 160
TAIL_LINES = 12

SYSTEM_PROMPT = (
    "You answer one spoken question about what a coding assistant (Claude Code) said recently, using ONLY "
    "the transcript lines provided. Reply in one or two short spoken sentences, plain prose, no markdown, "
    "no file paths longer than a basename. If the transcript does not contain the answer, say so in one "
    "sentence. Never invent details."
)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "description": "One or two short spoken sentences."}},
    "required": ["answer"],
    "additionalProperties": False,
}

_STOPWORDS = frozenset(
    """
    what which did do does it you your just last the a an of to in on at was were is are be been say said
    about that this those these there here how many much when where who whom whose why and or but so then
    tell me again recently earlier before now for with from by have has had get got go went it's its claude
    code mean out up thing things
    """.split()
)

# Question verbs and the words the transcript tends to use for the same event.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "change": ("changed", "edit", "edited", "modified", "updated", "wrote", "rewrote", "created", "added", "removed"),
    "edit": ("edited", "changed", "modified", "updated", "wrote"),
    "modify": ("modified", "edited", "changed"),
    "touch": ("edited", "changed", "modified", "created"),
    "fix": ("fixed", "fixing", "repaired", "resolved"),
    "run": ("ran", "running", "executed"),
    "commit": ("committed", "commit", "message"),
    "test": ("tests", "passed", "failed", "passing", "failing"),
    "tests": ("test", "passed", "failed", "passing", "failing"),
    "pass": ("passed", "passing", "pass"),
    "fail": ("failed", "failing", "failure", "error"),
    "error": ("errors", "failed", "exception", "traceback"),
    "file": ("files", "edited", "created", "wrote", "dot"),
    "delete": ("deleted", "removed"),
    "install": ("installed", "added"),
    "write": ("wrote", "written", "created"),
    "create": ("created", "wrote", "added"),
    "happen": ("happened",),
}

NOTHING_YET = "I haven't heard anything from this session yet."


def fallback_answer(question: str, tail: Sequence[str]) -> str:
    """Deterministic: read back the most recent lines that share content words with the question."""
    lines = [t.strip() for t in tail if t and t.strip()]
    if not lines:
        return NOTHING_YET
    keys = {w for w in words(question) if w not in _STOPWORDS and len(w) > 2}
    for k in list(keys):
        keys.update(_SYNONYMS.get(k, ()))
    if keys:
        scored: list[tuple[int, int]] = []
        for idx, line in enumerate(lines):
            toks = {t for t in words(line) if len(t) > 2}
            # Prefix match so "test" finds "tests" and "commit" finds "committed".
            hits = sum(1 for k in keys if any(t.startswith(k) or k.startswith(t) for t in toks))
            if hits:
                scored.append((hits, idx))
        if scored:
            best = max(h for h, _ in scored)
            picked = [idx for h, idx in scored if h == best][-2:]
            text = " ".join(lines[i] for i in sorted(picked))
            return f"The last thing it said about that was: {text}"
    return f"The last thing it said was: {lines[-1]}"


class TranscriptAnswerer:
    """Owns the model target (normalizer or client) so the dispatcher can answer repeatedly."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        client: Any | None = None,
        normalizer: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.target: Any | None = None
        if _can_answer(normalizer):
            self.target = normalizer
        elif client is not None:
            self.target = client
        elif (api_key or "").strip():
            self.target = _make_client(api_key, timeout=timeout, http_client=http_client)
        log.info("transcript queries answered by %s", "model" if self.target is not None else "fallback")

    @property
    def uses_model(self) -> bool:
        return self.target is not None

    def answer(self, question: str, tail: Sequence[str]) -> str:
        return answer(question, tail, self.target, model=self.model, timeout=self.timeout)


def answer(
    question: str,
    tail: Sequence[str],
    normalizer_or_client: Any | None = None,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Answer ``question`` from ``tail``. Never raises.

    ``normalizer_or_client`` may be an object with ``answer_transcript_query(question, tail)``,
    an Anthropic normalizer (anything with a ``.client``), a bare ``anthropic.Anthropic``
    client, or None for the deterministic fallback.
    """
    tail = [t for t in tail if t and t.strip()][-TAIL_LINES:]
    if not tail:
        return NOTHING_YET
    target = normalizer_or_client
    if target is None:
        return fallback_answer(question, tail)
    try:
        out = _ask_model(target, question, tail, model=model, timeout=timeout)
        out = _clean(out)
        return out or fallback_answer(question, tail)
    except ProviderError as e:
        log.warning("transcript query via model failed (%s); using fallback", e)
    except Exception:  # noqa: BLE001 - a transcript answer must never raise
        log.exception("transcript query raised; using fallback")
    return fallback_answer(question, tail)


def _ask_model(target: Any, question: str, tail: list[str], *, model: str, timeout: float) -> str:
    method = getattr(target, "answer_transcript_query", None)
    if callable(method):
        return str(method(question, list(tail)) or "")
    fn = _module_answer_fn()
    if fn is not None:
        # The shared implementation accepts a normalizer or a bare client and picks the model itself
        # for a normalizer; for a bare client we pass ours. It never raises: a canned sentence
        # means the call failed, and the deterministic readback is the better answer then.
        kwargs: dict[str, Any] = {"timeout": timeout}
        if _is_bare_client(target):
            kwargs["model"] = model
        out = str(fn(target, question, list(tail), **kwargs) or "")
        if out in _module_sentinels():
            raise ProviderError("transcript query: shared implementation reported failure")
        return out
    client = target if _is_bare_client(target) else getattr(target, "client", None)
    if client is None:
        raise ProviderError("transcript query: no usable client")
    return ask_haiku(client, question, tail, model=model, timeout=timeout)


def ask_haiku(
    client: Any,
    question: str,
    tail: Sequence[str],
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """One structured-output call. Raises ProviderError on any API failure."""
    import anthropic  # noqa: PLC0415

    body = "\n".join(f"- {t}" for t in tail)
    user = f"Transcript (oldest first):\n{body}\n\nQuestion: {question}"
    try:
        msg = client.with_options(timeout=timeout, max_retries=0).messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            extra_body={"temperature": 0.0},
        )
    except anthropic.APITimeoutError as e:
        raise ProviderError(f"transcript query timed out after {timeout:.1f}s") from e
    except anthropic.APIConnectionError as e:
        raise ProviderError(f"transcript query connection error: {e}") from e
    except anthropic.APIStatusError as e:
        raise ProviderError(f"transcript query API error {e.status_code} {e.type}") from e
    except TypeError as e:
        raise ProviderError(f"transcript query: {e}") from e
    if getattr(msg, "stop_reason", None) == "refusal":
        raise ProviderError("transcript query: refusal")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _clean(text.splitlines()[0] if text else "")
    return _clean(str(data.get("answer", ""))) if isinstance(data, dict) else ""


# ---- helpers -------------------------------------------------------------------------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _can_answer(obj: Any) -> bool:
    """A normalizer we can route a transcript question through."""
    if obj is None:
        return False
    if callable(getattr(obj, "answer_transcript_query", None)):
        return True
    return getattr(obj, "client", None) is not None and getattr(obj, "name", "") == "anthropic"


def _is_bare_client(obj: Any) -> bool:
    return hasattr(obj, "messages") and hasattr(obj, "with_options")


def _module_sentinels() -> set[str]:
    """The canned sentences the shared implementation returns instead of raising."""
    try:
        from zordon.output.normalizer import anthropic as mod  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for name in ("TRANSCRIPT_UNAVAILABLE",):
        value = getattr(mod, name, None)
        if isinstance(value, str) and value:
            out.add(value)
    return out


def _module_answer_fn() -> Callable[..., str] | None:
    """``zordon.output.normalizer.anthropic.answer_transcript_query`` if it exists."""
    try:
        from zordon.output.normalizer import anthropic as mod  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(mod, "answer_transcript_query", None)
    return fn if callable(fn) else None


def _make_client(api_key: str | None, *, timeout: float, http_client: Any | None) -> Any | None:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    kwargs: dict[str, Any] = {"api_key": (api_key or "").strip() or None, "timeout": timeout, "max_retries": 0}
    if http_client is not None:
        kwargs["http_client"] = http_client
    client = anthropic.Anthropic(**kwargs)
    if not (client.api_key or client.auth_token or client.credentials is not None):
        return None
    return client
