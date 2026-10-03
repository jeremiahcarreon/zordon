"""Jev and Haiku routers against mock transports (no network), plus the fallback chain."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from zordon.config import Config
from zordon.providers import DESTINATIONS, ProviderError, ProviderNotConfigured
from zordon.routing import commands
from zordon.routing.anthropic import GATE_SCHEMA, PROMPT_SCHEMA, ROUTER_SCHEMA, HaikuRouter
from zordon.routing.base import RouteContext, RouteResult, YesNoResult
from zordon.routing.keyword import KeywordRouter
from zordon.routing.select import FallbackRouter, make_router
from zordon.routing.typesafe import JevRouter

CTX = RouteContext(
    session_state="idle",
    transcript_tail=["I edited auth dot py.", "All tests pass."],
    focused_session="s1",
    session_names=["zordon", "api"],
    commands=list(commands.COMMAND_NAMES),
)


# ---- TypeSafe mock -----------------------------------------------------------------------


def _choice(choice: str, probs: dict[str, float], confidence: float = 0.9) -> dict[str, Any]:
    return {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": probs}


def _noul(p: float) -> dict[str, Any]:
    return {"type": "noul", "noul": p}


def _score(probs: dict[str, float]) -> dict[str, Any]:
    legend = {str(i): f"level {i}" for i in range(len(probs))}
    expected = sum(int(k) * v for k, v in probs.items())
    return {"type": "score", "score": expected, "confidence": 0.8, "legend": legend, "probabilities": probs}


def _ts_response(answers: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={"model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 0}, "answers": answers},
        headers={"x-typesafe-request-id": "req_test"},
    )


class TSMock:
    """Records request bodies and replies from a queue of responses or a callable."""

    def __init__(self, reply):
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        if callable(self.reply):
            return self.reply(request)
        return self.reply

    def client(self):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        return TypeSafeClient(
            api_key="k",
            transport=httpx2.MockTransport(self),
            timeout=1.5,
            retry=RetryPolicy(max_retries=0),
        )


def _jev(mock: TSMock, **kw) -> JevRouter:
    return JevRouter("k", client=mock.client(), **kw)


def _route_answers(dest: str, probs: dict[str, float], p_tq: float, command: str = "mute", p_cmd: float = 0.9):
    cmd_probs = {name: (p_cmd if name == command else (1 - p_cmd) / (len(commands.COMMAND_NAMES) - 1)) for name in commands.COMMAND_NAMES}
    return {
        "route": _choice(dest, probs),
        "is_transcript_question": _noul(p_tq),
        "command": _choice(command, cmd_probs),
    }


def test_jev_constructor_without_key_raises_not_configured(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured):
        JevRouter("")
    with pytest.raises(ProviderNotConfigured):
        JevRouter(None)
    with pytest.raises(ProviderNotConfigured):
        JevRouter("has whitespace")


def test_jev_route_request_body_and_shim_result():
    mock = TSMock(_ts_response(_route_answers("shim_command", {"claude_code": 0.03, "transcript_query": 0.01, "shim_command": 0.95, "unclear": 0.01}, 0.05)))
    r = _jev(mock).route("mute", CTX)

    body = mock.bodies[0]
    assert body["model"] == "jev-latest"
    assert set(body["state"]) == {"utterance", "recent_transcript_tail", "session_state", "session_names"}
    assert body["state"]["utterance"] == "mute"
    assert body["state"]["session_state"] == "idle"
    assert body["state"]["recent_transcript_tail"] == CTX.transcript_tail
    q = body["questions"]
    assert q["route"]["type"] == "choice"
    assert set(q["route"]["criteria"]) == set(DESTINATIONS)
    for crit in q["route"]["criteria"].values():
        assert "what" in crit and "examples" in crit
    assert "shim_commands" in q["route"]["instructions"]
    assert "mute" in q["route"]["instructions"]["shim_commands"]
    assert q["is_transcript_question"]["type"] == "noul"
    assert set(q["is_transcript_question"]["criteria"]) == {"true", "false"}
    assert q["command"]["type"] == "choice"
    assert tuple(q["command"]["criteria"]) == commands.COMMAND_NAMES
    assert mock.headers[0]["authorization"] == "Bearer k"

    assert r.destination == "shim_command"
    assert r.command == "mute"
    assert r.confidence == pytest.approx(0.9)  # min(route prob, command prob)
    assert r.probabilities["shim_command"] == pytest.approx(0.95)
    assert r.probabilities["is_transcript_question"] == pytest.approx(0.05)


def test_jev_confidence_is_the_choice_probability_not_the_spread():
    probs = {"claude_code": 0.1, "transcript_query": 0.7, "shim_command": 0.1, "unclear": 0.1}
    mock = TSMock(_ts_response({"route": _choice("transcript_query", probs, confidence=0.99), "is_transcript_question": _noul(0.9)}))
    r = _jev(mock).route("what did you change", CTX)
    assert r.destination == "transcript_query"
    assert r.confidence == pytest.approx(0.7)
    # Below the design threshold the dispatcher sends it to Claude Code.
    from zordon.routing.base import effective_destination

    assert effective_destination(r, 0.85) == "claude_code"


def test_jev_transcript_query_needs_noul_agreement():
    probs = {"claude_code": 0.05, "transcript_query": 0.9, "shim_command": 0.03, "unclear": 0.02}
    mock = TSMock(_ts_response({"route": _choice("transcript_query", probs), "is_transcript_question": _noul(0.3)}))
    r = _jev(mock).route("change what you did", CTX)
    assert r.destination == "claude_code"

    mock = TSMock(_ts_response({"route": _choice("transcript_query", probs), "is_transcript_question": _noul(0.97)}))
    r = _jev(mock).route("what did you change", CTX)
    assert r.destination == "transcript_query"
    assert r.confidence == pytest.approx(0.9)


def test_jev_shim_argument_extraction_and_unknown_choice():
    probs = {"claude_code": 0.02, "transcript_query": 0.01, "shim_command": 0.96, "unclear": 0.01}
    mock = TSMock(_ts_response(_route_answers("shim_command", probs, 0.02, command="set_verbosity", p_cmd=0.93)))
    r = _jev(mock).route("set verbosity to technical", CTX)
    assert (r.command, r.argument) == ("set_verbosity", "technical")

    mock = TSMock(_ts_response(_route_answers("shim_command", probs, 0.02, command="focus", p_cmd=0.93)))
    r = _jev(mock).route("switch to the api session", CTX)
    assert (r.command, r.argument) == ("focus", "api")

    # A label outside the destination set is treated as unclear.
    mock = TSMock(_ts_response({"route": _choice("something_else", {"something_else": 1.0}), "is_transcript_question": _noul(0.1)}))
    assert _jev(mock).route("x", CTX).destination == "unclear"

    # shim_command without a command answer is unclear (closed set).
    mock = TSMock(_ts_response({"route": _choice("shim_command", probs), "is_transcript_question": _noul(0.1)}))
    assert _jev(mock).route("x", CTX).destination == "unclear"


def test_jev_errors_become_provider_error():
    mock = TSMock(httpx2.Response(401, json={"error": {"message": "Invalid API key"}}))
    with pytest.raises(ProviderError):
        _jev(mock).route("mute", CTX)

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError):
        _jev(TSMock(timeout)).route("mute", CTX)

    mock = TSMock(httpx2.Response(529, json={"error": {"message": "Overloaded"}}))
    with pytest.raises(ProviderError):
        _jev(mock).yes_no("yes")

    mock = TSMock(httpx2.Response(200, json={"model": "jev", "answers": {"route": {"type": "choice", "choice": 1}}}))
    with pytest.raises(ProviderError):
        _jev(mock).route("mute", CTX)


def test_jev_yes_no_strict_rule():
    def answers(p_yes, p_no, p_always):
        return _ts_response({"yes": _noul(p_yes), "no": _noul(p_no), "always": _noul(p_always)})

    mock = TSMock(answers(0.98, 0.01, 0.02))
    r = _jev(mock).yes_no("yeah go ahead")
    assert (r.answer, r.confidence) == ("yes", pytest.approx(0.98))
    body = mock.bodies[0]
    assert body["state"]["utterance"] == "yeah go ahead"
    assert {q["type"] for q in body["questions"].values()} == {"noul"}
    assert set(body["questions"]) == {"yes", "no", "always"}

    assert _jev(TSMock(answers(0.01, 0.99, 0.0))).yes_no("nope").answer == "no"
    # yes high but no not low enough: unclear
    assert _jev(TSMock(answers(0.96, 0.2, 0.0))).yes_no("yeah I guess, actually no").answer == "unclear"
    # yes below threshold: unclear
    assert _jev(TSMock(answers(0.9, 0.02, 0.0))).yes_no("sure I think").answer == "unclear"
    # always allow: unclear, confidence 0, and no network call needed for the literal phrase
    mock = TSMock(answers(0.99, 0.0, 0.9))
    r = _jev(mock).yes_no("yes always allow")
    assert (r.answer, r.confidence) == ("unclear", 0.0)
    assert mock.bodies == []
    # always detected by the model only
    r = _jev(TSMock(answers(0.99, 0.0, 0.9))).yes_no("remember this choice")
    assert (r.answer, r.confidence) == ("unclear", 0.0)


def test_jev_prompt_score_is_probability_of_top_level():
    mock = TSMock(_ts_response({"waiting": _score({"0": 0.1, "1": 0.1, "2": 0.8})}))
    jev = _jev(mock)
    assert jev.prompt_score(["Do you want to proceed?", "❯ 1. Yes"]) == pytest.approx(0.8)
    body = mock.bodies[0]
    assert body["questions"]["waiting"]["type"] == "score"
    assert len(body["questions"]["waiting"]["criteria"]) == 3
    assert body["state"]["pane_tail"] == ["Do you want to proceed?", "❯ 1. Yes"]
    assert jev.last_model == "jev-1.13.0"
    assert jev.last_request_id == "req_test"


# ---- Anthropic mock ------------------------------------------------------------------------


def _msg(payload: dict[str, Any], stop_reason: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    )


class AnthropicMock:
    def __init__(self, reply):
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        return self.reply(request) if callable(self.reply) else self.reply

    def router(self) -> HaikuRouter:
        import anthropic

        return HaikuRouter(
            "sk-ant-test",
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self)),
        )


def test_haiku_constructor_requires_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    import anthropic

    class NoCreds:
        api_key = None
        auth_token = None
        credentials = None

    with pytest.raises(ProviderNotConfigured):
        HaikuRouter("", client=NoCreds())
    # A real client with a key constructs fine.
    HaikuRouter("sk-ant-test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(lambda r: _msg({}))))


def test_haiku_route_request_shape_and_result():
    mock = AnthropicMock(_msg({"destination": "shim_command", "confidence": 0.93, "command": "set_verbosity", "argument": None}))
    r = mock.router().route("set verbosity to technical", CTX)
    body = mock.bodies[0]
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.0
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": ROUTER_SCHEMA}}
    assert ROUTER_SCHEMA["additionalProperties"] is False
    assert set(ROUTER_SCHEMA["properties"]) == {"destination", "confidence", "command", "argument"}
    assert ROUTER_SCHEMA["properties"]["destination"]["enum"] == list(DESTINATIONS)
    system_text = body["system"][0]["text"]
    for name in commands.COMMAND_NAMES:
        assert name in system_text
    user = body["messages"][0]["content"]
    assert "Session state: idle" in user and "Utterance: set verbosity to technical" in user
    assert "I edited auth dot py." in user

    assert r.destination == "shim_command"
    assert r.command == "set_verbosity"
    assert r.argument == "technical"  # heuristic fills what the model left null
    assert r.confidence == pytest.approx(0.93)


def test_haiku_route_fallbacks():
    # Unknown command name -> unclear (closed set).
    r = AnthropicMock(_msg({"destination": "shim_command", "confidence": 0.9, "command": "self_destruct", "argument": None})).router().route("x", CTX)
    assert r.destination == "unclear"
    # Unknown destination -> unclear; confidence clamped.
    r = AnthropicMock(_msg({"destination": "bogus", "confidence": 7, "command": None, "argument": None})).router().route("x", CTX)
    assert (r.destination, r.confidence) == ("unclear", 1.0)
    # Plain destinations pass through with clamped confidence.
    r = AnthropicMock(_msg({"destination": "transcript_query", "confidence": 0.91, "command": None, "argument": None})).router().route("what did you change", CTX)
    assert (r.destination, r.confidence) == ("transcript_query", pytest.approx(0.91))
    # Argument dropped for commands that take none.
    r = AnthropicMock(_msg({"destination": "shim_command", "confidence": 0.9, "command": "mute", "argument": "now"})).router().route("mute", CTX)
    assert (r.command, r.argument) == ("mute", None)


def test_haiku_errors_become_provider_error():
    def err(status: int, typ: str):
        return httpx2.Response(status, json={"type": "error", "error": {"type": typ, "message": "nope"}})

    with pytest.raises(ProviderError):
        AnthropicMock(err(401, "authentication_error")).router().route("mute", CTX)
    with pytest.raises(ProviderError):
        AnthropicMock(err(429, "rate_limit_error")).router().route("mute", CTX)
    with pytest.raises(ProviderError):
        AnthropicMock(err(529, "overloaded_error")).router().yes_no("yes")

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError):
        AnthropicMock(timeout).router().route("mute", CTX)
    with pytest.raises(ProviderError):
        AnthropicMock(_msg({"answer": "yes", "confidence": 0.9}, stop_reason="refusal")).router().yes_no("yes")

    bad = httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [{"type": "text", "text": "not json"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
    with pytest.raises(ProviderError):
        AnthropicMock(bad).router().route("mute", CTX)


def test_haiku_yes_no_and_prompt_score():
    mock = AnthropicMock(_msg({"answer": "yes", "confidence": 0.97}))
    r = mock.router().yes_no("yeah go ahead")
    assert (r.answer, r.confidence) == ("yes", pytest.approx(0.97))
    assert mock.bodies[0]["output_config"]["format"]["schema"] == GATE_SCHEMA
    assert "yeah go ahead" in mock.bodies[0]["messages"][0]["content"]

    assert AnthropicMock(_msg({"answer": "maybe", "confidence": 0.9})).router().yes_no("x").answer == "unclear"
    # Forbidden phrase never reaches the model.
    mock = AnthropicMock(_msg({"answer": "yes", "confidence": 0.99}))
    assert mock.router().yes_no("always allow").answer == "unclear"
    assert mock.bodies == []

    mock = AnthropicMock(_msg({"waiting": "blocked", "confidence": 0.9}))
    assert mock.router().prompt_score(["Do you want to proceed?"]) == pytest.approx(0.9)
    assert mock.bodies[0]["output_config"]["format"]["schema"] == PROMPT_SCHEMA
    assert AnthropicMock(_msg({"waiting": "idle", "confidence": 0.8})).router().prompt_score(["> "]) == pytest.approx(0.1)


# ---- FallbackRouter -------------------------------------------------------------------------


class Scripted:
    """A Router that returns canned answers or raises, and records calls."""

    def __init__(self, name: str, route=None, yes_no=None, prompt=None, fail: bool = False):
        self.name = name
        self._route = route
        self._yes_no = yes_no
        self._prompt = prompt
        self.fail = fail
        self.calls: list[str] = []

    def route(self, utterance, ctx):
        self.calls.append(f"route:{utterance}")
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return self._route or RouteResult("claude_code", 0.99)

    def yes_no(self, utterance):
        self.calls.append(f"yes_no:{utterance}")
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return self._yes_no or YesNoResult("unclear", 0.0)

    def prompt_score(self, lines):
        self.calls.append("prompt_score")
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return self._prompt if self._prompt is not None else 0.0


def test_fallback_fast_path_never_calls_the_network_for_exact_shim_or_yes_no():
    jev = Scripted("jev", route=RouteResult("claude_code", 0.99))
    fb = FallbackRouter([KeywordRouter(), jev], 0.85)
    r = fb.route("mute", CTX)
    assert (r.destination, r.command) == ("shim_command", "mute")
    assert jev.calls == []
    assert fb.yes_no("yes").answer == "yes"
    assert fb.yes_no("always allow").answer == "unclear"
    assert jev.calls == []


def test_fallback_ordering_and_provider_error_moves_on():
    jev = Scripted("jev", fail=True)
    haiku = Scripted("anthropic", route=RouteResult("transcript_query", 0.92), yes_no=YesNoResult("yes", 0.96), prompt=0.7)
    fb = FallbackRouter([KeywordRouter(), jev, haiku], 0.85)
    assert fb.name == "fallback(keyword,jev,anthropic)"

    r = fb.route("what did you change", CTX)
    assert r.destination == "transcript_query" and r.confidence == pytest.approx(0.92)
    assert jev.calls == ["route:what did you change"]
    assert haiku.calls == ["route:what did you change"]

    # Not in the strict vocabulary: smart routers decide.
    assert fb.yes_no("yeah sure why not").answer == "yes"
    assert haiku.calls[-1] == "yes_no:yeah sure why not"

    # Regex unsure: ask the chain.
    assert fb.prompt_score(["some output"]) == pytest.approx(0.7)
    # Regex sure: no call.
    n = len(haiku.calls)
    assert fb.prompt_score(["Do you want to proceed?", "❯ 1. Yes"]) == pytest.approx(0.95)
    assert len(haiku.calls) == n


def test_fallback_remembers_the_last_failure_per_router():
    """Health reads ``last_errors`` to explain a silent fall-through (a rejected key)."""
    jev = Scripted("jev", fail=True)
    fb = FallbackRouter([KeywordRouter(), jev])
    assert fb.last_errors == {}
    fb.route("please refactor the upload handler", CTX)
    assert fb.last_errors == {"jev": "jev down"}
    jev.fail = False
    fb.route("please refactor the upload handler", CTX)
    assert fb.last_errors == {}  # cleared by the next success


def test_fallback_last_resort_is_keyword_answer():
    jev = Scripted("jev", fail=True)
    haiku = Scripted("anthropic", fail=True)
    fb = FallbackRouter([KeywordRouter(), jev, haiku], 0.85)
    r = fb.route("what did you change", CTX)
    assert (r.destination, r.confidence) == ("transcript_query", pytest.approx(0.9))
    r = fb.route("refactor the parser", CTX)
    assert (r.destination, r.confidence) == ("claude_code", pytest.approx(0.6))
    assert fb.yes_no("hmm maybe").answer == "unclear"
    assert fb.prompt_score(["plain output"]) == 0.0


def test_fallback_smart_router_decides_when_keyword_is_not_certain():
    jev = Scripted("jev", route=RouteResult("claude_code", 0.97))
    fb = FallbackRouter([KeywordRouter(), jev], 0.85)
    # Keyword says transcript_query at 0.9 (not fast path); Jev overrides.
    assert fb.route("what did you change", CTX).destination == "claude_code"
    # Empty utterance: nothing to decide, no call.
    assert fb.route("um", CTX).destination == "unclear"
    assert jev.calls == ["route:what did you change"]


# ---- make_router ------------------------------------------------------------------------------


def _config(router: str, typesafe: str = "", anthropic_key: str = "") -> Config:
    cfg = Config()
    cfg.providers.router = router
    cfg.providers.keys = {"typesafe": typesafe, "anthropic": anthropic_key, "openai": "", "elevenlabs": "", "groq": ""}
    return cfg


def test_make_router_keyword_only_when_no_keys(monkeypatch, caplog):
    for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    caplog.set_level("INFO", logger="zordon.routing.select")
    r = make_router(_config("jev"))
    assert isinstance(r, FallbackRouter)
    assert [x.name for x in r.chain] == ["keyword"]
    assert "active routers: keyword" in caplog.text


def test_make_router_jev_chain_and_preference_order(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    caplog.set_level("INFO", logger="zordon.routing.select")
    r = make_router(_config("jev", typesafe="ts-secret-key-value", anthropic_key="sk-ant-secret-value"))
    assert [x.name for x in r.chain] == ["keyword", "jev", "anthropic"]
    assert "ts-secret-key-value" not in caplog.text
    assert "sk-ant-secret-value" not in caplog.text

    r = make_router(_config("anthropic", typesafe="ts-secret-key-value", anthropic_key="sk-ant-secret-value"))
    assert [x.name for x in r.chain] == ["keyword", "anthropic"]

    r = make_router(_config("keyword", typesafe="ts-secret-key-value", anthropic_key="sk-ant-secret-value"))
    assert [x.name for x in r.chain] == ["keyword"]

    r = make_router(_config("jev", typesafe="ts-secret-key-value"))
    assert [x.name for x in r.chain] == ["keyword", "jev"]
    assert r.confidence_threshold == pytest.approx(0.85)


# ---- transcript queries ------------------------------------------------------------------------


def test_transcript_fallback_answer_is_deterministic():
    from zordon.routing.transcript_query import (
        NOTHING_YET,
        TranscriptAnswerer,
        answer,
        fallback_answer,
    )

    tail = ["I'm adding retry logic to the upload handler.", "I edited auth dot py, changing eight lines.", "All forty-two tests pass."]
    assert fallback_answer("did the tests pass", tail) == "The last thing it said about that was: All forty-two tests pass."
    assert "auth dot py" in fallback_answer("what did you just change", tail)
    assert fallback_answer("what did it say", tail) == "The last thing it said was: All forty-two tests pass."
    assert fallback_answer("anything", []) == NOTHING_YET
    assert answer("did the tests pass", tail, None) == fallback_answer("did the tests pass", tail)
    a = TranscriptAnswerer(None)
    assert a.uses_model is False
    assert a.answer("did the tests pass", tail).endswith("All forty-two tests pass.")


def test_transcript_answer_via_mock_client_and_fallback_on_error():
    import anthropic

    from zordon.routing.transcript_query import TranscriptAnswerer, answer, ask_haiku

    tail = ["I edited auth dot py.", "All tests pass."]

    def reply(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert "Question: did the tests pass" in body["messages"][0]["content"] or "did the tests pass" in body["messages"][0]["content"]
        return _msg({"answer": "Yes, all tests passed."}) if "output_config" in body else httpx2.Response(
            200,
            json={"id": "m", "type": "message", "role": "assistant", "model": "x",
                  "content": [{"type": "text", "text": "Yes, all tests passed."}],
                  "stop_reason": "end_turn", "stop_sequence": None,
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    client = anthropic.Anthropic(api_key="sk-ant-test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(reply)), max_retries=0)
    assert ask_haiku(client, "did the tests pass", tail) == "Yes, all tests passed."
    assert answer("did the tests pass", tail, client) == "Yes, all tests passed."

    answerer = TranscriptAnswerer("sk-ant-test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(reply)))
    assert answerer.uses_model is True
    assert answerer.answer("did the tests pass", tail) == "Yes, all tests passed."

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no network", request=request)

    broken = anthropic.Anthropic(api_key="sk-ant-test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(down)), max_retries=0)
    with pytest.raises(ProviderError):
        ask_haiku(broken, "did the tests pass", tail)
    # answer() never raises: it falls back to the deterministic readback.
    assert answer("did the tests pass", tail, broken).startswith("The last thing it said")


def test_transcript_answer_prefers_a_normalizer_that_can_answer():
    from zordon.routing.transcript_query import TranscriptAnswerer, answer

    class Normalizer:
        name = "fake"

        def __init__(self):
            self.calls = []

        def answer_transcript_query(self, question, tail):
            self.calls.append((question, list(tail)))
            return "  It edited auth.  "

    n = Normalizer()
    assert answer("what did it change", ["I edited auth dot py."], n) == "It edited auth."
    assert n.calls == [("what did it change", ["I edited auth dot py."])]
    a = TranscriptAnswerer("", normalizer=n)
    assert a.uses_model is True
    assert a.answer("what did it change", ["I edited auth dot py."]) == "It edited auth."
    # An empty tail never calls the model.
    assert a.answer("what did it change", []) == "I haven't heard anything from this session yet."
    assert len(n.calls) == 2
