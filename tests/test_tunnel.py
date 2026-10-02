"""Tunnel helper tests. The real cloudflared/ngrok binaries are never run: a
Python stand-in (``tests/fake_cloudflared.py``) prints cloudflared's real stderr
box, and ngrok's local API is answered by ``httpx.MockTransport``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from zordon.transport import tunnel as T

FAKE = Path(__file__).resolve().parent / "fake_cloudflared.py"
URL = "https://quiet-ocean-example-1234.trycloudflare.com"


def fake_cmd() -> list[str]:
    return [sys.executable, str(FAKE)]


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # Still exists; it may be a zombie already reaped by Popen.wait, which counts as dead.
        time.sleep(0.05)
    return False


# ---- regex -----------------------------------------------------------------------------


def test_regex_matches_box_line_not_disclaimer():
    box = "2026-10-01T20:40:02Z INF |  https://quiet-ocean-example-1234.trycloudflare.com         |"
    disclaimer = (
        "2026-10-01T20:40:00Z INF Thank you for trying Cloudflare Tunnel ... "
        "(https://www.cloudflare.com/website-terms/) ... https://developers.cloudflare.com/x"
    )
    assert T.TRYCLOUDFLARE_RE.search(box).group(0) == URL
    assert T.TRYCLOUDFLARE_RE.search(disclaimer) is None
    assert T.TRYCLOUDFLARE_RE.search("INF Requesting new quick Tunnel on trycloudflare.com...") is None


# ---- cloudflared via the fake -------------------------------------------------------------


def test_fake_prints_the_box_to_stderr():
    proc = subprocess.Popen(
        [*fake_cmd(), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        lines = []
        deadline = time.monotonic() + 5
        assert proc.stderr is not None
        while time.monotonic() < deadline and len(lines) < 6:
            lines.append(proc.stderr.readline().rstrip("\n"))
        assert any(" INF +----" in line for line in lines)
        assert any("Your quick Tunnel has been created!" in line for line in lines)
        assert any(URL in line for line in lines)
        assert all(" INF " in line for line in lines if line)
    finally:
        proc.terminate()
        proc.wait(5)


def test_start_returns_url_and_stop_kills(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CLOUDFLARED_URL", URL)
    t = T.Tunnel("cloudflared", 8765, binary=fake_cmd())
    assert not t.is_running and t.url is None
    url = t.start(timeout=10)
    assert url == URL and t.url == URL
    assert t.is_running
    pid = t.pid
    assert pid is not None
    t.stop()
    assert not t.is_running
    assert wait_dead(pid)
    assert any(URL in line for line in t.recent_lines())
    # stop twice is harmless
    t.stop()


def test_command_line_is_the_verified_one(monkeypatch: pytest.MonkeyPatch):
    t = T.Tunnel("cloudflared", 9001, binary="/opt/bin/cloudflared")
    assert t._command() == [
        "/opt/bin/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:9001",
    ]
    n = T.Tunnel("ngrok", 9001, binary="/opt/bin/ngrok")
    assert n._command() == ["/opt/bin/ngrok", "http", "9001", "--log", "stdout", "--log-format", "json"]


def test_binary_lookup_uses_assets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(T.assets, "find_binary", lambda name: f"/found/{name}")
    assert T.Tunnel("cloudflared", 1)._command()[0] == "/found/cloudflared"
    monkeypatch.setattr(T.assets, "find_binary", lambda name: None)
    with pytest.raises(T.TunnelError, match="not found"):
        T.Tunnel("cloudflared", 1).start()


def test_exit_before_url_raises_with_tail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CLOUDFLARED_MODE", "exit")
    t = T.Tunnel("cloudflared", 8765, binary=fake_cmd())
    with pytest.raises(T.TunnelError) as ei:
        t.start(timeout=10)
    msg = str(ei.value)
    assert "exited" in msg
    assert "fake failure" in msg  # last lines are included
    assert "Requesting new quick Tunnel" in msg
    assert not t.is_running and t.url is None


def test_timeout_before_url_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CLOUDFLARED_MODE", "silent")
    t = T.Tunnel("cloudflared", 8765, binary=fake_cmd())
    t0 = time.monotonic()
    with pytest.raises(T.TunnelError, match="did not print"):
        t.start(timeout=1.0)
    assert time.monotonic() - t0 < 6
    assert not t.is_running


def test_missing_binary_is_tunnel_error():
    t = T.Tunnel("cloudflared", 8765, binary="/nonexistent/cloudflared-zordon-test")
    with pytest.raises(T.TunnelError, match="could not start"):
        t.start(timeout=1)


def test_unknown_provider_rejected():
    with pytest.raises(T.TunnelError):
        T.Tunnel("tailscale", 1)


def test_bad_command_line_is_noticed_by_fake():
    """The stand-in refuses anything but ``tunnel`` so a wrong argv surfaces as an exit."""
    t = T.Tunnel("cloudflared", 8765, binary=[*fake_cmd(), "not-tunnel"])
    with pytest.raises(T.TunnelError, match="exited"):
        t.start(timeout=5)


# ---- ngrok via a sleeping child and a mocked local API -------------------------------------


def test_ngrok_polls_local_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == T.NGROK_API
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "tunnels": [
                    {"public_url": "http://d95211d2.ngrok.app", "proto": "http"},
                    {"public_url": "https://d95211d2.ngrok.app", "proto": "https"},
                ],
                "uri": "/api/tunnels",
            },
        )

    monkeypatch.setattr(T, "NGROK_POLL_S", 0.01)
    # A child that records its argv and sleeps stands in for ngrok.
    argv_file = tmp_path / "ngrok-argv.txt"
    script = "import sys, time; open(sys.argv[1], 'w').write(' '.join(sys.argv[2:])); time.sleep(60)"
    t = T.Tunnel(
        "ngrok",
        8765,
        binary=[sys.executable, "-c", script, str(argv_file)],
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    url = t.start(timeout=5)
    assert url == "https://d95211d2.ngrok.app"
    assert calls["n"] >= 2
    assert t.is_running
    pid = t.pid
    t.stop()
    assert not t.is_running and pid is not None and wait_dead(pid)
    assert argv_file.read_text().split() == ["http", "8765", "--log", "stdout", "--log-format", "json"]


def test_ngrok_api_parsing_ignores_garbage():
    bad = [httpx.Response(200, text="not json"), httpx.Response(200, json={"tunnels": "x"}), httpx.Response(200, json={"tunnels": [{"public_url": "http://only-plain"}]})]

    def handler(request: httpx.Request) -> httpx.Response:
        return bad.pop(0) if bad else httpx.Response(404)

    t = T.Tunnel("ngrok", 1, binary="x", http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert t._poll_ngrok_api() is None
    assert t._poll_ngrok_api() is None
    assert t._poll_ngrok_api() is None
    assert t._poll_ngrok_api() is None


def test_tail_text_is_last_ten_lines():
    t = T.Tunnel("cloudflared", 1, binary="x")
    for i in range(25):
        t._lines.append(f"line {i}")
    tail = t.recent_lines()
    assert tail == [f"line {i}" for i in range(15, 25)]
    assert json.dumps(tail)  # plain strings


# ---- environment scrubbing (SEC-1) --------------------------------------------------------


def test_scrubbed_env_drops_keys_tokens_and_provider_prefixes():
    src = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "OPENAI_API_KEY": "sk-x",
        "ELEVENLABS_API_KEY": "x",
        "GROQ_API_KEY": "gsk_x",
        "TYPESAFE_API_KEY": "x",
        "anthropic_base_url": "x",
        "OPENAI_ORG": "x",
        "GITHUB_TOKEN": "x",
        "MY_SECRET": "x",
        "HF_TOKEN": "x",
        "NO_AUTOUPDATE": "false",
        "LANG": "C.UTF-8",
    }
    env = T.scrubbed_env(src)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "NO_AUTOUPDATE": "true"}
    assert T.is_secret_env_name("TYPESAFE_WHATEVER")
    assert not T.is_secret_env_name("TOKENIZERS_PARALLELISM")


def test_tunnel_child_does_not_inherit_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_file = tmp_path / "child-env.json"
    monkeypatch.setenv("FAKE_CLOUDFLARED_ENV_FILE", str(env_file))
    monkeypatch.setenv("FAKE_CLOUDFLARED_URL", URL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-LEAKED")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_LEAKED")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_LEAKED")
    monkeypatch.setenv("ZORDON_HOOK_SECRET", "LEAKED")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://leaked")
    monkeypatch.setenv("ZORDON_KEEP_ME", "kept")
    t = T.Tunnel("cloudflared", 8765, binary=fake_cmd())
    try:
        assert t.start(timeout=10) == URL
    finally:
        t.stop()
    child = json.loads(env_file.read_text())
    assert "ANTHROPIC_API_KEY" not in child
    assert "GROQ_API_KEY" not in child
    assert "GITHUB_TOKEN" not in child
    assert "ZORDON_HOOK_SECRET" not in child
    assert "OPENAI_BASE_URL" not in child
    assert "LEAKED" not in json.dumps(child)
    assert child["ZORDON_KEEP_ME"] == "kept"
    assert child["NO_AUTOUPDATE"] == "true"
    assert "PATH" in child
