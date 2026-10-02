"""FastAPI app factory and the uvicorn runner.

Routes:

* ``GET /`` and the static client from ``zordon/web`` (importlib.resources)
* ``POST /auth`` token -> session cookie (rate limited per IP)
* ``POST /logout``
* ``GET /healthz`` (no auth)
* ``POST /hooks/claude`` Claude Code Notification hook, guarded by a per-process secret
* ``POST /upload`` (cookie) file for the focused session's ``.zordon/uploads``
* ``WS /ws`` (cookie) the client protocol, see ``docs/protocol.md``

Under ``--tunnel`` the cookie is Secure, the idle disconnect and the rate limiter
are forced on. The app refuses to build when ``server.bind`` is not loopback and
no token is configured.
"""

from __future__ import annotations

import contextlib
import email.parser
import email.policy
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import AsyncIterator, Callable
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette import status
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from zordon import __version__
from zordon.config import LOOPBACK, Config, ConfigError
from zordon.transport.auth import (
    RateLimiter,
    TokenAuth,
    client_ip,
    log_auth_attempt,
    retry_after_header,
)
from zordon.transport.ws import UPLOAD_MAX_BYTES, Broadcaster, ClientConnection

log = logging.getLogger("zordon.transport.server")

UPLOAD_FILENAME_HEADER = "x-zordon-filename"
HOOK_SECRET_HEADER = "x-zordon-hook-secret"
WS_MAX_SIZE = 4 * 1024 * 1024
DEFAULT_IDLE_MINUTES = 30
DEFAULT_RATE_LIMIT = 5
# Runs of anything outside the safe set (whitespace and control chars included) collapse to one "_".
_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9.-]+")


# ---- app factory -------------------------------------------------------------------------


