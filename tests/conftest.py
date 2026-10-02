from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real ~/.zordon or ~/.claude during tests."""
    zh = tmp_path / "zordon-home"
    ch = tmp_path / "claude-home"
    ch.mkdir()
    monkeypatch.setenv("ZORDON_HOME", str(zh))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ch))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "GROQ_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return zh


FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def read_fixture(name: str) -> str:
    return (FIXTURES / "pane" / name).read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def pane_fixture():
    return read_fixture


def pytest_configure(config):
    os.environ.setdefault("ZORDON_TESTING", "1")
