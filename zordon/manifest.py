"""What Zordon installed, so uninstall can take it away again.

``~/.zordon/installed.json`` records every piece Zordon put on the machine at
the user's request: system packages (tmux, node, the agent, Ollama), the uv
installation when install.sh made it, Ollama models it pulled, the models and
binaries under ``~/.zordon``. Inside-the-environment items are removed without
asking; outside items are offered one at a time.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from zordon import paths

KINDS = ("system", "uv", "ollama-model", "model", "binary", "tool")


@dataclass(slots=True)
class Entry:
    kind: str  # one of KINDS
    name: str  # tmux | node | claude-code | ollama | uv | qwen2.5:3b-instruct | silero_vad.onnx ...
    command: str = ""  # how it was installed (for the record and to derive removal)
    removal: str = ""  # how to remove it; empty = derive from kind/name at uninstall time
    outside: bool = True  # True: lives outside Zordon's environment -> ask before removing
    installed_at: float = field(default_factory=time.time)
    note: str = ""


def manifest_path() -> Path:
    return paths.zordon_home() / "installed.json"


def load() -> list[Entry]:
    p = manifest_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    out: list[Entry] = []
    for raw in data.get("entries", []) if isinstance(data, dict) else []:
        try:
            out.append(Entry(**{k: raw[k] for k in Entry.__dataclass_fields__ if k in raw}))  # type: ignore[arg-type]
        except TypeError:
            continue
    return out


def save(entries: list[Entry]) -> Path:
    p = manifest_path()
    paths.ensure_private_dir(p.parent)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "entries": [asdict(e) for e in entries]}, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def record(kind: str, name: str, *, command: str = "", removal: str = "", outside: bool = True, note: str = "") -> Entry:
    """Add (or refresh) one entry. Idempotent on (kind, name)."""
    if kind not in KINDS:
        raise ValueError(f"unknown manifest kind {kind!r}")
    entries = [e for e in load() if not (e.kind == kind and e.name == name)]
    entry = Entry(kind=kind, name=name, command=command, removal=removal, outside=outside, note=note)
    entries.append(entry)
    save(entries)
    return entry


def forget(kind: str, name: str) -> None:
    entries = [e for e in load() if not (e.kind == kind and e.name == name)]
    save(entries)


def has(kind: str, name: str) -> bool:
    return any(e.kind == kind and e.name == name for e in load())
