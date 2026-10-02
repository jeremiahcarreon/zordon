"""Jev router: TypeSafe System One (``typesafe-sdk``).

One HTTP call per utterance carrying three questions: a 4-way ``Choice`` over the
destinations, a ``Noul`` second opinion ("is this a question about the past?")
and a ``Choice`` over the closed shim command set. Destination confidence is the
Choice's *probability* for the winning label (the design thresholds a
probability, not Jev's spread statistic). ``transcript_query`` is only returned
when the Noul agrees; otherwise the utterance falls back to ``claude_code``,
because the unsafe failure is a work request answered from a stale transcript.

Every SDK failure (auth, 5xx, timeout, connection, bad body) is raised as
``ProviderError`` so ``FallbackRouter`` moves on to the next router. A missing or
malformed key is detected at construction and raised as ``ProviderNotConfigured``.
"""

from __future__ import annotations

import logging
from typing import Any

from zordon.routing import commands
from zordon.routing.base import (
    DESTINATIONS,
    ProviderError,
    ProviderNotConfigured,
    RouteContext,
    RouteResult,
    YesNoResult,
    extract_argument,
    forbidden_permission_phrase,
)

log = logging.getLogger("zordon.routing.typesafe")

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 1.5
TAIL_LINES = 20


def _sdk() -> Any:
    try:
        import typesafe_sdk  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - optional dependency
        raise ProviderNotConfigured("typesafe-sdk is not installed (pip install 'zordon[jev]')") from e
    return typesafe_sdk


def default_retry_policy() -> Any:
    """Fast retry for voice: one retry, short backoff, two-second total budget."""
    sdk = _sdk()
    return sdk.RetryPolicy(max_retries=1, backoff_initial=0.1, backoff_max=0.2, timeout=2.0)


# ---- question definitions ----------------------------------------------------------

ROUTE_INSTRUCTIONS: dict[str, Any] = {
    "task": (
        "The user is speaking to a voice assistant that sits in front of a running Claude Code "
        "coding-agent session. Decide where this utterance should be delivered."
    ),
    "context": (
        "`utterance` is the speech-to-text result (may contain recognition errors). "
        "`recent_transcript_tail` is what Claude Code said most recently, oldest first. "
        "`session_state` is the session's current state. `session_names` are the sessions the "
        "user could switch to."
    ),
    "rule_of_thumb": (
        "Asking ABOUT something that already happened in the transcript is transcript_query. "
        "Asking Claude Code TO DO anything, including redoing, undoing, explaining, or continuing "
        "work, is claude_code. Controlling the voice assistant itself is shim_command and is "
        "limited to the closed command list below. If it could be either a question about the "
        "past or a work request, prefer claude_code."
    ),
    "shim_commands": commands.describe_for_router(),
}

ROUTE_CRITERIA: dict[str, Any] = {
    "claude_code": {
        "what": "An instruction, request, answer, or follow-up intended for the Claude Code agent.",
        "examples": [
            "add retry logic to the upload handler",
            "run the tests again",
            "change what you did in auth.py",
            "undo that last edit",
            "explain why the build is failing",
            "yes",
            "go ahead",
            "use the second approach",
            "stop retrying on 500s",
        ],
        "not_for": (
            "Questions about what was already said or done that can be answered from the transcript."
        ),
    },
    "transcript_query": {
        "what": (
            "A question about what Claude Code already printed, did, or said earlier in this "
            "session; answerable from the transcript without Claude Code doing anything."
        ),
        "examples": [
            "what file did it just change",
            "what did you change",
            "what was the commit message",
            "did the tests pass",
            "how many lines did it edit",
            "what did it say about the migration",
        ],
        "not_for": "Anything asking for new or changed work, e.g. 'change what you did'.",
    },
    "shim_command": {
        "what": (
            "A command to the voice assistant itself, not to Claude Code. Closed set: see "
            "shim_commands in the instructions."
        ),
        "examples": [
            "mute",
            "unmute",
            "stop",
            "repeat that",
            "switch to the API session",
            "set verbosity to technical",
            "turn on tool chatter",
            "what sessions are there",
            "switch to plan mode",
        ],
        "not_for": "Work requests that happen to contain the word stop, such as 'stop retrying on 500s'.",
    },
    "unclear": {
        "what": (
            "Noise, a false trigger, a fragment, or speech that is not addressed to the assistant "
            "or Claude Code."
        ),
        "examples": ["um", "hold on", "hey can you grab the", "[inaudible]"],
    },
}

