"""Widgets shared by the setup and uninstall screens.

Everything here is presentation: the title bar, the step indicator, the option
cards that make a question clickable, a panel that streams command output with a
spinner, and the yes/no modal. No zordon logic lives in this module.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Static

# install.sh palette (xterm-256 141 / 75 / 114 / 203), as hex for Rich markup.
PURPLE = "#af87ff"
BLUE = "#5fafff"
GREEN = "#87d787"
RED = "#ff5f5f"
DIM = "#8a8a8a"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def widget_id(key: str) -> str:
    """A DOM id from any key (``ollama-model:qwen2.5:3b`` -> ``ollama-model_qwen2_5_3b``)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


# ---- chrome ----------------------------------------------------------------------------------


class TitleBar(Static):
    """``Z O R D O N`` on the left, the screen's title on the right."""

    DEFAULT_CSS = """
    TitleBar { height: 1; padding: 0 2; background: $surface; color: $text; }
    """

    def __init__(self, title: str, *, right: str = "") -> None:
        super().__init__()
        self._title = title
        self._right = right

    def render(self) -> RenderableType:
        t = Text()
        t.append("Z O R D O N", style=f"bold {PURPLE}")
        t.append("  ")
        t.append(self._title, style="bold")
        if self._right:
            pad = max(1, self.size.width - 4 - len(t.plain) - len(self._right))
            t.append(" " * pad)
            t.append(self._right, style=DIM)
        return t


