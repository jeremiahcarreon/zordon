"""Claude Code without a terminal: ``claude -p`` driven over stream-json (decision 0020).

The session is a long-lived ``claude -p --input-format stream-json --output-format
stream-json`` process. Each user turn is one JSON line on its stdin; every event
comes back as one JSON line on stdout. There is no screen to read: prose and tool
calls arrive as ``assistant`` / ``user`` records (the same shapes as the session
jsonl, so ``session.jsonl.parse_record`` turns them into pane lines), a ``result``
record ends the turn, and permission requests go to Zordon's MCP permission tool
(``zordon mcp-permission``, named by ``--permission-prompt-tool``), which the
manager answers through the same path as the ``PermissionRequest`` hook.

Verified against Claude Code 2.1.288: the ``system/init`` record carries
``session_id``; ``assistant`` records carry ``content`` blocks (``text``,
``tool_use``, ``thinking``) with ``stop_reason`` null while the turn continues;
tool outcomes come as ``user`` records with ``tool_result`` blocks; ``result``
carries ``subtype``, ``is_error``, ``session_id`` and ``permission_denials``.
``system/permission_denied``, ``rate_limit_event`` and ``system/commands_changed``
also appear and are ignored.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zordon.agents.base import AgentInfo, HookRequest, LaunchSpec, SessionInfo, TranscriptSource
from zordon.agents.claude_code import ClaudeCodeAdapter
from zordon.bus import PaneLine
from zordon.session import discovery
from zordon.session.jsonl import parse_record, to_pane_lines

log = logging.getLogger("zordon.agents.headless")

KEY = "claude-headless"
HEADLESS_INFO = AgentInfo(
    key=KEY,
    display_name="Claude Code (headless)",
    binary="claude",
    install_hint="npm install -g @anthropic-ai/claude-code, then run `claude` once to log in",
    docs_url="https://code.claude.com",
)
IGNORED_SYSTEM_SUBTYPES = frozenset({"commands_changed", "thinking_tokens", "permission_denied"})


class HeadlessSession:
    """One ``claude -p`` process: write turns to its stdin, read its events off a thread."""

    def __init__(self, argv: Sequence[str], cwd: str, *, env: dict[str, str] | None = None, log_path: Path | None = None, popen: Any = subprocess.Popen) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.session_id: str | None = None
        self.turn_open = False
        self.exit_code: int | None = None
        self.started_at = time.time()
        self._stderr = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115 - handed to the child
        self.proc = popen(
            self.argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read, name="headless-reader", daemon=True)
        self._reader.start()

    # ---- process -----------------------------------------------------------------------

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("headless: non-JSON line: %s", line[:120])
                    continue
                if isinstance(msg, dict):
                    self._events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.exit_code = self.proc.wait()
            self._events.put({"type": "_exited", "code": self.exit_code})

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, text: str) -> None:
        """One user turn."""
        if not self.alive or self.proc.stdin is None:
            raise RuntimeError("the headless Claude Code process is gone")
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        self.turn_open = True

    def interrupt(self) -> None:
        """Abort the current turn (Claude Code treats SIGINT like Escape)."""
        if self.alive:
            try:
                self.proc.send_signal(signal.SIGINT)
            except OSError:
                pass

    def close(self, timeout: float = 5.0) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.alive:
            try:
                self.proc.terminate()
                self.proc.wait(timeout)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        if self._stderr is not subprocess.DEVNULL:
            try:
                self._stderr.close()  # type: ignore[union-attr]
            except OSError:
                pass

    # ---- events ------------------------------------------------------------------------

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out


class HeadlessEvents:
    """What one poll learned from the process: lines to speak and control signals."""

    def __init__(self) -> None:
        self.lines: list[PaneLine] = []
        self.session_id: str | None = None
        self.turn_ended = False
        self.turn_error: str | None = None
        self.exited: int | None = None
        self.permission_mode: str | None = None


def translate(events: Sequence[dict[str, Any]], session_id: str, *, now: float | None = None) -> HeadlessEvents:
    """stream-json records -> pane lines (through the jsonl parser) and signals."""
    out = HeadlessEvents()
    ts = time.time() if now is None else now
    for msg in events:
        t = msg.get("type")
        if t == "_exited":
            out.exited = int(msg.get("code") or 0)
            continue
        if t == "system":
            if msg.get("subtype") == "init":
                sid = msg.get("session_id")
                if isinstance(sid, str) and sid:
                    out.session_id = sid
                mode = msg.get("permissionMode") or msg.get("permission_mode")
                if isinstance(mode, str) and mode:
                    out.permission_mode = mode
            continue
        if t == "result":
            out.turn_ended = True
            if msg.get("is_error"):
                out.turn_error = str(msg.get("result") or msg.get("subtype") or "error")
            out.lines.append(PaneLine(session_id=session_id, text="", ts=ts, source="jsonl", block="turn_end", meta={"stop_reason": str(msg.get("stop_reason") or msg.get("subtype") or "")}))
            continue
        if t in ("assistant", "user"):
            rec = dict(msg)
            rec.setdefault("sessionId", session_id)
            for ev in parse_record(rec, default_session=session_id, now=ts):
                if ev.kind == "turn_end":
                    continue  # the result record is the turn boundary here
                out.lines.extend(to_pane_lines(ev, session_id))
            continue
        # stream_event, rate_limit_event, other system subtypes: nothing to say
    return out


class HeadlessAdapter(ClaudeCodeAdapter):
    """Claude Code launched headless. Screen slots are inherited and unused (no pane):
    the manager drives ``HeadlessSession`` objects for sessions under this adapter."""

    info = HEADLESS_INFO

    def __init__(self, config: Any | None = None, **kw: Any) -> None:
        super().__init__(config, **kw)
        self.info = HEADLESS_INFO

    def uses_alternate_screen(self) -> bool:
        return False

    def transcript_source(self, session_id: str, cwd: str, info: SessionInfo | None) -> TranscriptSource | None:
        return None  # the process's own stdout is the transcript

    def new_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None, *, allow_bypass: bool = False, system_prompt: str | None = None) -> LaunchSpec:
        raise NotImplementedError("headless sessions are started with launch(), not in a pane")

    def resume_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None, *, allow_bypass: bool = False, system_prompt: str | None = None) -> LaunchSpec:
        raise NotImplementedError("headless sessions are started with launch(), not in a pane")

    # ---- launching -----------------------------------------------------------------------

    def command(
        self,
        session_id: str,
        *,
        resume: bool,
        permission_mode: str | None,
        settings_path: Path | None,
        mcp_config: Path,
        allow_bypass: bool = False,
        system_prompt: str | None = None,
    ) -> list[str]:
        """The ``claude -p`` argv. ``validate_command`` keeps the same refusals as the pane."""
        argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        argv += ["--permission-prompt-tool", PERMISSION_TOOL, "--mcp-config", str(mcp_config), "--strict-mcp-config"]
        argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if settings_path is not None:
            argv += ["--settings", str(settings_path)]
        if permission_mode:
            argv += ["--permission-mode", discovery.normalize_mode(permission_mode, allow_bypass=allow_bypass)]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        discovery.validate_command(argv, allow_bypass=allow_bypass)
        return argv

    def launch(
        self,
        session_id: str,
        cwd: str,
        permission_mode: str | None,
        hooks: HookRequest | None,
        *,
        resume: bool = False,
        allow_bypass: bool = False,
        system_prompt: str | None = None,
        zordon_argv: Sequence[str] = ("zordon",),
        log_path: Path | None = None,
        popen: Any = subprocess.Popen,
    ) -> tuple[HeadlessSession, list[Path]]:
        """Start the process. Returns it and the files to remove when it goes."""
        if hooks is None:
            raise RuntimeError("headless Claude Code needs Zordon's hook port for its permission tool")
        settings, paths_ = self._hook_files_headless(hooks)
        mcp_path = discovery.hook_settings_path(hooks.zordon_home, session_id).with_suffix(".mcp.json")
        secret_file = discovery.hook_curl_config_path(discovery.hook_settings_path(hooks.zordon_home, session_id))
        cfg = mcp_config(
            zordon_argv=list(zordon_argv),
            port=hooks.port,
            host=discovery.hook_host(hooks.host),
            secret_file=secret_file,
            session_id=session_id,
            cwd=cwd,
            permission_mode=permission_mode,
        )
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh)
        paths_.append(mcp_path)
        argv = self.command(
            session_id,
            resume=resume,
            permission_mode=permission_mode,
            settings_path=settings,
            mcp_config=mcp_path,
            allow_bypass=allow_bypass,
            system_prompt=system_prompt,
        )
        # Same environment as a pane would get: provider keys and nesting markers scrubbed.
        # CLAUDE_CONFIG_DIR is left as the user has it: pointing Claude Code at ~/.claude
        # explicitly makes it look for its config file there instead of ~/.claude.json.
        env = {k: v for k, v in os.environ.items() if k not in set(self.scrub_names())}
        sess = HeadlessSession(argv, cwd, env=env, log_path=log_path, popen=popen)
        return sess, paths_

    def _hook_files_headless(self, req: HookRequest) -> tuple[Path | None, list[Path]]:
        """The ``--settings`` file for a headless session: Stop/UserPromptSubmit signals and
        the edit-scope hook, but no PermissionRequest hook (the MCP tool is the channel).
        The curl config written beside it is also the secret file the MCP tool reads."""
        path = discovery.hook_settings_path(req.zordon_home, req.session_id)
        try:
            host = discovery.hook_host(req.host)
            curl_config = discovery.hook_curl_config_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            discovery._write_private(curl_config, discovery.hook_curl_config_text(req.secret))
            data = discovery.hook_settings_json(req.port, curl_config, host=host, scope=bool(getattr(req, "scope", False)), permission=False)
            discovery._write_private(path, json.dumps(data, indent=2) + "\n")
        except (OSError, ValueError) as e:
            log.warning("hook settings not written (%s); launching without hooks", e)
            return None, []
        return path, [path, discovery.hook_curl_config_path(path)]

    @staticmethod
    def scrub_names() -> list[str]:
        from zordon.session import tmux  # noqa: PLC0415

        return list(tmux.scrub_names())


from zordon.mcp_permission import (  # noqa: E402 - after the names it needs
    PERMISSION_TOOL,
    mcp_config,
)

__all__ = ["KEY", "HEADLESS_INFO", "HeadlessAdapter", "HeadlessEvents", "HeadlessSession", "translate"]
