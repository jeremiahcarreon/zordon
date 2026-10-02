"""Claude Code hook plumbing for the session manager (decision 0009).

On every launch Zordon passes ``--settings <file>`` with ``command`` hooks that
POST each ``Notification``, ``UserPromptSubmit`` and ``Stop`` event to
``/hooks/claude`` (the secret header comes from a 0600 curl config file, see
``discovery.write_hook_settings``). The transport verifies the shared secret and hands the JSON
body to ``SessionControl.hook_event``; this module holds the pure pieces of that
path: building the settings, checking a payload's shape and secret, and turning
a payload into a ``Notice`` or a state hint.

Nothing here can answer a prompt: no ``PermissionRequest`` hook is ever built
(``discovery.hook_settings_json`` refuses it) and the handlers always exit 0.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zordon import paths
from zordon.bus import Notice
from zordon.session import discovery

log = logging.getLogger("zordon.session.hooks")

HOOK_SECRET_HEADER = discovery.HOOK_SECRET_HEADER

# Notification types Zordon registered a matcher for, and what each one hints.
PROMPT_NOTIFICATIONS: frozenset[str] = frozenset(
    {"permission_prompt", "agent_needs_input", "elicitation_dialog"}
)
IDLE_NOTIFICATIONS: frozenset[str] = frozenset({"idle_prompt"})
HOOK_EVENTS: tuple[str, ...] = tuple(discovery.HOOK_EVENTS)

# How many polls a hint stays in force once received.
HINT_POLLS = 2
# Second-signal prompt score fed to the state machine while a prompt hint is live.
HINT_PROMPT_SCORE = 0.9


@dataclass(slots=True)
class HookHint:
    """What a hook payload tells the state machine about one session."""

    kind: str  # prompt | idle | working | stop | none
    message: str = ""
    notification_type: str = ""
    polls_left: int = HINT_POLLS
    notified: bool = False  # the spoken fallback notice has been published
    score: float = HINT_PROMPT_SCORE  # prompt score the state machine sees while a prompt hint is live

    @property
    def active(self) -> bool:
        return self.polls_left > 0

    def consume(self) -> None:
        if self.polls_left > 0:
            self.polls_left -= 1


def build_hook_settings(
    port: int,
    secret: str,
    curl_config: Path | str | None = None,
    host: str = discovery.HOOK_DEFAULT_HOST,
) -> dict[str, Any]:
    """Settings JSON for ``--settings``: command hooks that POST to Zordon.

    Delegates to ``discovery.hook_settings_json`` so there is exactly one place
    that knows the handler shape. ``secret`` is validated but never written into
    the JSON: the handler reads it from ``curl_config`` (``discovery.write_hook_settings``
    writes that file with mode 0600).
    """
    discovery.validate_hook_secret(secret)
    if curl_config is None:
        curl_config = paths.zordon_home() / "hooks" / "zordon.curlrc"
    return discovery.hook_settings_json(port, curl_config, host=host)


def verify_hook_payload(payload: Any, secret_header: str, expected_secret: str) -> bool:
    """True when the secret matches (constant time) and the payload has the hook shape.

    The shape check is deliberately loose: ``session_id`` must be a non-empty
    string and ``hook_event_name`` (when present) a string; a ``Notification``
    must carry a string ``notification_type``.
    """
    if not expected_secret or not isinstance(secret_header, str):
        return False
    try:
        same = hmac.compare_digest(secret_header.encode("utf-8"), expected_secret.encode("utf-8"))
    except (UnicodeError, TypeError):
        return False
    if not same:
        return False
    return payload_shape_ok(payload)


def payload_shape_ok(payload: Any) -> bool:
    """Structural check only (no secret): is this something ``hook_event`` can use?"""
    if not isinstance(payload, dict):
        return False
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return False
    event = payload.get("hook_event_name")
    if event is not None and not isinstance(event, str):
        return False
    if event == "Notification" and not isinstance(payload.get("notification_type"), str):
        return False
    return True


def hint_for(payload: dict[str, Any]) -> HookHint:
    """Classify a verified payload into a state hint."""
    event = str(payload.get("hook_event_name") or "")
    ntype = str(payload.get("notification_type") or "")
    message = str(payload.get("message") or "").strip()
    if event == "Notification":
        if ntype in PROMPT_NOTIFICATIONS:
            return HookHint("prompt", message, ntype)
        if ntype in IDLE_NOTIFICATIONS:
            return HookHint("idle", message, ntype)
        return HookHint("none", message, ntype)
    if event == "UserPromptSubmit":
        return HookHint("working", message, ntype)
    if event == "Stop":
        return HookHint("stop", message, ntype)
    return HookHint("none", message, ntype)


def payload_to_notice(payload: dict[str, Any]) -> Notice:
    """A client-facing notice for a hook payload. Spoken only for prompt notifications."""
    sid = str(payload.get("session_id") or "")
    event = str(payload.get("hook_event_name") or "hook")
    ntype = str(payload.get("notification_type") or "")
    message = str(payload.get("message") or "").strip()
    title = str(payload.get("title") or "").strip()
    if event == "Notification" and ntype in PROMPT_NOTIFICATIONS:
        text = message or title or "Claude Code is waiting for your input."
        return Notice(text=text, level="warning", session_id=sid, speak=True)
    if event == "Notification" and ntype in IDLE_NOTIFICATIONS:
        return Notice(text=message or "Claude Code is idle.", level="info", session_id=sid, speak=False)
    if event == "Stop":
        return Notice(text="Claude Code finished its turn.", level="info", session_id=sid, speak=False)
    if event == "UserPromptSubmit":
        return Notice(text="Claude Code received a prompt.", level="info", session_id=sid, speak=False)
    label = f"{event}: {ntype}" if ntype else event
    return Notice(text=message or title or label, level="info", session_id=sid, speak=False)
