"""Public tunnel as a child process: cloudflared quick tunnel (default) or ngrok.

cloudflared facts, verified against 2026.9.x ``--help`` and source
(``cmd/cloudflared/tunnel/quick_tunnel.go``, ``cmd/cloudflared/cliutil/logger.go``):

* command: ``cloudflared tunnel --no-autoupdate --url http://127.0.0.1:PORT``
* logs go to **stderr** through zerolog's console writer (RFC3339 time, ``INF``)
* the URL is printed inside an ASCII box::

    INF +----------------------------------------------------------------------------+
    INF |  Your quick Tunnel has been created! Visit it at (it may take some time ... |
    INF |  https://quiet-ocean-example-1234.trycloudflare.com                        |
    INF +----------------------------------------------------------------------------+

ngrok: ``ngrok http PORT --log stdout --log-format json``; the public URL is read
from the local agent API at ``http://127.0.0.1:4040/api/tunnels``.

The tunnel only ever points at ``127.0.0.1``; the server's bind never changes.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from typing import IO

import httpx

from zordon import assets

log = logging.getLogger("zordon.transport.tunnel")

PROVIDERS = ("cloudflared", "ngrok")
TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
NGROK_POLL_S = 0.5
STOP_GRACE_S = 3.0
LOG_TAIL = 10


class TunnelError(RuntimeError):
    pass


class Tunnel:
    """Spawn the tunnel binary, resolve its public URL, stop it on request.

    ``binary`` may be a path (one executable) or a command prefix (a sequence,
    used by tests to run a Python fake). When omitted the binary is looked up
    with :func:`zordon.assets.find_binary`.
    """

    def __init__(
        self,
        provider: str = "cloudflared",
        port: int = 8765,
        binary: str | Sequence[str] | None = None,
        *,
        http_client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise TunnelError(f"unknown tunnel provider {provider!r}; use one of {PROVIDERS}")
        self.provider = provider
        self.port = int(port)
        self._binary = binary
        self._http_client_factory = http_client_factory or (lambda: httpx.Client(timeout=2.0))
        self._proc: subprocess.Popen[str] | None = None
        self._url: str | None = None
        self._url_ready = threading.Event()
        self._exited = threading.Event()
        self._lines: collections.deque[str] = collections.deque(maxlen=200)
        self._pump_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- public ------------------------------------------------------------------

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def recent_lines(self, n: int = LOG_TAIL) -> list[str]:
        with self._lock:
            return list(self._lines)[-n:]

    def start(self, timeout: float = 30.0) -> str:
        if self._proc is not None:
            raise TunnelError("tunnel already started")
        cmd = self._command()
        log.info("starting %s tunnel to 127.0.0.1:%d", self.provider, self.port)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.provider == "ngrok" else subprocess.DEVNULL,
                stderr=subprocess.PIPE if self.provider == "cloudflared" else subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env={**os.environ, "NO_AUTOUPDATE": "true"},
            )
        except OSError as e:
            raise TunnelError(f"could not start {self.provider}: {e}") from e
        stream = self._proc.stderr if self.provider == "cloudflared" else self._proc.stdout
        self._pump_thread = threading.Thread(
            target=self._pump, args=(stream,), name=f"{self.provider}-log", daemon=True
        )
        self._pump_thread.start()
        try:
            if self.provider == "cloudflared":
                url = self._wait_cloudflared(timeout)
            else:
                url = self._wait_ngrok(timeout)
        except TunnelError:
            self.stop()
            raise
        self._url = url
        log.info("%s tunnel up at %s", self.provider, url)
        return url

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    log.warning("%s pid %d did not die after SIGKILL", self.provider, proc.pid)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
        self._exited.set()
        log.info("%s tunnel stopped", self.provider)

    def __enter__(self) -> Tunnel:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- internals ---------------------------------------------------------------

    def _command(self) -> list[str]:
        prefix: list[str]
        if self._binary is None:
            found = assets.find_binary(self.provider)
            if not found:
                raise TunnelError(
                    f"{self.provider} binary not found; run `zordon doctor` to download it"
                )
            prefix = [found]
        elif isinstance(self._binary, str):
            prefix = [self._binary]
        else:
            prefix = list(self._binary)
        if self.provider == "cloudflared":
            return [*prefix, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]
        return [*prefix, "http", str(self.port), "--log", "stdout", "--log-format", "json"]

    def _pump(self, stream: IO[str] | None) -> None:
        if stream is None:
            self._exited.set()
            return
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                with self._lock:
                    self._lines.append(line)
                if self._url is None and self.provider == "cloudflared":
                    m = TRYCLOUDFLARE_RE.search(line)
                    if m:
                        self._url = m.group(0)
                        self._url_ready.set()
        except (OSError, ValueError):
            pass
        finally:
            # Stream closed: the process is gone (or is about to be). Unblock any waiter.
            self._exited.set()
            self._url_ready.set()

    def _wait_cloudflared(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TunnelError(
                    f"cloudflared did not print a trycloudflare.com URL within {timeout:.0f}s"
                    + self._tail_text()
                )
            self._url_ready.wait(min(remaining, 0.5))
            if self._url:
                return self._url
            if self._exited.is_set() or not self.is_running:
                code = self._proc.poll() if self._proc else None
                raise TunnelError(
                    f"cloudflared exited (code {code}) before printing a URL" + self._tail_text()
                )

    def _wait_ngrok(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                code = self._proc.poll() if self._proc else None
                raise TunnelError(
                    f"ngrok exited (code {code}) before reporting a URL" + self._tail_text()
                )
            url = self._poll_ngrok_api()
            if url:
                return url
            time.sleep(NGROK_POLL_S)
        raise TunnelError(
            f"ngrok did not report a public_url via {NGROK_API} within {timeout:.0f}s"
            + self._tail_text()
        )

    def _poll_ngrok_api(self) -> str | None:
        try:
            with self._http_client_factory() as client:
                resp = client.get(NGROK_API)
                if resp.status_code != 200:
                    return None
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        tunnels = data.get("tunnels") if isinstance(data, dict) else None
        if not isinstance(tunnels, list):
            return None
        for t in tunnels:
            url = t.get("public_url", "") if isinstance(t, dict) else ""
            if isinstance(url, str) and url.startswith("https://"):
                return url
        return None

    def _tail_text(self) -> str:
        tail = self.recent_lines(LOG_TAIL)
        if not tail:
            return ""
        return "; last output:\n" + "\n".join(tail)
