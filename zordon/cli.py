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
import os
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


ROOT_REFUSAL = """zordon {command}: not as root.

Zordon drives a coding agent with the permissions of the account it runs under,
so as root every project could change anything on this machine. Claude Code
itself refuses to run in bypass-permissions mode as root. Create a normal user
that can use sudo (the setup wizard installs tmux, Node and the agent with it)
and continue there. As root:

  useradd -m -s /bin/bash <name>        # or: adduser <name>
  usermod -aG sudo <name>               # the sudo group; "wheel" on Fedora/Arch
  passwd <name>                         # sudo asks for this password later
  su - <name>                           # a fresh login so the group applies
  curl -fsSL https://raw.githubusercontent.com/jeremiahcarreon/zordon/main/install.sh | sh

Already have the user but sudo says "not in the sudoers file"? As root:
usermod -aG sudo <name>, then log in again (su - <name>). For a container
where no password prompt is wanted at all:
echo '<name> ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/<name>; chmod 0440 /etc/sudoers.d/<name>

(Set ZORDON_ALLOW_ROOT=1 to override; not recommended.)"""


def running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def refuse_root(command: str) -> None:
    """Raise CliError for the commands that start or configure Zordon when run as root.

    Everything that only inspects or stops (status, stop, logs, doctor, update,
    uninstall, token) keeps working as root; a container's operator needs those.
    """
    if running_as_root() and os.environ.get("ZORDON_ALLOW_ROOT") != "1":
        raise CliError(ROOT_REFUSAL.format(command=command), EXIT_ERROR)


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
    serve.add_argument("--no-setup", action="store_true", help="on a first run, write defaults instead of asking")
    serve.add_argument("--no-update", action="store_true", help="skip the update check for this run")
    serve.set_defaults(func=cmd_serve)

    setup_p = sub.add_parser("setup", help="guided setup: choose providers, download models, write the config")
    setup_p.add_argument("--config", type=Path, default=None, help="config file (default: $ZORDON_HOME/config.toml)")
    setup_p.add_argument("--yes", action="store_true", help="take the detected defaults without asking")
    setup_p.add_argument("--no-download", action="store_true", help="only write the config; skip downloads and pulls")
    setup_p.add_argument("--plain", action="store_true", help="question-and-answer mode instead of the full-screen TUI")
    setup_p.set_defaults(func=cmd_setup)

    st = sub.add_parser("start", help="run zordon serve detached from this terminal (log in ~/.zordon/serve.log)")
    st.add_argument("--bind", default=None)
    st.add_argument("--port", type=int, default=None)
    st.add_argument("--tunnel", nargs="?", const="config", default=None, metavar="PROVIDER")
    st.add_argument("--config", type=Path, default=None)
    st.set_defaults(func=cmd_start)
    sp = sub.add_parser("stop", help="stop the detached zordon")
    sp.set_defaults(func=cmd_stop)
    rs = sub.add_parser(
        "restart",
        help="stop and start the detached zordon (picks up an installed update); without flags it reuses the ones it was started with",
    )
    rs.add_argument("--bind", default=None)
    rs.add_argument("--port", type=int, default=None)
    rs.add_argument("--tunnel", nargs="?", const="config", default=None, metavar="PROVIDER")
    rs.add_argument("--config", type=Path, default=None)
    rs.set_defaults(func=cmd_restart)
    stt = sub.add_parser("status", help="is zordon running, where, and is it healthy")
    stt.add_argument("--qr", action="store_true", help="also print the tunnel URL as a QR code")
    stt.set_defaults(func=cmd_status)
    lg = sub.add_parser("logs", help="show the detached zordon's log")
    lg.add_argument("-n", type=int, default=60, help="lines (default 60)")
    lg.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    lg.set_defaults(func=cmd_logs)
    sv = sub.add_parser("service", help="start at login and restart on failure (systemd --user or launchd)")
    sv_sub = sv.add_subparsers(dest="service_command", metavar="action")
    svi = sv_sub.add_parser("install", help="write, enable and start the service")
    svi.add_argument("--tunnel", nargs="?", const="config", default=None, metavar="PROVIDER")
    svi.add_argument("--bind", default=None)
    svi.set_defaults(func=cmd_service_install)
    svu = sv_sub.add_parser("uninstall", help="stop, disable and remove the service")
    svu.set_defaults(func=cmd_service_uninstall)
    svs = sv_sub.add_parser("status", help="is the service installed and active")
    svs.set_defaults(func=cmd_service_status)
    sv.set_defaults(func=cmd_service_status)

    up = sub.add_parser("update", help="check for and install a newer zordon (restart picks it up)")
    up.add_argument("--check", action="store_true", help="only report whether an update exists")
    up.add_argument("--channel", default=None, help="branch or tag to track (default: config [update] channel)")
    up.set_defaults(func=cmd_update)

    un = sub.add_parser("uninstall", help="remove zordon, its data, and (on request) what it installed")
    un.add_argument("--yes", action="store_true", help="remove the isolated environment and data without asking; never touches outside items")
    un.add_argument("--plain", action="store_true", help="question-and-answer mode instead of the full-screen TUI")
    un.add_argument("--dry-run", action="store_true", help="show the plan only")
    un.set_defaults(func=cmd_uninstall)

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


