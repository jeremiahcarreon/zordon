"""Haiku fallback router: Claude Haiku 4.5 with structured outputs.

Each method is one ``messages.create`` call with ``output_config`` holding a JSON
schema, ``max_tokens`` 100, ``max_retries`` 0 and temperature 0 via
``extra_body`` (the 1.x SDK has no ``temperature`` kwarg). Schemas carry no
``minimum``/``maximum`` (unsupported by the API); confidences are clamped here.

Every API failure is raised as ``ProviderError`` so ``FallbackRouter`` moves on.
The constructor raises ``ProviderNotConfigured`` when no credential resolves,
because the SDK itself only fails with a bare ``TypeError`` on the first request.
"""

from __future__ import annotations

import json
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

log = logging.getLogger("zordon.routing.anthropic")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT = 1.5
MAX_TOKENS = 100
TAIL_LINES = 12

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "enum": list(DESTINATIONS),
            "description": (
                "claude_code: a request for the coding assistant to do or answer something. "
                "transcript_query: a question about what the assistant already said or did, answerable "
                "from the transcript alone. shim_command: a local control command from the allowed list. "
                "unclear: noise or cannot tell."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "Probability between 0 and 1 that destination is correct.",
        },
        "command": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "When destination is shim_command, the exact command name from the allowed list; otherwise null."
            ),
        },
        "argument": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "The command's argument when it takes one: a verbosity level (minimal, normal, technical), "
                "a session name, a permission mode (default, acceptEdits, plan) or on/off; otherwise null."
            ),
        },
    },
    "required": ["destination", "confidence", "command", "argument"],
    "additionalProperties": False,
}

GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "confidence": {
            "type": "number",
            "description": "Probability between 0 and 1 that answer is correct.",
        },
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}

PROMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "waiting": {
            "type": "string",
            "enum": ["working", "idle", "blocked"],
            "description": (
                "working: output is still being produced. idle: at the normal input prompt, nothing to "
                "answer. blocked: a permission request, plan approval, menu or question is waiting for the user."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "Probability between 0 and 1 that waiting is correct.",
        },
    },
    "required": ["waiting", "confidence"],
    "additionalProperties": False,
}


def router_system_prompt() -> str:
    return (
        "You classify one transcribed voice utterance from a developer talking to a running Claude Code "
        "session through a voice assistant.\n"
        "Decide where it should go:\n"
        "- claude_code: work requests, follow-ups, answers and questions for Claude Code itself "
        "(\"add retry logic\", \"run the tests again\", \"yes\", \"change what you did\", "
        "\"stop retrying on 500s\").\n"
        "- transcript_query: questions about what Claude Code has ALREADY said or done, answerable from the "
        "transcript without Claude Code doing anything (\"what file did it just change?\", "
        "\"what was the commit message?\", \"did the tests pass?\").\n"
        "- shim_command: a command to the voice assistant itself. This is a closed set; the command field "
        "must be exactly one of these names:\n"
        + commands.describe_for_router()
        + "\n- unclear: noise, a fragment, or not addressed to anyone.\n"
        "Misrouting a work request away from claude_code is the worst outcome; when torn between claude_code "
        "and transcript_query, lower the confidence rather than guessing transcript_query. "
        "A bare 'stop' is the shim command; 'stop doing X' is a request for Claude Code.\n"
        "Respond with JSON only."
    )


GATE_SYSTEM = (
    "Claude Code is waiting on a yes/no permission prompt. Classify the user's utterance as yes, no, or "
    "unclear. Only a clear, unhedged affirmative is yes (yes, yeah, yep, sure, ok, go ahead, do it, approve, "
    "allow); only a clear negative is no (no, nope, don't, deny, reject, cancel, stop). Anything mixed, "
    "hedged, off-topic, or asking to always allow is unclear. \"yeah I guess, actually no\" is unclear. "
    "Respond with JSON only."
)

PROMPT_SYSTEM = (
    "You look at the last lines of a Claude Code terminal (ANSI stripped, oldest first) and say whether "
    "it is working, idle at its input prompt, or blocked on a question the user must answer (a permission "
    "request such as 'Do you want to proceed? 1. Yes 2. No', a plan approval, a numbered menu, a (y/n) "
    "prompt). Respond with JSON only."
)


def normalize_key(raw: str | None) -> str | None:
    """``""`` from config.toml must become None so env/profile discovery can apply."""
    raw = (raw or "").strip()
    return raw or None


