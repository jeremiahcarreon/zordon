"""``zordon setup`` as a full-screen Textual app.

Same questions, same order and same trade-off text as the plain wizard in
:mod:`zordon.setup` (the option texts are parsed from its ``*_TEXT`` constants so
the two never drift), with one screen per step: welcome and detection, the five
questions, prerequisites with live install output, downloads with a progress bar,
and a summary card holding the token.

Nothing is written before the Downloads step: quitting earlier leaves the machine
as it was. Installs run only after a click on their Install button; commands that
need the terminal (sudo, the Ollama installer, logging in to an agent) run with the
app suspended so the real tty answers the prompts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Input, ProgressBar, Static

from zordon import __version__, assets, manifest, paths, prereqs, setup
from zordon.config import Config
from zordon.tui import ZORDON_THEME, TuiUnavailable
from zordon.tui.widgets import (
    BLUE,
    DIM,
    GREEN,
    RED,
    Choice,
    Chooser,
    Confirm,
    Hint,
    LogPanel,
    Panel,
    StepIndicator,
    ThreadWriter,
    TitleBar,
    streaming_runner,
    widget_id,
)

STEPS = ["Welcome", "Agent", "Speech", "Rewriter", "Routing", "Reach", "Prereqs", "Downloads", "Done"]

EXIT_OK = 0
EXIT_CANCELLED = 1
EXIT_PROBLEMS = 3
SERVE_REQUESTED = 100  # run_setup_tui's answer when the user chose "Start zordon serve now" and no ``serve`` callback was given

Runner = Callable[..., Any]


# ---- option text, parsed from the plain wizard ---------------------------------------------------

_OPTION = re.compile(r"^\s*\[(\d+)\]\s+(.+?)\s{2,}(\S.*)$")


def parse_options(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(heading, [(title, description), ...])`` from a ``setup.*_TEXT`` block.

    The heading is every line before the first ``[n]`` (the leading ``N.`` is dropped);
    each option's continuation lines are joined into one paragraph.
    """
    heading: list[str] = []
    options: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _OPTION.match(raw)
        if m:
            options.append((m.group(2).strip(), [m.group(3).strip()]))
        elif options:
            options[-1][1].append(raw.strip())
        else:
            heading.append(raw.strip())
    head = re.sub(r"^\d+\.\s*", "", " ".join(heading))
    return head, [(t, " ".join(d)) for t, d in options]


def _choices(text: str, keys: list[str], *, badges: dict[str, tuple[str, str]] | None = None) -> tuple[str, list[Choice]]:
    heading, opts = parse_options(text)
    out: list[Choice] = []
    for key, (title, desc) in zip(keys, opts, strict=True):
        badge, style = (badges or {}).get(key, ("", GREEN))
        if "(recommended" in title:
            title, _, rest = title.partition("(")
            title = title.strip()
            badge = badge or rest.rstrip(")")
            style = BLUE
        out.append(Choice(key, title, desc, badge, style))
    return heading, out


# ---- the app -----------------------------------------------------------------------------------