def create_app(
    agent: Any,
    *,
    tunnel_mode: bool = False,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app around ``agent`` (``AgentAPI`` in ``docs/architecture.md``)."""
    cfg: Config = agent.config
    check_bind(cfg)

    auth = TokenAuth(cfg.server.token, secure=tunnel_mode)
    if not auth.enabled:
        log.warning("no server token is configured; /auth will refuse every attempt")
    limiter = _build_limiter(cfg, tunnel_mode)
    idle_seconds = _idle_seconds(cfg, tunnel_mode)
    broadcaster = Broadcaster(agent.bus)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broadcaster.start()
        with contextlib.ExitStack() as stack:
            root = static_dir if static_dir is not None else stack.enter_context(_packaged_web())
            if root is not None and root.is_dir():
                if app.state.static_root is None:  # a second lifespan (tests) must not mount twice
                    app.mount("/", StaticFiles(directory=str(root), html=True), name="static")
                app.state.static_root = root
            else:
                app.state.static_root = None
                log.warning("web client directory not found; only the API is served")
            try:
                yield
            finally:
                broadcaster.stop()

    app = FastAPI(
        title="Zordon",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.agent = agent
    app.state.auth = auth
    app.state.limiter = limiter
    app.state.broadcaster = broadcaster
    app.state.tunnel_mode = tunnel_mode
    app.state.idle_seconds = idle_seconds
    app.state.static_root = None
    app.add_middleware(SecurityHeadersMiddleware)

    # ---- dependencies ---------------------------------------------------------------

    def require_cookie_http(request: Request) -> str:
        value = request.cookies.get(auth.cookie_name)
        if not auth.validate_cookie(value):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
        return value or ""

    def require_cookie_ws(websocket: WebSocket) -> str:
        """Raised before ``accept`` this becomes HTTP 403 on the upgrade request."""
        value = websocket.cookies.get(auth.cookie_name)
        if not auth.validate_cookie(value):
            raise WebSocketException(status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        if not same_origin(websocket.headers):
            log.warning("websocket origin mismatch ip=%s", client_ip(websocket))
            raise WebSocketException(status.WS_1008_POLICY_VIOLATION, reason="bad origin")
        return value or ""

    # ---- routes -----------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        root: Path | None = app.state.static_root
        if root is not None and (root / "index.html").is_file():
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})
        return JSONResponse({"error": "web client not installed"}, status_code=404)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": str(agent.version)}

    @app.post("/auth")
    async def auth_route(body: AuthBody, request: Request) -> Any:
        ip = client_ip(request)
        if limiter is not None:
            limited, retry_after = limiter.is_limited(ip)
            if limited:
                log_auth_attempt(ip, "rate-limited")
                return JSONResponse(
                    {"error": "too_many_attempts"},
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={"Retry-After": retry_after_header(retry_after)},
                )
        if not auth.check_token(body.token):
            if limiter is not None:
                limiter.record_failure(ip)
            log_auth_attempt(ip, "failed")
            return JSONResponse({"error": "bad_token"}, status_code=status.HTTP_401_UNAUTHORIZED)
        spec = auth.issue_cookie()
        log_auth_attempt(ip, "ok")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key=spec.key,
            value=spec.value,
            max_age=spec.max_age,
            path=spec.path,
            httponly=spec.httponly,
            samesite=spec.samesite,
            secure=spec.secure,
        )
        return resp

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        auth.revoke(request.cookies.get(auth.cookie_name))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.cookie_name, path="/")
        return resp

    @app.post("/hooks/claude")
    async def hooks_claude(request: Request) -> Any:
        presented = request.headers.get(HOOK_SECRET_HEADER, "")
        expected = str(getattr(agent, "hook_secret", "") or "")
        if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="body must be a JSON object")
        try:
            agent.sessions.hook_event(payload)
        except Exception:  # noqa: BLE001
            log.exception("hook_event failed")
        return {}

    @app.post("/upload")
    async def upload(request: Request, _sid: str = Depends(require_cookie_http)) -> Any:
        name, data = await read_upload(request, UPLOAD_MAX_BYTES)
        safe = sanitize_filename(name)
        dest = Path(agent.upload_path(safe))
        write_atomic(dest, data)
        log.info("stored upload %s (%d bytes)", dest.name, len(data))
        return {"path": str(dest), "name": dest.name, "size": len(data)}

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket, _sid: str = Depends(require_cookie_ws)) -> None:
        conn = ClientConnection(websocket, agent, broadcaster, idle_seconds=idle_seconds)
        await conn.run()

    return app


class AuthBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)


# ---- config checks --------------------------------------------------------------------


def check_bind(cfg: Config) -> None:
    """Re-check the one rule the server must never get wrong, even if Config changed."""
    if cfg.server.bind not in LOOPBACK and not cfg.server.token:
        raise ConfigError(
            f"server.bind={cfg.server.bind!r} is not loopback and no server.token is set"
        )
    cfg.validate()


def _build_limiter(cfg: Config, tunnel_mode: bool) -> RateLimiter | None:
    per_minute = int(cfg.server.auth_rate_limit_per_minute)
    if tunnel_mode and per_minute <= 0:
        per_minute = DEFAULT_RATE_LIMIT
    if per_minute <= 0:
        return None
    return RateLimiter(limit=per_minute, window_s=60.0)


def _idle_seconds(cfg: Config, tunnel_mode: bool) -> float | None:
    minutes = int(cfg.server.idle_disconnect_minutes)
    if tunnel_mode and minutes <= 0:
        minutes = DEFAULT_IDLE_MINUTES
    if minutes <= 0:
        return None
    return float(minutes * 60)


@contextlib.contextmanager
def _packaged_web():
    """Real filesystem path for ``zordon/web`` for the lifetime of the context."""
    try:
        resource = files("zordon") / "web"
    except (ModuleNotFoundError, TypeError):
        yield None
        return
    try:
        with as_file(resource) as path:
            yield Path(path)
    except (FileNotFoundError, OSError):
        yield None


# ---- security headers ---------------------------------------------------------------------


def build_csp(host: str) -> str:
    """Only our own origin, our inline script/style, our WebSocket, and data/blob audio."""
    connect = "'self'"
    if host:
        connect += f" ws://{host} wss://{host}"
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        f"connect-src {connect}; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "worker-src 'self' blob:; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: adds the security headers to every HTTP response."""

    NO_STORE_PATHS = ("/", "/index.html")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "")
        path = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = build_csp(host)
                headers["Permissions-Policy"] = "microphone=(self), camera=(self), geolocation=()"
                if path in self.NO_STORE_PATHS:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def same_origin(headers: Headers) -> bool:
    """Browsers always send ``Origin`` on a WebSocket upgrade; it must match ``Host``.

    Non-browser clients send no Origin and are allowed through (the cookie is
    still required).
    """
    origin = headers.get("origin")
    if not origin:
        return True
    host = headers.get("host", "")
    if not host:
        return False
    netloc = origin.split("://", 1)[-1].split("/", 1)[0]
    return netloc.lower() == host.lower()