def credentials_configured(client: Any) -> bool:
    """No-network check mirroring the SDK's own resolution order."""
    return bool(getattr(client, "api_key", None)) or bool(getattr(client, "auth_token", None)) or (
        getattr(client, "credentials", None) is not None
    )


def _clamp01(x: Any) -> float:
    try:
        return min(1.0, max(0.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


class HaikuRouter:
    """``Router`` backed by the Anthropic API with structured outputs."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        http_client: Any | None = None,
        client: Any | None = None,
    ) -> None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as e:  # pragma: no cover - hard dependency in practice
            raise ProviderNotConfigured("anthropic SDK is not installed") from e
        self._anthropic = anthropic
        self.model = model
        self.timeout = timeout
        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {
                "api_key": normalize_key(api_key),
                "timeout": timeout,
                "max_retries": 0,
            }
            if http_client is not None:
                kwargs["http_client"] = http_client
            self._client = anthropic.Anthropic(**kwargs)
        if not credentials_configured(self._client):
            raise ProviderNotConfigured("anthropic: no credentials configured for the router")

    # ---- internals ---------------------------------------------------------------------

    def _structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        a = self._anthropic
        try:
            msg = self._client.with_options(timeout=self.timeout, max_retries=0).messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
                extra_body={"temperature": 0.0},
            )
        except a.APITimeoutError as e:
            raise ProviderError(f"anthropic router: timed out after {self.timeout:.1f}s") from e
        except a.APIConnectionError as e:
            raise ProviderError(f"anthropic router: connection error: {e}") from e
        except a.APIStatusError as e:
            raise ProviderError(f"anthropic router: API error {e.status_code} {e.type}") from e
        except TypeError as e:
            # No credential resolved at request time.
            raise ProviderError(f"anthropic router: {e}") from e
        if getattr(msg, "stop_reason", None) == "refusal":
            raise ProviderError("anthropic router: refusal")
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError("anthropic router: non-JSON response") from e
        if not isinstance(data, dict):
            raise ProviderError("anthropic router: unexpected response shape")
        return data

    @staticmethod
    def _route_user(utterance: str, ctx: RouteContext) -> str:
        tail = "\n".join(f"- {t}" for t in ctx.transcript_tail[-TAIL_LINES:]) or "- (nothing yet)"
        names = ", ".join(ctx.session_names) or "(none)"
        return (
            f"Session state: {ctx.session_state}\n"
            f"Known sessions: {names}\n"
            f"Recent transcript (oldest first):\n{tail}\n"
            f"Utterance: {utterance}"
        )

    # ---- Router ------------------------------------------------------------------------

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        data = self._structured(router_system_prompt(), self._route_user(utterance, ctx), ROUTER_SCHEMA)
        dest = data.get("destination")
        confidence = _clamp01(data.get("confidence"))
        if dest not in DESTINATIONS:
            return RouteResult("unclear", confidence)
        command = data.get("command")
        argument = data.get("argument")
        if dest != "shim_command":
            return RouteResult(dest, confidence, probabilities={dest: confidence})
        if not isinstance(command, str) or command not in commands.BY_NAME:
            # Closed set: the model may pick, never construct.
            return RouteResult("unclear", confidence, probabilities={"unclear": confidence})
        arg = argument if isinstance(argument, str) and argument.strip() else None
        arg = arg or extract_argument(command, utterance, ctx)
        if commands.BY_NAME[command].takes_argument is None:
            arg = None
        return RouteResult(
            "shim_command",
            confidence,
            command=command,
            argument=arg,
            probabilities={"shim_command": confidence},
        )

    def yes_no(self, utterance: str) -> YesNoResult:
        if forbidden_permission_phrase(utterance):
            return YesNoResult("unclear", 0.0)
        data = self._structured(GATE_SYSTEM, f"Utterance: {utterance}", GATE_SCHEMA)
        answer = data.get("answer")
        if answer not in ("yes", "no", "unclear"):
            return YesNoResult("unclear", 0.0)
        return YesNoResult(answer, _clamp01(data.get("confidence")))

    def prompt_score(self, lines: list[str]) -> float:
        body = "\n".join(lines[-10:]) or "(empty screen)"
        data = self._structured(PROMPT_SYSTEM, f"Screen:\n{body}", PROMPT_SCHEMA)
        confidence = _clamp01(data.get("confidence"))
        if data.get("waiting") == "blocked":
            return confidence
        # The remaining probability mass, split between the two other states.
        return (1.0 - confidence) / 2.0
