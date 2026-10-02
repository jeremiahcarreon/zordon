"""permissions.py: settings merge, the spoken summary and mode-switch validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import fixtures_store as fs
from zordon.session import permissions as P
from zordon.session.permissions import (
    MODE_LABELS,
    TAP_SWITCHABLE,
    VOICE_SWITCHABLE,
    PermissionSummary,
    btab_presses,
    mode_label,
    normalize_target_mode,
    read_settings,
    settings_paths,
    summary_sentence,
)

BYPASS = MODE_LABELS["bypass permissions"]  # never spelled out as a literal in package code


@pytest.fixture
def homes(tmp_path: Path) -> tuple[Path, Path]:
    claude_home = tmp_path / "claude-home"
    project = tmp_path / "proj"
    claude_home.mkdir(exist_ok=True)  # conftest's isolated-home fixture may have made it
    project.mkdir(exist_ok=True)
    return claude_home, project


def test_settings_paths_order(homes):
    claude_home, project = homes
    paths = settings_paths(claude_home, project)
    assert paths == [
        claude_home / "settings.json",
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
    ]
    assert settings_paths(claude_home, None) == [claude_home / "settings.json"]


def test_no_files_gives_unknown_mode_and_no_rules(homes):
    """RR-2: Claude Code's built-in default differs between releases (2.1.x starts in
    auto), so nothing names a mode when no settings file does."""
    claude_home, project = homes
    s = read_settings(claude_home, project)
    assert s.default_mode is None
    assert P.BUILTIN_DEFAULT_MODE is None
    assert s.allow == [] and s.deny == [] and s.ask == []
    assert not s.bypass_configured and s.sources == [] and s.errors == []
    sentence = summary_sentence(s, None)
    assert sentence.startswith(P.UNKNOWN_MODE_TEXT)
    assert "default mode" not in sentence and "auto mode" not in sentence
    assert "no allow or deny rules" in sentence
    assert "switch to default, accept edits or plan mode" in sentence
    # The observed mode always wins over the configured one.
    assert summary_sentence(s, "auto").startswith("This session is in auto mode with no allow or deny rules")
    assert summary_sentence(s, "manual").startswith("This session is in default mode")
    fs.write_settings(project / ".claude" / "settings.local.json", {"permissions": {"defaultMode": "plan"}})
    assert summary_sentence(read_settings(claude_home, project), None).startswith("This session is in plan mode")


def test_merge_scalars_later_wins_lists_concatenate(homes):
    claude_home, project = homes
    fs.write_settings(
        claude_home / "settings.json",
        {
            "permissions": {"deny": [f"Edit(.env{i})" for i in range(28)], "defaultMode": "plan"},
            "statusLine": {"type": "command", "command": "x"},
        },
    )
    fs.write_settings(
        project / ".claude" / "settings.json",
        {"permissions": {"allow": ["Bash(pytest:*)", "Bash(ruff:*)"], "ask": ["Bash(git push:*)"]}},
    )
    fs.write_settings(
        project / ".claude" / "settings.local.json",
        {"permissions": {"allow": ["Bash(php artisan:*)"], "defaultMode": "acceptEdits", "additionalDirectories": ["/tmp/x"]}},
    )
    s = read_settings(claude_home, project)
    assert s.default_mode == "acceptEdits"  # local wins
    assert s.allow == ["Bash(pytest:*)", "Bash(ruff:*)", "Bash(php artisan:*)"]
    assert len(s.deny) == 28 and s.ask == ["Bash(git push:*)"]
    assert s.additional_directories == ["/tmp/x"]
    assert len(s.sources) == 3
    assert not s.bypass_configured
    assert s.rule_counts == {"allow": 3, "deny": 28, "ask": 1}


def test_manual_mode_in_settings_maps_to_default(homes):
    claude_home, project = homes
    fs.write_settings(claude_home / "settings.json", {"permissions": {"defaultMode": "manual"}})
    assert read_settings(claude_home, project).default_mode == "default"


def test_bypass_in_project_local_is_reported(homes):
    claude_home, project = homes
    fs.write_settings(claude_home / "settings.json", {"permissions": {"deny": ["Edit(.env)"]}, "skipDangerousModePermissionPrompt": True})
    fs.write_settings(
        project / ".claude" / "settings.local.json",
        {"permissions": {"allow": ["Bash(ls:*)"], "defaultMode": BYPASS}},
    )
    s = read_settings(claude_home, project)
    assert s.bypass_configured
    assert s.bypass_sources == [project / ".claude" / "settings.local.json"]
    assert s.default_mode == BYPASS
    assert s.skip_dangerous_prompt
    sentence = summary_sentence(s, None)
    assert "bypass permissions mode" in sentence
    assert "will not ask" in sentence
    assert "1 allow rule and 1 deny rule" in sentence
    # With an active mode that is not bypass, the configuration is still mentioned.
    sentence2 = summary_sentence(s, "default")
    assert sentence2.startswith("This session is in default mode with 1 allow rule and 1 deny rule; say switch to accept edits or plan mode to change it.")
    assert "Bypass permissions is configured in .claude/settings.local.json; Zordon never switches to it." in sentence2


def test_invalid_json_is_recorded_not_fatal(homes):
    claude_home, project = homes
    (claude_home / "settings.json").write_text("{not json")
    fs.write_settings(project / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(ls:*)"]}, "disableAllHooks": True})
    s = read_settings(claude_home, project)
    assert len(s.errors) == 1 and "invalid JSON" in s.errors[0]
    assert s.allow == ["Bash(ls:*)"] and s.sources == [project / ".claude" / "settings.json"]
    assert s.hooks_disabled


def test_summary_sentence_shapes():
    s = PermissionSummary(default_mode="default", allow=["a", "b", "c"], deny=["d"] * 28)
    assert summary_sentence(s, "default") == (
        "This session is in default mode with 3 allow rules and 28 deny rules; "
        "say switch to accept edits or plan mode to change it."
    )
    assert summary_sentence(s, "manual").startswith("This session is in default mode")
    assert summary_sentence(s, "acceptEdits").startswith(
        "This session is in accept edits mode with 3 allow rules and 28 deny rules; say switch to default or plan mode"
    )
    assert summary_sentence(s, "plan").endswith("say switch to default or accept edits mode to change it.")
    empty = PermissionSummary(default_mode="default")
    assert "with no allow or deny rules;" in summary_sentence(empty)
    asks = PermissionSummary(default_mode="plan", allow=["x"], deny=[], ask=["y", "z"])
    assert "1 allow rule, 0 deny rules and 2 ask rules" in summary_sentence(asks)
    auto = summary_sentence(PermissionSummary(default_mode="auto"))
    assert auto.startswith("This session is in auto mode") and "say switch to default, accept edits or plan mode" in auto
    # No sentence ever suggests switching to bypass.
    for mode in (None, "default", "acceptEdits", "plan", "auto", "dontAsk", BYPASS):
        text = summary_sentence(s, mode)
        assert "switch to bypass" not in text


def test_mode_labels_both_directions():
    assert MODE_LABELS["manual"] == "default" and MODE_LABELS["accept edits"] == "acceptEdits"
    assert MODE_LABELS["don't ask"] == "dontAsk" and MODE_LABELS["bypass permissions"] == BYPASS
    assert mode_label("acceptEdits") == "accept edits" and mode_label("accept edits") == "accept edits"
    assert mode_label("default") == "default" and mode_label("manual") == "default"
    assert mode_label("dontAsk") == "don't ask" and mode_label(BYPASS) == "bypass permissions"
    assert VOICE_SWITCHABLE == ("default", "acceptEdits", "plan")
    assert TAP_SWITCHABLE == ("default", "acceptEdits", "plan", "auto", "dontAsk")
    assert BYPASS not in TAP_SWITCHABLE


def test_normalize_target_mode():
    assert normalize_target_mode("accept edits", by_voice=True) == "acceptEdits"
    assert normalize_target_mode("acceptEdits", by_voice=True) == "acceptEdits"
    assert normalize_target_mode("manual", by_voice=True) == "default"
    assert normalize_target_mode("Plan", by_voice=True) == "plan"
    assert normalize_target_mode("auto", by_voice=False) == "auto"
    assert normalize_target_mode("don't ask", by_voice=False) == "dontAsk"
    with pytest.raises(ValueError):
        normalize_target_mode("auto", by_voice=True)
    with pytest.raises(ValueError):
        normalize_target_mode("dontAsk", by_voice=True)
    for spelling in (BYPASS, "bypass permissions", "Bypass Permissions", "bypasspermissions"):
        with pytest.raises(ValueError, match="never"):
            normalize_target_mode(spelling, by_voice=False)
        with pytest.raises(ValueError, match="never"):
            normalize_target_mode(spelling, by_voice=True)
    with pytest.raises(ValueError):
        normalize_target_mode("yolo", by_voice=False)
    with pytest.raises(ValueError):
        normalize_target_mode("", by_voice=False)


def test_btab_presses():
    assert btab_presses("default", "acceptEdits") == 1
    assert btab_presses("default", "plan") == 2
    assert btab_presses("plan", "default") == 1
    assert btab_presses("manual", "default") == 0
    assert btab_presses("acceptEdits", "accept edits") == 0
    assert btab_presses("auto", "plan") is None
    assert btab_presses(None, "plan") is None
    assert btab_presses("default", "auto") is None
    with pytest.raises(ValueError):
        btab_presses("default", BYPASS)
