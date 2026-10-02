"""The Codex CLI adapter (OpenAI's terminal coding agent, ``@openai/codex``).

Built against ``codex-cli 0.160.0`` from three sources, in decreasing order of
trust; every constant below says which one it came from:

* **live**: the binary installed in ``.scratch/codex`` and its TUI driven in a
  private tmux server (``eval/fixtures/codex/PROVENANCE.md``). The command line
  surface, the welcome/login screen, the folder-trust dialog, the idle composer,
  the working status row, a failed turn, the exit screen and the session store
  layout were all observed live. No OpenAI login exists on this machine, so no
  agent turn ran: nothing below that depends on the model answering was seen.
* **source**: the Rust TUI and protocol crates of the matching upstream commit
  (``codex-rs/tui/src/bottom_pane/approval_overlay.rs`` and its insta snapshots,
  ``codex-rs/protocol/src/protocol.rs``, ``codex-rs/rollout/src/policy.rs``).
  The approval modal titles, option labels and shortcut keys come from here and
  from the snapshot renderings; they have not been seen on a live screen.
* **unverified**: a guess, marked as such in the comment next to it.

Layout of the TUI (live): the composer is ``› Ask Codex to do anything`` (also
``›`` before typed text, and the same glyph prefixes the echo of a sent message
in the history); while a turn runs a status row ``• Working (12s • esc to
interrupt)`` sits above the composer and a braille spinner ends the model row;
the approval overlay replaces the composer with a title, an optional ``Reason:``
and ``$ command`` block, a ``›``-pointed numbered menu whose labels end in the
shortcut ``(y)``, and the footer ``Press enter to confirm or esc to cancel``.

Safety: only the plain approve option (``Yes, proceed`` and its two per-prompt
spellings) is ever the ``yes``; every ``Yes, and …`` / ``for this session`` /
``in the future`` label is ``unsafe``. ``--dangerously-bypass-approvals-and-sandbox``
(alias ``--yolo``), ``--approve-for-me`` (``--not-so-yolo``), the removed
``--full-auto``, ``--sandbox danger-full-access`` and ``approval_policy = never``
are refused by ``normalize_mode`` / ``validate_command`` and never constructed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zordon.agents.base import (
    AgentInfo,
    BaseAdapter,
    HookRequest,
    LaunchSpec,
    SessionInfo,
    TranscriptSource,
)
from zordon.bus import PaneLine, PromptKind
from zordon.session.jsonl import parse_ts
from zordon.session.prompts import PromptMatch, PromptOption
from zordon.session.screen import Screen, parse_screen

log = logging.getLogger("zordon.agents.codex")

CODEX_VERSION = "codex-cli 0.160.0"  # the version every string in this file was checked against

CODEX_INFO = AgentInfo(
    key="codex",
    display_name="Codex",
    binary="codex",
    install_hint="npm install -g @openai/codex, then run `codex` once to sign in",
    docs_url="https://github.com/openai/codex",
)

BINARY_ENV = "ZORDON_CODEX_BINARY"  # tests and odd installs: an explicit path to the binary
HOME_ENV = "CODEX_HOME"  # live: the store moved with it (codex-rs/codex-home)
VERSION_TIMEOUT = 5.0

# ---- command line (live: `codex --help`, `codex resume --help`, .scratch/codex/help.txt) ----

# Zordon's mode names are Codex's ``approval_policy`` values (source: protocol.rs
# ``AskForApproval``: untrusted | on-request | on-failure (alias of on-request) | never).
ALLOWED_MODES: tuple[str, ...] = ("untrusted", "on-request", "on-failure")
DEFAULT_MODE = "on-request"  # live: the mode the TUI ran in (turn_context.approval_policy)
# ``--ask-for-approval`` in 0.160.0 accepts only these two (live: clap rejects the others);
# the other allowed modes are passed as a config override instead.
CLI_APPROVAL_VALUES: frozenset[str] = frozenset({"on-request", "never"})
MODE_ALIASES: dict[str, str] = {
    "default": "on-request",
    "ask": "on-request",
    "on_request": "on-request",
    "onrequest": "on-request",
    "on_failure": "on-failure",
    "onfailure": "on-failure",
    "unless-trusted": "untrusted",
    "unless_trusted": "untrusted",
}
# Refused outright: approval policies and flags that remove the human from the loop.
# The flag names may appear in this file only inside this REFUSED block.
REFUSED_MODES: frozenset[str] = frozenset(
    {"never", "full-auto", "full_auto", "fullauto", "yolo", "danger-full-access", "bypass", "auto"}
)
REFUSED_FLAGS: tuple[str, ...] = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",  # alias of the flag above (source: utils/cli/src/shared_options.rs)
    "--approve-for-me",  # routes approvals to an automatic reviewer instead of the user
    "--not-so-yolo",  # alias of --approve-for-me
    "--full-auto",  # removed in 0.160.0 (live: "unexpected argument"); refused if it comes back
    "--dangerously-bypass-hook-trust",
)
REFUSED_CONFIG_VALUES: dict[str, frozenset[str]] = {
    "approval_policy": frozenset({"never", "granular"}),
    "sandbox_mode": frozenset({"danger-full-access"}),
    "approvals_reviewer": frozenset({"auto_review"}),
}
SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")  # source: config_types.rs

# ---- session store (live: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl) ----

ROLLOUT_NAME = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEAD_LINES = 40
HEAD_BYTES = 2 * 1024 * 1024
TAIL_START = 256 * 1024
TAIL_MAX = 8 * 1024 * 1024
MAX_SESSION_FILES = 400  # newest files considered by list_sessions
RESULT_PREVIEW_CHARS = 200
MAX_READ_PER_POLL = 16 * 1024 * 1024
MAX_PARTIAL_LINE = 64 * 1024 * 1024
NEW_SESSION_SLACK = 5.0  # a rollout written this long before our launch is not ours (clock skew)

# ---- screen (live unless noted) ----

COMPOSER = re.compile(r"^› ?(?P<text>.*)$")  # the input box AND the echo of a sent message
COMPOSER_PLACEHOLDER = "Ask Codex to do anything"  # source: chat_composer.rs; live
STATUS_WORKING = re.compile(
    r"^\s*(?:[•◌]\s+)?(?P<verb>\S.*?)\s+\((?P<elapsed>[\dhms ]+) • esc to interrupt\)"
)  # live: "• Working (0s • esc to interrupt)", "• Reconnecting... 2/5 (1s • esc to interrupt)"
STATUS_ELAPSED_ONLY = re.compile(r"^\s*(?:[•◌]\s+)?(?P<verb>\S.*?)\s+\((?P<elapsed>[\dhms ]+)\)\s*$")
ESC_TO_INTERRUPT = re.compile(r"esc to interrupt", re.IGNORECASE)
BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # source: motion.rs FRAMES; live: ends the model row while working
MODEL_ROW_SPINNER = re.compile(rf"·\s*[{BRAILLE}◌]\s*$")
FOOTER_HINTS = re.compile(r"(\? for shortcuts|for agents|% context left|⌃c again to quit)")  # live/source
WORKED_FOR = re.compile(r"^\s*(?:─\s*)?Worked for (?P<elapsed><?[\dhms ]+)")  # source: separators.rs
ERROR_LINE = re.compile(r"^■ (?P<text>\S.*)$")  # live: "■ unexpected status 401 ..."
INTERRUPTED = re.compile(r"^■ Conversation interrupted")  # source: input_restore.rs
EXIT_RESUME = re.compile(r"^\s*codex resume (?P<id>[0-9a-f-]{36})\s*$")  # live exit screen
EXIT_NOTICE = re.compile(r"^\s*(Disconnected from this task|To reconnect, run:)")  # live
SHELL_PROMPT = re.compile(r"^\S+@\S+:.*[$#%] ?$|^[$#%] ?$")

# approval overlay (source: approval_overlay.rs + chatwidget snapshots; not seen live)
APPROVAL_FOOTER = re.compile(r"^\s*Press enter to confirm or esc to cancel\s*$")
APPROVAL_FOOTER_WRAPPED = re.compile(r"^\s*Press enter to confirm or esc to\s*$")  # 40-col snapshot
APPROVAL_TITLE = re.compile(
    r"^\s*(?P<title>Would you like to (?:run the following command|make the following edits|"
    r"grant these permissions|send input to .*)\??|Do you want to approve network access to .*\??|"
    r"\S.* needs your approval\.)\s*$"
)
APPROVAL_TITLE_START = re.compile(r"^\s*(Would you like to |Do you want to approve network access)")
APPROVAL_KIND: dict[str, str] = {
    "run the following command": "exec",
    "make the following edits": "patch",
    "grant these permissions": "permissions",
    "approve network access": "network",
    "send input to": "stdin",
    "needs your approval": "mcp",
}
MENU_OPTION = re.compile(r"^\s*(?P<ptr>›)?\s*(?P<n>\d{1,2})\. (?P<label>\S.*?)\s*$")
MENU_CONTINUATION = re.compile(r"^\s{4,}(?P<text>\S.*?)\s*$")  # a wrapped label (40-col snapshot)
SHORTCUT_SUFFIX = re.compile(r"\s\((?P<key>[a-z]|esc|enter|ctrl\+\w|⌃\w)\)$")
COMMAND_LINE = re.compile(r"^\s{2}\$ (?P<cmd>.*)$")  # "  $ echo hello world"
HEADER_FIELD = re.compile(r"^\s{2}(?P<key>Reason|Description|Destination|Environment|Thread|Server|Permission rule|Input): (?P<value>.*)$")
VIEW_ALL = re.compile(r"^\s*\[… \d+ lines\]")  # clipped header marker in the overlay
# Labels that are the plain, one-time approval (source: exec_options / patch_options /
# permissions_options / elicitation_options). Anything else that starts with Yes widens.
SAFE_YES_LABELS: frozenset[str] = frozenset(
    {
        "Yes, proceed",
        "Yes, just this once",
        "Yes, grant these permissions for this turn",
        "Yes, provide the requested info",
    }
)
UNSAFE_PHRASES: tuple[str, ...] = (
    "don't ask again",
    "for this session",
    "for this conversation",
    "in the future",
    "yes, and",
    "with strict auto review",  # grants for the turn, but hands review to the auto reviewer
)
NO_LABEL_START = re.compile(r"^(No\b|Cancel this request)", re.IGNORECASE)
PERSISTENT_NO = re.compile(r"block this host in the future", re.IGNORECASE)

# folder trust (live: eval/fixtures/codex/trust_dialog.txt; source: onboarding/trust_directory.rs)
TRUST_HEADER = re.compile(r"^\s*Folder access\s*$")
TRUST_QUESTION = re.compile(r"^\s*Trust this folder\?")
TRUST_ACCEPT = "Trust and continue"
TRUST_DECLINES: tuple[str, ...] = ("Quit", "Back to Agent Command Center", "Keep current directory")
TRUST_FOOTER = re.compile(r"^\s*enter continue(?: and create sandbox)? · esc (?:quit|back|cancel)\s*$")
TRUST_RESTRICTED = re.compile(r"^\s*(Config, hooks, and exec policies from untrusted folders|This existing task may retain)")

# login / welcome (live: eval/fixtures/codex/welcome_login.txt; source: onboarding/auth.rs)
WELCOME = re.compile(r"^\s*Welcome to Codex, OpenAI's command-line coding agent\s*$")
LOGIN_OPTION = re.compile(r"^\s*(?P<ptr>>)?\s*(?P<n>\d)\. (?P<label>Sign in with ChatGPT|Sign in with Device Code|Provide your own API key|Use Amazon Bedrock)\s*$")
LOGIN_FOOTER = re.compile(r"^\s*Press enter to continue\s*$")
APIKEY_TITLE = re.compile(r"^\s*>?\s*Use your own OpenAI API key for usage-based billing")
APIKEY_FOOTER = re.compile(r"^\s*Press enter to save\s*$")
BROWSER_LOGIN = re.compile(r"^\s*Finish signing in via your browser\s*$")
BRAILLE_ART = re.compile(r"^\s*[⠀-⣿]")  # the welcome logo rows (live)

# status row below the composer (live: "  GPT-x default · ~/path · ⠙"). The second word is the
# collaboration mode (source: footer.rs CollaborationModeIndicator: Plan), not the approval policy.
PLAN_MODE = re.compile(r"Plan mode(?: \(⇧tab to cycle\))?")


# ---- helpers --------------------------------------------------------------------------------


def codex_home(env: dict[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    raw = e.get(HOME_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def sessions_dir(home: Path) -> Path:
    return home / "sessions"


def config_path(home: Path) -> Path:
    return home / "config.toml"


def normalize_mode(mode: str) -> str:
    """Map a user-facing name onto ``ALLOWED_MODES``; refuse every bypass spelling."""
    raw = (mode or "").strip()
    low = raw.lower()
    if not low:
        raise ValueError("permission mode is empty")
    if "bypass" in low or "danger" in low or "yolo" in low or "full-access" in low or "full_access" in low:
        raise ValueError("Zordon never selects a bypass-approvals mode for Codex")
    m = MODE_ALIASES.get(low, low)
    if m in REFUSED_MODES:
        raise ValueError(f"approval policy {mode!r} removes the human from the loop; refused")
    if m not in ALLOWED_MODES:
        raise ValueError(f"unknown Codex approval policy {mode!r}; choose one of {ALLOWED_MODES}")
    return m


def approval_args(mode: str) -> list[str]:
    """argv fragment selecting ``mode``: ``-a`` where clap accepts it, ``-c`` otherwise."""
    m = normalize_mode(mode)
    if m in CLI_APPROVAL_VALUES:
        return ["--ask-for-approval", m]
    # live: `codex -a untrusted` is rejected by 0.160.0; the config key still takes every value
    # of AskForApproval (source: protocol.rs serde names), and -c parses its value as TOML.
    return ["--config", f'approval_policy="{m}"']


def validate_command(argv: Sequence[str]) -> None:
    """Raise ValueError if ``argv`` would widen approvals or disable the sandbox."""
    if not argv or Path(argv[0]).name != "codex":
        raise ValueError("a codex command line must start with 'codex'")
    for i, arg in enumerate(argv):
        base = arg.split("=", 1)[0]
        if base in REFUSED_FLAGS:
            raise ValueError(f"refused flag {base!r}")
        if base in ("--ask-for-approval", "-a"):
            value = arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            if value not in ALLOWED_MODES:
                raise ValueError(f"approval policy {value!r} is not allowed")
        if base in ("--sandbox", "-s"):
            value = arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            if value == "danger-full-access":
                raise ValueError("sandbox danger-full-access is never passed")
        if base in ("--config", "-c"):
            value = arg.split("=", 1)[1] if "=" in arg and base != arg else (argv[i + 1] if i + 1 < len(argv) else "")
            _check_config_override(value)


def _check_config_override(text: str) -> None:
    key, _, value = text.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    refused = REFUSED_CONFIG_VALUES.get(key)
    if refused and value in refused:
        raise ValueError(f"refused config override {key}={value!r}")
    if key == "approval_policy" and value not in ALLOWED_MODES:
        raise ValueError(f"approval policy {value!r} is not allowed")


def _toml_load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.debug("%s unreadable: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def read_config(home: Path) -> dict[str, Any]:
    """``approval_policy``, ``sandbox_mode`` and the trusted projects from ``config.toml``."""
    data = _toml_load(config_path(home))
    out: dict[str, Any] = {}
    for key in ("approval_policy", "sandbox_mode", "approvals_reviewer", "model"):
        v = data.get(key)
        if isinstance(v, str):
            out[key] = v
        elif isinstance(v, dict) and key == "approval_policy":
            out[key] = "granular"
    projects = data.get("projects")
    trusted: list[str] = []
    if isinstance(projects, dict):
        for path_key, entry in projects.items():
            if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
                trusted.append(str(path_key))
    out["trusted_projects"] = trusted
    return out


def _loads(raw: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _tail_lines(path: Path, start: int = TAIL_START, maximum: int = TAIL_MAX) -> list[bytes]:
    size = path.stat().st_size
    window = start
    while True:
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            data = fh.read()
        lines = data.split(b"\n")
        if size > window:
            lines = lines[1:]
        lines = [line for line in lines if line.strip()]
        if lines or window >= maximum or window >= size:
            return lines
        window *= 4


def _user_text(payload: dict[str, Any]) -> str | None:
    """The typed text of a ``response_item`` user message; None for injected context."""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    parts: list[str] = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("type") in ("input_text", "text") and isinstance(item.get("text"), str):
            parts.append(item["text"])
    text = "\n".join(parts).strip()
    if not text or text.startswith("<"):  # <environment_context>, <skills_instructions>, ...
        return None
    return text


# ---- discovery ----------------------------------------------------------------------------------


@dataclass(slots=True)
class RolloutMeta:
    path: Path
    session_id: str
    cwd: str = ""
    started_at: float | None = None
    last_active: float | None = None
    title: str = ""
    cli_version: str = ""
    originator: str = ""
    approval_policy: str | None = None
    sandbox_mode: str | None = None
    model: str = ""


def read_rollout_head(path: Path, meta: RolloutMeta) -> None:
    """First ``HEAD_LINES`` lines (``HEAD_BYTES`` cap): session_meta, turn_context, first prompt."""
    read = 0
    with path.open("rb") as fh:
        for i, raw in enumerate(fh):
            read += len(raw)
            if i >= HEAD_LINES or read > HEAD_BYTES:
                break
            rec = _loads(raw)
            if rec is None:
                continue
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            rtype = rec.get("type")
            if rtype == "session_meta":
                sid = payload.get("id") or payload.get("session_id")
                if isinstance(sid, str) and UUID_RE.match(sid):
                    meta.session_id = sid
                if isinstance(payload.get("cwd"), str):
                    meta.cwd = payload["cwd"]
                meta.started_at = parse_ts(payload.get("timestamp")) or parse_ts(rec.get("timestamp"))
                meta.cli_version = str(payload.get("cli_version") or "")
                meta.originator = str(payload.get("originator") or "")
            elif rtype == "turn_context":
                if isinstance(payload.get("approval_policy"), str):
                    meta.approval_policy = payload["approval_policy"]
                sp = payload.get("sandbox_policy")
                if isinstance(sp, dict) and isinstance(sp.get("type"), str):
                    meta.sandbox_mode = sp["type"]
                if isinstance(payload.get("cwd"), str) and not meta.cwd:
                    meta.cwd = payload["cwd"]
                meta.model = str(payload.get("model") or meta.model)
            elif rtype == "response_item" and not meta.title:
                text = _user_text(payload)
                if text:
                    meta.title = text.splitlines()[0][:200]
            if meta.cwd and meta.title and meta.started_at:
                break


def read_rollout_tail(path: Path, meta: RolloutMeta) -> None:
    """Latest timestamp and the last approval policy from the end of the file."""
    want_ts, want_policy = True, True
    for raw in reversed(_tail_lines(path)):
        rec = _loads(raw)
        if rec is None:
            continue
        if want_ts:
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None:
                meta.last_active = ts
                want_ts = False
        if want_policy and rec.get("type") == "turn_context":
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if isinstance(payload.get("approval_policy"), str):
                meta.approval_policy = payload["approval_policy"]
                sp = payload.get("sandbox_policy")
                if isinstance(sp, dict) and isinstance(sp.get("type"), str):
                    meta.sandbox_mode = sp["type"]
            want_policy = False
        if not want_ts and not want_policy:
            break


def rollout_files(home: Path, limit: int = MAX_SESSION_FILES) -> list[Path]:
    """Rollout files under ``sessions/``, newest first, bounded."""
    root = sessions_dir(home)
    if not root.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        for p in root.glob("*/*/*/rollout-*.jsonl"):
            if not ROLLOUT_NAME.match(p.name):
                continue
            try:
                found.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError as e:
        log.debug("sessions scan failed: %s", e)
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found[:limit]]


def rollout_meta(path: Path) -> RolloutMeta | None:
    m = ROLLOUT_NAME.match(path.name)
    if m is None:
        return None
    meta = RolloutMeta(path=path, session_id=m.group("id"))
    try:
        read_rollout_head(path, meta)
        read_rollout_tail(path, meta)
        if meta.last_active is None:
            meta.last_active = path.stat().st_mtime
    except OSError as e:
        log.debug("rollout %s unreadable: %s", path.name, e)
        return None
    return meta


def find_rollout(home: Path, session_id: str) -> Path | None:
    """The rollout file whose name carries ``session_id`` (any day)."""
    root = sessions_dir(home)
    if not root.is_dir() or not UUID_RE.match(session_id or ""):
        return None
    try:
        hits = sorted(root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    except OSError:
        return None
    return hits[-1] if hits else None


# ---- transcript ----------------------------------------------------------------------------------


@dataclass(slots=True)
class RolloutEvent:
    kind: str  # text | tool_use | tool_result | turn_start | turn_end | permission_mode | user_prompt | thinking
    ts: float
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str = ""
    is_error: bool = False
    stop_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def describe_call(name: str, args: dict[str, Any]) -> str:
    """A short spoken description of a Codex tool call: tool name plus the command or file."""
    cmd = args.get("command") if "command" in args else args.get("cmd")
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
        # the shell tool wraps commands as ["bash", "-lc", "<cmd>"] (source: core shell tool)
        parts = args.get("command")
        if isinstance(parts, list) and len(parts) == 3 and parts[1] in ("-lc", "-c"):
            cmd = str(parts[2])
    if isinstance(cmd, str) and cmd.strip():
        return f"{name}: {cmd.strip()}"
    patch = args.get("input") if isinstance(args.get("input"), str) else args.get("patch")
    if isinstance(patch, str) and "*** " in patch:
        files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
        if files:
            return f"{name}: {', '.join(f.strip() for f in files[:5])}"
    path = args.get("path") or args.get("file_path") or args.get("workdir")
    if isinstance(path, str) and path:
        return f"{name}: {path}"
    return name


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        body = output.get("body", output.get("content", ""))
        return _output_text(body)
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError:
            return {"raw": raw[:500]}
        return v if isinstance(v, dict) else {"value": v}
    return {}


def parse_rollout_record(rec: dict[str, Any], *, include_thinking: bool = False, now: float | None = None) -> list[RolloutEvent]:
    """Events for one rollout line (0..n).

    Dedup rule: assistant prose, tool calls and tool outputs are taken from
    ``response_item`` records only (persisted in both history modes; source:
    rollout/src/policy.rs ``should_persist_response_item``). The legacy
    ``event_msg agent_message`` and the paginated ``event_msg item_completed``
    copies of the same content are skipped so nothing is spoken twice. Turn
    boundaries come from ``event_msg`` (``task_started`` / ``task_complete`` /
    ``turn_aborted``, persisted in both modes).
    """
    rtype = rec.get("type")
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else None
    if not isinstance(rtype, str) or payload is None:
        return []
    ts = parse_ts(rec.get("timestamp")) or (now if now is not None else time.time())

    def ev(kind: str, **kw: Any) -> RolloutEvent:
        return RolloutEvent(kind=kind, ts=ts, **kw)

    out: list[RolloutEvent] = []
    if rtype == "response_item":
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role == "assistant":
                parts = [
                    c["text"]
                    for c in payload.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str)
                ]
                text = "\n".join(parts).strip()
                if text and not text.startswith("codex:code-mode-delivery:"):
                    out.append(ev("text", text=text, meta={"phase": payload.get("phase")}))
            elif role == "user":
                text = _user_text(payload)
                if text:
                    out.append(ev("user_prompt", text=text))
        elif ptype == "function_call":
            name = str(payload.get("name") or "tool")
            args = _args(payload.get("arguments"))
            out.append(ev("tool_use", name=name, input=args, text=describe_call(name, args), tool_use_id=str(payload.get("call_id") or "")))
        elif ptype == "local_shell_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            args = {"command": action.get("command"), "workdir": action.get("working_directory")}
            out.append(ev("tool_use", name="shell", input=args, text=describe_call("shell", args), tool_use_id=str(payload.get("call_id") or "")))
        elif ptype == "custom_tool_call":
            name = str(payload.get("name") or "tool")
            args = {"input": payload.get("input")} if isinstance(payload.get("input"), str) else _args(payload.get("input"))
            out.append(ev("tool_use", name=name, input=args, text=describe_call(name, args), tool_use_id=str(payload.get("call_id") or "")))
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            full = _output_text(payload.get("output"))
            success = payload.get("output", {}).get("success") if isinstance(payload.get("output"), dict) else None
            is_error = success is False or bool(re.search(r"^Exit code: [1-9]\d*", full, re.M)) or full.startswith("Error:")
            out.append(
                ev(
                    "tool_result",
                    text=full[:RESULT_PREVIEW_CHARS],
                    is_error=is_error,
                    tool_use_id=str(payload.get("call_id") or ""),
                    meta={"chars": len(full), "name": payload.get("name") or ""},
                )
            )
        elif ptype == "reasoning" and include_thinking:
            parts = [s.get("text", "") for s in payload.get("summary") or [] if isinstance(s, dict)]
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if text:
                out.append(ev("thinking", text=text))
        return out

    if rtype == "event_msg":
        etype = payload.get("type")
        if etype in ("task_started", "turn_started"):
            return [ev("turn_start", meta={"turn_id": payload.get("turn_id")})]
        if etype in ("task_complete", "turn_complete"):
            err = payload.get("error") if isinstance(payload.get("error"), dict) else None
            events: list[RolloutEvent] = []
            if err and isinstance(err.get("message"), str):
                events.append(ev("text", text=f"Codex error: {err['message']}", is_error=True, meta={"error": True}))
            events.append(ev("turn_end", stop_reason="error" if err else "task_complete", meta={"turn_id": payload.get("turn_id"), "duration_ms": payload.get("duration_ms")}))
            return events
        if etype == "turn_aborted":
            reason = payload.get("reason")
            return [ev("turn_end", stop_reason=str(reason or "aborted"), meta={"turn_id": payload.get("turn_id")})]
        if etype == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "Plan":
                text = item.get("text") if isinstance(item.get("text"), str) else ""
                if text.strip():
                    return [ev("text", text=text.strip(), meta={"plan": True})]
        return []  # agent_message / user_message / other item_completed: duplicates or noise

    if rtype == "turn_context":
        policy = payload.get("approval_policy")
        if isinstance(policy, str):
            sp = payload.get("sandbox_policy")
            sandbox = sp.get("type") if isinstance(sp, dict) else None
            return [ev("permission_mode", text=policy, meta={"sandbox_mode": sandbox, "model": payload.get("model")})]
        return []

    return []


def to_pane_lines(event: RolloutEvent, session_id: str) -> list[PaneLine]:
    meta: dict[str, Any] = {"agent": "codex"}
    if event.kind == "text":
        meta.update(event.meta)
        if event.is_error:
            meta["is_error"] = True
    elif event.kind == "tool_use":
        meta.update({"name": event.name, "input": event.input, "tool_use_id": event.tool_use_id})
    elif event.kind == "tool_result":
        meta.update({"is_error": event.is_error, "is_rejection": False, "tool_use_id": event.tool_use_id, **event.meta})
    elif event.kind == "turn_end":
        meta.update({"stop_reason": event.stop_reason, **event.meta})
    elif event.kind == "turn_start":
        meta.update(event.meta)
    elif event.kind == "permission_mode":
        meta.update({"mode": event.text, **event.meta})
    elif event.kind in ("thinking", "user_prompt"):
        pass
    else:
        return []
    return [PaneLine(session_id=session_id, text=event.text, ts=event.ts, source="jsonl", block=event.kind, meta=meta)]


class RolloutTail:
    """Incremental reader over one rollout file; ``poll()`` yields PaneLines.

    Same contract as ``session.jsonl.JsonlTail``: starts at EOF unless
    ``offset`` is given, keeps a partial last line, and restarts on truncation
    or a replaced inode. ``session_id`` is Zordon's id for the pane (the Codex
    thread id may differ for sessions Zordon started) and goes on every line.
    """

    def __init__(self, path: Path | str, session_id: str, *, offset: int | None = None, start_at_end: bool = True, include_thinking: bool = False) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.include_thinking = include_thinking
        self.last_mode: str | None = None
        self.events_seen = 0
        self.records_seen = 0
        self.parse_errors = 0
        self.resets = 0
        self._buf = b""
        self._ino: int | None = None
        self._offset = 0
        if offset is not None:
            self._offset = max(0, int(offset))
            self._remember_inode()
        elif start_at_end:
            try:
                st = self.path.stat()
                self._offset, self._ino = st.st_size, st.st_ino
            except OSError:
                self._offset = 0

    @property
    def offset(self) -> int:
        return self._offset

    def exists(self) -> bool:
        return self.path.is_file()

    def _remember_inode(self) -> None:
        try:
            self._ino = self.path.stat().st_ino
        except OSError:
            self._ino = None

    def _reset(self, ino: int) -> None:
        self._buf, self._offset, self._ino = b"", 0, ino
        self.resets += 1

    def poll(self) -> list[PaneLine]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self._ino is None:
            self._ino = st.st_ino
        elif st.st_ino != self._ino:
            log.info("%s was replaced; reading the new file from the start", self.path.name)
            self._reset(st.st_ino)
        if st.st_size < self._offset:
            log.info("%s shrank; reading from the start", self.path.name)
            self._reset(st.st_ino)
        if st.st_size == self._offset:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read(MAX_READ_PER_POLL)
        except OSError as e:
            log.debug("read failed on %s: %s", self.path.name, e)
            return []
        self._offset += len(data)
        return self._consume(data)

    def _consume(self, data: bytes) -> list[PaneLine]:
        self._buf += data
        if b"\n" not in self._buf:
            if len(self._buf) > MAX_PARTIAL_LINE:
                log.warning("%s: discarding an unterminated %d-byte line", self.path.name, len(self._buf))
                self._buf = b""
            return []
        head, self._buf = self._buf.rsplit(b"\n", 1)
        out: list[PaneLine] = []
        now = time.time()
        for raw in head.split(b"\n"):
            if not raw.strip():
                continue
            rec = _loads(raw)
            if rec is None:
                self.parse_errors += 1
                continue
            self.records_seen += 1
            for ev in parse_rollout_record(rec, include_thinking=self.include_thinking, now=now):
                self.events_seen += 1
                if ev.kind == "permission_mode":
                    self.last_mode = ev.text
                out.extend(to_pane_lines(ev, self.session_id))
        return out


# ---- the adapter -----------------------------------------------------------------------------------


class CodexAdapter(BaseAdapter):
    info = CODEX_INFO

    def __init__(self, config: Any | None = None, *, codex_home: Path | None = None, tmux: Any | None = None, zordon_home: Path | None = None) -> None:
        super().__init__(config)
        self.codex_home = Path(codex_home) if codex_home else None
        self.tmux = tmux
        self.zordon_home = Path(zordon_home) if zordon_home else None
        self._version: str | None = None
        self._version_checked = False
        self._version_lock = threading.Lock()
        self._launched: dict[str, tuple[float, str]] = {}  # zordon session id -> (launch ts, cwd)

    def bind(self, *, tmux: Any | None = None, zordon_home: Path | None = None, codex_home: Path | None = None, **_: Any) -> None:
        super().bind(tmux=tmux, zordon_home=zordon_home)
        if codex_home is not None:
            self.codex_home = Path(codex_home)

    @property
    def home(self) -> Path:
        return self.codex_home or codex_home()

    # ---- availability -------------------------------------------------------------

    def available(self) -> str | None:
        override = os.environ.get(BINARY_ENV)
        if override:
            p = Path(override).expanduser()
            return str(p) if p.is_file() and os.access(p, os.X_OK) else None
        return shutil.which(self.info.binary)

    def version(self) -> str | None:
        """``codex --version`` (``codex-cli 0.160.0``), cached; None when missing or slow."""
        with self._version_lock:
            if self._version_checked:
                return self._version
            self._version_checked = True
            binary = self.available()
            if not binary:
                return None
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [binary, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT, check=False
                )
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("codex --version failed: %s", e)
                return None
            first = (proc.stdout or proc.stderr or "").strip().splitlines()
            self._version = first[0].strip() if first else None
            return self._version

    # ---- launching ----------------------------------------------------------------

    def new_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec:
        """``codex [approval args]``. Codex picks its own thread id (UUIDv7), so
        ``session_id`` is only Zordon's handle; ``transcript_source`` finds the
        rollout file by cwd and launch time. Hooks: Codex has lifecycle hooks in
        ``config.toml`` but no per-launch settings file; none are installed."""
        argv = ["codex", *approval_args(permission_mode or DEFAULT_MODE)]
        validate_command(argv)
        self._launched[session_id] = (time.time(), cwd)
        return LaunchSpec(command=argv, cwd=cwd)

    def resume_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec:
        """``codex resume <thread id> [approval args]`` (live: `codex resume --help`)."""
        if not UUID_RE.match(session_id or ""):
            raise ValueError(f"Codex thread ids are UUIDs; got {session_id!r}")
        argv = ["codex", "resume", session_id, *approval_args(permission_mode or DEFAULT_MODE)]
        validate_command(argv)
        return LaunchSpec(command=argv, cwd=cwd)

    def supports_resume(self) -> bool:
        return True

    def allowed_modes(self) -> tuple[str, ...]:
        return ALLOWED_MODES

    def voice_switchable_modes(self) -> tuple[str, ...]:
        return ()  # no key or command switches the approval policy mid-session (see mode_cycle_key)

    def default_launch_mode(self) -> str | None:
        return DEFAULT_MODE

    def mode_cycle_key(self) -> str | None:
        # source: footer.rs / keymap: Shift+Tab cycles the *collaboration* mode (Default / Plan),
        # not the approval policy; the policy is set at launch or via the /permissions picker.
        return None

    def normalize_mode(self, mode: str) -> str:
        return normalize_mode(mode)

    def forbidden_modes(self) -> frozenset[str]:
        return REFUSED_MODES

    # ---- discovery -----------------------------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        out: list[SessionInfo] = []
        panes = self._codex_panes()
        for path in rollout_files(self.home):
            meta = rollout_meta(path)
            if meta is None:
                continue
            out.append(self._to_info(meta, panes))
        return out

    def find_session(self, session_id: str) -> SessionInfo | None:
        path = find_rollout(self.home, session_id)
        if path is None:
            return None
        meta = rollout_meta(path)
        return self._to_info(meta, self._codex_panes()) if meta else None

    def _codex_panes(self) -> dict[str, list[Any]]:
        """cwd -> tmux panes running ``codex`` (best effort; no registry exists)."""
        if self.tmux is None:
            return {}
        try:
            panes = self.tmux.list_panes()
        except Exception as e:  # noqa: BLE001 - tmux may be gone
            log.debug("list_panes failed: %s", e)
            return {}
        by_cwd: dict[str, list[Any]] = {}
        for p in panes:
            if (getattr(p, "command", "") or "").split("/")[-1] == "codex":
                by_cwd.setdefault(getattr(p, "cwd", "") or "", []).append(p)
        return by_cwd

    def _to_info(self, meta: RolloutMeta, panes: dict[str, list[Any]]) -> SessionInfo:
        hits = panes.get(meta.cwd, [])
        pane = hits[0] if len(hits) == 1 else None  # ambiguous when several run in one cwd
        return SessionInfo(
            agent=CODEX_INFO.key,
            session_id=meta.session_id,
            cwd=meta.cwd,
            title=meta.title or meta.session_id[:8],
            last_active=meta.last_active,
            running_pid=getattr(pane, "pid", None) if pane else None,
            tmux_target=getattr(pane, "target", None) if pane else None,
            permission_mode=meta.approval_policy,
            transcript_path=meta.path,
            extra={
                "running": "1" if pane else "",
                "status": "",
                "version": meta.cli_version,
                "sandbox_mode": meta.sandbox_mode or "",
                "model": meta.model,
                "started_at": meta.started_at or "",
            },
        )

    # ---- screen --------------------------------------------------------------------

    def parse(self, lines: Sequence[str]) -> Screen:
        return parse_screen(list(lines))

    def _composer_index(self, lines: list[str]) -> int | None:
        """Index of the input box: the bottom-most ``›`` line that is not a menu row.

        The echo of a sent message uses the same glyph, so the composer is the
        last one; the pointed row of an approval menu (``› 1. Yes, proceed (y)``)
        is excluded, and while the overlay is up there is no composer at all.
        """
        for i in range(len(lines) - 1, -1, -1):
            if COMPOSER.match(lines[i]) and not MENU_OPTION.match(lines[i]):
                return i
        return None

    def detect_prompt(self, screen: Screen) -> PromptMatch | None:
        lines = [ln.rstrip() for ln in screen.lines]
        return (
            self._detect_approval(lines)
            or self._detect_trust(lines)
            or self._detect_login(lines)
            or self._fallback_prompt(screen)
        )

    def _fallback_prompt(self, screen: Screen) -> PromptMatch | None:
        # The generic detector as a backstop for a modal we have not catalogued
        # (an unknown approval shape). It is quiet on the composer and on prose.
        if self._composer_index([ln.rstrip() for ln in screen.lines]) is not None:
            return None
        m = BaseAdapter.detect_prompt(self, screen)
        if m is not None:
            m.extra["codex_unknown_modal"] = "1"
            for o in m.options:
                if o.label.lower().startswith("yes") and o.label not in SAFE_YES_LABELS:
                    o.unsafe = True
        return m

    def _detect_approval(self, lines: list[str]) -> PromptMatch | None:
        footer_i = next((i for i in range(len(lines) - 1, -1, -1) if APPROVAL_FOOTER.match(lines[i]) or APPROVAL_FOOTER_WRAPPED.match(lines[i])), None)
        if footer_i is None:
            return None
        if self._composer_index(lines[footer_i:]) is not None:
            return None  # the composer is back: the modal is gone, this is scrollback
        options, first_opt = self._menu_above(lines, footer_i)
        if len(options) < 2:
            return None
        # header: walk up from the menu to the title
        title_i = None
        for i in range(first_opt - 1, max(-1, first_opt - 40), -1):
            if APPROVAL_TITLE_START.match(lines[i]) or re.match(r"^\s*\S.* needs your approval\.", lines[i]):
                title_i = i
                break
        header = lines[title_i:first_opt] if title_i is not None else lines[max(0, first_opt - 12) : first_opt]
        title_lines: list[str] = []
        if title_i is not None:
            title_lines.append(lines[title_i].strip())
            j = title_i + 1
            while j < first_opt and lines[j].strip() and not HEADER_FIELD.match(lines[j]) and not COMMAND_LINE.match(lines[j]):
                title_lines.append(lines[j].strip())  # a wrapped title in a narrow pane
                j += 1
        title = " ".join(title_lines)
        kind_key = next((v for k, v in APPROVAL_KIND.items() if k in title), "unknown")
        fields: dict[str, str] = {}
        command_lines: list[str] = []
        in_cmd = False
        for ln in header:
            cm = COMMAND_LINE.match(ln)
            fm = HEADER_FIELD.match(ln)
            if cm:
                command_lines.append(cm.group("cmd"))
                in_cmd = True
                continue
            if fm:
                fields[fm.group("key").lower().replace(" ", "_")] = fm.group("value").strip()
                in_cmd = False
                continue
            if in_cmd and ln.startswith("  ") and ln.strip() and not VIEW_ALL.match(ln):
                command_lines.append(ln[2:])  # continuation row of a multi-line command
            elif not ln.strip():
                in_cmd = False
        command = "\n".join(command_lines) if command_lines else None
        for o in options:
            o.unsafe = is_unsafe_label(o.label)
        extra: dict[str, str] = {"codex_kind": kind_key}
        for o in options:
            key = o.description
            if key and len(key) == 1 and key.isalpha():
                extra[f"key{o.index}"] = key  # typed as a single key; the manager sends no Enter
            if key:
                extra[f"shortcut{o.index}"] = key
            o.description = ""
        if "permission_rule" in fields:
            extra["permission_rule"] = fields["permission_rule"]
        return PromptMatch(
            kind=PromptKind.PERMISSION,
            title=title or "Codex approval",
            question=title,
            options=options,
            raw_lines=[ln.strip() for ln in lines[(title_i if title_i is not None else first_opt) : footer_i + 1] if ln.strip()],
            confidence=0.9 if title_i is not None else 0.7,
            command=command,
            target_file=fields.get("destination"),
            header={"exec": "Run command", "patch": "Edit files", "permissions": "Grant permissions", "network": "Network access", "stdin": "Terminal input", "mcp": "MCP approval"}.get(kind_key, "Approval"),
            description=fields.get("reason") or fields.get("description") or "",
            extra=extra,
        )

    def _menu_above(self, lines: list[str], footer_i: int) -> tuple[list[PromptOption], int]:
        """Numbered ``›``-pointed options between the header and ``footer_i`` (with wrapped labels)."""
        options: list[PromptOption] = []
        first_opt = footer_i
        pending_cont: list[str] = []  # wrapped tail rows of the option above them (walking upward)
        i = footer_i - 1
        while i >= 0:
            ln = lines[i]
            if not ln.strip():
                if options or pending_cont:
                    break  # a blank row above the first option ends the menu
                i -= 1  # blank rows between the footer and the menu
                continue
            m = MENU_OPTION.match(ln)
            if m:
                label = " ".join([m.group("label"), *reversed(pending_cont)])
                pending_cont = []
                key = ""
                sm = SHORTCUT_SUFFIX.search(label)
                if sm:
                    key = sm.group("key")
                    label = label[: sm.start()]
                options.insert(0, PromptOption(int(m.group("n")), label.strip(), bool(m.group("ptr")), False, key))
                first_opt = i
                i -= 1
                continue
            cm = MENU_CONTINUATION.match(ln)
            if cm:
                pending_cont.append(cm.group("text"))
                i -= 1
                continue
            break
        return options, first_opt

    def _detect_trust(self, lines: list[str]) -> PromptMatch | None:
        footer_i = next((i for i in range(len(lines) - 1, -1, -1) if TRUST_FOOTER.match(lines[i])), None)
        if footer_i is None:
            return None
        options, first_opt = self._menu_above(lines, footer_i)
        if not options:
            return None
        header_i = next((i for i in range(first_opt - 1, -1, -1) if TRUST_HEADER.match(lines[i])), None)
        path = ""
        if header_i is not None:
            nxt = next((lines[j].strip() for j in range(header_i + 1, first_opt) if lines[j].strip()), "")
            path = nxt
        question = next((lines[j].strip() for j in range(first_opt - 1, -1, -1) if TRUST_QUESTION.match(lines[j]) or TRUST_RESTRICTED.match(lines[j])), "Trust this folder?")
        for o in options:
            o.unsafe = False  # trusting is the user's explicit decision; the manager confirms first
        return PromptMatch(
            kind=PromptKind.TRUST,
            title="Folder access",
            question=question,
            options=options,
            raw_lines=[ln for ln in lines[(header_i if header_i is not None else first_opt) : footer_i + 1] if ln.strip()],
            confidence=0.95,
            target_file=path or None,
            header="Folder access",
            extra={"path": path} if path else {},
        )

    def _detect_login(self, lines: list[str]) -> PromptMatch | None:
        if any(APIKEY_TITLE.match(ln) for ln in lines) and any(APIKEY_FOOTER.match(ln) for ln in lines):
            return PromptMatch(
                kind=PromptKind.QUESTION,
                title="Codex sign-in",
                question="Codex is asking for an OpenAI API key to be typed into the terminal.",
                options=[],
                raw_lines=[ln for ln in lines if ln.strip()][-6:],
                confidence=0.9,
                header="Sign in",
                extra={"codex_login": "api_key"},
            )
        if any(BROWSER_LOGIN.match(ln) for ln in lines):
            return PromptMatch(
                kind=PromptKind.QUESTION,
                title="Codex sign-in",
                question="Codex is waiting for the browser sign-in to finish.",
                options=[],
                raw_lines=[ln for ln in lines if ln.strip()][-6:],
                confidence=0.9,
                header="Sign in",
                extra={"codex_login": "browser"},
            )
        footer_i = next((i for i in range(len(lines) - 1, -1, -1) if LOGIN_FOOTER.match(lines[i])), None)
        if footer_i is None or not any(WELCOME.match(ln) for ln in lines):
            return None
        options: list[PromptOption] = []
        for ln in lines[:footer_i]:
            m = LOGIN_OPTION.match(ln)
            if m:
                options.append(PromptOption(int(m.group("n")), m.group("label"), bool(m.group("ptr"))))
        if not options:
            return None
        return PromptMatch(
            kind=PromptKind.QUESTION,
            title="Codex sign-in",
            question="Sign in to Codex: choose how to authenticate.",
            options=options,
            raw_lines=[ln for ln in lines[: footer_i + 1] if ln.strip() and not BRAILLE_ART.match(ln)],
            confidence=0.95,
            header="Sign in",
            extra={"codex_login": "menu"},
        )

    def is_working(self, screen: Screen) -> bool:
        lines = [ln.rstrip() for ln in screen.lines]
        tail = lines[-12:]
        if any(STATUS_WORKING.match(ln) or ESC_TO_INTERRUPT.search(ln) for ln in tail):
            return True
        return any(MODEL_ROW_SPINNER.search(ln) for ln in tail)

    def is_idle(self, screen: Screen) -> bool:
        lines = [ln.rstrip() for ln in screen.lines]
        ci = self._composer_index(lines)
        if ci is None or self.is_working(screen):
            return False
        # nothing but status/footer rows may follow the composer
        below = [ln for ln in lines[ci + 1 :] if ln.strip()]
        return len(below) <= 3

    def input_quiet(self, screen: Screen) -> bool:
        return self.is_idle(screen)

    def exited(self, screen: Screen) -> bool:
        lines = [ln.rstrip() for ln in screen.lines]
        if self._composer_index(lines) is not None:
            return False
        nb = [ln for ln in lines if ln.strip()]
        if not nb:
            return False
        if any(EXIT_RESUME.match(ln) for ln in nb) or any(EXIT_NOTICE.match(ln) for ln in nb):
            return True
        return bool(SHELL_PROMPT.match(nb[-1]))

    def permission_mode_from_screen(self, screen: Screen) -> str | None:
        return None  # the approval policy is not shown on screen (footer shows model + collaboration mode)

    def collaboration_mode_from_screen(self, screen: Screen) -> str | None:
        """``plan`` when the footer shows ``Plan mode``; ``default`` when the composer is up; else None."""
        lines = [ln.rstrip() for ln in screen.lines]
        if any(PLAN_MODE.search(ln) for ln in lines[-4:]):
            return "plan"
        return "default" if self._composer_index(lines) is not None else None

    def uses_alternate_screen(self) -> bool:
        return True  # live: scrollback intact after /quit; source: tui.rs alt_screen_enabled = true

    # ---- prompt answers ---------------------------------------------------------------

    def yes_option(self, m: PromptMatch) -> int | None:
        for o in m.options:
            if o.label in SAFE_YES_LABELS and not o.unsafe:
                return o.index
        if m.extra.get("codex_unknown_modal"):
            return BaseAdapter.yes_option(self, m)
        return None

    def no_option(self, m: PromptMatch) -> int | None:
        candidates = [o for o in m.options if NO_LABEL_START.match(o.label) and not PERSISTENT_NO.search(o.label)]
        if not candidates:
            return None
        # prefer the plain decline over "tell Codex what to do differently" (which also aborts the turn)
        for o in candidates:
            if o.label.startswith("No, continue without") or o.label.startswith("No, but continue"):
                return o.index
        return candidates[-1].index

    def plan_approve_option(self, m: PromptMatch) -> int | None:
        return None  # Codex plan mode has no approval modal; the user switches modes with Shift+Tab

    def plan_revise_option(self, m: PromptMatch) -> int | None:
        return None

    def trust_accept_option(self, m: PromptMatch) -> int | None:
        return next((o.index for o in m.options if o.label == TRUST_ACCEPT), None)

    def trust_decline_option(self, m: PromptMatch) -> int | None:
        return next((o.index for o in m.options if o.label in TRUST_DECLINES), None)

    # ---- transcript ------------------------------------------------------------------

    def transcript_source(self, session_id: str, cwd: str, info: SessionInfo | None) -> TranscriptSource | None:
        """The rollout file once it exists; None until then (the manager retries).

        A resumed or attached session (``info`` given, or ``session_id`` is a Codex
        thread id with a file) starts at EOF. A session Zordon started is matched
        by cwd among rollouts created after the launch and replayed from the start.
        """
        path = info.transcript_path if info is not None and info.transcript_path else None
        if path is None:
            path = find_rollout(self.home, session_id)
        if path is not None and path.is_file():
            tail = RolloutTail(path, session_id, start_at_end=True)
            return tail
        launched = self._launched.get(session_id)
        if launched is None:
            return None
        since, want_cwd = launched
        path = self._rollout_for_launch(since, want_cwd)
        if path is None:
            return None
        return RolloutTail(path, session_id, offset=0)

    def _rollout_for_launch(self, since: float, cwd: str) -> Path | None:
        want = os.path.realpath(cwd) if cwd else ""
        for path in rollout_files(self.home, limit=50):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since - NEW_SESSION_SLACK:
                continue
            m = ROLLOUT_NAME.match(path.name)
            if m is None:
                continue
            meta = RolloutMeta(path=path, session_id=m.group("id"))
            try:
                read_rollout_head(path, meta)
            except OSError:
                continue
            if meta.started_at is not None and meta.started_at < since - NEW_SESSION_SLACK:
                continue
            if not want or os.path.realpath(meta.cwd or "") == want:
                return path
        return None

    def transcript_path(self, session_id: str) -> Path | None:
        return find_rollout(self.home, session_id)

    # ---- permissions / wording ----------------------------------------------------

    def permission_summary(self, cwd: str, active_mode: str | None) -> str:
        cfg = read_config(self.home)
        policy = active_mode or cfg.get("approval_policy") or DEFAULT_MODE
        sandbox = cfg.get("sandbox_mode")
        parts = [f"Codex asks for approval {self.mode_label(policy)}"]
        if sandbox:
            parts.append(f"commands run in the {sandbox} sandbox")
        if cfg.get("approvals_reviewer") == "auto_review":
            parts.append("approvals are routed to Codex's automatic reviewer, not to you")
        if cwd:
            real = os.path.realpath(cwd)
            trusted = any(real == os.path.realpath(p) or real.startswith(os.path.realpath(p).rstrip("/") + "/") for p in cfg.get("trusted_projects", []))
            parts.append("this folder is trusted" if trusted else "this folder is not yet trusted")
        return "; ".join(parts) + "."

    def mode_label(self, mode: str) -> str:
        return {
            "untrusted": "for every command that is not explicitly allowed",
            "on-request": "when the model asks",
            "on-failure": "when the model asks (on-failure is an alias of on-request)",
            "never": "never (refused by Zordon)",
        }.get(mode, mode)


def is_unsafe_label(label: str) -> bool:
    """True for an approval option that widens or persists permissions."""
    low = label.lower()
    if label in SAFE_YES_LABELS:
        return False
    return any(p in low for p in UNSAFE_PHRASES)


def iter_rollout_events(path: Path, *, include_thinking: bool = False) -> Iterable[RolloutEvent]:
    """Replay a whole rollout file (tests and `zordon sessions --dump`)."""
    with path.open("rb") as fh:
        for raw in fh:
            rec = _loads(raw)
            if rec is not None:
                yield from parse_rollout_record(rec, include_thinking=include_thinking)
