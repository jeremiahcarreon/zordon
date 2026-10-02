"""``zordon`` command line.

    zordon serve [--bind ADDR|tailscale] [--port N] [--tunnel [cloudflared|ngrok]] [--config PATH] [-v]
    zordon doctor [--download] [--json] [--probe] [--tunnel]
    zordon token show|rotate
    zordon sessions
    zordon --version

Exit codes: 0 ok, 2 configuration error, 3 missing dependency.

``serve`` loads (or creates) the config, applies the overrides, refuses a
non-loopback bind without a token, builds the :class:`zordon.app.Agent`, serves
the FastAPI app from a uvicorn thread, optionally opens a tunnel and prints its
URL as text and as a QR code, then waits for SIGINT/SIGTERM and stops
everything in order: tunnel, server, agent. Keys are never printed; the session
token is printed on first run and by ``zordon token show`` because the browser
needs it.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zordon import __version__, doctor, paths
from zordon.config import LOOPBACK, Config, ConfigError, generate_token

log = logging.getLogger("zordon.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_MISSING = 3

TUNNEL_PROVIDERS = ("cloudflared", "ngrok")
TAILSCALE_TIMEOUT = 5.0


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# ---- parser --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zordon",
        description="Full-duplex voice interface for a Claude Code session running in tmux.",
    )
    parser.add_argument("--version", action="version", version=f"zordon {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", metavar="command")

    serve = sub.add_parser("serve", help="start the agent and the web client")
    serve.add_argument("--bind", default=None, help="address to listen on, or 'tailscale' (default: config, 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="port to listen on (default: config, 8765)")
    serve.add_argument(
        "--tunnel",
        nargs="?",
        const="config",
        default=None,
        choices=TUNNEL_PROVIDERS + ("config",),
        metavar="PROVIDER",
        help="open a public tunnel (cloudflared or ngrok; default from [tunnel] provider)",
    )
    serve.add_argument("--config", type=Path, default=None, help="config file (default: $ZORDON_HOME/config.toml)")
    serve.add_argument("--no-warm-up", action="store_true", help="do not load the TTS model before serving")
    serve.set_defaults(func=cmd_serve)

    doc = sub.add_parser("doctor", help="check dependencies, providers, models and binaries")
    doctor.add_arguments(doc)
    doc.set_defaults(func=cmd_doctor)

    token = sub.add_parser("token", help="show or rotate the session token")
    token_sub = token.add_subparsers(dest="token_command", metavar="show|rotate")
    show = token_sub.add_parser("show", help="print the token")
    show.add_argument("--config", type=Path, default=None)
    show.set_defaults(func=cmd_token_show)
    rotate = token_sub.add_parser("rotate", help="generate a new token and save it")
    rotate.add_argument("--config", type=Path, default=None)
    rotate.set_defaults(func=cmd_token_rotate)
    token.set_defaults(func=cmd_token_show)

    sessions = sub.add_parser("sessions", help="list the Claude Code sessions Zordon can see")
    sessions.set_defaults(func=cmd_sessions)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    setup_logging(args.verbose)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_OK
    try:
        return int(func(args) or EXIT_OK)
    except CliError as e:
        print(f"zordon: {e}", file=sys.stderr)
        return e.code
    except ConfigError as e:
        print(f"zordon: config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_OK


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING if verbose else logging.ERROR)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO if verbose else logging.WARNING)


# ---- commands ------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor.run(args)


def cmd_token_show(args: argparse.Namespace) -> int:
    cfg, created = load_or_create(getattr(args, "config", None))
    if created:
        print(f"Wrote {cfg.path}", file=sys.stderr)
    if not cfg.server.token:
        raise CliError("no token is set; run `zordon token rotate`", EXIT_CONFIG)
    print(cfg.server.token)
    return EXIT_OK


def cmd_token_rotate(args: argparse.Namespace) -> int:
    cfg, _ = load_or_create(getattr(args, "config", None))
    cfg.server.token = generate_token()
    cfg.save()
    print(cfg.server.token)
    print("Saved. Browsers that are logged in will need the new token.", file=sys.stderr)
    return EXIT_OK


def cmd_sessions(args: argparse.Namespace) -> int:
    from zordon.session import discovery  # noqa: PLC0415
    from zordon.session.tmux import Tmux  # noqa: PLC0415

    tmux: Tmux | None = Tmux() if shutil.which("tmux") else None
    infos = discovery.list_sessions(paths.claude_home(), tmux)
    print(format_sessions(infos))
    return EXIT_OK


def format_sessions(infos: Sequence[Any]) -> str:
    if not infos:
        return "No Claude Code sessions found under " + str(paths.claude_home() / "projects")
    rows = [("SESSION", "STATE", "LAST ACTIVE", "DIRECTORY", "TITLE")]
    for info in infos:
        sid = str(getattr(info, "session_id", ""))[:8]
        running = bool(getattr(info, "running", False))
        target = getattr(info, "tmux_target", None)
        state = "running" + (f" ({target})" if target else "") if running else "stopped"
        ts = getattr(info, "last_active_ts", None) or 0.0
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"
        directory = str(getattr(info, "directory", "") or getattr(info, "cwd", "") or "")
        title = str(getattr(info, "display_title", "") or getattr(info, "title", "") or "")
        rows.append((sid, state, when, directory, title[:60]))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    out = []
    for r in rows:
        out.append("  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4])
    return "\n".join(out)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg, created = load_or_create(args.config)
    if created:
        print(f"Wrote {cfg.path} (mode 0600).")
        print(f"Your session token is: {cfg.server.token}")
        print("Type it into the browser once; `zordon token show` prints it again.")
    apply_overrides(cfg, bind=args.bind, port=args.port)
    tunnel_provider = resolve_tunnel(cfg, args.tunnel)
    check_dependencies(need_tunnel=tunnel_provider)
    return serve(cfg, tunnel_provider=tunnel_provider, warm_up=not args.no_warm_up)


# ---- serve pieces --------------------------------------------------------------------------


def load_or_create(path: Path | None) -> tuple[Config, bool]:
    try:
        return Config.load_or_create(path)
    except FileNotFoundError as e:
        raise CliError(f"config not found: {e}", EXIT_CONFIG) from e
    except (OSError, ValueError, TypeError) as e:  # ConfigError is a ValueError; tomllib and type errors too
        raise CliError(f"could not load config: {e}", EXIT_CONFIG) from e


def apply_overrides(cfg: Config, *, bind: str | None, port: int | None) -> None:
    if port is not None:
        cfg.server.port = int(port)
    if bind:
        cfg.server.bind = resolve_tailscale_ip() if bind == "tailscale" else bind
    if cfg.server.bind not in LOOPBACK and not cfg.server.token:
        raise CliError(
            f"server.bind={cfg.server.bind!r} is not loopback and no server.token is set; "
            "run `zordon token rotate` first",
            EXIT_CONFIG,
        )
    cfg.validate()


def resolve_tailscale_ip(run: Any = None, which: Any = None) -> str:
    run = run or subprocess.run
    which = which or shutil.which
    binary = which("tailscale")
    if not binary:
        raise CliError("--bind tailscale needs the tailscale CLI on PATH", EXIT_MISSING)
    try:
        proc = run([binary, "ip", "-4"], capture_output=True, text=True, timeout=TAILSCALE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise CliError(f"tailscale ip -4 failed: {e}", EXIT_MISSING) from e
    ip = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not ip:
        detail = (proc.stderr or "").strip() or "no address returned"
        raise CliError(f"tailscale ip -4 failed: {detail}", EXIT_MISSING)
    return ip[0].strip()


def resolve_tunnel(cfg: Config, requested: str | None) -> str | None:
    if requested is None:
        return None
    provider = cfg.tunnel.provider if requested == "config" else requested
    if provider not in TUNNEL_PROVIDERS:
        raise CliError(f"unknown tunnel provider {provider!r}", EXIT_CONFIG)
    return provider


def check_dependencies(*, need_tunnel: str | None, which: Any = None) -> None:
    which = which or shutil.which
    if not which("tmux"):
        raise CliError("tmux is not installed; `zordon doctor` lists what is missing", EXIT_MISSING)
    if need_tunnel:
        from zordon import assets  # noqa: PLC0415

        if not assets.find_binary(need_tunnel):
            hint = "`zordon doctor --download --tunnel`" if need_tunnel == "cloudflared" else "install ngrok"
            raise CliError(f"{need_tunnel} is not installed; {hint}", EXIT_MISSING)


def serve(cfg: Config, *, tunnel_provider: str | None, warm_up: bool = True) -> int:
    """Run the whole thing until a signal arrives. Returns the exit code."""
    from zordon.app import Agent  # noqa: PLC0415
    from zordon.transport.server import create_app, run_server, serve_in_thread  # noqa: PLC0415

    agent = Agent(cfg)
    for w in agent.warnings:
        print(f"warning: {w}", file=sys.stderr)
    app = create_app(agent, tunnel_mode=bool(tunnel_provider))
    agent.start(warm_up=warm_up)
    stop_server = None
    tunnel = None
    code = EXIT_OK
    try:
        server = run_server(app, cfg.server.bind, cfg.server.port, log_level="warning")
        try:
            _thread, stop_server = serve_in_thread(server)
        except RuntimeError as e:
            raise CliError(f"could not listen on {cfg.server.bind}:{cfg.server.port}: {e}", EXIT_CONFIG) from e
        print(f"Zordon {__version__} listening on http://{display_host(cfg.server.bind)}:{cfg.server.port}")
        if tunnel_provider:
            tunnel = start_tunnel(agent, tunnel_provider, cfg.server.port)
        print("Press Ctrl-C to stop.")
        wait_for_signal()
    except CliError as e:
        print(f"zordon: {e}", file=sys.stderr)
        code = e.code
    finally:
        shutdown(tunnel, stop_server, agent)
    return code


def display_host(bind: str) -> str:
    return "127.0.0.1" if bind == "0.0.0.0" else bind


def start_tunnel(agent: Any, provider: str, port: int) -> Any:
    from zordon.transport.qr import terminal_qr  # noqa: PLC0415
    from zordon.transport.tunnel import Tunnel, TunnelError  # noqa: PLC0415

    tunnel = Tunnel(provider, port)
    print(f"Starting {provider} tunnel...")
    try:
        url = tunnel.start(timeout=45.0)
    except TunnelError as e:
        tunnel.stop()
        raise CliError(f"tunnel failed: {e}", EXIT_MISSING) from e
    print(f"Public URL: {url}")
    print(terminal_qr(url))
    print("Scan the code, then enter the token from `zordon token show`.")
    agent.set_tunnel_url(url)
    return tunnel


def wait_for_signal(signals: Sequence[int] = (signal.SIGINT, signal.SIGTERM)) -> None:
    """Block the main thread until one of ``signals`` arrives (or Ctrl-C)."""
    done = threading.Event()

    def _handler(signum: int, _frame: Any) -> None:
        log.info("received %s", signal.Signals(signum).name)
        done.set()

    previous = {}
    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # not the main thread
            pass
    try:
        while not done.is_set():
            done.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, prev in previous.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError):
                pass


def shutdown(tunnel: Any, stop_server: Any, agent: Any) -> None:
    print("\nStopping...", file=sys.stderr)
    if tunnel is not None:
        try:
            tunnel.stop()
        except Exception:  # noqa: BLE001
            log.exception("tunnel stop failed")
    if stop_server is not None:
        try:
            stop_server()
        except Exception:  # noqa: BLE001
            log.exception("server stop failed")
    try:
        agent.stop()
    except Exception:  # noqa: BLE001
        log.exception("agent stop failed")



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
