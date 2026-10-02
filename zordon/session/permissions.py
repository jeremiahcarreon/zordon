"""Claude Code permission settings: read them, summarise them for speech, and
validate mode switches. Pure apart from reading the three settings files.

Claude Code's ``settings.json`` files are the only source of truth for
permissions (design, "Permission modes"). Zordon reads them, never writes them.
Precedence as documented: ``~/.claude/settings.json`` < ``<project>/.claude/settings.json``
< ``<project>/.claude/settings.local.json``; later files win for scalars and the
``allow`` / ``ask`` / ``deny`` lists are concatenated.

Mode names: Claude Code's are ``default`` (``manual`` in the status row and in
``--help``), ``acceptEdits``, ``plan``, ``auto``, ``dontAsk`` and
``bypassPermissions``. Voice may switch between the first three; ``auto`` and
``dontAsk`` are tap-only; ``bypassPermissions`` is never a target (``ValueError``)
and is only ever reported.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("zordon.session.permissions")

# status-row wording <-> Claude Code mode name (both directions below)
MODE_LABELS: dict[str, str] = {
    "manual": "default",
    "default": "default",
    "accept edits": "acceptEdits",
    "plan": "plan",
    "auto": "auto",
    "bypass permissions": "bypassPermissions",
    "don't ask": "dontAsk",
    "dont ask": "dontAsk",
}
# mode name -> spoken label (what the status row says, and what the user says)
_SPOKEN_PREFERRED = ("default", "accept edits", "plan", "auto", "bypass permissions", "don't ask")
MODE_SPOKEN: dict[str, str] = {MODE_LABELS[label]: label for label in _SPOKEN_PREFERRED}
ALL_MODES: tuple[str, ...] = tuple(MODE_SPOKEN)
VOICE_SWITCHABLE: tuple[str, ...] = ("default", "acceptEdits", "plan")
TAP_SWITCHABLE: tuple[str, ...] = VOICE_SWITCHABLE + ("auto", "dontAsk")
FORBIDDEN_TARGET_MODES: frozenset[str] = frozenset({"bypassPermissions"})
# Shift+Tab cycle in the TUI (observed order for the three voice modes; the bypass
# step only exists when the user enabled it, and is never selected by Zordon).
MODE_CYCLE: tuple[str, ...] = ("default", "acceptEdits", "plan")

RULE_LISTS = ("allow", "ask", "deny")


@dataclass(slots=True)
class PermissionSummary:
    default_mode: str  # effective permissions.defaultMode, "default" when none is set
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    bypass_configured: bool = False  # some file sets defaultMode to bypass permissions
    sources: list[Path] = field(default_factory=list)  # files that existed and parsed
    bypass_sources: list[Path] = field(default_factory=list)
    skip_dangerous_prompt: bool = False  # skipDangerousModePermissionPrompt in any file
    additional_directories: list[str] = field(default_factory=list)
    disable_bypass: bool = False  # permissions.disableBypassPermissionsMode == "disable"
    hooks_disabled: bool = False  # disableAllHooks: the Notification signal will not fire
    errors: list[str] = field(default_factory=list)  # files that exist but did not parse

    @property
    def rule_counts(self) -> dict[str, int]:
        return {"allow": len(self.allow), "deny": len(self.deny), "ask": len(self.ask)}


def settings_paths(claude_home: Path, project_dir: Path | str | None) -> list[Path]:
    """Lowest precedence first."""
    paths = [Path(claude_home) / "settings.json"]
    if project_dir:
        pd = Path(project_dir) / ".claude"
        paths += [pd / "settings.json", pd / "settings.local.json"]
    return paths


def read_settings(claude_home: Path, project_dir: Path | str | None) -> PermissionSummary:
    """Merge the user, project and local settings into one PermissionSummary."""
    summary = PermissionSummary(default_mode="default")
    mode_set = False
    for path in settings_paths(claude_home, project_dir):
        data = _load(path, summary)
        if data is None:
            continue
        summary.sources.append(path)
        perms = data.get("permissions")
        perms = perms if isinstance(perms, dict) else {}
        for name in RULE_LISTS:
            rules = perms.get(name)
            if isinstance(rules, list):
                getattr(summary, name).extend(str(r) for r in rules if isinstance(r, str))
        dirs = perms.get("additionalDirectories")
        if isinstance(dirs, list):
            summary.additional_directories.extend(str(d) for d in dirs if isinstance(d, str))
        mode = perms.get("defaultMode")
        if isinstance(mode, str) and mode:
            if mode in FORBIDDEN_TARGET_MODES:
                summary.bypass_configured = True
                summary.bypass_sources.append(path)
            summary.default_mode = MODE_LABELS.get(mode, mode)
            mode_set = True
        if perms.get("disableBypassPermissionsMode") == "disable":
            summary.disable_bypass = True
        if data.get("skipDangerousModePermissionPrompt") is True:
            summary.skip_dangerous_prompt = True
        if data.get("disableAllHooks") is True:
            summary.hooks_disabled = True
    if not mode_set:
        summary.default_mode = "default"
    return summary


def _load(path: Path, summary: PermissionSummary) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        summary.errors.append(f"{path}: {e}")
        return None
    try:
        data = json.loads(text)
    except ValueError as e:
        summary.errors.append(f"{path}: invalid JSON ({e})")
        log.warning("settings file %s is not valid JSON", path)
        return None
    return data if isinstance(data, dict) else None


# ---- speech -------------------------------------------------------------------------------


def mode_label(mode: str) -> str:
    """Spoken label for a mode name or status-row word (``acceptEdits`` -> ``accept edits``)."""
    canonical = MODE_LABELS.get(mode, mode)
    return MODE_SPOKEN.get(canonical, canonical)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def summary_sentence(summary: PermissionSummary, active_mode: str | None = None) -> str:
    """One spoken sentence about the active mode and the rule counts, with the switch hint."""
    mode = MODE_LABELS.get(active_mode, active_mode) if active_mode else summary.default_mode
    mode = mode or "default"
    label = mode_label(mode)
    counts = summary.rule_counts
    parts = [f"{counts['allow']} allow rule" + ("" if counts["allow"] == 1 else "s")]
    parts.append(_plural(counts["deny"], "deny rule"))
    if counts["ask"]:
        parts.append(_plural(counts["ask"], "ask rule"))
    if not any(counts.values()):
        rules = "no allow or deny rules"
    elif len(parts) == 2:
        rules = f"{parts[0]} and {parts[1]}"
    else:
        rules = f"{parts[0]}, {parts[1]} and {parts[2]}"

    if mode in FORBIDDEN_TARGET_MODES:
        sentence = (
            f"This session is in {label} mode, so Claude Code will not ask before acting; "
            f"it has {rules}. Zordon cannot change that by voice; "
            "say switch to default, accept edits or plan mode to leave it."
        )
    else:
        others = [mode_label(m) for m in VOICE_SWITCHABLE if m != mode]
        if mode not in VOICE_SWITCHABLE:
            others = [mode_label(m) for m in VOICE_SWITCHABLE]
        hint = f"say switch to {others[0]} or {others[1]} mode to change it" if len(others) == 2 else (
            f"say switch to {others[0]}, {others[1]} or {others[2]} mode to change it"
        )
        sentence = f"This session is in {label} mode with {rules}; {hint}."
    if summary.bypass_configured and mode not in FORBIDDEN_TARGET_MODES:
        where = ", ".join(_short_source(p) for p in summary.bypass_sources) or "a settings file"
        sentence += f" Bypass permissions is configured in {where}; Zordon never switches to it."
    return sentence


def _short_source(path: Path) -> str:
    parts = path.parts
    if len(parts) >= 2 and parts[-2] == ".claude":
        return f".claude/{parts[-1]}"
    return path.name


# ---- mode switching -----------------------------------------------------------------------------


def normalize_target_mode(mode: str, *, by_voice: bool) -> str:
    """Canonical mode name for a switch request, or ValueError.

    Accepts mode names and spoken labels (``"accept edits"``, ``"manual"``).
    Voice may only pick ``VOICE_SWITCHABLE``; taps may also pick ``auto`` and
    ``dontAsk``; ``bypassPermissions`` is refused for everyone.
    """
    raw = (mode or "").strip()
    canonical = MODE_LABELS.get(raw.lower(), MODE_LABELS.get(raw, raw))
    if canonical in FORBIDDEN_TARGET_MODES or raw.lower().replace(" ", "") == "bypasspermissions":
        raise ValueError("bypass permissions is never a target mode")
    allowed = VOICE_SWITCHABLE if by_voice else TAP_SWITCHABLE
    if canonical not in allowed:
        who = "by voice" if by_voice else "from the client"
        raise ValueError(f"mode {mode!r} cannot be selected {who}; choose one of {allowed}")
    return canonical


def btab_presses(current: str | None, target: str) -> int | None:
    """How many Shift+Tab presses move from ``current`` to ``target`` in the TUI cycle.

    None when either mode is outside the cycle (``auto``, ``dontAsk`` or an
    unknown current mode): the manager then uses the ``/permissions`` command
    instead of guessing. Never computes a path through bypass permissions.
    """
    cur = MODE_LABELS.get(current or "", current or "")
    tgt = normalize_target_mode(target, by_voice=False)
    if cur not in MODE_CYCLE or tgt not in MODE_CYCLE:
        return None
    return (MODE_CYCLE.index(tgt) - MODE_CYCLE.index(cur)) % len(MODE_CYCLE)
