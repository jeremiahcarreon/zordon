"""``zordon.health.collect`` against a fake agent whose providers, threads and tmux are
controllable, so every item's status logic and the overall rules are exercised
without tmux, models or a network."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zordon import health as H
from zordon.config import Config
from zordon.output.normalizer import PassthroughNormalizer
from zordon.output.tts import SilenceTTS
from zordon.providers import ProviderNotConfigured
from zordon.routing.keyword import KeywordRouter
from zordon.routing.select import FallbackRouter
from zordon.speech.vad import FakeVAD
from zordon.transport import protocol as P

# ---- fakes --------------------------------------------------------------------------------------


class Thread:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class Tmux:
    binary = "tmux"

    def __init__(self, *, binary_present: bool = True, alive: bool = True, hang: float = 0.0) -> None:
        self.present = binary_present
        self.alive = alive
        self.hang = hang

    def binary_available(self) -> bool:
        return self.present

    def server_alive(self) -> bool:
        if self.hang:
            time.sleep(self.hang)
        return self.alive


class Named:
    def __init__(self, name: str, **attrs: Any) -> None:
        self.name = name
        for k, v in attrs.items():
            setattr(self, k, v)


class SileroVAD:  # the class *name* is what the check looks at
    model_path = "/models/silero_vad.onnx"


class FakeAgent:
    version = "0.1.0"

    def __init__(self, tmp_path: Path) -> None:
        self.config = Config.default()
        self.config.providers.keys = {k: "" for k in self.config.providers.keys}
        self.tmp = tmp_path
        self.tunnel_url: str | None = None
        self.update_status: dict[str, Any] | None = None
        self.manager = SimpleNamespace(tmux=Tmux(), sessions={}, is_alive=lambda: True)
        self.pipeline = Thread()
        self.audio = Thread()
        self.dispatcher = Thread()
        self.focused: str | None = "abc12345-6789"
        self.sessions = SimpleNamespace(focused=lambda: self.focused)
        self.adapters = {"claude-code": SimpleNamespace(info=SimpleNamespace(display_name="Claude Code"))}
        self.agents: dict[str, str | None] = {"claude-code": "/usr/bin/claude", "codex": None, "generic": ""}
        self.providers = SimpleNamespace(
            normalizer=Named("anthropic", model="claude-haiku-4-5", credentials_configured=lambda: True),
            tts=self._kokoro(),
            stt=Named("faster-whisper", loaded=True, device="cpu", model_spec="small.en"),
            vad=SileroVAD(),
            router=FallbackRouter([KeywordRouter(), Named("jev")]),
        )

    def _kokoro(self) -> Named:
        model = self.tmp / "kokoro.onnx"
        voices = self.tmp / "voices.bin"
        model.write_bytes(b"x")
        voices.write_bytes(b"x")
        return Named("kokoro", model_path=model, voices_path=voices, voice="af_heart")

    def available_agents(self) -> dict[str, str | None]:
        return dict(self.agents)


def which_all(name: str) -> str | None:
    return f"/usr/bin/{name}"


def which_none(name: str) -> str | None:
    return None


@pytest.fixture
def agent(tmp_path: Path) -> FakeAgent:
    return FakeAgent(tmp_path)


def item(report: H.HealthReport, key: str) -> H.Item:
    found = [i for i in report.items if i.key == key]
    assert len(found) == 1, f"{key}: {[i.key for i in report.items]}"
    return found[0]


# ---- a healthy agent -----------------------------------------------------------------------


def test_everything_ok(agent: FakeAgent):
    r = H.collect(agent, which=which_all)
    assert r.status == "ok", [(i.key, i.status, i.detail) for i in r.degraded()]
    assert [i.key for i in r.items] == [
        "account", "tmux", "agent", "sessions", "normalizer", "tts", "stt", "vad", "router", "threads", "hooks", "update",
    ]
    assert item(r, "account").detail.startswith("running as ")
    assert r.summary_sentence() == ""
    assert item(r, "sessions").detail.endswith("focused")
    assert item(r, "router").detail == "keyword -> jev"
    assert item(r, "update").detail == "zordon 0.1.0"


def test_to_out_matches_the_protocol_model(agent: FakeAgent):
    out = H.collect(agent, which=which_all).to_out()
    assert isinstance(out, P.HealthOut) and out.type == "health"
    assert out.status == "ok" and out.ts > 0
    assert all(isinstance(i, P.HealthItem) and i.label for i in out.items)
    # Round-trips through JSON (what /health and the socket send).
    assert P.HealthOut.model_validate_json(P.dump(out)) == out


def test_bounded_probe_wall_time(agent: FakeAgent):
    agent.manager.tmux = Tmux(hang=3.0)
    t0 = time.monotonic()
    r = H.collect(agent, which=which_all, timeout=0.2)
    assert time.monotonic() - t0 < 1.5
    t = item(r, "tmux")
    assert t.status == "warn" and "did not answer" in t.detail


# ---- per-item logic ----------------------------------------------------------------------------


def test_tmux_states(agent: FakeAgent):
    agent.manager.tmux = Tmux(alive=False)
    t = item(H.collect(agent, which=which_all), "tmux")
    assert t.status == "warn" and "no tmux server yet" in t.detail
    agent.manager.tmux = Tmux(binary_present=False)
    t = item(H.collect(agent, which=which_all), "tmux")
    assert t.status == "fail" and t.fix == H.FIX_TMUX
    agent.manager.tmux = SimpleNamespace(binary="tmux", binary_available=lambda: True)  # no server_alive (tests' FakeTmux)
    assert item(H.collect(agent, which=which_all), "tmux").status == "ok"


def test_agent_binary(agent: FakeAgent):
    agent.agents["claude-code"] = None
    a = item(H.collect(agent, which=which_all), "agent")
    assert a.status == "fail" and "Claude Code not found" in a.detail and a.fix == H.FIX_CLAUDE
    agent.config.providers.agent = "generic"
    assert item(H.collect(agent, which=which_all), "agent").status == "ok"


def test_no_focused_session_warns(agent: FakeAgent):
    agent.focused = None
    s = item(H.collect(agent, which=which_all), "sessions")
    assert s.status == "warn" and s.detail == "no session focused" and s.fix == H.FIX_SESSION


def test_normalizer_passthrough_warns_with_terse_output(agent: FakeAgent):
    agent.providers.normalizer = PassthroughNormalizer()
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "warn" and "terse" in n.detail and n.fix == H.FIX_NORMALIZER
    agent.config.providers.normalizer = "passthrough"
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "warn" and "terse" in n.detail and "providers.normalizer" in n.fix


def test_normalizer_anthropic_without_credentials_fails(agent: FakeAgent):
    agent.providers.normalizer = Named("anthropic", model="m", credentials_configured=lambda: False)
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "fail" and n.fix == H.FIX_ANTHROPIC_KEY


def test_normalizer_claude_cli_checks_the_binary(agent: FakeAgent, tmp_path: Path):
    agent.providers.normalizer = Named("claude-cli", model="m", binary=str(tmp_path / "missing"))
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "fail" and n.fix == H.FIX_CLAUDE
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    agent.providers.normalizer = Named("claude-cli", model="m", binary=str(exe))
    assert item(H.collect(agent, which=which_all), "normalizer").status == "ok"


def test_normalizer_ollama_reachability(agent: FakeAgent, monkeypatch: pytest.MonkeyPatch):
    from zordon.output.normalizer import ollama as O

    agent.providers.normalizer = Named("ollama", model="qwen2.5:3b-instruct", url="http://127.0.0.1:1")

    def down(url: str, timeout: float = 1.0, client: Any = None) -> list[str]:
        raise ProviderNotConfigured(f"ollama: no server at {url} (ConnectError)")

    monkeypatch.setattr(O, "server_models", down)
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "fail" and "no server" in n.detail and n.fix == "run `ollama serve`"

    monkeypatch.setattr(O, "server_models", lambda url, timeout=1.0, client=None: ["llama3:latest"])
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "fail" and "not pulled" in n.detail and n.fix == "ollama pull qwen2.5:3b-instruct"

    monkeypatch.setattr(O, "server_models", lambda url, timeout=1.0, client=None: ["qwen2.5:3b-instruct"])
    n = item(H.collect(agent, which=which_all), "normalizer")
    assert n.status == "ok" and "qwen2.5:3b-instruct" in n.detail


def test_tts_states(agent: FakeAgent, tmp_path: Path):
    agent.providers.tts = SilenceTTS()
    t = item(H.collect(agent, which=which_all), "tts")
    assert t.status == "fail" and "nothing will be spoken" in t.detail and t.fix == H.FIX_DOWNLOAD
    agent.config.providers.tts = "openai"
    t = item(H.collect(agent, which=which_all), "tts")
    assert t.status == "fail" and "OPENAI_API_KEY" in t.fix
    agent.config.providers.tts = "kokoro"
    (tmp_path / "kokoro.onnx").unlink()
    agent.providers.tts = Named("kokoro", model_path=tmp_path / "kokoro.onnx", voices_path=tmp_path / "voices.bin", voice="af_heart")
    t = item(H.collect(agent, which=which_all), "tts")
    assert t.status == "fail" and "kokoro.onnx" in t.detail and t.fix == H.FIX_DOWNLOAD
    agent.providers.tts = Named("openai", voice="alloy")
    assert item(H.collect(agent, which=which_all), "tts").status == "fail"
    agent.config.providers.keys["openai"] = "sk-test"
    assert item(H.collect(agent, which=which_all), "tts").status == "ok"


def test_stt_states(agent: FakeAgent, tmp_path: Path):
    agent.providers.stt = Named("unavailable", reason="faster-whisper is not installed")
    s = item(H.collect(agent, which=which_all), "stt")
    assert s.status == "fail" and "voice input is unavailable" in s.detail and s.fix == H.FIX_DOWNLOAD
    agent.providers.stt = Named("faster-whisper", loaded=False, device="cpu", model_spec="small.en")
    s = item(H.collect(agent, which=which_all), "stt")
    assert s.status == "warn" and "not downloaded" in s.detail and s.fix == H.FIX_DOWNLOAD
    folder = tmp_path / "faster-whisper-small.en"
    folder.mkdir()
    (folder / "model.bin").write_bytes(b"x")
    agent.providers.stt = Named("faster-whisper", loaded=False, device="cuda", model_spec=str(folder))
    s = item(H.collect(agent, which=which_all), "stt")
    assert s.status == "ok" and "cuda" in s.detail
    agent.providers.stt = Named("groq")
    s = item(H.collect(agent, which=which_all), "stt")
    assert s.status == "fail" and "GROQ_API_KEY" in s.fix


def test_vad_fallback_fails_with_voice_input_off(agent: FakeAgent):
    agent.providers.vad = FakeVAD(lambda _c: 0.0)
    v = item(H.collect(agent, which=which_all), "vad")
    assert v.status == "fail" and v.detail.startswith("voice input is off") and v.fix == H.FIX_DOWNLOAD
    # A SpeechGate wrapping the VAD is unwrapped.
    agent.providers.vad = SimpleNamespace(vad=SileroVAD(), feed=lambda *a: None)
    v = item(H.collect(agent, which=which_all), "vad")
    assert v.status == "ok" and "silero_vad.onnx" in v.detail


def test_router_keyword_only_warns(agent: FakeAgent):
    agent.providers.router = FallbackRouter([KeywordRouter()])
    r = item(H.collect(agent, which=which_all), "router")
    assert r.status == "warn" and "keyword router only" in r.detail and r.fix == H.FIX_ROUTER
    agent.providers.router = KeywordRouter()
    assert item(H.collect(agent, which=which_all), "router").status == "warn"
    agent.providers.router = Named("fake-router")
    assert item(H.collect(agent, which=which_all), "router").status == "ok"


def test_router_rejected_key_is_explained(agent: FakeAgent):
    """A chain with a smart router looks healthy until the first utterance; the last
    failure it recorded turns the dot amber with the key to check."""
    fb = FallbackRouter([KeywordRouter(), Named("jev")])
    agent.providers.router = fb
    assert item(H.collect(agent, which=which_all), "router").status == "ok"
    fb.last_errors["jev"] = "jev: TypeSafeAuthenticationError: POST https://api.example/v1: 401 Cannot authenticate with the server."
    r = item(H.collect(agent, which=which_all), "router")
    assert r.status == "warn" and r.detail.startswith("jev rejected the API key") and "providers.keys.typesafe" in r.fix
    fb.last_errors["jev"] = "jev: timed out"
    r = item(H.collect(agent, which=which_all), "router")
    assert r.status == "warn" and r.detail.startswith("jev failed on the last request") and r.fix == H.FIX_ROUTER
    fb.last_errors.clear()
    assert item(H.collect(agent, which=which_all), "router").status == "ok"


def test_root_account_warns(agent: FakeAgent, monkeypatch):
    """Root is amber: every project would act with root's power and Claude Code refuses
    bypass mode there (decision 0018). A normal user is green."""
    monkeypatch.setattr(H.os, "geteuid", lambda: 0)
    a = item(H.collect(agent, which=which_all), "account")
    assert a.status == "warn" and a.detail.startswith("running as root") and "useradd" in a.fix
    monkeypatch.setattr(H.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("USER", "jane")
    a = item(H.collect(agent, which=which_all), "account")
    assert a.status == "ok" and a.detail == "running as jane"


def test_dead_thread_fails(agent: FakeAgent):
    agent.pipeline = Thread(alive=False)
    t = item(H.collect(agent, which=which_all), "threads")
    assert t.status == "fail" and t.detail == "pipeline thread not running" and t.fix == H.FIX_RESTART
    agent.audio = Thread(alive=False)
    assert "pipeline, audio threads" in item(H.collect(agent, which=which_all), "threads").detail


def test_hooks_without_curl_warn(agent: FakeAgent):
    def which(name: str) -> str | None:
        return None if name == "curl" else f"/usr/bin/{name}"

    h = item(H.collect(agent, which=which), "hooks")
    assert h.status == "warn" and "curl not found" in h.detail and h.fix == H.FIX_CURL and h.optional


def test_update_item(agent: FakeAgent):
    agent.update_status = {"current": "0.1.0", "latest": "0.1.0", "available": False}
    assert item(H.collect(agent, which=which_all), "update").status == "ok"
    agent.update_status = {"current": "0.1.0", "latest": "0.2.0", "available": True}
    u = item(H.collect(agent, which=which_all), "update")
    assert u.status == "warn" and "0.2.0 is available" in u.detail and u.fix == "zordon update"
    agent.update_status = {"current": "0.1.0", "latest": "0.2.0", "available": True, "installed": True}
    u = item(H.collect(agent, which=which_all), "update")
    assert u.status == "warn" and "installed" in u.detail and u.fix == "restart zordon serve"


def test_tunnel_item_only_when_active(agent: FakeAgent):
    assert "tunnel" not in [i.key for i in H.collect(agent, which=which_all).items]
    agent.tunnel_url = "https://x.trycloudflare.com"
    t = item(H.collect(agent, which=which_all), "tunnel")
    assert t.status == "ok" and t.detail == agent.tunnel_url and t.optional


# ---- overall status rules ----------------------------------------------------------------


def test_overall_is_the_worst_item(agent: FakeAgent):
    agent.providers.router = KeywordRouter()
    assert H.collect(agent, which=which_all).status == "warn"
    agent.providers.vad = FakeVAD(lambda _c: 0.0)
    r = H.collect(agent, which=which_all)
    assert r.status == "fail"
    assert r.summary_sentence().startswith("Needs attention: ")
    assert "voice detection failed: voice input is off" in r.summary_sentence()
    assert "router warning: keyword router only" in r.summary_sentence()


def test_optional_items_never_fail():
    items = [
        H.Item("hooks", "fail", "forced"),
        H.Item("update", "fail", "forced"),
        H.Item("tunnel", "fail", "forced"),
        H.Item("tmux", "ok"),
    ]
    r = H.make_report(items)
    assert r.status == "warn"
    assert all(i.status == "warn" and i.optional for i in r.items if i.key in H.OPTIONAL_KEYS)
    assert H.overall_status([H.Item("hooks", "fail", optional=True)]) == "warn"
    assert H.overall_status([H.Item("tts", "fail")]) == "fail"
    assert H.overall_status([]) == "ok"


def test_a_broken_probe_becomes_a_warning(agent: FakeAgent):
    class Boom:
        @property
        def name(self) -> str:
            raise RuntimeError("kaput")

    agent.providers.tts = Boom()
    t = item(H.collect(agent, which=which_all), "tts")
    assert t.status == "warn" and "check failed: kaput" in t.detail


def test_signature_ignores_the_timestamp(agent: FakeAgent):
    a = H.collect(agent, which=which_all)
    time.sleep(0.01)
    b = H.collect(agent, which=which_all)
    assert a.ts != b.ts and a.signature() == b.signature()
    agent.update_status = {"current": "0.1.0", "latest": "0.2.0", "available": True}
    assert H.collect(agent, which=which_all).signature() != a.signature()
