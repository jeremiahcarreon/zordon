"""Normalizer tests: passthrough cleanup, the Anthropic request shape and every
fallback path (all through ``httpx2.MockTransport``; no network), credential
handling, the transcript-query helper and the factory."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic
import httpx2
import pytest

from zordon.config import Config
from zordon.output.normalizer import PassthroughNormalizer, make_normalizer
from zordon.output.normalizer import anthropic as norm
from zordon.output.normalizer.base import (
    build_user_content,
    clean_spoken,
    first_line,
    strip_markdown,
)
from zordon.providers import Normalizer, ProviderNotConfigured

# ---- helpers -------------------------------------------------------------------------


def _message(
    text: str | None = "I edited auth dot py, changing eight lines.", stop_reason: str = "end_turn"
) -> dict:
    content = [] if text is None else [{"type": "text", "text": text}]
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _error(status: int, err_type: str, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": err_type, "message": f"{err_type} (test)"}},
        headers=headers or {},
    )


class Recorder:
    """A MockTransport handler that records requests and replies from a queue."""

    def __init__(self, *responses) -> None:
        self.requests: list[httpx2.Request] = []
        self.bodies: list[dict] = []
        self._responses = list(responses)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        reply = self._responses.pop(0) if self._responses else httpx2.Response(200, json=_message())
        if isinstance(reply, BaseException):
            if isinstance(reply, httpx2.RequestError) and reply._request is None:  # noqa: SLF001
                reply.request = request
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    @property
    def body(self) -> dict:
        return self.bodies[-1]


def make(recorder: Recorder, model: str = norm.HAIKU, **kw) -> norm.AnthropicNormalizer:
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recorder))
    return norm.AnthropicNormalizer("sk-ant-test-key", model=model, http_client=http, **kw)


@pytest.fixture(autouse=True)
def _no_profile_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK can discover an ``ant auth login`` profile under ``~/.config/anthropic``;
    give it an empty home so "no credentials" really means none."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)


# ---- passthrough and pure helpers -------------------------------------------------


class TestPassthrough:
    def test_is_a_normalizer(self):
        p = PassthroughNormalizer()
        assert isinstance(p, Normalizer)
        assert p.name == "passthrough"

    @pytest.mark.parametrize(
        ("raw", "spoken"),
        [
            ("Edited `auth.py`, 8 lines changed.", "Edited auth.py, 8 lines changed."),
            ("## Summary", "Summary"),
            ("**Root cause:** the buffer never flushed.", "Root cause: the buffer never flushed."),
            ("- Added `SpeechGate`", "Added SpeechGate"),
            ("See [the doc](docs/protocol.md) for details.", "See the doc for details."),
            ("a   b\t\tc\n d", "a b c d"),
            ("Coverage 87% -> 91%.", "Coverage 87 percent to 91 percent."),
            ("Fix:", "Fix"),
            ("PR #412 ready", "PR number 412 ready"),
            ("use < not <=", "use less than not at most"),
        ],
    )
    def test_cleanup(self, raw: str, spoken: str):
        assert PassthroughNormalizer().normalize(raw, []) == spoken

    def test_identifiers_survive(self):
        out = PassthroughNormalizer().normalize("call __init__ and snake_case_name", [])
        assert "__init__" in out and "snake_case_name" in out

    def test_empty_and_markdown_only(self):
        assert PassthroughNormalizer().normalize("", []) == ""
        assert PassthroughNormalizer().normalize("```", []) == ""
        assert PassthroughNormalizer().normalize("---", []) == ""

    def test_context_is_ignored(self):
        p = PassthroughNormalizer()
        assert p.normalize("Done.", ["earlier", "sentences"]) == "Done."


class TestHelpers:
    def test_strip_markdown_fence(self):
        assert strip_markdown("```python\nx = 1\n```").strip() == "x = 1"

    def test_first_line(self):
        assert first_line('Spoken: "I did it."\nsecond line') == "I did it."
        assert first_line("\n\n  Hello.  \n") == "Hello."
        assert first_line("") == ""
        assert first_line("“Quoted.”") == "Quoted."

    def test_clean_spoken_collapses(self):
        assert clean_spoken("  Done ,  tests   pass .  ") == "Done, tests pass."

    def test_user_content_shape(self):
        msg = build_user_content("Edited auth.py", ["first", "second", "third"])
        assert msg.startswith("<context>\n")
        assert "first" not in msg  # only the previous two
        assert "second\nthird\n</context>" in msg
        assert msg.endswith("<sentence>Edited auth.py</sentence>")

    def test_user_content_budget(self):
        long_ctx = ["x" * 300, "y" * 300]
        msg = build_user_content("Short sentence.", long_ctx)
        assert len(msg) <= 600
        assert "<sentence>Short sentence.</sentence>" in msg
        msg2 = build_user_content("z" * 1000, long_ctx)
        assert len(msg2) <= 600


# ---- request shape -------------------------------------------------------------------


class TestRequestShape:
    def test_haiku_body(self):
        rec = Recorder()
        n = make(rec)
        out = n.normalize(
            "Edited `auth.py`, 8 lines changed.", ["I ran the tests.", "They passed."]
        )
        assert out == "I edited auth dot py, changing eight lines."
        body = rec.body
        assert body["model"] == "claude-haiku-4-5"
        assert body["max_tokens"] == 200
        assert body["temperature"] == 0
        assert "thinking" not in body
        system = body["system"]
        assert isinstance(system, list) and len(system) == 1
        assert system[0]["type"] == "text"
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "Rewrite ONLY" in system[0]["text"]
        assert "<sentence>" in system[0]["text"]  # few-shots live inside the system block
        assert len(body["messages"]) == 1
        content = body["messages"][0]["content"]
        assert body["messages"][0]["role"] == "user"
        assert "<context>\nI ran the tests.\nThey passed.\n</context>" in content
        assert content.endswith("<sentence>Edited `auth.py`, 8 lines changed.</sentence>")
        req = rec.requests[0]
        assert str(req.url) == "https://api.anthropic.com/v1/messages"
        assert req.extensions["timeout"]["read"] == 1.5

    def test_prompt_is_versioned(self):
        version, text = norm.load_prompt()
        assert version == "v1"
        assert norm.PROMPT_PATH.read_text().splitlines()[0] == "# normalizer prompt v1"
        assert "# normalizer prompt" not in text
        n = make(Recorder())
        assert n.prompt_version == "v1"

    def test_sonnet_body(self):
        rec = Recorder()
        n = make(rec, model=norm.SONNET)
        n.normalize("tests pass. 42/42.", [])
        body = rec.body
        assert "temperature" not in body
        assert body["thinking"] == {"type": "disabled"}

    def test_unknown_model_sends_neither(self):
        assert norm.model_kwargs("some-other-model") == {}
        assert norm.model_kwargs("claude-haiku-4-5-20251001") == {
            "extra_body": {"temperature": 0.0}
        }

    def test_no_context(self):
        rec = Recorder()
        make(rec).normalize("Done.", [])
        assert "<context>\n\n</context>" in rec.body["messages"][0]["content"]

    def test_empty_sentence_makes_no_request(self):
        rec = Recorder()
        assert make(rec).normalize("   ", []) == ""
        assert rec.requests == []

    def test_only_one_attempt(self):
        rec = Recorder(_error(529, "overloaded_error"))
        make(rec).normalize("Fix lint.", [])
        assert len(rec.requests) == 1


# ---- output cleanup -------------------------------------------------------------------


class TestOutputCleanup:
    def test_first_line_only(self):
        rec = Recorder(httpx2.Response(200, json=_message("I fixed lint.\n\nAnything else?")))
        assert make(rec).normalize("Fix lint.", []) == "I fixed lint."

    def test_label_and_quotes_removed(self):
        rec = Recorder(httpx2.Response(200, json=_message('Spoken: "All forty-two tests pass."')))
        assert make(rec).normalize("tests pass. 42/42.", []) == "All forty-two tests pass."

    def test_stray_markdown_removed(self):
        rec = Recorder(httpx2.Response(200, json=_message("I edited `auth.py`, **eight** lines.")))
        assert make(rec).normalize("Edited auth.py", []) == "I edited auth.py, eight lines."

    def test_leading_blank_lines(self):
        rec = Recorder(httpx2.Response(200, json=_message("\n\n  Done.  ")))
        assert make(rec).normalize("Done.", []) == "Done."


# ---- fallbacks ----------------------------------------------------------------------------


SENTENCE = "Edited `auth.py`, 8 lines changed."


class TestFallbacks:
    @pytest.mark.parametrize(
        "reply",
        [
            pytest.param(httpx2.ReadTimeout("read timed out"), id="timeout"),
            pytest.param(httpx2.ConnectError("connection refused"), id="connect-error"),
            pytest.param(_error(401, "authentication_error"), id="401"),
            pytest.param(_error(429, "rate_limit_error", {"retry-after": "7"}), id="429"),
            pytest.param(_error(529, "overloaded_error"), id="529"),
            pytest.param(_error(500, "api_error"), id="500"),
            pytest.param(_error(400, "invalid_request_error"), id="400"),
            pytest.param(httpx2.Response(200, json=_message("", "refusal")), id="refusal"),
            pytest.param(httpx2.Response(200, json=_message(None)), id="no-content"),
            pytest.param(httpx2.Response(200, json=_message("   \n  ")), id="blank-text"),
            pytest.param(
                httpx2.Response(200, json=_message("I edited", "max_tokens")), id="max-tokens"
            ),
        ],
    )
    def test_returns_input(self, reply, caplog):
        caplog.set_level(logging.DEBUG, logger="zordon.output.normalizer.anthropic")
        rec = Recorder(reply)
        out = make(rec).normalize(SENTENCE, ["ctx"])
        assert out == SENTENCE
        assert len(rec.requests) == 1
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "sk-ant" not in joined

    def test_timeout_is_logged_without_secrets(self, caplog):
        caplog.set_level(logging.WARNING, logger="zordon.output.normalizer.anthropic")
        rec = Recorder(httpx2.ReadTimeout("slow"))
        make(rec).normalize(SENTENCE, [])
        assert any("timed out" in r.getMessage() for r in caplog.records)

    def test_no_credentials_at_request_time(self, monkeypatch):
        """Belt and braces: the SDK raises a bare TypeError when the key vanished."""
        rec = Recorder()
        n = make(rec)
        n.client = anthropic.Anthropic(
            api_key=None,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(rec)),
            max_retries=0,
        )
        assert n.normalize(SENTENCE, []) == SENTENCE
        assert rec.requests == []


# ---- credentials ------------------------------------------------------------------------


class TestCredentials:
    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_key_is_none(self, raw):
        assert norm.normalize_key(raw) is None

    def test_key_is_stripped(self):
        assert norm.normalize_key("  sk-ant-x  ") == "sk-ant-x"

    @pytest.mark.parametrize("key", ["", None])
    def test_not_configured_without_key_or_env(self, key):
        client = norm.make_client(key)
        assert norm.credentials_configured(client) is False
        with pytest.raises(ProviderNotConfigured):
            norm.AnthropicNormalizer(key)

    def test_env_var_counts(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        client = norm.make_client("")
        assert norm.credentials_configured(client) is True
        n = norm.AnthropicNormalizer("")
        assert n.credentials_configured() is True

    def test_explicit_key_counts(self):
        assert norm.credentials_configured(norm.make_client("sk-ant-explicit")) is True

    def test_broken_profile_selection_is_not_configured(self, tmp_path, monkeypatch):
        """ANTHROPIC_CONFIG_DIR pointing at nothing makes the SDK raise at construction;
        that must surface as ProviderNotConfigured so the factory can fall back."""
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "nowhere"))
        with pytest.raises(ProviderNotConfigured):
            norm.make_client("")
        cfg = Config()
        assert isinstance(make_normalizer(cfg), PassthroughNormalizer)

    def test_client_settings(self):
        client = norm.make_client("sk-ant-x", timeout=1.5)
        assert client.max_retries == 0
        assert client.timeout == 1.5


# ---- transcript query --------------------------------------------------------------------


class TestTranscriptQuery:
    TAIL = ["I edited auth dot py, changing eight lines.", "All forty-two tests pass."]

    def test_request_and_answer(self):
        rec = Recorder(httpx2.Response(200, json=_message("It changed auth dot py.")))
        n = make(rec)
        out = norm.answer_transcript_query(n, "what file did it change?", self.TAIL)
        assert out == "It changed auth dot py."
        body = rec.body
        assert body["max_tokens"] == 150
        assert body["model"] == norm.HAIKU
        assert body["temperature"] == 0
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "only" in body["system"][0]["text"].lower()
        assert norm.TRANSCRIPT_ABSENT in body["system"][0]["text"]
        content = body["messages"][0]["content"]
        assert content.startswith("<transcript>\n")
        assert self.TAIL[0] in content and self.TAIL[1] in content
        assert content.endswith("<question>what file did it change?</question>")
        assert rec.requests[0].extensions["timeout"]["read"] == norm.TRANSCRIPT_TIMEOUT_S

    def test_accepts_bare_client(self):
        rec = Recorder(httpx2.Response(200, json=_message("Eight lines.")))
        client = anthropic.Anthropic(
            api_key="sk-ant-test",
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(rec)),
            max_retries=0,
        )
        assert norm.answer_transcript_query(client, "how many lines?", self.TAIL) == "Eight lines."

    def test_multiline_answer_joined(self):
        rec = Recorder(
            httpx2.Response(200, json=_message("It edited auth dot py.\nThe tests pass."))
        )
        out = norm.answer_transcript_query(make(rec), "what happened?", self.TAIL)
        assert out == "It edited auth dot py. The tests pass."

    def test_empty_transcript_short_circuits(self):
        rec = Recorder()
        assert norm.answer_transcript_query(make(rec), "what?", []) == norm.TRANSCRIPT_ABSENT
        assert rec.requests == []

    @pytest.mark.parametrize(
        "reply",
        [
            httpx2.ReadTimeout("slow"),
            _error(529, "overloaded_error"),
            _error(401, "authentication_error"),
        ],
    )
    def test_failure_is_spoken(self, reply):
        rec = Recorder(reply)
        out = norm.answer_transcript_query(make(rec), "what?", self.TAIL)
        assert out == norm.TRANSCRIPT_UNAVAILABLE

    def test_long_transcript_is_truncated(self):
        rec = Recorder(httpx2.Response(200, json=_message("Yes.")))
        tail = [f"line {i} " + "x" * 100 for i in range(200)]
        norm.answer_transcript_query(make(rec), "q", tail)
        content = rec.body["messages"][0]["content"]
        assert len(content) < norm.TRANSCRIPT_MAX_CHARS + 200
        assert "line 199" in content and "line 0 " not in content


# ---- factory --------------------------------------------------------------------------------


class TestFactory:
    def test_anthropic_without_key_falls_back_with_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="zordon.output.normalizer")
        cfg = Config()
        assert cfg.providers.normalizer == "anthropic"
        n = make_normalizer(cfg)
        assert isinstance(n, PassthroughNormalizer)
        assert any("passthrough" in r.getMessage() for r in caplog.records)

    def test_anthropic_with_config_key(self):
        cfg = Config()
        cfg.providers.keys["anthropic"] = "sk-ant-config"
        cfg.providers.normalizer_timeout_seconds = 0.9
        n = make_normalizer(cfg)
        assert isinstance(n, norm.AnthropicNormalizer)
        assert n.timeout == 0.9
        assert n.model == cfg.providers.normalizer_model

    def test_anthropic_with_env_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
        assert isinstance(make_normalizer(Config()), norm.AnthropicNormalizer)

    def test_passthrough_configured(self, caplog):
        caplog.set_level(logging.WARNING, logger="zordon.output.normalizer")
        cfg = Config()
        cfg.providers.normalizer = "passthrough"
        assert isinstance(make_normalizer(cfg), PassthroughNormalizer)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_unknown_name_warns(self, caplog):
        caplog.set_level(logging.WARNING, logger="zordon.output.normalizer")
        cfg = Config()
        cfg.providers.normalizer = "gpt"
        assert isinstance(make_normalizer(cfg), PassthroughNormalizer)
        assert any("unknown" in r.getMessage() for r in caplog.records)

    def test_result_satisfies_protocol(self):
        cfg = Config()
        cfg.providers.keys["anthropic"] = "sk-ant-config"
        assert isinstance(make_normalizer(cfg), Normalizer)
