"""Ollama normalizer and router against an httpx MockTransport (no server needed)."""

from __future__ import annotations

import json

import httpx
import pytest

from zordon.config import Config
from zordon.output.normalizer import make_normalizer
from zordon.output.normalizer.ollama import OllamaNormalizer, has_model, too_long
from zordon.providers import ProviderError, ProviderNotConfigured
from zordon.routing.base import RouteContext
from zordon.routing.ollama import OllamaRouter


class Server:
    """Scripted Ollama: /api/tags lists models, /api/chat answers from a function."""

    def __init__(self, models=("qwen2.5:3b-instruct",), reply=None, status=200, slow=False):
        self.models = list(models)
        self.reply = reply or (lambda body: "I edited auth dot py, changing eight lines.")
        self.status = status
        self.slow = slow
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            self.requests.append(body)
            if self.slow:
                raise httpx.ReadTimeout("slow", request=request)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "boom"})
            return httpx.Response(200, json={"message": {"role": "assistant", "content": self.reply(body)}})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=1.0)


def make(server: Server, **kw) -> OllamaNormalizer:
    return OllamaNormalizer("http://ollama.test", kw.pop("model", "qwen2.5:3b-instruct"), client=server.client(), **kw)


def test_requires_server_and_model():
    with pytest.raises(ProviderNotConfigured):
        OllamaNormalizer("http://127.0.0.1:1", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    srv = Server(models=("llama3:8b",))
    with pytest.raises(ProviderNotConfigured) as e:
        make(srv)
    assert "ollama pull qwen2.5:3b-instruct" in str(e.value)
    assert has_model(["qwen2.5:3b-instruct"], "qwen2.5:3b-instruct")
    assert has_model(["foo:latest"], "foo") and not has_model(["foo:7b"], "foo")


def test_normalize_request_shape_and_cleanup():
    srv = Server(reply=lambda b: "**I edited auth dot py**, changing eight lines.\n\nExtra line.")
    n = make(srv)
    assert n.granularity == "sentence"
    out = n.normalize("Edited `auth.py`, 8 lines changed.", ["Previous one."])
    assert out == "I edited auth dot py, changing eight lines."
    body = srv.requests[0]
    assert body["model"] == "qwen2.5:3b-instruct" and body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert body["messages"][0]["role"] == "system" and "Keep every fact" in body["messages"][0]["content"]
    assert "Previous one." in body["messages"][1]["content"]
    assert body["messages"][1]["content"].endswith("Sentence: Edited `auth.py`, 8 lines changed.")


def test_length_guard_retries_then_falls_back():
    long = "I added retry logic so that whenever anything at all goes wrong during an upload it will try again and again with backoff forever."
    srv = Server(reply=lambda b: long)
    n = make(srv)
    with pytest.raises(ProviderError):
        n.normalize("Added retry logic w/ backoff.", [])
    assert len(srv.requests) == 2 and "too long" in srv.requests[1]["messages"][0]["content"]
    assert n.guarded == 1
    assert too_long("Added retry logic w/ backoff.", long)
    assert not too_long("Done.", "I am done with that now.")  # short inputs may grow


def test_length_guard_accepts_a_brief_retry():
    answers = iter(["way too many words " * 6, "I added retry logic with backoff."])
    srv = Server(reply=lambda b: next(answers))
    n = make(srv)
    assert n.normalize("Added retry logic w/ backoff.", []) == "I added retry logic with backoff."


def test_errors_become_provider_error():
    assert isinstance(make(Server(status=500)), OllamaNormalizer)
    with pytest.raises(ProviderError):
        make(Server(status=500)).normalize("x y z w", [])
    with pytest.raises(ProviderError):
        make(Server(slow=True)).normalize("x y z w", [])
    with pytest.raises(ProviderError):
        make(Server(reply=lambda b: "")).normalize("x y z w", [])


def test_transcript_answer():
    srv = Server(reply=lambda b: "It changed auth dot py.")
    n = make(srv)
    assert n.answer_transcript_query("what changed?", []) == "I don't see that in the transcript."
    assert n.answer_transcript_query("what changed?", ["editing auth.py", "Done."]) == "It changed auth dot py."
    assert "Transcript (oldest first)" in srv.requests[0]["messages"][1]["content"]


def test_factory_auto_order(monkeypatch, tmp_path):
    cfg = Config.default()
    cfg.providers.ollama_url = "http://ollama.test"
    srv = Server()
    real_client = httpx.Client
    monkeypatch.setattr(
        "zordon.output.normalizer.ollama.httpx.Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(srv.handler), timeout=1.0),
    )
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude binary
    assert make_normalizer(cfg).name == "ollama"
    cfg.providers.keys["anthropic"] = "sk-ant-x"
    assert make_normalizer(cfg).name == "anthropic"
    cfg.providers.keys["anthropic"] = ""
    cfg.providers.normalizer = "ollama"
    cfg.providers.ollama_model = "missing:1b"
    assert make_normalizer(cfg).name == "passthrough"  # explicit but not pulled: degrade with a warning


def test_router_structured_output_and_closed_set():
    def reply(body):
        schema = body["format"]
        if "destination" in schema["properties"]:
            return json.dumps({"destination": "shim_command", "confidence": 0.97, "command": "mute", "argument": None})
        if "answer" in schema["properties"]:
            return json.dumps({"answer": "yes", "confidence": 0.99})
        return json.dumps({"waiting": "blocked", "confidence": 0.9})

    srv = Server(reply=reply)
    r = OllamaRouter("http://ollama.test", "qwen2.5:3b-instruct", client=srv.client())
    res = r.route("mute", RouteContext(session_state="idle"))
    assert res.destination == "shim_command" and res.command == "mute" and res.confidence == 0.97
    assert r.yes_no("yes").answer == "yes"
    assert r.yes_no("always allow").answer == "unclear"
    assert r.prompt_score(["Do you want to proceed?", "1. Yes"]) == 0.9
    assert srv.requests[0]["format"]["type"] == "object"

    srv2 = Server(reply=lambda b: json.dumps({"destination": "shim_command", "confidence": 0.9, "command": "rm_rf"}))
    r2 = OllamaRouter("http://ollama.test", "qwen2.5:3b-instruct", client=srv2.client())
    assert r2.route("x", RouteContext(session_state="idle")).destination == "unclear"

    srv3 = Server(reply=lambda b: "not json")
    r3 = OllamaRouter("http://ollama.test", "qwen2.5:3b-instruct", client=srv3.client())
    with pytest.raises(ProviderError):
        r3.route("x", RouteContext(session_state="idle"))