def want_tui(args: argparse.Namespace) -> bool:
    """The full-screen UI needs a terminal on both ends and no --plain/--yes."""
    return not getattr(args, "plain", False) and not getattr(args, "yes", False) and sys.stdin.isatty() and sys.stdout.isatty()


def run_setup_tui_or_none(config_path: Path | None, *, do_actions: bool, serve: Any = None) -> int | None:
    """The TUI's exit code, or None when it is unavailable and the plain wizard should run."""
    try:
        from zordon.tui import TuiUnavailable  # noqa: PLC0415
        from zordon.tui.setup import run_setup_tui  # noqa: PLC0415
    except ImportError as e:
        log.debug("setup TUI unavailable: %s", e)
        return None
    try:
        return run_setup_tui(config_path, do_actions=do_actions, serve=serve)
    except TuiUnavailable as e:
        log.debug("setup TUI could not start: %s", e)
        return None


def cmd_setup(args: argparse.Namespace) -> int:
    refuse_root("setup")
    from zordon import setup as wizard  # noqa: PLC0415

    config_path = getattr(args, "config", None)
    do_actions = not getattr(args, "no_download", False)
    if want_tui(args):

        started = {"ok": False}

        def start_now(extra: list[str]) -> int:
            # "Start Zordon" on the summary card: detached, with the tunnel when the user
            # chose phone access, then the page address (and QR) in the terminal.
            code = start_after_setup(config_path, extra)
            started["ok"] = code == EXIT_OK
            return code

        code = run_setup_tui_or_none(config_path, do_actions=do_actions, serve=start_now)
        if code is not None:
            if not started["ok"]:
                print_setup_summary(config_path)
            return code
    try:
        _cfg, _choices, problems = wizard.run(config_path, assume_yes=bool(getattr(args, "yes", False)), do_actions=do_actions)
    except wizard.SetupAborted as e:
        print(f"Setup stopped: {e}. Install a coding agent, then run `zordon setup` again.")
        return EXIT_MISSING
    except (OSError, ValueError, TypeError) as e:
        raise CliError(f"setup failed: {e}", EXIT_CONFIG) from e
    return EXIT_OK if not problems else EXIT_MISSING


def _serve_extra(args: argparse.Namespace) -> list[str]:
    extra: list[str] = []
    if getattr(args, "bind", None):
        extra += ["--bind", str(args.bind)]
    if getattr(args, "port", None):
        extra += ["--port", str(args.port)]
    t = getattr(args, "tunnel", None)
    if t:
        extra += ["--tunnel"] if t == "config" else ["--tunnel", str(t)]
    if getattr(args, "config", None):
        extra += ["--config", str(args.config)]
    return extra


