"""A stand-in for ``claude -p --input-format stream-json --output-format stream-json``.

Reads user turns from stdin (one JSON line each) and answers with a scripted
conversation in the record shapes Claude Code 2.1.288 emits. The script per turn is
chosen by the user text:

* contains "touch" -> a Bash tool_use, then (if the permission tool answered allow)
  a tool_result and a final text; the permission tool is called the way Claude Code
  would: by launching the MCP server named in --mcp-config and calling ``permission``;
* otherwise -> one text block "pong: <text>".

Usage: fake_claude_headless.py <args as claude would get them>. ``--session-id``
or ``--resume`` sets the session id reported in the init record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ask_permission(mcp_config: str, tool_name: str, tool_input: dict) -> dict:
    cfg = json.load(open(mcp_config))
    server = cfg["mcpServers"]["zordon"]
    env = dict(os.environ)
    env.update(server.get("env") or {})
    proc = subprocess.Popen([server["command"], *server.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "permission", "arguments": {"tool_name": tool_name, "input": tool_input, "tool_use_id": "toolu_fake"}}},
    ]
    assert proc.stdin is not None and proc.stdout is not None
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    result = None
    for line in proc.stdout:
        m = json.loads(line)
        if m.get("id") == 3:
            result = json.loads(m["result"]["content"][0]["text"])
            break
    proc.stdin.close()
    proc.wait(10)
    return result or {"behavior": "deny", "message": "no answer"}


def main(argv: list[str]) -> int:
    sid = "00000000-0000-4000-8000-000000000000"
    mcp_config = None
    for i, a in enumerate(argv):
        if a in ("--session-id", "--resume") and i + 1 < len(argv):
            sid = argv[i + 1]
        if a == "--mcp-config" and i + 1 < len(argv):
            mcp_config = argv[i + 1]
    emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sid, "tools": ["Bash"], "mcp_servers": [{"name": "zordon", "status": "connected"}], "permissionMode": next((argv[i + 1] for i, a in enumerate(argv) if a == "--permission-mode"), "default")})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        text = msg.get("message", {}).get("content", "")
        if isinstance(text, list):
            text = " ".join(b.get("text", "") for b in text)
        if "exit" in text:
            emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "bye"})
            return 0
        if "touch" in text and mcp_config:
            inp = {"command": "touch marker.txt", "description": "Create a marker file"}
            emit({"type": "assistant", "message": {"role": "assistant", "model": "fake", "content": [{"type": "tool_use", "id": "toolu_fake", "name": "Bash", "input": inp}], "stop_reason": None}, "session_id": sid})
            decision = ask_permission(mcp_config, "Bash", inp)
            if decision.get("behavior") == "allow":
                emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_fake", "content": "", "is_error": False}]}, "session_id": sid})
                emit({"type": "assistant", "message": {"role": "assistant", "model": "fake", "content": [{"type": "text", "text": "I created the marker file."}], "stop_reason": None}, "session_id": sid})
            else:
                emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_fake", "content": decision.get("message", "denied"), "is_error": True}]}, "session_id": sid})
                emit({"type": "assistant", "message": {"role": "assistant", "model": "fake", "content": [{"type": "text", "text": "I could not create the marker file."}], "stop_reason": None}, "session_id": sid})
            emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "num_turns": 2, "stop_reason": "end_turn"})
            continue
        emit({"type": "assistant", "message": {"role": "assistant", "model": "fake", "content": [{"type": "text", "text": f"pong: {text}"}], "stop_reason": None}, "session_id": sid})
        emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "num_turns": 1, "stop_reason": "end_turn"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
