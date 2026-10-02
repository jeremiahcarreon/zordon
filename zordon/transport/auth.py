"""Token auth, session cookies and the per-IP rate limiter for ``/auth``.

One token (``config.server.token``), presented once by the browser, exchanged
for a random session cookie that lives only in this process. There is no user
table. Everything here is synchronous and thread-safe; the FastAPI layer in
``server.py`` is the only caller.

Nothing in this module logs the token or a cookie value.
"""

from __future__ import annotations

import collections
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import HTTPConnection

log = logging.getLogger("zordon.transport.auth")

COOKIE_NAME = "zordon_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})
# Headers a loopback proxy (cloudflared, ngrok) uses to carry the real client IP.
PROXY_IP_HEADERS = ("cf-connecting-ip", "x-forwarded-for")


@dataclass(frozen=True, slots=True)
class CookieSpec:
    """Everything ``Response.set_cookie`` needs, so the server never guesses the flags."""

    key: str
    value: str
    max_age: int
    httponly: bool
    samesite: str
    secure: bool
    path: str = "/"


class TokenAuth:
    """Compare the presented token in constant time; issue and validate session cookies."""

    def __init__(self, token: str, cookie_name: str = COOKIE_NAME, secure: bool = False) -> None:
        self._token = token or ""
        self.cookie_name = cookie_name
        self.secure = secure
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """False when no token is configured; ``/auth`` then refuses everything."""
        return bool(self._token)

    def check_token(self, presented: str) -> bool:
        if not self._token or not isinstance(presented, str):
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self._token.encode("utf-8"))

    def issue_cookie(self) -> CookieSpec:
        value = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions.add(value)
        return CookieSpec(
            key=self.cookie_name,
            value=value,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=self.secure,
        )

    def validate_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        with self._lock:
            return value in self._sessions

    def revoke(self, value: str | None) -> None:
        if not value:
            return
        with self._lock:
            self._sessions.discard(value)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


class RateLimiter:
    """Sliding window of FAILED attempts per IP.

    Once an IP has ``limit`` failures inside ``window_s`` seconds, every attempt
    from it is refused until the oldest failure ages out, whatever the token says.
    """

    def __init__(
        self,
        limit: int = 5,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._clock = clock
        self._failures: dict[str, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        self._lock = threading.Lock()

    def is_limited(self, ip: str) -> tuple[bool, float]:
        """Return ``(limited, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            dq = self._failures.get(ip)
            if not dq:
                return False, 0.0
            self._expire(dq, now)
            if not dq:
                del self._failures[ip]
                return False, 0.0
            if len(dq) >= self.limit:
                return True, max(0.0, self.window_s - (now - dq[0]))
        return False, 0.0

    def record_failure(self, ip: str) -> int:
        """Record one failed attempt; return how many are in the window now."""
        now = self._clock()
        with self._lock:
            dq = self._failures[ip]
            self._expire(dq, now)
            dq.append(now)
            return len(dq)

    def reset(self, ip: str | None = None) -> None:
        with self._lock:
            if ip is None:
                self._failures.clear()
            else:
                self._failures.pop(ip, None)

    def _expire(self, dq: collections.deque[float], now: float) -> None:
        while dq and now - dq[0] > self.window_s:
            dq.popleft()


def retry_after_header(seconds: float) -> str:
    return str(int(seconds) + 1)


def client_ip(conn: HTTPConnection) -> str:
    """The address rate limiting and logging key on.

    Behind cloudflared or ngrok the TCP peer is always loopback and the real
    client arrives in ``CF-Connecting-IP`` / ``X-Forwarded-For``. Those headers are
    honoured only when the direct peer is loopback; from anywhere else they are
    attacker-controlled and ignored. For ``X-Forwarded-For`` the right-most entry
    is the one appended by the proxy that connected to us.
    """
    peer = conn.client.host if conn.client else "unknown"
    if peer not in LOOPBACK_PEERS:
        return peer
    cf = conn.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = conn.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer


def log_auth_attempt(ip: str, outcome: str) -> None:
    """Attempts are logged with the IP only; never the token, never the cookie."""
    log.warning("auth %s ip=%s", outcome, ip)
