"""``zordon mcp-permission``: the permission tool a headless Claude Code calls.

Claude Code in ``--print`` mode has no dialog to draw. With
``--permission-prompt-tool mcp__zordon__permission`` it asks an MCP tool instead,
once per tool use that needs approval, and acts on the tool's answer: the SDK's
``PermissionResult``, ``{"behavior": "allow", "updatedInput": {...}}`` or
``{"behavior": "deny", "message": "..."}``, returned as the tool's text content.

This module is that tool. Claude Code launches it per ``--mcp-config`` as a stdio
MCP server (newline-delimited JSON-RPC 2.0). Each call is forwarded to the running
Zordon server as a ``PermissionRequest``-shaped payload on ``/hooks/permission``,
where the session manager speaks it and waits for the user's spoken or tapped
answer (decision 0019); the hook decision comes back and is translated. No answer
(``{}``: timeout, unknown session, Zordon unreachable) is a deny with a message,
never an allow.

Configuration arrives through the environment the mcp-config sets:
``ZORDON_HOOK_PORT``, ``ZORDON_HOOK_HOST`` (loopback), ``ZORDON_HOOK_SECRET_FILE``
(the session's 0600 curl config or the ``hook.secret`` file), ``ZORDON_SESSION_ID``
(the Claude session id the manager knows the session by) and ``ZORDON_CWD``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from zordon import __version__

log = logging.getLogger("zordon.mcp_permission")

PROTOCOL_VERSION = "2024-11-05"
TOOL_NAME = "permission"
NO_ANSWER = "No answer from the user through Zordon; not allowed."
WAIT_S = 900.0

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Ask the Zordon user, by voice, whether Claude Code may use a tool.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}

_SECRET_LINE = re.compile(r'header\s*=\s*"X-Zordon-Hook-Secret:\s*(?P<secret>[^"]+)"')


def read_secret(path: str | Path) -> str:
    """The secret from a curl config (``header = "X-Zordon-Hook-Secret: ..."``) or a bare file."""
    text = Path(path).read_text().strip()
    m = _SECRET_LINE.search(text)
    return m.group("secret").strip() if m else text


def hook_payload(tool_name: str, tool_input: dict[str, Any], *, session_id: str, cwd: str, permission_mode: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "PermissionRequest",
        "session_id": session_id,
        "cwd": cwd,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if permission_mode:
        payload["permission_mode"] = permission_mode
    return payload


def decision_to_result(answer: Any) -> dict[str, Any]:
    """Hook answer -> PermissionResult. Anything but an explicit decision is a deny."""
    decision = None
    if isinstance(answer, dict):
        decision = (answer.get("hookSpecificOutput") or {}).get("decision") if isinstance(answer.get("hookSpecificOutput"), dict) else None
    if not isinstance(decision, dict):
        return {"behavior": "deny", "message": NO_ANSWER}
    if decision.get("behavior") == "allow":
        out: dict[str, Any] = {"behavior": "allow"}
        if isinstance(decision.get("updatedInput"), dict):
            out["updatedInput"] = decision["updatedInput"]
        return out
    return {"behavior": "deny", "message": str(decision.get("message") or "The user said no.")}


def post_permission(payload: dict[str, Any], *, host: str, port: int, secret: str, timeout: float = WAIT_S) -> Any:
    """POST the payload to Zordon and return the parsed answer; ``{}`` on any failure."""
    import httpx  # noqa: PLC0415 - a dependency of the server already

    url = f"http://{host}:{port}/hooks/permission"
    try:
        resp = httpx.post(url, json=payload, headers={"X-Zordon-Hook-Secret": secret}, timeout=httpx.Timeout(timeout, connect=5.0))
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - never crash the tool; a deny goes back instead
        log.warning("permission request to %s failed: %s", url, e)
        return {}
    return data if isinstance(data, dict) else {}


class PermissionServer:
    """A minimal MCP stdio server with the one ``permission`` tool."""

    def __init__(
        self,
        ask: Callable[[dict[str, Any]], Any],
        *,
        session_id: str,
        cwd: str,
        permission_mode: str = "",
        inp: TextIO | None = None,
        out: TextIO | None = None,
    ) -> None:
        self.ask = ask
        self.session_id = session_id
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout

    # ---- transport -------------------------------------------------------------------

    def serve(self) -> None:
        for raw in self.inp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(msg, list):
                for one in msg:
                    self._dispatch(one)
            else:
                self._dispatch(msg)

    def _send(self, msg: dict[str, Any]) -> None:
        self.out.write(json.dumps(msg) + "\n")
        self.out.flush()

    def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            return  # a response to something we never asked
        if rid is None:
            self.handle_notification(method, msg.get("params") or {})
            return
        try:
            result = self.handle_request(method, msg.get("params") or {})
        except _RpcError as e:
            self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}})
            return
        except Exception as e:  # noqa: BLE001
            log.exception("mcp request %s failed", method)
            self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
            return
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    # ---- methods ----------------------------------------------------------------------

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        log.debug("mcp notification %s", method)

    def handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "zordon", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [TOOL_SCHEMA]}
        if method == "tools/call":
            return self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        raise _RpcError(-32601, f"method not found: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != TOOL_NAME:
            raise _RpcError(-32602, f"unknown tool {name!r}")
        tool_name = str(arguments.get("tool_name") or "")
        tool_input = arguments.get("input")
        if not tool_name or not isinstance(tool_input, dict):
            result = {"behavior": "deny", "message": "malformed permission request"}
        else:
            payload = hook_payload(tool_name, tool_input, session_id=self.session_id, cwd=self.cwd, permission_mode=self.permission_mode)
            result = decision_to_result(self.ask(payload))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def main(environ: dict[str, str] | None = None) -> int:
    """Entry point for ``zordon mcp-permission``."""
    env = dict(os.environ if environ is None else environ)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="zordon mcp-permission: %(message)s")
    try:
        port = int(env.get("ZORDON_HOOK_PORT") or "0")
        secret = read_secret(env["ZORDON_HOOK_SECRET_FILE"])
    except (KeyError, ValueError, OSError) as e:
        print(f"zordon mcp-permission: missing configuration ({e})", file=sys.stderr)
        return 2
    host = env.get("ZORDON_HOOK_HOST") or "127.0.0.1"
    session_id = env.get("ZORDON_SESSION_ID") or ""
    cwd = env.get("ZORDON_CWD") or os.getcwd()
    mode = env.get("ZORDON_PERMISSION_MODE") or ""

    def ask(payload: dict[str, Any]) -> Any:
        return post_permission(payload, host=host, port=port, secret=secret)

    PermissionServer(ask, session_id=session_id, cwd=cwd, permission_mode=mode).serve()
    return 0


def mcp_config(
    *,
    zordon_argv: list[str],
    port: int,
    host: str,
    secret_file: str | Path,
    session_id: str,
    cwd: str,
    permission_mode: str | None,
) -> dict[str, Any]:
    """The ``--mcp-config`` JSON that makes Claude Code launch this tool."""
    env = {
        "ZORDON_HOOK_PORT": str(int(port)),
        "ZORDON_HOOK_HOST": host,
        "ZORDON_HOOK_SECRET_FILE": str(secret_file),
        "ZORDON_SESSION_ID": session_id,
        "ZORDON_CWD": cwd,
    }
    if permission_mode:
        env["ZORDON_PERMISSION_MODE"] = permission_mode
    return {"mcpServers": {"zordon": {"command": zordon_argv[0], "args": [*zordon_argv[1:], "mcp-permission"], "env": env}}}


PERMISSION_TOOL = "mcp__zordon__permission"  # what --permission-prompt-tool names