def cmd_start(args: argparse.Namespace) -> int:
    refuse_root("start")
    from zordon import daemon  # noqa: PLC0415

    cfg, _ = load_or_create(getattr(args, "config", None))
    try:
        st = daemon.start(_serve_extra(args))
    except RuntimeError as e:
        raise CliError(str(e), EXIT_ERROR) from e
    print(f"zordon is running in the background (pid {st.pid}).")
    print(f"  page: http://{display_host(getattr(args, 'bind', None) or cfg.server.bind)}:{getattr(args, 'port', None) or cfg.server.port}")
    print(f"  log:  {st.log}")
    if getattr(args, "tunnel", None):
        url = _wait_for_tunnel_url(timeout_s=60.0)
        if url:
            from zordon.transport.qr import terminal_qr  # noqa: PLC0415

            print(f"  tunnel: {url}")
            print(terminal_qr(url))
            print("  Scan the code, then enter the token from `zordon token show`.")
        else:
            print("  tunnel: not up yet; `zordon status --qr` shows it once cloudflared connects (see `zordon logs`).")
    print("  zordon status · zordon logs -f · zordon stop")
    return EXIT_OK


def _wait_for_tunnel_url(*, timeout_s: float) -> str | None:
    import time  # noqa: PLC0415

    from zordon import daemon  # noqa: PLC0415

    p = tunnel_url_path()
    p.unlink(missing_ok=True) if p.exists() and not daemon.status().running else None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if p.exists():
            url = p.read_text().strip()
            if url:
                return url
        if not daemon.status().running:
            return None
        time.sleep(0.5)
    return None


def cmd_stop(args: argparse.Namespace) -> int:
    from zordon import daemon  # noqa: PLC0415

    before = daemon.status()
    st = daemon.stop()
    if before.running:
        print(f"stopped zordon (pid {before.pid}).")
    elif before.stale:
        print("zordon was not running (removed a stale pid file).")
    else:
        print("zordon is not running.")
    return EXIT_OK if not st.running else EXIT_ERROR


def cmd_restart(args: argparse.Namespace) -> int:
    """Stop and start again. Without flags of its own, the restart reuses the flags
    the running instance was started with (``--tunnel``, ``--bind``, ``--port``), so
    a restart after an update does not silently drop the tunnel."""
    refuse_root("restart")
    from zordon import daemon  # noqa: PLC0415

    previous = daemon.last_args()
    daemon.stop()
    if not _serve_extra(args) and previous:
        print(f"  reusing: zordon start {' '.join(previous)}")
        return cmd_start_with(args, previous)
    return cmd_start(args)


def cmd_start_with(args: argparse.Namespace, extra: list[str]) -> int:
    """``cmd_start`` with an explicit serve flag list (restart's replay)."""
    ns = argparse.Namespace(**vars(args))
    it = iter(range(len(extra)))
    for i in it:
        flag = extra[i]
        nxt = extra[i + 1] if i + 1 < len(extra) else None
        if flag == "--tunnel":
            if nxt is not None and not nxt.startswith("--"):
                ns.tunnel = nxt
                next(it, None)
            else:
                ns.tunnel = "config"
        elif flag in ("--bind", "--port", "--config") and nxt is not None:
            setattr(ns, flag[2:], nxt)
            next(it, None)
    return cmd_start(ns)


