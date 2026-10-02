"""Documentation invariants.

The docs are part of the product: the README-level promises (which providers
exist, which models are downloaded, how remote access works) must track the code,
and nothing under ``docs/`` may carry author attribution of any kind.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from zordon import assets
from zordon import config as cfg

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DECISIONS = DOCS / "decisions"

STATUS_RE = re.compile(r"^\*\*Status:\*\* (accepted|superseded by \d{4}|rejected), \d{4}-\d{2}-\d{2}$", re.M)


def _docs() -> list[Path]:
    files = sorted(DOCS.rglob("*.md"))
    assert files, "no docs found"
    return files


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---- decision records ------------------------------------------------------------


def _decision_files() -> list[Path]:
    files = sorted(p for p in DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert len(files) >= 9, f"expected at least nine decision records, found {len(files)}"
    return files


@pytest.mark.parametrize("path", _decision_files(), ids=lambda p: p.name)
def test_decision_has_status_line(path: Path):
    text = _read(path)
    assert STATUS_RE.search(text), f"{path.name} lacks a '**Status:** accepted, YYYY-MM-DD' line"
    number = path.name[:4]
    assert text.lstrip().startswith(f"# {number}:"), f"{path.name} title must start with '# {number}:'"


def test_decision_numbers_are_unique_and_indexed():
    files = _decision_files()
    numbers = [p.name[:4] for p in files]
    assert len(set(numbers)) == len(numbers), "duplicate decision numbers"
    index = _read(DECISIONS / "README.md")
    for p in files:
        assert p.name in index, f"{p.name} is not linked from decisions/README.md"


@pytest.mark.parametrize(
    "name, needles",
    [
        ("0002-session-store-and-registry.md", ["lossy", "history.jsonl", "sessions/<pid>.json", "/proc", "--resume"]),
        ("0004-vad-placement.md", ["0.08", "150 ms", "onnxruntime", "20 ms"]),
        ("0005-tts-default-and-streaming.md", ["fp32", "int8", "510", "af_heart", "440-590"]),
        ("0006-router-jev-via-sdk.md", ["typesafe-sdk", "3.14", "probabilities[choice]", "4,096", "0.042"]),
        ("0007-permission-prompt-handling.md", ["always allow", "manually approve edits", "No, exit", "0.95"]),
        ("0008-pane-capture-and-diffing.md", ["alternate screen", "spinner", "100 ms", "1.5"]),
        ("0009-notification-hook-second-signal.md", ["--settings", "curl", "0600", "exit 0", "PermissionRequest"]),
    ],
)
def test_decision_mentions_its_evidence(name: str, needles: list[str]):
    text = _read(DECISIONS / name)
    for n in needles:
        assert n in text, f"{name} should mention {n!r}"


# ---- providers.md ---------------------------------------------------------------


def test_providers_doc_mentions_every_provider_name():
    text = _read(DOCS / "providers.md")
    defaults = cfg.ProvidersConfig()
    for name in (defaults.stt, defaults.tts, defaults.normalizer, defaults.router):
        assert f"`{name}`" in text, f"providers.md must name the default provider {name!r}"
    # Alternatives documented in the config comments.
    for name in ("openai", "groq", "elevenlabs", "passthrough", "anthropic", "keyword", "jev"):
        assert f"`{name}`" in text, f"providers.md must name provider {name!r}"
    for key, env in cfg.ENV_KEYS.items():
        assert f"`{key}`" in text, f"providers.md must list the [providers.keys] entry {key!r}"
        assert env in text, f"providers.md must list the env fallback {env!r}"
    for knob in (
        "stt_model",
        "stt_device",
        "tts_voice",
        "tts_speed",
        "normalizer_model",
        "router_model",
        "normalizer_timeout_seconds",
    ):
        assert knob in text, f"providers.md must document providers.{knob}"
    assert defaults.tts_voice in text
    assert defaults.normalizer_model in text


def test_providers_doc_mentions_every_asset():
    text = _read(DOCS / "providers.md")
    for asset in assets.ASSETS.values():
        assert f"`{asset.name}`" in text, f"providers.md must list asset {asset.name!r}"
        assert asset.filename.removesuffix(".tgz") in text, f"providers.md must name {asset.filename!r}"
        if asset.sha256:
            assert asset.sha256 in text, f"providers.md must carry the sha256 of {asset.name}"
        if asset.size:
            assert f"{asset.size:,}" in text, f"providers.md must carry the size of {asset.name}"
    assert assets.WHISPER_DIRNAME in text
    assert assets.WHISPER_REPO in text


# ---- remote-access.md --------------------------------------------------------------


def test_remote_access_doc_covers_the_three_routes():
    text = _read(DOCS / "remote-access.md")
    for needle in ("--tunnel", "tailscale", "0.0.0.0", "token", "trycloudflare.com", "zordon token show"):
        assert needle in text, f"remote-access.md must mention {needle!r}"
    # The enforced tunnel requirements, with the numbers from config defaults.
    srv = cfg.ServerConfig()
    assert str(srv.auth_rate_limit_per_minute) in text
    assert str(srv.idle_disconnect_minutes) in text
    assert "Secure" in text
    # Phones need HTTPS for the microphone; the doc must say so plainly.
    assert "HTTPS" in text and "microphone" in text.lower()


# ---- security.md / troubleshooting.md / prompts-version.md ------------------------


def test_security_doc_lists_the_invariants():
    text = _read(DOCS / "security.md")
    for needle in (
        "--dangerously-skip-permissions",
        "bypassPermissions",
        "send-keys -l",
        "0600",
        "[redacted]",
        "zordon token show",
    ):
        assert needle in text, f"security.md must mention {needle!r}"


def test_troubleshooting_doc_covers_the_known_gotchas():
    text = _read(DOCS / "troubleshooting.md")
    for needle in (
        "No, exit",
        "Nothing changed on screen",
        "prompts-version.md",
        "Talk",
        "HTTPS",
        "libcublas",
        "160",
        "cloudflared",
    ):
        assert needle in text, f"troubleshooting.md must mention {needle!r}"


def test_prompts_version_doc_has_the_capture_procedure():
    text = _read(DOCS / "prompts-version.md")
    assert "PROMPTS_VERSION" in text
    assert "capture-pane" in text and "-p -J" in text
    assert "tmux -L" in text, "the procedure must use a private tmux server"
    assert "No, exit" in text and "Enter" in text, "warn about Enter on the trust dialog"
    assert "PROVENANCE.md" in text
    assert "--dangerously-skip-permissions" in text  # told never to pass it


# ---- attribution hygiene -------------------------------------------------------------

MODEL_ID_RE = re.compile(r"claude-[a-z]+-[0-9][\w.\-\[\]]*")
# Spelled in pieces so this file does not itself contain the markers it forbids.
ATTRIBUTION_RE = re.compile(
    r"generated (?:by|with)|co-?" + "authored|written by an ai|authored by claude|"
    + "session_" + "01(?!MASKED)[A-Za-z0-9]{10,}",
    re.I,
)
# The marketing names are spelled in pieces so this file does not itself contain them.
_MODEL_WORDS = ("fa" + "ble", "op" + "us", "son" + "net")
MODEL_NAME_RE = re.compile(r"\b(" + "|".join(_MODEL_WORDS) + r")\b", re.I)


@pytest.mark.parametrize("path", _docs(), ids=lambda p: str(p.relative_to(DOCS)))
def test_no_attribution_in_docs(path: Path):
    text = _read(path)
    m = ATTRIBUTION_RE.search(text)
    assert not m, f"{path.relative_to(ROOT)} contains an attribution phrase: {m.group(0)!r}"
    # Model ids such as claude-haiku-4-5 or claude-sonnet-5 are provider configuration and
    # allowed; the marketing names as words are not.
    stripped = MODEL_ID_RE.sub("", text)
    m = MODEL_NAME_RE.search(stripped)
    assert not m, f"{path.relative_to(ROOT)} names a model outside a model id: {m.group(0)!r}"
