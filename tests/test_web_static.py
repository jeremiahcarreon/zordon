"""Static checks on the browser client in ``zordon/web``.

No browser is available in CI, so correctness comes from three places: the Node
self-tests under ``tests/web`` (worklet numerics, protocol mirror), ``node
--check`` on every script, and the grep-level invariants below (same-origin
assets only, protocol strings present, no permission-widening strings, no
author attribution).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from zordon.transport import protocol as P

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "zordon" / "web"
NODE_TESTS = ROOT / "tests" / "web"

EXPECTED_FILES = (
    "index.html",
    "style.css",
    "app.js",
    "audio.js",
    "worklet.js",
    "protocol.js",
    "manifest.webmanifest",
    "favicon.svg",
)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _web_files() -> list[Path]:
    return sorted(p for p in WEB.iterdir() if p.is_file())


def _js_files() -> list[Path]:
    return sorted(WEB.glob("*.js"))


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


# ---- files exist ------------------------------------------------------------------


def test_expected_files_present():
    for name in EXPECTED_FILES:
        assert (WEB / name).is_file(), f"zordon/web/{name} missing"


# ---- node: syntax and self tests ----------------------------------------------------


@needs_node
@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_node_check(path: Path):
    proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@needs_node
@pytest.mark.parametrize("script", ["worklet_test.cjs", "protocol_test.cjs", "app_test.cjs"])
def test_node_self_tests(script: str):
    proc = subprocess.run(
        [NODE, str(NODE_TESTS / script)], capture_output=True, text=True, timeout=120, cwd=str(ROOT)
    )
    assert proc.returncode == 0, f"{script} failed:\n{proc.stdout}\n{proc.stderr}"
    assert "all passed" in proc.stdout


# ---- index.html ---------------------------------------------------------------------


class _Refs(HTMLParser):
    """Collect every URL-bearing attribute and the ids used in the page."""

    URL_ATTRS = {"src", "href", "action", "poster", "data", "formaction"}

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[tuple[str, str, str]] = []
        self.ids: list[str] = []
        self.inline_handlers: list[str] = []
        self.has_viewport_fit = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        for k, v in attrs:
            if k in self.URL_ATTRS and v is not None:
                self.urls.append((tag, k, v))
            if k.startswith("on"):
                self.inline_handlers.append(f"<{tag} {k}>")
        if tag == "meta" and a.get("name") == "viewport" and "viewport-fit=cover" in (a.get("content") or ""):
            self.has_viewport_fit = True
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])


def _parse_index() -> _Refs:
    p = _Refs()
    p.feed(_read("index.html"))
    return p


def test_index_references_only_same_origin_assets():
    refs = _parse_index()
    assert refs.urls, "index.html references no assets at all?"
    for tag, attr, url in refs.urls:
        if tag == "svg" or tag == "use" or url.startswith("#"):
            continue
        low = url.lower().strip()
        assert not low.startswith(("http://", "https://", "//", "data:", "javascript:")), (
            f"<{tag} {attr}={url!r}> is not a same-origin relative asset"
        )
        assert not low.startswith("/"), f"<{tag} {attr}={url!r}> should be relative so the page works under any mount"


def test_index_loads_every_script_and_stylesheet():
    refs = _parse_index()
    assert refs.scripts == ["protocol.js", "audio.js", "app.js"], refs.scripts
    hrefs = {u for _, k, u in refs.urls if k == "href"}
    assert "style.css" in hrefs
    assert "manifest.webmanifest" in hrefs
    assert "favicon.svg" in hrefs
    for src in refs.scripts:
        assert (WEB / src).is_file(), src


def test_index_mobile_meta_and_no_inline_handlers():
    refs = _parse_index()
    assert refs.has_viewport_fit, "viewport meta must include viewport-fit=cover for safe areas"
    assert not refs.inline_handlers, f"inline event handlers found: {refs.inline_handlers}"
    html = _read("index.html")
    assert 'name="color-scheme"' in html
    assert re.search(r"<html[^>]*\blang=", html)
    assert "<title>" in html
    # Every id the app looks up exists exactly once.
    ids = refs.ids
    assert len(ids) == len(set(ids)), "duplicate ids in index.html"
    looked_up = set(re.findall(r"\$\('([a-z0-9-]+)'\)", _read("app.js")))
    missing = looked_up - set(ids)
    assert not missing, f"app.js looks up ids that index.html lacks: {sorted(missing)}"


def test_index_regions_present():
    html = _read("index.html")
    for element_id in (
        "gate",
        "gate-form",
        "token",
        "session-list",
        "new-session",
        "new-mode",
        "tunnel",
        "tunnel-qr",
        "btn-talk",
        "btn-mute",
        "btn-stop",
        "btn-repeat",
        "rows",
        "prompts",
        "text",
        "raw-send",
        "file-input",
        "camera-input",
        "settings",
        "set-verbosity",
        "set-tool-chatter",
        "set-speaker-mute",
        "set-stt",
        "set-tts",
        "set-voice",
        "set-mode",
        "st-conn",
        "st-bargein",
        "st-state",
        "toasts",
    ):
        assert f'id="{element_id}"' in html, f"region/control #{element_id} missing from index.html"
    assert 'type="file"' in html and "capture=" in html, "mobile camera input missing"


def test_style_has_touch_targets_safe_areas_and_schemes():
    css = _read("style.css")
    assert "safe-area-inset" in css
    assert re.search(r"--touch:\s*44px", css)
    assert "prefers-color-scheme: light" in css
    for state in ("idle", "working", "awaiting", "stalled", "detached"):
        assert f".state-{state}" in css, f"state colour for {state} missing"


# ---- protocol strings -------------------------------------------------------------------


def test_every_outbound_type_is_handled_by_the_client():
    text = _read("app.js") + _read("protocol.js")
    for t in P.OUTBOUND_TYPES:
        assert f"'{t}'" in text or f'"{t}"' in text, f"outbound type {t!r} not referenced by app.js/protocol.js"


def test_every_inbound_type_is_produced_by_the_client():
    text = _read("app.js") + _read("protocol.js") + _read("audio.js")
    for t in P.INBOUND_TYPES:
        assert f"'{t}'" in text or f'"{t}"' in text, f"inbound type {t!r} never built by the client"


def test_every_command_is_in_protocol_js():
    js = _read("protocol.js")
    m = re.search(r"var COMMANDS = \[(.*?)\];", js, re.S)
    assert m, "COMMANDS list missing from protocol.js"
    js_commands = re.findall(r"'([a-z_]+)'", m.group(1))
    assert tuple(js_commands) == P.COMMANDS, "protocol.js COMMANDS must equal protocol.py COMMANDS verbatim"


def test_client_commands_are_a_subset_of_the_closed_set():
    """Every cmd('name') call in app.js names a command the agent accepts."""
    used = set(re.findall(r"cmd\(\s*'([a-z_]+)'", _read("app.js")))
    assert used, "app.js sends no commands?"
    unknown = used - set(P.COMMANDS)
    assert not unknown, f"app.js sends commands outside the closed set: {sorted(unknown)}"


def test_prompt_cards_never_offer_permission_widening_buttons():
    app = _read("app.js")
    # The permission card has exactly two actions, approve and deny, and the plan card
    # uses the manual-approval command. There is no code path that answers an option
    # by its index for permission or plan prompts.
    perm = re.search(r"case 'permission':(.*?)break;", app, re.S)
    assert perm, "permission card branch missing"
    assert "cmd('approve'" in perm.group(1) and "cmd('deny'" in perm.group(1)
    assert "answer" not in perm.group(1)
    plan = re.search(r"case 'plan':(.*?)break;", app, re.S)
    assert plan, "plan card branch missing"
    assert "cmd('plan_approve'" in plan.group(1) and "cmd('plan_revise'" in plan.group(1)
    assert "cmd('plan_deny'" in plan.group(1)
    assert "isUnsafeOption" in app


def test_permission_mode_options_are_the_safe_list():
    js = _read("protocol.js")
    m = re.search(r"var PERMISSION_MODES = \[(.*?)\];", js, re.S)
    assert m
    modes = re.findall(r"'([A-Za-z]+)'", m.group(1))
    assert modes == ["default", "acceptEdits", "plan", "auto", "dontAsk"]


# ---- forbidden strings --------------------------------------------------------------------

FORBIDDEN = ("bypassPermissions", "dangerously")


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_no_permission_bypass_strings(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    for word in FORBIDDEN:
        assert word.lower() not in text.lower(), f"{path.name} mentions {word!r}"


# Model marketing names are spelled in pieces so this file does not itself contain them.
_MODEL_WORDS = "|".join(("fa" + "ble", "op" + "us", "son" + "net", "haiku"))
# The trailer and link markers are spelled in pieces for the same reason.
_MARKERS = "|".join(("co-" + "authored-by", r"claude\.ai/" + "code", "session_" + "01", r"@noreply\." + "anthropic"))
_ATTRIBUTION = re.compile(
    r"(?i)(?:generated|written|authored|created|made|built|co-?authored)\s+(?:by|with)\s+"
    r"(?:claude|anthropic|gpt|openai|copilot|gemini|an? (?:ai|llm|language model)|" + _MODEL_WORDS + r")"
    r"|" + _MARKERS
)


@pytest.mark.parametrize(
    "path",
    _web_files() + sorted(NODE_TESTS.glob("*.cjs")),
    ids=lambda p: p.name,
)
def test_no_author_attribution(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = [m.group(0) for m in _ATTRIBUTION.finditer(text)]
    assert not hits, f"{path.name} contains author attribution: {hits}"


# ---- audio invariants ---------------------------------------------------------------------


def test_audio_constants_match_the_wire_format():
    worklet = _read("worklet.js")
    audio = _read("audio.js")
    protocol = _read("protocol.js")
    assert "16000" in worklet and "320" in worklet
    assert re.search(r"CAPTURE_RATE = 16000", audio) and re.search(r"FRAME_SAMPLES = 320", audio)
    assert re.search(r"FRAME_SAMPLES = 320", protocol)
    assert "registerProcessor('pcm16-downsampler'" in worklet
    assert "'pcm16-downsampler'" in audio
    # Mic constraints from the design: echo cancellation on, mono.
    for c in ("echoCancellation: true", "noiseSuppression: true", "autoGainControl: true", "channelCount: 1"):
        assert c in audio, f"mic constraint {c} missing"
    # No forced sampleRate on the AudioContext (iOS): the device rate is used.
    assert not re.search(r"new Ctor\(\{[^}]*sampleRate", audio)
    # Barge-in: generation gate and flush ack.
    assert "lastFlushGeneration" in audio
    assert "flush_ack" in protocol
    assert "visibilitychange" in _read("app.js")


def test_websocket_url_and_auth_are_same_origin():
    app = _read("app.js")
    assert "location.protocol === 'https:' ? 'wss' : 'ws'" in app
    assert "location.host + '/ws'" in app
    assert "fetch('/auth'" in app and "credentials: 'same-origin'" in app
    assert "fetch('/upload'" in app
    # No absolute URLs to other hosts anywhere in the scripts.
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"https?://[^\s'\"]+", text):
            assert "w3.org" in m.group(0) or m.group(0).startswith("https://x."), (
                f"{path.name} contains an external URL: {m.group(0)}"
            )


def test_manifest_is_valid_and_relative():
    import json

    data = json.loads(_read("manifest.webmanifest"))
    assert data["name"] == "Zordon"
    assert data["start_url"].startswith(".")
    for icon in data["icons"]:
        assert not icon["src"].startswith(("http", "/")), icon
        assert (WEB / icon["src"]).is_file()


# ---- CSP: the client must stay free of inline script (script-src 'self') ---------------------


def test_index_has_no_inline_script_or_handlers():
    """SEC-13: ``build_csp`` sends ``script-src 'self'`` with no 'unsafe-inline'; the
    client must therefore never carry inline scripts, handler attributes or javascript: URLs."""
    html = _read("index.html")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I), "inline <script> block"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline on* handler attribute"
    assert "javascript:" not in html.lower()
    for name in ("app.js", "audio.js", "protocol.js"):
        js = _read(name)
        assert "setAttribute('style'" not in js and 'setAttribute("style"' not in js
        assert "innerHTML" not in js.replace("never innerHTML", "")
