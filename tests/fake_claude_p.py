"""A stand-in for ``claude -p --input-format stream-json --output-format stream-json``.

Reads one user message from stdin, answers with an assistant record and a result
record, then exits (the real one would wait for more; the provider kills it after
one request anyway). Behaviour is steered by environment variables so tests can
provoke every failure path:

  FAKE_CLAUDE_MODE=ok        rewrite = "SPOKEN: <text after the last blank line>"
  FAKE_CLAUDE_MODE=slow      sleep FAKE_CLAUDE_DELAY seconds before answering
  FAKE_CLAUDE_MODE=error     emit a result with is_error=true
  FAKE_CLAUDE_MODE=crash     exit 1 without answering
  FAKE_CLAUDE_MODE=markdown  answer with markdown that must be cleaned
  FAKE_CLAUDE_ENV_FILE       dump the process environment there as JSON
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    if "--version" in sys.argv:
        print("2.1.287 (fake)")
        return 0
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    env_file = os.environ.get("FAKE_CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "w") as fh:
            json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()}, fh)
    if mode == "crash":
        return 1
    line = sys.stdin.readline()
    if not line:
        return 0
    rec = json.loads(line)
    content = rec["message"]["content"]
    if mode == "slow":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "2")))
    if mode == "error":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}))
        sys.stdout.flush()
        return 0
    if "Question:" in content:
        answer = "It changed auth dot py."
    else:
        body = content.rsplit("\n\n", 1)[-1]
        body = body.removeprefix("Text:\n")
        answer = "SPOKEN: " + body.replace("\n", " ")
    if mode == "markdown":
        answer = "**" + answer + "**\n\n- extra"
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer, "duration_api_ms": 5}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