def cmd_status(args: argparse.Namespace) -> int:
    from zordon import daemon, service  # noqa: PLC0415

    st = daemon.status()
    cfg, _ = load_or_create(None)
    if st.running:
        print(f"zordon is running (pid {st.pid}) at http://{display_host(cfg.server.bind)}:{cfg.server.port}")
        print(f"  log: {st.log}")
        health = _fetch_health(cfg)
        if health:
            print(f"  health: {health}")
        tp = tunnel_url_path()
        if tp.exists():
            url = tp.read_text().strip()
            print(f"  tunnel: {url}   (token: zordon token show)")
            if getattr(args, "qr", False) and url:
                from zordon.transport.qr import terminal_qr  # noqa: PLC0415

                print(terminal_qr(url))
    else:
        print("zordon is not running." + (" (stale pid file removed)" if st.stale else ""))
        if st.stale:
            st.pidfile.unlink(missing_ok=True)
    svc = service.info()
    if svc.kind != "none":
        state = "active" if svc.active else ("installed, not active" if svc.installed else "not installed")
        print(f"  service ({svc.kind}): {state}")
    return EXIT_OK if st.running else EXIT_ERROR


def _fetch_health(cfg: Config) -> str:
    """One-line health from the running agent's unauthenticated liveness endpoint plus the
    pid; the full report needs the browser (cookie) or `zordon doctor`."""
    import httpx  # noqa: PLC0415

    try:
        r = httpx.get(f"http://{display_host(cfg.server.bind)}:{cfg.server.port}/healthz", timeout=2.0)
        return f"responding (version {r.json().get('version', '?')})" if r.status_code == 200 else f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"not responding ({type(e).__name__})"