TRANSCRIPT_Q_NOUL_INSTRUCTIONS = (
    "The user is asking about something that already happened in the session (what was said, "
    "changed, run, or printed), not asking for any new or changed work."
)
TRANSCRIPT_Q_NOUL_CRITERIA = {
    "true": "A question answerable from the transcript alone: 'what did you change', 'did the tests pass'.",
    "false": (
        "A request for work, an answer to a prompt, a command to the assistant, or unclear speech: "
        "'change what you did', 'run it again', 'yes', 'mute'."
    ),
}

COMMAND_INSTRUCTIONS = (
    "If the utterance is a command to the voice assistant, which command from the closed list "
    "is it? Pick the closest; the destination question decides whether it is a command at all."
)


def command_criteria() -> dict[str, Any]:
    return {
        c.name: {"what": c.description, "examples": list(c.examples[:4])}
        for c in commands.SHIM_COMMANDS
    }


YES_NOUL = {
    "instructions": "The user is answering YES: approving or allowing the pending permission request, once.",
    "criteria": {
        "true": (
            "An unambiguous affirmative: yes, yeah, yep, sure, ok, okay, go ahead, do it, approve, "
            "allow, confirmed, affirmative."
        ),
        "false": (
            "Anything else: a refusal, a question, a new instruction, hesitation, 'always allow', "
            "or a mixed or self-correcting answer such as 'yeah I guess, actually no'."
        ),
    },
}
NO_NOUL = {
    "instructions": "The user is answering NO: denying or rejecting the pending permission request.",
    "criteria": {
        "true": "An unambiguous negative: no, nope, don't, deny, reject, cancel, stop, negative.",
        "false": "Anything else: an approval, a question, a new instruction, hesitation, or a mixed answer.",
    },
}
ALWAYS_NOUL = {
    "instructions": (
        "The user is asking to ALWAYS allow this action (persist the permission), not just this once."
    ),
    "criteria": {
        "true": "always allow, always, don't ask again, allow all, remember this.",
        "false": "A plain yes or no, or anything else.",
    },
}

PROMPT_LEVELS = [
    (
        "Claude Code is busy: streaming prose, a spinner or progress line, tool output, a diff, "
        "test results, or a file listing is the most recent thing on screen, and no question is "
        "being asked."
    ),
    (
        "Claude Code is idle at its normal input prompt: the previous turn finished (often a "
        "summary line), there is an empty prompt box or cursor waiting for the user's NEXT "
        "instruction, and nothing needs approval."
    ),
    (
        "Claude Code is blocked on a question the user must answer before it can continue: a "
        "permission request ('Do you want to proceed?', 'Yes / Yes, and always allow / No'), a plan "
        "approval, a numbered or arrow-key menu, or any 'Do you want to ...?' / '(y/n)' prompt."
    ),
]
PROMPT_SCORE_INSTRUCTIONS = {
    "task": "Rate the state of a Claude Code terminal from its last lines of rendered screen output.",
    "context": "`pane_tail` is the last lines of the screen, oldest first, ANSI stripped.",
}


