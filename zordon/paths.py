"""Filesystem locations. Everything Zordon writes lives under ``zordon_home()``.

``ZORDON_HOME`` overrides the default ``~/.zordon`` so tests and CI never touch
the real home directory. ``CLAUDE_CONFIG_DIR`` is honoured the same way Claude
Code honours it.
"""

from __future__ import annotations

import os
from pathlib import Path


def zordon_home() -> Path:
    return Path(os.environ.get("ZORDON_HOME") or (Path.home() / ".zordon")).expanduser()


def config_path() -> Path:
    return zordon_home() / "config.toml"


def models_dir() -> Path:
    return zordon_home() / "models"


def bin_dir() -> Path:
    return zordon_home() / "bin"


def db_path() -> Path:
    return zordon_home() / "transcripts.db"


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) with mode 0700; tighten an existing dir too."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