class StepIndicator(Static):
    """``✓ Agent › ● Speech › ○ Rewriter ...`` across the top."""

    DEFAULT_CSS = """
    StepIndicator { height: 1; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, steps: list[str], current: int) -> None:
        super().__init__()
        self.steps = steps
        self.current = current

    def render(self) -> RenderableType:
        t = Text()
        for i, name in enumerate(self.steps):
            if i:
                t.append(" › ", style=DIM)
            if i < self.current:
                t.append("✓ ", style=GREEN)
                t.append(name, style=DIM)
            elif i == self.current:
                t.append("● ", style=BLUE)
                t.append(name, style=f"bold {BLUE}")
            else:
                t.append("○ ", style=DIM)
                t.append(name, style=DIM)
        if self.size.width and len(t.plain) > self.size.width:
            # Narrow terminal: dots plus the current step's name.
            t = Text()
            for i in range(len(self.steps)):
                t.append("✓" if i < self.current else ("●" if i == self.current else "○"), style=GREEN if i < self.current else (BLUE if i == self.current else DIM))
            t.append(f"  Step {self.current + 1} of {len(self.steps)}: ", style=DIM)
            t.append(self.steps[self.current], style=f"bold {BLUE}")
        return t


class Panel(Vertical):
    """A bordered box with a title in the frame."""

    DEFAULT_CSS = """
    Panel { border: round $primary; padding: 0 1; height: auto; }
    Panel > .panel--body { height: auto; }
    """

    def __init__(self, title: str, *children, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(*children, id=id, classes=classes)
        self.border_title = title


class Hint(Static):
    """One dim line of keyboard help at the bottom of a screen."""

    DEFAULT_CSS = """
    Hint { height: 1; padding: 0 2; color: $text-muted; }
    """


# ---- option cards ----------------------------------------------------------------------------


@dataclass(slots=True)
class Choice:
    key: str
    title: str
    desc: str = ""
    badge: str = ""  # e.g. "installed", "recommended"
    badge_style: str = GREEN


class OptionCard(Static):
    """One selectable card: marker, title, badge, then the trade-off text."""

    DEFAULT_CSS = """
    OptionCard { height: auto; padding: 0 1; margin: 0 0 1 0; border: round $panel; }
    OptionCard.-selected { border: round $accent; background: $boost; }
    OptionCard:hover { background: $boost; }
    """

    class Clicked(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    selected = reactive(False)

    def __init__(self, choice: Choice, index: int) -> None:
        super().__init__()
        self.choice = choice
        self.index = index

    def render(self) -> RenderableType:
        c = self.choice
        marker = Text()
        marker.append("◉ " if self.selected else "○ ", style=BLUE if self.selected else DIM)
        marker.append(f"{self.index + 1}", style=DIM)
        body = Text()
        body.append(c.title, style="bold" if self.selected else "")
        if c.badge:
            body.append("  ")
            body.append(c.badge, style=c.badge_style)
        if c.desc:
            body.append("\n")
            body.append(c.desc, style="" if self.selected else DIM)
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(width=3, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(marker, body)
        return grid

    def watch_selected(self, value: bool) -> None:
        self.set_class(value, "-selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked(self.index))


class Chooser(Vertical, can_focus=True):
    """A focusable list of :class:`OptionCard`.

    Keyboard: arrows or j/k move, 1-9 jump, Enter/Space commit. Mouse: a click
    selects; a second click on the selected card commits. ``Changed`` is posted
    when the selection moves, ``Committed`` when the user confirms it.
    """

    DEFAULT_CSS = """
    Chooser { height: auto; }
    """

    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("enter,space", "commit", "Choose", show=False),
        Binding("home", "jump(0)", show=False),
    ]

    class Changed(Message):
        def __init__(self, chooser: Chooser, key: str) -> None:
            super().__init__()
            self.chooser = chooser
            self.key = key

        @property
        def control(self) -> Chooser:
            return self.chooser

    class Committed(Message):
        def __init__(self, chooser: Chooser, key: str) -> None:
            super().__init__()
            self.chooser = chooser
            self.key = key

        @property
        def control(self) -> Chooser:
            return self.chooser

    def __init__(self, choices: list[Choice], value: str | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.choices = choices
        keys = [c.key for c in choices]
        self.index = keys.index(value) if value in keys else 0

    def compose(self) -> ComposeResult:
        for i, c in enumerate(self.choices):
            card = OptionCard(c, i)
            card.selected = i == self.index
            yield card

    @property
    def value(self) -> str:
        return self.choices[self.index].key

    def select(self, index: int, *, announce: bool = True) -> None:
        index = max(0, min(len(self.choices) - 1, index))
        changed = index != self.index
        self.index = index
        for card in self.query(OptionCard):
            card.selected = card.index == index
            if card.selected:
                card.scroll_visible()
        if announce and changed:
            self.post_message(self.Changed(self, self.value))

    def action_move(self, delta: int) -> None:
        self.select(self.index + delta)

    def action_jump(self, index: int) -> None:
        self.select(index)

    def action_commit(self) -> None:
        self.post_message(self.Committed(self, self.value))

    def on_key(self, event: events.Key) -> None:
        if event.character and event.character.isdigit() and event.character != "0":
            n = int(event.character) - 1
            if n < len(self.choices):
                event.stop()
                self.select(n)

    @on(OptionCard.Clicked)
    def _card_clicked(self, event: OptionCard.Clicked) -> None:
        event.stop()
        self.focus()
        if event.index == self.index:
            self.action_commit()
        else:
            self.select(event.index)


# ---- live command output ---------------------------------------------------------------------


class LogPanel(Vertical):
    """A RichLog with a header line that spins while something is running."""

    DEFAULT_CSS = """
    LogPanel { border: round $primary; height: 12; }
    LogPanel > .logpanel--status { height: 1; padding: 0 1; }
    LogPanel > RichLog { height: 1fr; padding: 0 1; background: $surface; }
    """

    busy = reactive(False)
    status_text = reactive("")

    def __init__(self, title: str = "Output", *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.border_title = title
        self._frame = 0
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("", classes="logpanel--status")
        yield RichLog(wrap=True, markup=False, highlight=False, max_lines=2000)

    @property
    def rich_log(self) -> RichLog:
        return self.query_one(RichLog)

    def write(self, line: str, *, style: str = "") -> None:
        text = Text(line.rstrip("\n"), style=style) if style else Text(line.rstrip("\n"))
        self.rich_log.write(text)

    def write_from_thread(self, line: str, *, style: str = "") -> None:
        self.app.call_from_thread(self.write, line, style=style)

    def start(self, status: str) -> None:
        self.status_text = status
        self.busy = True

    def finish(self, status: str, *, ok: bool = True) -> None:
        self.busy = False
        mark = Text("✓ ", style=GREEN) if ok else Text("✗ ", style=RED)
        self.query_one(".logpanel--status", Static).update(mark + Text(status))

    def watch_busy(self, busy: bool) -> None:
        if busy and self._timer is None:
            self._timer = self.set_interval(0.1, self._tick)
        elif not busy and self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER)
        t = Text(SPINNER[self._frame] + " ", style=BLUE)
        t.append(self.status_text)
        self.query_one(".logpanel--status", Static).update(t)


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def streaming_runner(write: Callable[[str], None]) -> Callable[..., _Result]:
    """A ``subprocess.run``-shaped callable that streams combined output line by line.

    ``prereqs.install`` and ``setup.run_actions`` take a ``run=``/``runner=`` of this
    shape. Meant to be called from a worker thread; ``write`` must be thread safe
    (``LogPanel.write_from_thread`` is).
    """

    def run(cmd: list[str], **kw: object) -> _Result:
        env = kw.get("env")
        proc = subprocess.Popen(  # noqa: S603 - the command was shown to and approved by the user
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            bufsize=1,
            env=env if isinstance(env, dict) else None,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            write(line.rstrip("\n"))
        return _Result(proc.wait())

    return run


class ThreadWriter:
    """A ``TextIO``-ish ``write()`` target that forwards whole lines to a callable."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._write(line)
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._write(self._buf)
            self._buf = ""


def needs_terminal(command: str) -> bool:
    """Commands that may ask for a password or talk to the user need the real tty."""
    return "sudo" in command or "install.sh" in command or "ollama.com" in command


# ---- modal ------------------------------------------------------------------------------------


class Confirm(ModalScreen[bool]):
    """Yes/No. ``y``/Enter confirms, ``n``/Esc declines."""

    DEFAULT_CSS = """
    Confirm { align: center middle; background: $background 60%; }
    Confirm > Vertical { width: 64; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    Confirm .confirm--title { text-style: bold; }
    Confirm .confirm--body { margin: 1 0; color: $text-muted; }
    Confirm Horizontal { height: auto; align-horizontal: right; }
    Confirm Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("escape,n", "answer(False)", "No", show=False),
        Binding("y", "answer(True)", "Yes", show=False),
    ]

    def __init__(self, title: str, body: str, *, yes: str = "Yes", no: str = "No", danger: bool = False) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._yes = yes
        self._no = no
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="confirm--title")
            yield Static(self._body, classes="confirm--body")
            with Horizontal():
                yield Button(self._no, id="confirm-no")
                yield Button(self._yes, id="confirm-yes", variant="error" if self._danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def _yes_pressed(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no_pressed(self) -> None:
        self.dismiss(False)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)