class SetupApp(App[str]):
    """The wizard. ``run()`` returns ``"serve"``, ``"done"`` or ``"cancelled"``."""

    CSS_PATH = "theme.tcss"
    TITLE = "Zordon setup"
    BINDINGS = [
        Binding("ctrl+c", "request_quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "request_quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, config_path: Path | None = None, *, runner: Runner | None = None, do_actions: bool = True) -> None:
        super().__init__()
        self.config_path = config_path
        self.runner = runner  # None: real subprocesses (streamed or under suspend); tests pass a fake
        self.do_actions = do_actions
        self.started = False
        self.saved = False
        self.cfg: Config | None = None
        self.detected: setup.Detected | None = None
        self.choices: setup.Choices | None = None
        self.env: prereqs.Environment | None = None
        self.tts_override: str | None = None
        self.still_missing: list[str] = []
        self.problems: list[str] = []

    def on_mount(self) -> None:
        self.register_theme(ZORDON_THEME)
        self.theme = "zordon"
        self.started = True
        # Load without creating: nothing lands on disk until the Downloads step.
        path = self.config_path or paths.config_path()
        if path.exists():
            self.cfg = Config.load(path)
        else:
            self.cfg = Config.default()
            self.cfg.path = path
        self.detected = setup.detect(self.cfg.providers.ollama_url)
        self.choices = setup.recommend(self.detected)
        self.push_screen(WelcomeScreen())

    # -- navigation --

    def advance(self, from_step: int) -> None:
        nxt = from_step + 1
        if nxt == STEPS.index("Prereqs") and not self.do_actions:
            nxt = STEPS.index("Done")
        if nxt == STEPS.index("Done"):
            self.save_config()
        screen = {
            "Agent": AgentScreen,
            "Speech": SpeechScreen,
            "Rewriter": NormalizerScreen,
            "Routing": RouterScreen,
            "Reach": AccessScreen,
            "Prereqs": PrereqScreen,
            "Downloads": DownloadScreen,
            "Done": DoneScreen,
        }[STEPS[nxt]]
        self.push_screen(screen())

    def back(self) -> None:
        if len(self.screen_stack) > 2:  # default screen + welcome stay
            self.pop_screen()

    def action_request_quit(self) -> None:
        if isinstance(self.screen, Confirm):
            return
        body = (
            "config.toml has already been written; re-run `zordon setup` to change it."
            if self.saved
            else "Nothing has been written yet; the machine stays as it is."
        )
        self.push_screen(Confirm("Quit setup?", body, yes="Quit", no="Stay"), self._quit_answer)

    def _quit_answer(self, yes: bool | None) -> None:
        if yes:
            self.exit("cancelled", return_code=EXIT_CANCELLED)

    # -- work shared by screens --

    def save_config(self) -> None:
        assert self.cfg is not None and self.choices is not None
        if self.saved:
            return
        setup.apply(self.choices, self.cfg)
        if self.tts_override:
            self.cfg.providers.tts = self.tts_override
        self.cfg.save()
        self.saved = True
        if os.environ.get("ZORDON_INSTALLED_UV"):
            try:
                manifest.record("uv", "uv", command="install.sh", removal=os.environ.get("UV_INSTALL_DIR", "") or str(Path.home() / ".local" / "share" / "uv"))
            except OSError:
                pass

    def serve_argv(self) -> list[str]:
        assert self.choices is not None
        c = self.choices
        return ["--tunnel"] if c.access == "tunnel" else (["--bind", "tailscale"] if c.access == "tailscale" else [])


# ---- screens -------------------------------------------------------------------------------------


class WizardScreen(Screen):
    """Title bar, step indicator, body, hint; ``q`` asks to quit, Esc goes back."""

    STEP = 0
    HINT = "Esc back · q quit"
    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("q", "request_quit", "Quit", show=False),
    ]

    @property
    def wizard(self) -> SetupApp:
        app = self.app
        assert isinstance(app, SetupApp)
        return app

    def compose(self) -> ComposeResult:
        yield TitleBar(STEPS[self.STEP], right=f"zordon {__version__}")
        yield StepIndicator(STEPS, self.STEP)
        with VerticalScroll(classes="screen-body"):
            yield from self.body()
        with Horizontal(classes="row-buttons"):
            yield from self.buttons()
        yield Hint(self.HINT)

    def body(self) -> ComposeResult:  # pragma: no cover - overridden
        yield from ()

    def buttons(self) -> ComposeResult:
        yield Button("Back", id="back")
        yield Button("Next", id="next", variant="primary")

    def action_back(self) -> None:
        self.wizard.back()

    def action_request_quit(self) -> None:
        self.wizard.action_request_quit()

    @on(Button.Pressed, "#back")
    def _back_pressed(self) -> None:
        self.wizard.back()


