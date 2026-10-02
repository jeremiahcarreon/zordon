"""Router backed by a local Ollama model (zero-key fallback for the Jev/Haiku routers).

Same prompts and result discipline as the Haiku router; the JSON comes from
Ollama's ``format`` constraint (a JSON schema) with temperature 0. A small
instruct model answers in 200-500 ms on a desktop GPU. Accuracy is measured by
``eval/run_router_eval.py --router ollama``; see decision 0011.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from zordon.providers import ProviderError, ProviderNotConfigured
from zordon.routing import commands
from zordon.routing.anthropic import (
    GATE_SCHEMA,
    GATE_SYSTEM,
    PROMPT_SCHEMA,
    PROMPT_SYSTEM,
    ROUTER_SCHEMA,
    _clamp01,
    router_system_prompt,
)
from zordon.routing.base import (
    DESTINATIONS,
    RouteContext,
    RouteResult,
    YesNoResult,
    extract_argument,
    forbidden_permission_phrase,
)

log = logging.getLogger("zordon.routing.ollama")

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b-instruct"
DEFAULT_TIMEOUT_S = 1.5
KEEP_ALIVE = "30m"


def _schema_for_ollama(schema: dict[str, Any]) -> dict[str, Any]:
    # Ollama accepts a JSON schema in ``format``; ``additionalProperties`` is fine.
    return schema


class OllamaRouter:
    name = "ollama"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
        check: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.client = client or httpx.Client(timeout=httpx.Timeout(self.timeout, connect=0.5))
        if check:
            from zordon.output.normalizer.ollama import has_model, server_models  # noqa: PLC0415

            if not has_model(server_models(self.url, client=self.client), model):
                raise ProviderNotConfigured(f"ollama router: model {model!r} is not pulled; run `ollama pull {model}`")

    def _structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "format": _schema_for_ollama(schema),
            "options": {"temperature": 0, "num_predict": 400},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = self.client.post(self.url + "/api/chat", json=body, timeout=self.timeout)
            resp.raise_for_status()
            content = (resp.json().get("message") or {}).get("content") or ""
            data = json.loads(content)
        except httpx.TimeoutException as e:
            raise ProviderError(f"ollama router: timed out after {self.timeout:.1f}s") from e
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderError(f"ollama router: {type(e).__name__}: {e}") from e
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _route_user(utterance: str, ctx: RouteContext) -> str:
        tail = "\n".join(ctx.transcript_tail[-8:]) or "(nothing yet)"
        return (
            f"Session state: {ctx.session_state}\n"
            f"Known sessions: {', '.join(ctx.session_names) or '(none)'}\n"
            f"Recent transcript (oldest first):\n{tail}\n"
            f"Utterance: {utterance}"
        )

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        data = self._structured(router_system_prompt(), self._route_user(utterance, ctx), ROUTER_SCHEMA)
        dest = data.get("destination")
        confidence = _clamp01(data.get("confidence"))
        if dest not in DESTINATIONS:
            return RouteResult("unclear", confidence)
        if dest != "shim_command":
            return RouteResult(dest, confidence, probabilities={dest: confidence})
        command = data.get("command")
        if not isinstance(command, str) or command not in commands.BY_NAME:
            return RouteResult("unclear", confidence, probabilities={"unclear": confidence})
        argument = data.get("argument")
        arg = argument if isinstance(argument, str) and argument.strip() else None
        arg = arg or extract_argument(command, utterance, ctx)
        if commands.BY_NAME[command].takes_argument is None:
            arg = None
        return RouteResult("shim_command", confidence, command=command, argument=arg, probabilities={"shim_command": confidence})

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
        return (1.0 - confidence) / 2.0
