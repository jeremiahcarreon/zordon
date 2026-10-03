from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from zordon import paths
from zordon.config import Config, ConfigError


def test_first_run_creates_private_file_with_token(_isolated_home: Path):
    cfg, created = Config.load_or_create()
    assert created
    p = paths.config_path()
    assert p.exists()
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert len(cfg.server.token) >= 24
    text = p.read_text()
    assert "bypassPermissions" not in text
    assert "[providers.keys]" in text

    again, created2 = Config.load_or_create()
    assert not created2
    assert again.server.token == cfg.server.token


def test_roundtrip_and_defaults(_isolated_home: Path):
    cfg = Config.default()
    cfg.voice.verbosity = "technical"
    cfg.providers.keys["anthropic"] = "k"
    cfg.save()
    loaded = Config.load()
    assert loaded.voice.verbosity == "technical"
    assert loaded.providers.key("anthropic") == "k"
    assert loaded.server.bind == "127.0.0.1"
    assert loaded.providers.stt == "faster-whisper"
    assert loaded.providers.tts == "kokoro"
    assert loaded.providers.router == "jev"
    assert loaded.voice.idle_watchdog_seconds == 20
    assert loaded.voice.router_confidence == 0.85


def test_env_fallback_for_keys(monkeypatch):
    cfg = Config.default()
    assert cfg.providers.key("openai") == ""
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert cfg.providers.key("openai") == "from-env"
    cfg.providers.keys["openai"] = "from-file"
    assert cfg.providers.key("openai") == "from-file"


def test_non_loopback_requires_token():
    with pytest.raises(ConfigError):
        Config.from_dict({"server": {"bind": "0.0.0.0", "token": ""}})
    cfg = Config.from_dict({"server": {"bind": "0.0.0.0", "token": "abc"}})
    assert not cfg.is_loopback()


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({"server": {"bind": "127.0.0.1", "bypass": True}})


def test_invalid_verbosity_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({"voice": {"verbosity": "loud"}})


def test_sessions_launch_mode_roundtrip_and_limits(_isolated_home: Path):
    cfg = Config.default()
    assert cfg.sessions.permission_mode == "default"
    cfg.sessions.permission_mode = "auto"
    cfg.save()
    assert Config.load().sessions.permission_mode == "auto"
    with pytest.raises(ConfigError):
        Config.from_dict({"sessions": {"permission_mode": "yolo"}})
    with pytest.raises(ConfigError) as e:
        Config.from_dict({"sessions": {"permission_mode": "bypassPermissions"}})
    assert "never launches with permissions bypassed" in str(e.value)


def test_no_bypass_option_exists():
    cfg = Config.default()
    dumped = str(cfg.to_dict()).lower()
    assert "bypass" not in dumped
    assert "dangerously" not in dumped


def test_providers_agent_defaults_to_claude_code_and_is_validated():
    from zordon.agents import ADAPTERS

    cfg = Config.default()
    assert cfg.providers.agent == "claude-code"
    assert "agent" in cfg.to_dict()["providers"]
    for key in ADAPTERS:
        cfg.providers.agent = key
        cfg.validate()
    cfg.providers.agent = "vim"
    with pytest.raises(ConfigError, match="providers.agent"):
        cfg.validate()
    loaded = Config.from_dict({"providers": {"agent": "generic"}})
    assert loaded.providers.agent == "generic"
    with pytest.raises(ConfigError):
        Config.from_dict({"providers": {"agent": "nope"}})