# ---- uploads -------------------------------------------------------------------------------


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Basename only, ASCII letters/digits/._- only, no leading dots, never empty."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = _UNSAFE_RUN.sub("_", name)
    name = re.sub(r"\.{2,}", ".", name).strip("._-")
    if not name:
        name = "upload"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            name = stem[: max_len - len(ext) - 1].rstrip("._-") + "." + ext
        else:
            name = name[:max_len]
    return name


async def read_upload(request: Request, cap: int) -> tuple[str, bytes]:
    """Return ``(filename, bytes)``; multipart ``file`` field or a raw body.

    Raises 413 past ``cap`` (checked on Content-Length and again while streaming)
    and 400 when there is no file.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="file too large")
    body = await _read_capped(request, cap)
    content_type = request.headers.get("content-type", "")
    if content_type.lower().startswith("multipart/form-data"):
        name, data = parse_multipart_file(content_type, body)
        if data is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no file field")
        return name or "upload", data
    if content_type.lower().startswith("application/x-www-form-urlencoded"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no file field")
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="empty upload")
    name = request.headers.get(UPLOAD_FILENAME_HEADER) or request.query_params.get("name") or ""
    return name or "upload.bin", body


async def _read_capped(request: Request, cap: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="file too large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart_file(content_type: str, body: bytes) -> tuple[str, bytes | None]:
    """Stdlib multipart parsing (no python-multipart): the first part with a filename,
    preferring the field named ``file``."""
    head = b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    try:
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    except Exception:  # noqa: BLE001
        return "", None
    if not msg.is_multipart():
        return "", None
    candidates: list[tuple[str, str, bytes]] = []
    for part in msg.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        field = part.get_param("name", header="content-disposition") or ""
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            candidates.append((str(field), filename, payload))
    if not candidates:
        return "", None
    for field, filename, payload in candidates:
        if field == "file":
            return filename, payload
    _field, filename, payload = candidates[0]
    return filename, payload


def write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---- running -------------------------------------------------------------------------------


def run_server(
    app: FastAPI,
    bind: str,
    port: int,
    *,
    ws_max_size: int = WS_MAX_SIZE,
    ping_interval: float = 20.0,
    ping_timeout: float = 20.0,
    log_level: str = "info",
    access_log: bool = False,
) -> uvicorn.Server:
    """A configured, not yet started, uvicorn server. Call ``.run()`` or :func:`serve_in_thread`."""
    config = uvicorn.Config(
        app,
        host=bind,
        port=port,
        log_level=log_level,
        access_log=access_log,
        ws="websockets-sansio",
        ws_max_size=ws_max_size,
        ws_ping_interval=ping_interval,
        ws_ping_timeout=ping_timeout,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        lifespan="on",
    )
    return uvicorn.Server(config)


def serve_in_thread(
    server: uvicorn.Server,
    *,
    wait: bool = True,
    timeout: float = 10.0,
) -> tuple[threading.Thread, Callable[[], None]]:
    """Run ``server`` on a daemon thread; return ``(thread, stop)``.

    With ``wait`` the call returns once the server is listening, or raises
    ``RuntimeError`` if it died first (port in use, bad bind).
    """
    thread = threading.Thread(target=server.run, name="zordon-transport", daemon=True)
    thread.start()
    if wait:
        deadline = time.monotonic() + timeout
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("server exited before it started listening")
            if time.monotonic() > deadline:
                server.should_exit = True
                raise RuntimeError("server did not start in time")
            time.sleep(0.02)

    def stop(join_timeout: float = 10.0) -> None:
        server.should_exit = True
        thread.join(join_timeout)

    return thread, stop


__all__ = [
    "UPLOAD_MAX_BYTES",
    "SecurityHeadersMiddleware",
    "build_csp",
    "check_bind",
    "create_app",
    "parse_multipart_file",
    "read_upload",
    "run_server",
    "same_origin",
    "sanitize_filename",
    "serve_in_thread",
]