class JevRouter:
    """``Router`` backed by TypeSafe System One."""

    name = "jev"

    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Any | None = None,
        *,
        confidence_threshold: float = 0.85,
        yes_no_threshold: float = 0.95,
        transport: Any | None = None,
        client: Any | None = None,
    ) -> None:
        sdk = _sdk()
        self.model = model
        self.timeout = timeout
        self.confidence_threshold = confidence_threshold
        self.yes_no_threshold = yes_no_threshold
        self.last_model: str | None = None
        self.last_request_id: str | None = None
        self._sdk = sdk
        if client is not None:
            self._client = client
            return
        try:
            self._client = sdk.TypeSafeClient(
                api_key=(api_key or "").strip() or None,
                model=model,
                timeout=timeout,
                retry=retry or default_retry_policy(),
                transport=transport,
            )
        except sdk.TypeSafeError as e:
            # The SDK message names the env var, never the key itself.
            raise ProviderNotConfigured(f"jev: {e}") from e

    # ---- internals ---------------------------------------------------------------------

    def _ask(self, state: dict[str, Any], questions: dict[str, Any]) -> Any:
        try:
            resp = self._client.system_one(state=state, questions=questions)
        except self._sdk.TypeSafeError as e:
            raise ProviderError(f"jev: {type(e).__name__}: {e}") from e
        self.last_model = getattr(resp, "model", None)
        try:
            self.last_request_id = resp.raw_http_response.headers.get("x-typesafe-request-id")
        except Exception:  # noqa: BLE001
            self.last_request_id = None
        return resp

    def _questions(self) -> dict[str, Any]:
        sdk = self._sdk
        return {
            "route": sdk.Choice(instructions=ROUTE_INSTRUCTIONS, criteria=ROUTE_CRITERIA),
            "is_transcript_question": sdk.Noul(
                instructions=TRANSCRIPT_Q_NOUL_INSTRUCTIONS, criteria=TRANSCRIPT_Q_NOUL_CRITERIA
            ),
            "command": sdk.Choice(instructions=COMMAND_INSTRUCTIONS, criteria=command_criteria()),
        }

    # ---- Router ------------------------------------------------------------------------

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        state = {
            "utterance": utterance,
            "recent_transcript_tail": list(ctx.transcript_tail[-TAIL_LINES:]),
            "session_state": ctx.session_state,
            "session_names": list(ctx.session_names),
        }
        resp = self._ask(state, self._questions())
        try:
            route_ans = resp.choices["route"]
        except KeyError as e:
            raise ProviderError("jev: response is missing the route answer") from e
        probs = {str(k): float(v) for k, v in dict(route_ans.probabilities).items()}
        choice = str(route_ans.choice)
        destination = choice if choice in DESTINATIONS else "unclear"
        confidence = probs.get(choice, float(getattr(route_ans, "confidence", 0.0)))

        noul = resp.nouls.get("is_transcript_question")
        p_tq = float(noul.noul) if noul is not None else None

        command: str | None = None
        argument: str | None = None
        if destination == "transcript_query" and p_tq is not None and p_tq < self.confidence_threshold:
            log.debug("jev: choice says transcript_query (%.2f) but noul disagrees (%.2f)", confidence, p_tq)
            destination = "claude_code"
            confidence = max(probs.get("claude_code", 0.0), 1.0 - p_tq)
        elif destination == "shim_command":
            cmd_ans = resp.choices.get("command")
            if cmd_ans is not None and str(cmd_ans.choice) in commands.BY_NAME:
                command = str(cmd_ans.choice)
                p_cmd = float(dict(cmd_ans.probabilities).get(cmd_ans.choice, 0.0))
                confidence = min(confidence, p_cmd) if p_cmd else confidence
                argument = extract_argument(command, utterance, ctx)
            else:
                destination = "unclear"

        if p_tq is not None:
            probs["is_transcript_question"] = p_tq
        log.debug(
            "jev route %r -> %s %.2f cmd=%s model=%s req=%s",
            utterance[:60],
            destination,
            confidence,
            command,
            self.last_model,
            self.last_request_id,
        )
        return RouteResult(destination, confidence, command=command, argument=argument, probabilities=probs)

    def yes_no(self, utterance: str) -> YesNoResult:
        if forbidden_permission_phrase(utterance):
            return YesNoResult("unclear", 0.0)
        sdk = self._sdk
        resp = self._ask(
            {
                "utterance": utterance,
                "note": "The assistant must only accept an unambiguous yes or no.",
            },
            {
                "yes": sdk.Noul(**YES_NOUL),
                "no": sdk.Noul(**NO_NOUL),
                "always": sdk.Noul(**ALWAYS_NOUL),
            },
        )
        try:
            p_yes = float(resp.nouls["yes"].noul)
            p_no = float(resp.nouls["no"].noul)
            p_always = float(resp.nouls["always"].noul)
        except KeyError as e:
            raise ProviderError("jev: response is missing a yes/no answer") from e
        t = self.yes_no_threshold
        if p_always >= 0.5:
            return YesNoResult("unclear", 0.0)
        if p_yes >= t and p_no <= 1 - t:
            return YesNoResult("yes", p_yes)
        if p_no >= t and p_yes <= 1 - t:
            return YesNoResult("no", p_no)
        return YesNoResult("unclear", max(p_yes, p_no))

    def prompt_score(self, lines: list[str]) -> float:
        sdk = self._sdk
        resp = self._ask(
            {"pane_tail": list(lines[-10:])},
            {"waiting": sdk.Score(instructions=PROMPT_SCORE_INSTRUCTIONS, criteria=PROMPT_LEVELS)},
        )
        try:
            ans = resp.scores["waiting"]
        except KeyError as e:
            raise ProviderError("jev: response is missing the score answer") from e
        probs = {int(k): float(v) for k, v in dict(ans.probabilities).items()}
        top = len(PROMPT_LEVELS) - 1
        return max(0.0, min(1.0, probs.get(top, 0.0)))