def cmd_logs(args: argparse.Namespace) -> int:
    from zordon import daemon  # noqa: PLC0415

    path = daemon.log_path()
    if not path.exists():
        print(f"no log yet at {path}")
        return EXIT_ERROR
    print(daemon.tail(int(getattr(args, "n", 60))))
    if getattr(args, "follow", False):
        import time  # noqa: PLC0415

        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            try:
                while True:
                    chunk = fh.read()
                    if chunk:
                        sys.stdout.write(chunk.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    return EXIT_OK


def cmd_service_install(args: argparse.Namespace) -> int:
    refuse_root("service install")
    from zordon import service  # noqa: PLC0415

    extra = _serve_extra(args)
    info = service.install(extra)
    if info.kind == "none":
        print(info.note)
        return EXIT_MISSING
    print(f"{info.kind} service {'active' if info.active else 'installed but not active'}: {info.path}")
    if info.note:
        print(f"  {info.note}")
    return EXIT_OK if info.active else EXIT_ERROR


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    from zordon import service  # noqa: PLC0415

    info = service.uninstall()
    print(f"{info.kind}: {info.note}" if info.kind != "none" else info.note)
    return EXIT_OK


def cmd_service_status(args: argparse.Namespace) -> int:
    from zordon import service  # noqa: PLC0415

    info = service.info()
    if info.kind == "none":
        print(info.note)
        return EXIT_MISSING
    state = "active" if info.active else ("installed, not active" if info.installed else "not installed")
    print(f"{info.kind} service: {state}" + (f" ({info.path})" if info.installed else ""))
    return EXIT_OK if info.active else EXIT_ERROR


def cmd_update(args: argparse.Namespace) -> int:
    from zordon import update as upd  # noqa: PLC0415

    cfg, _ = load_or_create(getattr(args, "config", None))
    channel = getattr(args, "channel", None) or cfg.update.channel
    st = upd.check(channel, force=True)
    if st.error and st.latest is None:
        print(f"could not check for updates: {st.error}", file=sys.stderr)
        return EXIT_MISSING
    if not st.available:
        print(f"zordon {st.current} is current on {channel}.")
        return EXIT_OK
    print(f"zordon {st.latest} is available (you have {st.current}).")
    if getattr(args, "check", False):
        return EXIT_OK
    ok, msg = upd.apply(channel)
    print(msg)
    return EXIT_OK if ok else EXIT_MISSING


def start_after_setup(config_path: Path | None, extra: list[str]) -> int:
    """Launch ``zordon start`` detached once setup is done and print where to go.

    With ``--tunnel`` among ``extra`` (the user chose phone access) the public URL and
    its QR code are shown together with the token; otherwise the local address.
    """
    from zordon import daemon  # noqa: PLC0415
    from zordon.setup import path_hint  # noqa: PLC0415

    cfg, _ = load_or_create(config_path)
    argv = list(extra) + (["--config", str(config_path)] if config_path else [])
    try:
        if daemon.status().running:
            daemon.stop()
        st = daemon.start(argv)
    except RuntimeError as e:
        print(f"\nZordon did not start: {e}\nTry: zordon start{' ' + ' '.join(extra) if extra else ''}", file=sys.stderr)
        return EXIT_ERROR
    tunnel = "--tunnel" in extra
    print("\nGreat, everything is up and running. Have fun talking with Zordon!\n")
    local = f"http://{display_host(cfg.server.bind)}:{cfg.server.port}"
    if tunnel:
        url = _wait_for_tunnel_url(timeout_s=60.0)
        if url:
            from zordon.transport.qr import terminal_qr  # noqa: PLC0415

            print(terminal_qr(url))
            print(f"  On your phone:  {url}")
        else:
            print("  The tunnel is still connecting; `zordon status --qr` shows the address once it is up.")
        print(f"  On this machine: {local}")
    else:
        print(f"  Open:  {local}")
    print(f"  Token: {cfg.server.token}   (zordon token show prints it again)")
    print(f"\n  Running in the background (pid {st.pid}). zordon status · zordon logs -f · zordon stop")
    hint = path_hint()
    if hint:
        print("\n  " + hint)
    print()
    return EXIT_OK


def print_setup_summary(config_path: Path | None) -> None:
    """After the full-screen setup closes, leave the token, the serve command and the PATH
    hint in the plain terminal, where they can be selected and copied."""
    from zordon import setup as wizard  # noqa: PLC0415

    target = config_path or paths.config_path()
    if not target.exists():
        return
    try:
        cfg = Config.load(target)
    except Exception:  # noqa: BLE001
        return
    choices = wizard.Choices()
    if cfg.server.bind not in ("127.0.0.1", "localhost", "::1"):
        choices.access = "lan"
    print(wizard.next_steps(choices, cfg), end="")


def start_update_check(agent: Any, cfg: Config, *, skip: bool, interval_s: float | None = None) -> None:
    """Background: check the channel now and again every ``interval_s`` (the update
    module's cache interval, six hours) for as long as serve runs. A newer version is
    installed when configured, and the terminal, the web clients (banner) and the
    listener (one spoken notice) are told that a restart will pick it up.

    A serve that is left running for days used to check once at start, so a build
    pushed an hour later was never seen. The loop ends with ``agent.bus.stop``.
    """
    from zordon import update as upd  # noqa: PLC0415

    if skip or upd.disabled(cfg.update.check):
        return
    interval = float(interval_s if interval_s is not None else upd.CHECK_INTERVAL_S)
    stop = getattr(getattr(agent, "bus", None), "stop", None)
    announced: set[str] = set()

    def _once() -> None:
        st = upd.check(cfg.update.channel)
        if not st.available:
            agent.update_status = {"current": st.current, "latest": st.latest, "available": False}
            return
        if st.latest in announced:
            return  # already installed or reported; a restart is what is missing now
        announced.add(str(st.latest))
        if cfg.update.auto:
            ok, msg = upd.apply(cfg.update.channel, log=lambda line: None)
            st.installed = ok
            if ok:
                print(f"\nUpdated to zordon {st.latest}. Restart zordon serve to use it.", file=sys.stderr)
                spoken = f"Zordon {st.latest} is installed. Restart Zordon when convenient to use it."
            else:
                print(f"\nzordon {st.latest} is available but the update failed: {msg}. Run `zordon update`.", file=sys.stderr)
                spoken = f"A Zordon update to {st.latest} is available but could not be installed. Run zordon update."
        else:
            print(f"\nzordon {st.latest} is available (you have {st.current}). Run `zordon update`.", file=sys.stderr)
            spoken = f"Zordon {st.latest} is available. Run zordon update to install it."
        agent.update_status = {"current": st.current, "latest": st.latest, "available": True, "installed": st.installed}
        try:
            from zordon.bus import Notice  # noqa: PLC0415
            from zordon.transport.protocol import UpdateOut  # noqa: PLC0415

            agent.bus.publish(UpdateOut(current=st.current, latest=st.latest or "", command=st.command, auto=st.installed, notes_url=f"{upd.REPO}/commits/{cfg.update.channel}"))
            agent.bus.publish(Notice(text=spoken, level="info", speak=True))
        except Exception:  # noqa: BLE001
            pass

    def _run() -> None:
        while True:
            try:
                _once()
            except Exception:  # noqa: BLE001 - an update check must never hurt serve
                logging.getLogger("zordon.update").debug("update check failed", exc_info=True)
            if stop is None:
                return
            if stop.wait(interval):
                return

    threading.Thread(target=_run, name="zordon-update-check", daemon=True).start()


def ensure_local_services(cfg: Config) -> None:
    """Start what the configured providers need and can be started here: the Ollama server."""
    if cfg.providers.normalizer not in ("ollama", "auto") or cfg.providers.key("anthropic"):
        return
    binary = shutil.which("ollama")
    if not binary:
        return
    from zordon import setup as wizard  # noqa: PLC0415

    wizard.ensure_ollama_server(cfg.providers.ollama_url, binary, sys.stderr, wait_s=10.0)


def cmd_uninstall(args: argparse.Namespace) -> int:
    from zordon import uninstall as un  # noqa: PLC0415

    plan = un.build_plan()
    if want_tui(args) and not getattr(args, "dry_run", False):
        try:
            from zordon.tui import TuiUnavailable  # noqa: PLC0415
            from zordon.tui.uninstall import run_uninstall_tui  # noqa: PLC0415

            return run_uninstall_tui(plan)
        except (ImportError, TuiUnavailable) as e:
            log.debug("uninstall TUI unavailable: %s", e)
    print("Zordon uninstall. Inside its environment (removed):")
    for it in plan.inside:
        print(f"  - {it.label}: {it.detail}")
    if plan.outside:
        print("Installed by Zordon on request, outside its environment (asked one by one):")
        for it in plan.outside:
            print(f"  - {it.label}: {it.detail}")
    for n in plan.notes:
        print(f"  note: {n}")
    if getattr(args, "dry_run", False):
        return EXIT_OK
    chosen = list(plan.inside)
    if getattr(args, "yes", False):
        pass
    else:
        if input("Remove zordon and its data? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Nothing removed.")
            return EXIT_OK
        for it in plan.outside:
            if input(f"Also remove {it.label}? [y/N]: ").strip().lower() in ("y", "yes"):
                chosen.append(it)
    problems = un.execute(chosen)
    for p in problems:
        print(f"  problem: {p}", file=sys.stderr)
    print("Done." if not problems else "Done, with problems above.")
    return EXIT_OK if not problems else EXIT_MISSING


def first_run_needs_setup(path: Path | None, no_setup: bool) -> bool:
    """A missing config on an interactive terminal means the wizard runs first."""
    target = path or paths.config_path()
    return not target.exists() and not no_setup and sys.stdin.isatty() and sys.stdout.isatty()


def cmd_serve(args: argparse.Namespace) -> int:
    refuse_root("serve")
    if first_run_needs_setup(args.config, getattr(args, "no_setup", False)):
        from zordon import setup as wizard  # noqa: PLC0415

        extra: list[str] = []  # the wizard's Reach answer as serve flags (--tunnel / --bind tailscale)

        def remember(argv: list[str]) -> int:
            extra.extend(argv)
            return 0

        code = run_setup_tui_or_none(args.config, do_actions=True, serve=remember) if want_tui(args) else None
        if code is not None:
            from zordon.tui.setup import EXIT_CANCELLED  # noqa: PLC0415

            if code == EXIT_CANCELLED:
                print("Setup skipped; writing defaults.")
            elif not extra:  # the user chose Exit on the summary card
                print("Setup finished. Start with `zordon serve` when ready.")
                return code
            cfg, _ = load_or_create(args.config)
            if "--tunnel" in extra and args.tunnel is None:
                args.tunnel = "config"
            if "--bind" in extra and args.bind is None:
                args.bind = "tailscale"
        else:
            print("No configuration yet; running the guided setup first (Ctrl-C to skip).")
            try:
                cfg, _choices, _problems = wizard.run(args.config)
            except (KeyboardInterrupt, wizard.SetupAborted):
                print("\nSetup skipped; writing defaults.")
                cfg, _ = load_or_create(args.config)
        created = False
    else:
        cfg, created = load_or_create(args.config)
    if created:
        print(f"Wrote {cfg.path} (mode 0600).")
        print(f"Your session token is: {cfg.server.token}")
        print("Type it into the browser once; `zordon token show` prints it again.")
    apply_overrides(cfg, bind=args.bind, port=args.port)
    tunnel_provider = resolve_tunnel(cfg, args.tunnel)
    check_dependencies(need_tunnel=tunnel_provider)
    return serve(cfg, tunnel_provider=tunnel_provider, warm_up=not args.no_warm_up, skip_update=bool(getattr(args, "no_update", False)))


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
    """tmux must be there; a missing cloudflared is downloaded on first use (ngrok is not)."""
    which = which or shutil.which
    if not which("tmux"):
        raise CliError("tmux is not installed; `zordon doctor` lists what is missing", EXIT_MISSING)
    if need_tunnel:
        from zordon import assets  # noqa: PLC0415

        if assets.find_binary(need_tunnel):
            return
        if need_tunnel != "cloudflared":
            raise CliError(f"{need_tunnel} is not installed; install ngrok", EXIT_MISSING)
        print("cloudflared is not installed yet; downloading it (first use)...", file=sys.stderr)
        try:
            found = doctor.download_cloudflared()
        except Exception as e:  # noqa: BLE001
            raise CliError(
                f"cloudflared is not installed and the download failed: {e}; "
                "retry with `zordon doctor --download --tunnel` or install cloudflared yourself",
                EXIT_MISSING,
            ) from e
        print(f"cloudflared installed at {found}", file=sys.stderr)


def serve(cfg: Config, *, tunnel_provider: str | None, warm_up: bool = True, skip_update: bool = False) -> int:
    """Run the whole thing until a signal arrives. Returns the exit code."""
    from zordon import daemon  # noqa: PLC0415
    from zordon.app import Agent  # noqa: PLC0415
    from zordon.transport.server import create_app, run_server, serve_in_thread  # noqa: PLC0415

    if os.environ.get("ZORDON_DETACHED"):
        daemon.write_pidfile_for_current_process()
    ensure_local_services(cfg)
    agent = Agent(cfg)
    for w in agent.warnings:
        print(f"warning: {w}", file=sys.stderr)
    app = create_app(agent, tunnel_mode=bool(tunnel_provider))
    agent.start(warm_up=warm_up)
    start_update_check(agent, cfg, skip=skip_update)
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
        if os.environ.get("ZORDON_DETACHED"):
            daemon.clear_pidfile_if_mine()
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
    _write_tunnel_url(url)
    return tunnel


def tunnel_url_path() -> Path:
    return paths.zordon_home() / "tunnel.url"


def _write_tunnel_url(url: str | None) -> None:
    """Keep the live tunnel URL where `zordon status` can show it (the detached server has no
    terminal). Removed on shutdown."""
    p = tunnel_url_path()
    try:
        if url:
            paths.ensure_private_dir(p.parent)
            p.write_text(url + "\n")
            os.chmod(p, 0o600)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass


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
        _write_tunnel_url(None)
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