class WelcomeScreen(WizardScreen):
    STEP = 0
    HINT = "Enter continue · q quit"
    BINDINGS = [Binding("escape", "request_quit", "Quit", show=False), Binding("q", "request_quit", "Quit", show=False)]

    def body(self) -> ComposeResult:
        d = self.wizard.detected
        assert d is not None
        banner = Text()
        banner.append("Talk to your coding agent. Hear it back. Interrupt it.\n", style="bold")
        banner.append(
            "A few questions, each with the trade-offs spelled out, then the downloads. "
            "Nothing is written or installed until you say so.",
            style=DIM,
        )
        yield Static(banner, classes="banner")
        yield Panel("Found on this machine", Static(self._table(d)))
        installed = {k for k, v in d.agents.items() if v and k != "generic"}
        if not installed:
            yield Static(
                Text("No coding agent found. Pick the one you want next; the prerequisites step offers to install it.", style=RED),
                classes="muted",
            )

    def buttons(self) -> ComposeResult:
        yield Button("Quit", id="quit")
        yield Button("Continue", id="next", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#next", Button).focus()

    @staticmethod
    def _table(d: setup.Detected) -> Table:
        def mark(ok: bool, text: str, *, warn: bool = False) -> Text:
            if ok:
                return Text("✓ ", style=GREEN) + Text(text)
            return Text("✗ " if not warn else "– ", style=RED if not warn else DIM) + Text(text, style=DIM)

        pm, _ = prereqs.detect_package_manager()
        keys = [n for n, present in (("ANTHROPIC_API_KEY", d.anthropic_key_env), ("TYPESAFE_API_KEY", d.typesafe_key_env), ("OPENAI_API_KEY", d.openai_key_env), ("ELEVENLABS_API_KEY", d.elevenlabs_key_env)) if present]
        t = Table.grid(padding=(0, 2))
        t.add_column(style=DIM, no_wrap=True)
        t.add_column()
        t.add_row("tmux", mark(bool(d.tmux), d.tmux or "missing (the prerequisites step installs it)"))
        t.add_row("Claude Code", mark(bool(d.claude), d.claude or "not installed"))
        t.add_row("Codex", mark(bool(d.agents.get("codex")), d.agents.get("codex") or "not installed"))
        ollama = "server running" + (f", {len(d.ollama_models)} model(s)" if d.ollama_models else "") if d.ollama_server else (d.ollama_binary or "not installed")
        t.add_row("Ollama", mark(d.ollama_server or bool(d.ollama_binary), ollama))
        t.add_row("GPU", mark(bool(d.gpu), d.gpu or "none detected", warn=True))
        t.add_row("Package manager", mark(bool(pm), pm or "none known (apt, dnf, pacman, zypper, apk, brew)", warn=True))
        t.add_row("Keys in env", mark(bool(keys), ", ".join(keys) or "none", warn=True))
        t.add_row("Models", mark(bool(d.models_present), ", ".join(d.models_present) or "none downloaded yet", warn=True))
        t.add_row("Python", mark(True, d.python))
        return t

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self.wizard.advance(self.STEP)

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.wizard.action_request_quit()


class QuestionScreen(WizardScreen):
    """One question: a heading, option cards, a detail panel for the selected option, Back/Next."""

    HINT = "↑↓ move · 1-9 jump · Enter choose · click twice to choose · Esc back · q quit"

    def heading(self) -> str:  # pragma: no cover - overridden
        return ""

    def choices(self) -> list[Choice]:  # pragma: no cover - overridden
        return []

    def current(self) -> str:  # pragma: no cover - overridden
        return ""

    def detail(self, key: str) -> ComposeResult:
        yield from ()

    def commit(self, key: str) -> None:  # pragma: no cover - overridden
        pass

    def body(self) -> ComposeResult:
        yield Static(self.heading(), classes="question")
        yield Chooser(self.choices(), self.current(), id="choices")
        yield Vertical(id="detail")

    def on_mount(self) -> None:
        chooser = self.query_one(Chooser)
        chooser.focus()
        self._show_detail(chooser.value)

    def _show_detail(self, key: str) -> None:
        box = self.query_one("#detail", Vertical)
        box.remove_children()
        widgets = list(self.detail(key))
        if widgets:
            box.mount(Panel("Details", *widgets, classes="detail"))

    @on(Chooser.Changed)
    def _changed(self, event: Chooser.Changed) -> None:
        self._show_detail(event.key)

    @on(Chooser.Committed)
    def _committed(self, event: Chooser.Committed) -> None:
        self._go(event.key)

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self._go(self.query_one(Chooser).value)

    @on(Input.Submitted)
    def _input_done(self) -> None:
        self._go(self.query_one(Chooser).value)

    def _go(self, key: str) -> None:
        self.commit(key)
        self.wizard.advance(self.STEP)

    # helpers for subclasses
    def key_input(self, name: str, env: str, present: bool, *, id: str, current: str = "") -> ComposeResult:
        if present:
            yield Static(Text(f"✓ {env} is set in your environment; it stays there.", style=GREEN))
        else:
            yield Static(Text(f"{name} API key", style="bold") + Text("  stored 0600 in config.toml; blank to add later", style=DIM))
            yield Input(value=current, placeholder=f"paste your {name} key", password=True, compact=True, id=id)

    def input_value(self, id: str) -> str:
        try:
            return self.query_one(f"#{id}", Input).value.strip()
        except Exception:  # noqa: BLE001 - the input is not on screen for this option
            return ""

    def checkbox_value(self, id: str) -> bool:
        try:
            return self.query_one(f"#{id}", Checkbox).value
        except Exception:  # noqa: BLE001
            return False


class AgentScreen(QuestionScreen):
    STEP = 1

    def _keys(self) -> list[str]:
        d = self.wizard.detected
        assert d is not None
        return [k for k in ("claude-code", "codex", "generic") if k in d.agents or k == "generic"]

    def heading(self) -> str:
        return setup.AGENT_INTRO.strip().split(". ", 1)[1]

    def choices(self) -> list[Choice]:
        d = self.wizard.detected
        assert d is not None
        installed = {k for k, v in d.agents.items() if v}
        out = []
        for k in self._keys():
            title, _, desc = re.split(r"(\s{2,})", setup.AGENT_LINES[k], maxsplit=1)
            desc = " ".join(desc.split())
            if k == "generic":
                badge, style = "", GREEN
            elif k in installed:
                badge, style = "installed", GREEN
            else:
                badge, style = "not installed", RED
            out.append(Choice(k, title.strip(), desc, badge, style))
        return out

    def current(self) -> str:
        c = self.wizard.choices
        assert c is not None
        return c.agent if c.agent in self._keys() else self._keys()[0]

    def detail(self, key: str) -> ComposeResult:
        d = self.wizard.detected
        assert d is not None
        if key == "generic":
            yield Static("After `zordon serve`, use Attach in the web page with the pane target shown by `tmux list-panes -a`.")
        elif not d.agents.get(key):
            yield Static(Text(f"{key} is not installed yet; the prerequisites step will offer to install it.", style=DIM))
            yield Static(Text(f"By hand: {setup.INSTALL_LINES.get(key, '')}", style=DIM))

    def commit(self, key: str) -> None:
        c = self.wizard.choices
        assert c is not None
        c.agent = key


class SpeechScreen(QuestionScreen):
    STEP = 2
    KEYS = ["local", "cloud", "later"]

    def heading(self) -> str:
        return _choices(setup.SPEECH_TEXT, self.KEYS)[0]

    def choices(self) -> list[Choice]:
        return _choices(setup.SPEECH_TEXT, self.KEYS)[1]

    def current(self) -> str:
        c = self.wizard.choices
        assert c is not None
        return c.speech

    def detail(self, key: str) -> ComposeResult:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        if key == "local":
            have = len(d.models_present)
            yield Static(Text(f"{have} of 4 model files already present; " + ("nothing to download." if have >= 4 else "the rest is fetched in the Downloads step."), style=DIM))
        elif key == "cloud":
            yield from self.key_input("OpenAI", "OPENAI_API_KEY", d.openai_key_env, id="key-openai", current=c.keys.get("openai", ""))
            if d.elevenlabs_key_env:
                yield Checkbox("Use ElevenLabs for the voice instead of OpenAI (ELEVENLABS_API_KEY is set)", value=False, id="use-elevenlabs")
            else:
                yield Static(Text("ElevenLabs API key", style="bold") + Text("  optional: a filled key means ElevenLabs speaks instead of OpenAI", style=DIM))
                yield Input(value=c.keys.get("elevenlabs", ""), placeholder="paste your ElevenLabs key, or leave blank", password=True, compact=True, id="key-elevenlabs")
            yield Static(Text("Groq API key", style="bold") + Text("  optional: a filled key means Groq transcribes (faster than OpenAI)", style=DIM))
            yield Input(value=c.keys.get("groq", ""), placeholder="paste your Groq key, or leave blank", password=True, compact=True, id="key-groq")
        else:
            yield Static(Text("Text in the browser still works; `zordon setup` adds speech later.", style=DIM))

    def commit(self, key: str) -> None:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        c.speech = key
        if key == "cloud":
            c.keys["openai"] = self.input_value("key-openai")
            c.keys["elevenlabs"] = self.input_value("key-elevenlabs")
            c.keys["groq"] = self.input_value("key-groq")
            self.wizard.tts_override = "elevenlabs" if (d.elevenlabs_key_env and self.checkbox_value("use-elevenlabs")) else None
        c.download_models = key == "local" and len(d.models_present) < 4


class NormalizerScreen(QuestionScreen):
    STEP = 3
    KEYS = ["ollama", "anthropic", "claude-cli", "passthrough"]
    MODEL_14B = "qwen2.5:14b-instruct"

    def heading(self) -> str:
        return _choices(setup.NORMALIZER_TEXT, self.KEYS)[0]

    def choices(self) -> list[Choice]:
        d = self.wizard.detected
        assert d is not None
        badges: dict[str, tuple[str, str]] = {}
        if d.ollama_server:
            badges["ollama"] = ("Ollama running", GREEN)
        elif d.ollama_binary:
            badges["ollama"] = ("Ollama installed", GREEN)
        else:
            badges["ollama"] = ("Ollama not installed", RED)
        if d.anthropic_key_env:
            badges["anthropic"] = ("key in env", GREEN)
        badges["claude-cli"] = ("claude found", GREEN) if d.claude else ("claude not on PATH", RED)
        return _choices(setup.NORMALIZER_TEXT, self.KEYS, badges=badges)[1]

    def current(self) -> str:
        c = self.wizard.choices
        assert c is not None
        return c.normalizer

    def detail(self, key: str) -> ComposeResult:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        if key == "ollama":
            if not d.ollama_binary and not d.ollama_server:
                yield Static(Text("Ollama is not installed; the prerequisites step will offer to install it.", style=DIM))
            have = any(m.startswith("qwen2.5:3b-instruct") for m in d.ollama_models)
            yield Static(Text("qwen2.5:3b-instruct is already pulled." if have else "qwen2.5:3b-instruct (2 GB) is pulled in the Downloads step.", style=DIM))
            if d.gpu:
                yield Checkbox(f"GPU detected ({d.gpu}). Use the larger {self.MODEL_14B} (9 GB, better wording)", value=c.ollama_model == self.MODEL_14B, id="use-14b")
        elif key == "anthropic":
            yield from self.key_input("Anthropic", "ANTHROPIC_API_KEY", d.anthropic_key_env, id="key-anthropic", current=c.keys.get("anthropic", ""))
        elif key == "claude-cli" and not d.claude:
            yield Static(Text("`claude` is not on PATH; install Claude Code and log in first.", style=RED))

    def commit(self, key: str) -> None:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        c.normalizer = key
        if key == "ollama":
            c.ollama_model = self.MODEL_14B if (d.gpu and self.checkbox_value("use-14b")) else "qwen2.5:3b-instruct"
            c.pull_ollama_model = not any(m.startswith(c.ollama_model) for m in d.ollama_models)
        else:
            c.pull_ollama_model = False
        if key == "anthropic":
            c.keys["anthropic"] = self.input_value("key-anthropic")


class RouterScreen(QuestionScreen):
    STEP = 4
    KEYS = ["keyword", "jev", "anthropic"]

    def heading(self) -> str:
        return _choices(setup.ROUTER_TEXT, self.KEYS)[0]

    def choices(self) -> list[Choice]:
        d = self.wizard.detected
        assert d is not None
        badges: dict[str, tuple[str, str]] = {}
        if d.typesafe_key_env:
            badges["jev"] = ("key in env", GREEN)
        if d.anthropic_key_env:
            badges["anthropic"] = ("key in env", GREEN)
        return _choices(setup.ROUTER_TEXT, self.KEYS, badges=badges)[1]

    def current(self) -> str:
        c = self.wizard.choices
        assert c is not None
        return c.router

    def detail(self, key: str) -> ComposeResult:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        if key == "jev":
            yield from self.key_input("TypeSafe", "TYPESAFE_API_KEY", d.typesafe_key_env, id="key-typesafe", current=c.keys.get("typesafe", ""))
        elif key == "anthropic":
            if c.keys.get("anthropic"):
                yield Static(Text("✓ Using the Anthropic key you entered for the rewriter.", style=GREEN))
            else:
                yield from self.key_input("Anthropic", "ANTHROPIC_API_KEY", d.anthropic_key_env, id="key-anthropic")

    def commit(self, key: str) -> None:
        c = self.wizard.choices
        assert c is not None
        c.router = key
        if key == "jev":
            c.keys["typesafe"] = self.input_value("key-typesafe")
        if key == "anthropic" and not c.keys.get("anthropic"):
            c.keys["anthropic"] = self.input_value("key-anthropic")


class AccessScreen(QuestionScreen):
    STEP = 5
    KEYS = ["local", "tunnel", "tailscale", "lan"]

    def heading(self) -> str:
        return _choices(setup.ACCESS_TEXT, self.KEYS)[0]

    def choices(self) -> list[Choice]:
        d = self.wizard.detected
        assert d is not None
        badges: dict[str, tuple[str, str]] = {}
        if d.cloudflared:
            badges["tunnel"] = ("cloudflared found", GREEN)
        badges["tailscale"] = ("tailscale found", GREEN) if d.tailscale else ("tailscale not on PATH", RED)
        return _choices(setup.ACCESS_TEXT, self.KEYS, badges=badges)[1]

    def current(self) -> str:
        c = self.wizard.choices
        assert c is not None
        return c.access

    def detail(self, key: str) -> ComposeResult:
        d = self.wizard.detected
        assert d is not None
        if key == "tunnel":
            yield Static(Text(f"cloudflared found at {d.cloudflared}." if d.cloudflared else "cloudflared (about 40 MB) is downloaded in the Downloads step.", style=DIM))
        elif key == "tailscale" and not d.tailscale:
            yield Static(Text("Install Tailscale first (tailscale.com/download); `zordon serve --bind tailscale` needs its CLI.", style=RED))
        elif key == "lan":
            yield Static(Text("Phones get the page but not the microphone over plain http; the tunnel is the way to a phone mic.", style=DIM))

    def commit(self, key: str) -> None:
        d, c = self.wizard.detected, self.wizard.choices
        assert d is not None and c is not None
        c.access = key
        c.download_cloudflared = key == "tunnel" and not d.cloudflared


# ---- prerequisites -------------------------------------------------------------------------------


class SudoPassword(ModalScreen[str | None]):
    """Ask for the sudo password once; the TUI primes sudo so later steps never prompt."""

    DEFAULT_CSS = """
    SudoPassword { align: center middle; background: $background 60%; }
    SudoPassword > Vertical { width: 70; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    SudoPassword .sudo--title { text-style: bold; }
    SudoPassword .sudo--body { margin: 1 0; color: $text-muted; }
    SudoPassword .sudo--error { color: $error; }
    SudoPassword Horizontal { height: auto; align-horizontal: right; }
    SudoPassword Button { margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, user: str, commands: list[str], error: str = "") -> None:
        super().__init__()
        self._user = user
        self._commands = commands
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Administrator password for {self._user}", style="bold"), classes="sudo--title")
            body = Text("These steps need sudo. Zordon asks once, here, and runs them in order:\n", style=DIM)
            for c in self._commands:
                body.append(f"  $ {c}\n", style=BLUE)
            body.append("The password goes to sudo only and is not stored.", style=DIM)
            yield Static(body, classes="sudo--body")
            if self._error:
                yield Static(Text(self._error, style=RED), classes="sudo--error")
            yield Input(password=True, placeholder="password", id="sudo-input")
            with Horizontal():
                yield Button("Cancel", id="sudo-cancel")
                yield Button("Continue", id="sudo-ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#sudo-input", Input).focus()

    @on(Input.Submitted, "#sudo-input")
    @on(Button.Pressed, "#sudo-ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#sudo-input", Input).value)

    @on(Button.Pressed, "#sudo-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


def prime_sudo(password: str, *, run: Callable[..., Any] = subprocess.run) -> bool:
    """Validate the password and start sudo's credential timestamp (default 15 minutes),
    so the streamed steps' own ``sudo`` calls succeed without a terminal."""
    if not shutil.which("sudo"):
        return False  # the caller checks sudo_missing() first and explains; this is the backstop
    try:
        res = run(["sudo", "-S", "-k", "-v", "-p", ""], input=password + "\n", capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(res, "returncode", 1) == 0


class PrereqRow(Vertical):
    """One missing prerequisite: why, command, and a status line updated as the batch runs."""

    def __init__(self, prereq: prereqs.Prereq) -> None:
        super().__init__(classes="prereq", id=f"prereq-{widget_id(prereq.key)}")
        self.prereq = prereq
        self.state = "pending"  # pending | running | done | failed | skipped

    def compose(self) -> ComposeResult:
        p = self.prereq
        head = Text(p.label, style="bold")
        head.append("  required", style=RED if p.required else DIM)
        yield Static(head, classes="prereq--head")
        yield Static(Text(p.why + ".", style=DIM))
        if p.detail:
            yield Static(Text(p.detail, style=DIM))
        if not p.command:
            yield Static(Text("No install command known for this system; install it by hand.", style=RED))
        yield Static("", id=f"status-{widget_id(p.key)}", classes="prereq--status")

    def set_state(self, state: str, message: str) -> None:
        self.state = state
        style = {"done": GREEN, "failed": RED, "running": BLUE, "skipped": DIM}.get(state, "")
        mark = {"done": "✓ ", "failed": "✗ ", "running": "… ", "skipped": "– "}.get(state, "")
        self.query_one(".prereq--status", Static).update(Text(mark + message, style=style))
        self.set_class(state == "done", "-done")
        self.set_class(state == "failed", "-failed")


class PrereqScreen(WizardScreen):
    STEP = 6
    HINT = "Enter installs everything in order · Esc back · q quit"

    def body(self) -> ComposeResult:
        c = self.wizard.choices
        assert c is not None
        want_agents = (c.agent,) if c.agent != "generic" else ()
        env = self.wizard.env = prereqs.detect(want_agents=want_agents, want_ollama=(c.normalizer == "ollama"))
        self.missing = env.missing(required_only=True)
        self.steps = prereqs.plan_steps(env, self.missing)
        self.results: dict[str, tuple[bool, str]] = {}
        if not self.missing:
            yield Static(Text("✓ Everything your choices need is already installed.", style=f"bold {GREEN}"), classes="question")
            return
        intro = Text("Missing for your choices. ", style="bold")
        intro.append(
            f"Zordon installs them in dependency order as {len(self.steps)} command{'s' if len(self.steps) != 1 else ''}; "
            f"sudo is asked for once. Package manager: {env.package_manager or 'none known; adapt the commands shown'}.",
            style=DIM,
        )
        yield Static(intro, classes="question")
        with Vertical(id="prereq-list"):
            for p in self.missing:
                yield PrereqRow(p)
        plan = Text("Plan\n", style="bold")
        for i, st in enumerate(self.steps, 1):
            plan.append(f"  {i}. ", style=DIM)
            plan.append(st.command + "\n", style=BLUE)
        unplanned = [p for p in self.missing if not any(p.key in st.keys for st in self.steps)]
        for p in unplanned:
            plan.append(f"  ✗ {p.label}: no install command for this system\n", style=RED)
        yield Static(plan, id="prereq-plan")
        yield LogPanel("Install output", id="log")

    def buttons(self) -> ComposeResult:
        yield Button("Back", id="back")
        if getattr(self, "missing", None):
            yield Button("Skip", id="skip-all")
            yield Button("Install all prerequisites", id="install-all", variant="primary")
        yield Button("Continue", id="next", variant="primary", classes="hidden" if getattr(self, "missing", None) else "")

    def on_mount(self) -> None:
        if getattr(self, "missing", None):
            self.query_one("#install-all", Button).focus()
        else:
            self.query_one("#next", Button).focus()

    # -- the batch --

    @on(Button.Pressed, "#install-all")
    def _install_all(self) -> None:
        if not self.steps:
            self._finish_batch()
            return
        needs_sudo = any(st.terminal for st in self.steps) and not prereqs.is_root() and self.wizard.runner is None
        if needs_sudo and prereqs.sudo_missing():
            # No sudo binary: no password can work. Say what root has to do instead of
            # pretending the password was wrong.
            log = self.query_one(LogPanel)
            log.write("✗ sudo is not installed on this machine, so these steps cannot run as you.", style=RED)
            log.write("  " + prereqs.SUDO_FIX.format(user=prereqs.current_user()), style=DIM)
            self.notify("sudo is not installed; see the log for what to run as root.", severity="error", timeout=12)
            return
        if needs_sudo:
            self._ask_sudo([st.command for st in self.steps if st.terminal])
        else:
            self._start_batch()

    def _ask_sudo(self, commands: list[str], error: str = "") -> None:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "you"

        def got(pw: str | None) -> None:
            if pw is None:
                return  # cancelled: stay on the screen, nothing ran
            log = self.query_one(LogPanel)
            log.write("$ sudo -v  (checking the password)", style=BLUE)
            if prime_sudo(pw):
                log.write("✓ sudo ready for the next 15 minutes", style=GREEN)
                self._start_batch()
            else:
                log.write("✗ sudo did not accept that password", style=RED)
                self._ask_sudo(commands, error="That password was not accepted. Try again, or Cancel.")

        self.app.push_screen(SudoPassword(user, commands, error), got)

    def _start_batch(self) -> None:
        self.query_one("#install-all", Button).disabled = True
        self.query_one("#skip-all", Button).disabled = True
        self.query_one("#back", Button).disabled = True
        for row in self.query(PrereqRow):
            if any(row.prereq.key in st.keys for st in self.steps):
                row.set_state("pending", "queued")
        log = self.query_one(LogPanel)
        log.start(f"running {len(self.steps)} step{'s' if len(self.steps) != 1 else ''}")
        self._run_batch(log)

    @work(thread=True)
    def _run_batch(self, log: LogPanel) -> None:
        assert self.wizard.env is not None
        runner = self.wizard.runner or streaming_runner(log.write_from_thread)

        def on_step(st: prereqs.Step) -> None:
            self.app.call_from_thread(self._step_started, st)

        batch = [st for st in self.steps if st.kind != "login"]  # sign-in is interactive: offered after
        results = prereqs.run_steps(batch, self.wizard.env, run=runner, log=lambda line: log.write_from_thread(line), on_step=on_step)
        self.app.call_from_thread(self._batch_done, results)

    def _step_started(self, st: prereqs.Step) -> None:
        for row in self.query(PrereqRow):
            if row.prereq.key in st.keys:
                row.set_state("running", f"installing: {st.command}")

    def _batch_done(self, results: dict[str, tuple[bool, str]]) -> None:
        self.results = results
        log = self.query_one(LogPanel)
        ok_all = True
        for row in self.query(PrereqRow):
            res = results.get(row.prereq.key)
            if res is None:
                continue
            ok, msg = res
            if ok:
                try:
                    manifest.record("system", row.prereq.key, command=row.prereq.command or "", note=row.prereq.label)
                except OSError:
                    pass
                row.set_state("done", msg + (f". Next: {row.prereq.after}" if row.prereq.after else ""))
            else:
                ok_all = False
                row.set_state("failed", msg)
        log.finish("all prerequisites installed" if ok_all else "some steps failed; see above", ok=ok_all)
        self._finish_batch()

    def _finish_batch(self) -> None:
        nxt = self.query_one("#next", Button)
        nxt.remove_class("hidden")
        pending = [r for r in self.query(PrereqRow) if r.state != "done"]
        nxt.label = "Continue anyway" if pending else "Continue"
        self.query_one("#back", Button).disabled = False
        needs_login = [
            r for r in self.query(PrereqRow)
            if (r.state == "done" and prereqs.login_command(r.prereq.key)) or (r.prereq.key == "claude-login" and r.state not in ("done", "skipped"))
        ]
        if needs_login and self.wizard.runner is None:
            self._offer_login(needs_login[0])
        else:
            for r in needs_login:
                if self.wizard.runner is not None and r.prereq.key == "claude-login":
                    prereqs.open_for_login("claude-code", run=self.wizard.runner)
                    r.set_state("done", "signed in")
            nxt.focus()

    def _offer_login(self, row: PrereqRow) -> None:
        p = row.prereq
        agent_key = "claude-code" if p.key == "claude-login" else p.key
        label = "Claude Code" if p.key == "claude-login" else p.label

        def answer(yes: bool) -> None:
            if yes:
                with self.app.suspend():
                    print(f"\n  ◆ Opening {label} so you can log in. Exit it when done.\n", flush=True)
                    prereqs.open_for_login(agent_key, run=subprocess.run)
                if p.key == "claude-login":
                    row.set_state("done", "signed in")
            self.query_one("#next", Button).focus()

        self.app.push_screen(
            Confirm(f"Log in to {label} now?", f"{label} is installed. It opens in this terminal; exit it when you are done and setup resumes.", yes="Open it", no="Later"),
            answer,
        )

    @on(Button.Pressed, "#skip-all")
    def _skip_all(self) -> None:
        for row in self.query(PrereqRow):
            if row.state == "pending":
                row.set_state("skipped", "skipped; the command is listed under Still to do")
        self._finish_batch()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        still: list[str] = []
        for row in self.query(PrereqRow):
            p = row.prereq
            if row.state == "done":
                continue
            if not p.command:
                still.append(f"{p.label}: install it by hand ({p.detail or 'no command known for this system'})")
            else:
                still.append(f"{p.label}: {p.command}" + (f"; then {p.after}" if p.after else ""))
        self.wizard.still_missing = still
        self.wizard.advance(self.STEP)


# ---- downloads -------------------------------------------------------------------------------------


class DownloadScreen(WizardScreen):
    STEP = 7
    HINT = "Downloads run in the background · q quit (config is already written)"
    BINDINGS = [Binding("escape", "request_quit", "Quit", show=False), Binding("q", "request_quit", "Quit", show=False)]

    def body(self) -> ComposeResult:
        c = self.wizard.choices
        assert c is not None
        todo = []
        if c.normalizer == "ollama" and c.pull_ollama_model:
            todo.append(f"pull {c.ollama_model} with Ollama")
        if c.download_models and c.speech == "local":
            todo.append("download the local speech models (about 820 MB)")
        if c.download_cloudflared:
            todo.append("download cloudflared")
        head = Text("Writing config.toml", style="bold")
        head.append(" and then: " + ("; ".join(todo) if todo else "nothing else to fetch") + ".", style=DIM)
        yield Static(head, classes="question")
        yield Static("", id="progress-label", classes="muted")
        yield ProgressBar(total=None, show_eta=False, id="progress")
        yield LogPanel("Progress", id="log")

    def buttons(self) -> ComposeResult:
        yield Button("Continue", id="next", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.wizard.save_config()
        log = self.query_one(LogPanel)
        log.write(f"✓ config written to {self.wizard.cfg.path} (mode 0600)", style=GREEN)  # type: ignore[union-attr]
        log.start("working...")
        self.begin_capture_print(stdout=False, stderr=True)
        self._run(log)

    def on_print(self, event: events.Print) -> None:
        text = event.text.strip()
        if text:
            self.query_one(LogPanel).write(text)

    @work(thread=True)
    def _run(self, log: LogPanel) -> None:
        app = self.wizard
        c, cfg = app.choices, app.cfg
        assert c is not None and cfg is not None
        out = ThreadWriter(log.write_from_thread)
        # Re-detect: installs on the previous screen may have changed the picture.
        d = setup.detect(cfg.providers.ollama_url)
        if c.normalizer == "ollama":
            c.pull_ollama_model = not any(m.startswith(c.ollama_model) for m in d.ollama_models)

        def downloader(asset: assets.Asset) -> Path:
            app.call_from_thread(self._progress, asset.filename, 0, asset.size)
            return assets.download(asset, progress=lambda done, total: app.call_from_thread(self._progress, asset.filename, done, total))

        runner = app.runner or streaming_runner(log.write_from_thread)
        try:
            problems = setup.run_actions(c, cfg, out, runner=runner, downloader=downloader)
        except Exception as e:  # noqa: BLE001 - report, never crash the screen
            problems = [f"actions failed: {e}"]
        out.flush()
        app.call_from_thread(self._finished, problems)

    def _progress(self, name: str, done: int, total: int | None) -> None:
        bar = self.query_one(ProgressBar)
        label = self.query_one("#progress-label", Static)
        if total:
            bar.update(total=total, progress=done)
            label.update(Text(f"{name}  {done / 1e6:.0f} / {total / 1e6:.0f} MB", style=DIM))
        else:
            label.update(Text(f"{name}  {done / 1e6:.0f} MB", style=DIM))

    def _finished(self, problems: list[str]) -> None:
        self.end_capture_print()
        app = self.wizard
        app.problems = problems
        bar = self.query_one(ProgressBar)
        bar.update(total=1, progress=1)
        log = self.query_one(LogPanel)
        if problems:
            log.finish(f"finished with {len(problems)} problem(s)", ok=False)
            for p in problems:
                log.write(f"✗ {p}", style=RED)
        else:
            log.finish("all done", ok=True)
        self.query_one("#progress-label", Static).update(Text("done", style=GREEN))
        nxt = self.query_one("#next", Button)
        nxt.disabled = False
        nxt.focus()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self.wizard.advance(self.STEP)


# ---- done ------------------------------------------------------------------------------------------


class DoneScreen(WizardScreen):
    STEP = 8
    HINT = "Enter press the focused button · q exit"
    BINDINGS = [Binding("escape", "exit_done", "Exit", show=False), Binding("q", "exit_done", "Exit", show=False)]

    def body(self) -> ComposeResult:
        app = self.wizard
        c, cfg = app.choices, app.cfg
        assert c is not None and cfg is not None
        app.save_config()
        card = Text()
        card.append("Setup complete\n\n", style=f"bold {GREEN}")
        for line in setup.next_steps(c, cfg).strip().splitlines():
            if line.startswith("Your session token is:"):
                card.append("Your session token is:  ", style="bold")
                card.append(cfg.server.token or "", style=f"bold {BLUE}")
            elif line.startswith("Start with:"):
                card.append("Start with:  ", style="bold")
                card.append(line.split(":", 1)[1].strip(), style=BLUE)
            else:
                card.append(line, style="" if line else DIM)
            card.append("\n")
        yield Static(card, classes="summary-card")
        still = app.still_missing + app.problems
        if still:
            yield Panel("Still to do", *[Static(Text("• " + s, style=RED)) for s in still])

    def buttons(self) -> ComposeResult:
        yield Button("Exit", id="exit")
        yield Button("Start zordon serve now", id="serve", variant="success")

    def on_mount(self) -> None:
        self.query_one("#serve", Button).focus()

    def action_exit_done(self) -> None:
        self.app.exit("done", return_code=EXIT_PROBLEMS if (self.wizard.problems or self.wizard.still_missing) else EXIT_OK)

    @on(Button.Pressed, "#exit")
    def _exit(self) -> None:
        self.action_exit_done()

    @on(Button.Pressed, "#serve")
    def _serve(self) -> None:
        self.app.exit("serve", return_code=EXIT_OK)


# ---- entry point -----------------------------------------------------------------------------------


def run_setup_tui(
    config_path: Path | None = None,
    *,
    do_actions: bool = True,
    serve: Callable[[list[str]], int] | None = None,
) -> int:
    """Run the full-screen setup. Returns an exit code.

    ``serve`` is called (after the terminal is restored) with the extra ``zordon serve``
    arguments when the user presses "Start zordon serve now"; without it that choice
    returns :data:`SERVE_REQUESTED`. Raises :class:`TuiUnavailable` when the app could
    not start, so the caller can fall back to the plain wizard.
    """
    if os.environ.get("TERM", "") in ("", "dumb"):
        raise TuiUnavailable("TERM is not set or is 'dumb'")
    app = SetupApp(config_path, do_actions=do_actions)
    try:
        result = app.run()
    except Exception as e:  # noqa: BLE001 - only a start-up failure falls back
        if not app.started:
            raise TuiUnavailable(str(e)) from e
        raise
    if result == "serve":
        if serve is not None:
            return serve(app.serve_argv())
        return SERVE_REQUESTED
    if result == "done":
        return EXIT_PROBLEMS if (app.problems or app.still_missing) else EXIT_OK
    where = f"config.toml was already written to {app.cfg.path}." if (app.saved and app.cfg) else "Nothing was written."
    print(f"Setup cancelled. {where} Run `zordon setup` to start again.", file=sys.stderr)
    return EXIT_CANCELLED


__all__ = ["EXIT_CANCELLED", "EXIT_OK", "EXIT_PROBLEMS", "SERVE_REQUESTED", "SetupApp", "parse_options", "run_setup_tui"]
