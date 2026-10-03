from __future__ import annotations

import io
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from zordon.config import ConfigError
from zordon.transport import auth as A
from zordon.transport import server as S

from .fake_agent import FakeAgent

TOKEN = "test-token-abcdefghijklmnop"


@pytest.fixture
def agent(tmp_path: Path) -> FakeAgent:
    return FakeAgent(tmp_path / "uploads", token=TOKEN)


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>Zordon</title><p>zordon client</p>")
    (d / "app.js").write_text("console.log('zordon');")
    return d


@pytest.fixture
def client(agent: FakeAgent, static_dir: Path):
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def loop_client(agent: FakeAgent, static_dir: Path):
    """A client whose TCP peer is loopback, like the pane's curl hook."""
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        yield c


def login(client: TestClient, token: str = TOKEN):
    return client.post("/auth", json={"token": token})


def cookie_from(resp) -> SimpleCookie:
    jar = SimpleCookie()
    jar.load(resp.headers["set-cookie"])
    return jar


# ---- static + health ------------------------------------------------------------------


def test_index_served_with_no_store(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "zordon client" in r.text
    assert r.headers["cache-control"] == "no-store"
    r2 = client.get("/app.js")
    assert r2.status_code == 200 and "zordon" in r2.text


def test_index_missing_is_404_json(agent: FakeAgent, tmp_path: Path):
    empty = tmp_path / "empty-web"
    empty.mkdir()
    with TestClient(S.create_app(agent, static_dir=empty)) as c:
        r = c.get("/")
        assert r.status_code == 404
        assert r.json()["error"]


def test_healthz_needs_no_auth(client: TestClient, agent: FakeAgent):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": agent.version}


def test_health_requires_the_cookie(agent: FakeAgent, tmp_path: Path):
    """``/health`` names providers, paths and versions, so it is behind the cookie;
    ``/healthz`` stays the unauthenticated liveness ping."""
    from zordon.health import Item, make_report

    agent.health = lambda: make_report([Item("vad", "fail", "voice input is off", "zordon doctor --download")], ts=1.0)  # type: ignore[attr-defined]
    web = tmp_path / "web2"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>")
    with TestClient(S.create_app(agent, static_dir=web)) as c:
        assert c.get("/health").status_code in (401, 403)
        assert c.get("/healthz").status_code == 200
        assert c.post("/auth", json={"token": TOKEN}).status_code == 200
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "health" and body["status"] == "fail" and body["ts"] == 1.0
        assert body["items"][0]["key"] == "vad" and body["items"][0]["fix"] == "zordon doctor --download"
        assert r.headers["cache-control"] == "no-store"
        assert c.get("/health", headers={"Origin": "https://evil.example"}).status_code == 403


def test_health_without_a_probe_is_503(client: TestClient):
    assert login(client).status_code == 200
    assert client.get("/health").status_code == 503


def test_security_headers_present(client: TestClient):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self';" in csp
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "connect-src 'self' ws://testserver wss://testserver" in csp
    assert "media-src 'self' data: blob:" in csp
    assert "frame-ancestors 'none'" in csp
    assert "cache-control" not in r.headers  # only index is no-store


# ---- auth ---------------------------------------------------------------------------------


def test_auth_wrong_token_401(client: TestClient):
    r = login(client, "nope")
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


def test_auth_right_token_sets_cookie(client: TestClient):
    r = login(client)
    assert r.status_code == 200 and r.json() == {"ok": True}
    jar = cookie_from(r)
    morsel = jar[A.COOKIE_NAME]
    assert len(morsel.value) >= 40
    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    assert int(morsel["max-age"]) == 30 * 24 * 3600
    assert not morsel["secure"]


def test_auth_tunnel_mode_cookie_is_secure(agent: FakeAgent, static_dir: Path):
    app = S.create_app(agent, tunnel_mode=True, static_dir=static_dir)
    with TestClient(app, base_url="https://testserver") as c:
        r = login(c)
        assert r.status_code == 200
        assert cookie_from(r)[A.COOKIE_NAME]["secure"]


def test_auth_cookie_secure_behind_loopback_https_proxy(agent: FakeAgent, static_dir: Path):
    """SEC-12: X-Forwarded-Proto: https from a loopback peer marks the cookie Secure
    without --tunnel; the same header from a remote peer is ignored."""
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        r = c.post("/auth", json={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert cookie_from(r)[A.COOKIE_NAME]["secure"]
        r = c.post("/auth", json={"token": TOKEN})
        assert not cookie_from(r)[A.COOKIE_NAME]["secure"]
    with TestClient(app, client=("198.51.100.9", 5000)) as remote:
        r = remote.post("/auth", json={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert not cookie_from(r)[A.COOKIE_NAME]["secure"]


def test_auth_empty_token_rejected(client: TestClient):
    r = client.post("/auth", json={"token": ""})
    assert r.status_code in (401, 422)


def test_rate_limit_after_five_failures(client: TestClient):
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    r = login(client, TOKEN)  # a correct token is refused while limited
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["error"] == "too_many_attempts"
    assert login(client, "wrong").status_code == 429


def test_rate_limit_is_per_ip(agent: FakeAgent, static_dir: Path):
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app, client=("10.0.0.1", 1111)) as bad, TestClient(
        app, client=("10.0.0.2", 2222)
    ) as good:
        for _ in range(5):
            bad.post("/auth", json={"token": "wrong"})
        assert bad.post("/auth", json={"token": TOKEN}).status_code == 429
        assert good.post("/auth", json={"token": TOKEN}).status_code == 200


def test_rate_limit_honours_proxy_header_from_loopback_peer(agent: FakeAgent, static_dir: Path):
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        for _ in range(5):
            c.post("/auth", json={"token": "wrong"}, headers={"CF-Connecting-IP": "203.0.113.9"})
        # same proxied client -> limited
        r = c.post("/auth", json={"token": TOKEN}, headers={"CF-Connecting-IP": "203.0.113.9"})
        assert r.status_code == 429
        # another proxied client through the same loopback proxy -> fine
        r = c.post("/auth", json={"token": TOKEN}, headers={"CF-Connecting-IP": "203.0.113.10"})
        assert r.status_code == 200


def test_proxy_headers_ignored_from_non_loopback_peer():
    class Conn:
        def __init__(self, host: str, headers: dict[str, str]):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    assert A.client_ip(Conn("127.0.0.1", {"cf-connecting-ip": "1.2.3.4"})) == "1.2.3.4"
    assert A.client_ip(Conn("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 1.2.3.4"})) == "1.2.3.4"
    assert A.client_ip(Conn("192.168.1.7", {"cf-connecting-ip": "1.2.3.4"})) == "192.168.1.7"
    assert A.client_ip(Conn("127.0.0.1", {})) == "127.0.0.1"


def test_logout_revokes_cookie(client: TestClient):
    login(client)
    assert client.post("/logout").status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_token_auth_unit():
    auth = A.TokenAuth("abc")
    assert auth.check_token("abc") and not auth.check_token("abd") and not auth.check_token("")
    spec = auth.issue_cookie()
    assert auth.validate_cookie(spec.value)
    assert not auth.validate_cookie("other") and not auth.validate_cookie(None)
    auth.revoke_all()
    assert not auth.validate_cookie(spec.value)
    assert not A.TokenAuth("").check_token("")


def test_rate_limiter_window_slides():
    now = [1000.0]
    rl = A.RateLimiter(limit=2, window_s=60, clock=lambda: now[0])
    rl.record_failure("ip")
    assert rl.is_limited("ip") == (False, 0.0)
    rl.record_failure("ip")
    limited, retry = rl.is_limited("ip")
    assert limited and 59 <= retry <= 60
    now[0] += 61
    assert rl.is_limited("ip") == (False, 0.0)


# ---- websocket gate -----------------------------------------------------------------------


def test_ws_without_cookie_refused(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws"):
            pass
    assert ei.value.code == 1008


def test_ws_with_bogus_cookie_refused(client: TestClient):
    with pytest.raises(WebSocketDisconnect):
        client.cookies.set(A.COOKIE_NAME, "bogus")
        with client.websocket_connect("/ws"):
            pass


def test_ws_cross_origin_refused(client: TestClient):
    login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            pass
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "hello"


# ---- hooks ----------------------------------------------------------------------------------


def test_hook_without_secret_403(loop_client: TestClient, agent: FakeAgent):
    client = loop_client
    r = client.post("/hooks/claude", json={"hook_event_name": "Notification"})
    assert r.status_code == 403
    r = client.post(
        "/hooks/claude",
        json={"hook_event_name": "Notification"},
        headers={"X-Zordon-Hook-Secret": "wrong"},
    )
    assert r.status_code == 403
    assert agent.sessions.hook_events == []


def test_hook_with_secret_calls_session_manager(loop_client: TestClient, agent: FakeAgent):
    client = loop_client
    payload = {"hook_event_name": "Notification", "session_id": "sess-1", "message": "needs you"}
    r = client.post("/hooks/claude", json=payload, headers={"X-Zordon-Hook-Secret": agent.hook_secret})
    assert r.status_code == 200 and r.json() == {}
    assert agent.sessions.hook_events == [payload]
    assert ("hook_event", payload) in agent.sessions.calls


def test_hook_bad_json_400(loop_client: TestClient, agent: FakeAgent):
    r = loop_client.post(
        "/hooks/claude", content=b"not json", headers={"X-Zordon-Hook-Secret": agent.hook_secret}
    )
    assert r.status_code == 400


def test_hook_refuses_non_loopback_and_proxied_peers(agent: FakeAgent, static_dir: Path):
    """SEC-11: a correct secret from the tunnel (non-loopback peer, or loopback with a
    proxy header) is refused before the body is read."""
    payload = {"hook_event_name": "Notification", "session_id": "sess-1"}
    hdr = {"X-Zordon-Hook-Secret": agent.hook_secret}
    app = S.create_app(agent, static_dir=static_dir)
    with TestClient(app, client=("198.51.100.9", 1)) as remote:
        assert remote.post("/hooks/claude", json=payload, headers=hdr).status_code == 403
    with TestClient(app, client=("127.0.0.1", 1)) as proxied:
        r = proxied.post("/hooks/claude", json=payload, headers={**hdr, "CF-Connecting-IP": "198.51.100.9"})
        assert r.status_code == 403
        r = proxied.post("/hooks/claude", json=payload, headers={**hdr, "X-Forwarded-For": "198.51.100.9"})
        assert r.status_code == 403
        assert proxied.post("/hooks/claude", json=payload, headers=hdr).status_code == 200
    assert agent.sessions.hook_events == [payload]


def test_hook_body_is_capped(loop_client: TestClient, agent: FakeAgent):
    """SEC-11: the hook body is capped at 64 KB."""
    hdr = {"X-Zordon-Hook-Secret": agent.hook_secret}
    big = b'{"pad": "' + b"x" * (S.HOOK_MAX_BYTES + 100) + b'"}'
    r = loop_client.post("/hooks/claude", content=big, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 413
    # a chunked body with no Content-Length is cut off while streaming too
    def gen():
        for _ in range(70):
            yield b"x" * 1024
    r = loop_client.post("/hooks/claude", content=gen(), headers=hdr)
    assert r.status_code == 413
    assert agent.sessions.hook_events == []


# ---- upload --------------------------------------------------------------------------------


def test_upload_requires_cookie(client: TestClient):
    r = client.post("/upload", files={"file": ("a.txt", b"hi", "text/plain")})
    assert r.status_code == 401


def test_upload_stores_sanitized_name(client: TestClient, agent: FakeAgent):
    login(client)
    r = client.post(
        "/upload",
        files={"file": ("../../etc/pass wd; rm -rf.PNG", b"\x89PNG\r\n\x1a\n" * 3, "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "pass_wd_rm_-rf.PNG"
    stored = Path(body["path"])
    assert stored.parent == agent.upload_dir
    assert stored.read_bytes() == b"\x89PNG\r\n\x1a\n" * 3
    assert ("upload_path", "pass_wd_rm_-rf.PNG") in agent.calls


def test_upload_raw_body_with_header_name(client: TestClient, agent: FakeAgent):
    login(client)
    r = client.post("/upload", content=b"plain bytes", headers={"X-Zordon-Filename": "notes.md"})
    assert r.status_code == 200
    assert Path(r.json()["path"]).read_bytes() == b"plain bytes"
    assert r.json()["name"] == "notes.md"


def test_upload_rejects_over_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client)
    monkeypatch.setattr(S, "UPLOAD_MAX_BYTES", 1024)
    big = io.BytesIO(b"x" * 2048)
    r = client.post("/upload", files={"file": ("big.bin", big, "application/octet-stream")})
    assert r.status_code == 413
    small = client.post("/upload", files={"file": ("ok.bin", b"x" * 100, "application/octet-stream")})
    assert small.status_code == 200


def test_upload_and_logout_refuse_foreign_origin(client: TestClient, agent: FakeAgent):
    """SEC-14: the WebSocket's same-origin rule also guards POST /upload and /logout."""
    login(client)
    r = client.post(
        "/upload",
        content=b"csrf",
        headers={"X-Zordon-Filename": "csrf.txt", "Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert not (agent.upload_dir / "csrf.txt").exists()
    assert client.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    # still logged in: the matching origin works
    r = client.post(
        "/upload",
        content=b"ok",
        headers={"X-Zordon-Filename": "ok.txt", "Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert client.post("/logout", headers={"Origin": "http://testserver"}).status_code == 200


def test_upload_refuses_symlinked_directory(client: TestClient, agent: FakeAgent, tmp_path: Path):
    """SEC-9: a planted ``.zordon/uploads`` (or ``.zordon``) symlink is never written through."""
    login(client)
    victim = tmp_path / "victim"
    victim.mkdir()
    agent.upload_dir.parent.mkdir(parents=True, exist_ok=True)
    agent.upload_dir.symlink_to(victim, target_is_directory=True)
    r = client.post("/upload", content=b"evil", headers={"X-Zordon-Filename": "authorized_keys"})
    assert r.status_code == 403
    assert list(victim.iterdir()) == []
    # the parent being a symlink is refused too
    agent.upload_dir.unlink()
    agent.upload_dir = tmp_path / "link-parent" / "uploads"
    (tmp_path / "link-parent").symlink_to(victim, target_is_directory=True)
    r = client.post("/upload", content=b"evil", headers={"X-Zordon-Filename": "authorized_keys"})
    assert r.status_code == 403
    assert list(victim.iterdir()) == []


def test_write_atomic_replaces_a_symlinked_file_not_its_target(tmp_path: Path):
    folder = tmp_path / "uploads"
    folder.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("keep me")
    (folder / "notes.txt").symlink_to(target)
    S.write_atomic(folder / "notes.txt", b"new")
    assert target.read_text() == "keep me"
    assert not (folder / "notes.txt").is_symlink()
    assert (folder / "notes.txt").read_bytes() == b"new"
    assert (folder / "notes.txt").stat().st_mode & 0o777 == 0o600


def test_upload_without_file_field_400(client: TestClient):
    login(client)
    r = client.post("/upload", data={"note": "no file here"})
    assert r.status_code == 400
    boundary = "zordonboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhello\r\n--{boundary}--\r\n"
    ).encode()
    r = client.post(
        "/upload",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 400
    assert client.post("/upload", content=b"").status_code == 400


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("photo.jpg", "photo.jpg"),
        ("../../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\My Doc.docx", "My_Doc.docx"),
        (".hidden", "hidden"),
        ("", "upload"),
        ("///", "upload"),
        ("tab\tname\x00.txt", "tabname.txt"),
        ("ünïcödé.txt", "unicode.txt"),
        ("a" * 300 + ".txt", "a" * 96 + ".txt"),
    ],
)
def test_sanitize_filename(raw: str, expected: str):
    out = S.sanitize_filename(raw)
    assert out == expected
    assert "/" not in out and "\\" not in out and not out.startswith(".")


# ---- config -------------------------------------------------------------------------------


def test_refuses_non_loopback_without_token(agent: FakeAgent):
    agent.config.server.bind = "0.0.0.0"
    agent.config.server.token = ""
    with pytest.raises(ConfigError):
        S.create_app(agent)


def test_builds_for_non_loopback_with_token(agent: FakeAgent, static_dir: Path):
    agent.config.server.bind = "0.0.0.0"
    assert S.create_app(agent, static_dir=static_dir) is not None


def test_tunnel_mode_forces_idle_and_rate_limit(agent: FakeAgent, static_dir: Path):
    agent.config.server.idle_disconnect_minutes = 0
    agent.config.server.auth_rate_limit_per_minute = 0
    plain = S.create_app(agent, static_dir=static_dir)
    assert plain.state.idle_seconds is None and plain.state.limiter is None
    tunnel = S.create_app(agent, tunnel_mode=True, static_dir=static_dir)
    assert tunnel.state.idle_seconds == 30 * 60
    assert tunnel.state.limiter is not None and tunnel.state.limiter.limit == 5


def test_run_server_config():
    app = S.create_app(FakeAgent(Path("/nonexistent"), token=TOKEN), static_dir=Path("/nonexistent"))
    server = S.run_server(app, "127.0.0.1", 0)
    cfg = server.config
    assert cfg.host == "127.0.0.1"
    assert cfg.ws == "websockets-sansio"
    assert cfg.ws_max_size == 4 * 1024 * 1024
    assert cfg.ws_ping_interval == 20.0 and cfg.ws_ping_timeout == 20.0
    assert cfg.proxy_headers is True
    assert cfg.forwarded_allow_ips == "127.0.0.1"


def test_same_origin():
    from starlette.datastructures import Headers

    assert S.same_origin(Headers({"host": "a.b:8765"}))
    assert S.same_origin(Headers({"host": "a.b:8765", "origin": "http://A.B:8765"}))
    assert not S.same_origin(Headers({"host": "a.b:8765", "origin": "http://evil"}))
    assert not S.same_origin(Headers({"origin": "http://a.b"}))
