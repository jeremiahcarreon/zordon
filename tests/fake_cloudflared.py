"""A stand-in for the cloudflared binary, for tests only.

Prints the same stderr lines cloudflared's quick tunnel does (zerolog console
writer, RFC3339 time, ``INF`` level, the ASCII box from ``cliutil.LogTable``)
after a short delay, then sleeps until it is terminated.

Modes (``FAKE_CLOUDFLARED_MODE``):

* ``ok`` (default): print the box with the URL, then sleep
* ``exit``: print the disclaimer and the request line, then exit 1 before any URL
* ``silent``: print nothing and sleep (lets a caller test the start timeout)

The URL is ``FAKE_CLOUDFLARED_URL`` (default a fixed trycloudflare.com host).
Arguments are ignored except that the script refuses to run unless the first
argument is ``tunnel`` so a wrong command line is noticed.
"""

from __future__ import annotations

import os
import signal
import sys
import time

DISCLAIMER = (
    "Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick "
    "way to experiment and try it out. However, be aware that these account-less Tunnels have no "
    "uptime guarantee, are subject to the Cloudflare Online Services Terms of Use "
    "(https://www.cloudflare.com/website-terms/), and Cloudflare reserves the right to "
    "investigate your use of Tunnels for violations of such terms. If you intend to use Tunnels "
    "in production you should use a pre-created named tunnel by following: "
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps"
)
REQUESTING = "Requesting new quick Tunnel on trycloudflare.com..."
CREATED = "Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):"


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _inf(msg: str) -> None:
    sys.stderr.write(f"{_stamp()} INF {msg}\n")
    sys.stderr.flush()


def _box(lines: list[str]) -> list[str]:
    width = max(len(line) for line in lines)
    border = "+" + "-" * (width + 4) + "+"
    body = ["|  " + line.ljust(width) + "  |" for line in lines]
    return [border, *body, border]


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "tunnel":
        sys.stderr.write("fake cloudflared: expected `tunnel` as the first argument\n")
        return 2
    mode = os.environ.get("FAKE_CLOUDFLARED_MODE", "ok")
    url = os.environ.get("FAKE_CLOUDFLARED_URL", "https://quiet-ocean-example-1234.trycloudflare.com")
    delay = float(os.environ.get("FAKE_CLOUDFLARED_DELAY", "0.1"))

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if mode == "silent":
        while True:
            time.sleep(0.2)

    _inf(DISCLAIMER)
    _inf(REQUESTING)
    time.sleep(delay)
    if mode == "exit":
        sys.stderr.write(f"{_stamp()} ERR failed to request quick Tunnel: fake failure\n")
        sys.stderr.flush()
        return 1
    for line in _box([CREATED, url]):
        _inf(line)
    _inf("Version 2026.9.1 (Checksum fake)")
    _inf("GOOS: linux, GOVersion: go1.24, GoArch: amd64")
    while True:
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
