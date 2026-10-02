"""Registry of agent adapters. ``config.providers.agent`` (or a per-session choice)
selects one; ``get_adapter`` builds it. Keys are stable config values."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zordon.agents.base import (
    AgentAdapter,
    AgentInfo,
    BaseAdapter,
    HookRequest,
    LaunchSpec,
    SessionInfo,
)

__all__ = [
    "ADAPTERS",
    "AgentAdapter",
    "AgentInfo",
    "BaseAdapter",
    "HookRequest",
    "LaunchSpec",
    "SessionInfo",
    "available_agents",
    "get_adapter",
]


def _claude(config: Any) -> AgentAdapter:
    from zordon.agents.claude_code import ClaudeCodeAdapter  # noqa: PLC0415

    return ClaudeCodeAdapter(config)


def _codex(config: Any) -> AgentAdapter:
    from zordon.agents.codex import CodexAdapter  # noqa: PLC0415

    return CodexAdapter(config)


def _generic(config: Any) -> AgentAdapter:
    return BaseAdapter(config)


ADAPTERS: dict[str, Callable[[Any], AgentAdapter]] = {
    "claude-code": _claude,
    "codex": _codex,
    "generic": _generic,
}

DEFAULT_AGENT = "claude-code"

# Binary names, so availability can be answered even when an adapter module is absent.
BINARIES: dict[str, str] = {"claude-code": "claude", "codex": "codex", "generic": ""}


def get_adapter(key: str | None, config: Any | None = None) -> AgentAdapter:
    k = (key or DEFAULT_AGENT).strip().lower()
    if k not in ADAPTERS:
        raise ValueError(f"unknown agent {key!r}; known: {', '.join(sorted(ADAPTERS))}")
    return ADAPTERS[k](config)


def available_agents(config: Any | None = None) -> dict[str, str | None]:
    """key -> binary path (None when not installed; '' for the generic adapter)."""
    out: dict[str, str | None] = {}
    import shutil  # noqa: PLC0415

    for key in ADAPTERS:
        try:
            out[key] = get_adapter(key, config).available()
        except Exception:  # noqa: BLE001 - a missing or broken adapter must not hide the binary
            binary = BINARIES.get(key, "")
            out[key] = shutil.which(binary) if binary else ""
    return out
