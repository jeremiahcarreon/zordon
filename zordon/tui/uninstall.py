"""``zordon uninstall`` as a one-screen Textual app.

What lives inside Zordon's environment is listed as removed (locked); what Zordon
installed outside it on request is a checkbox each, pre-checked only when the plan
recommends it (Ollama models yes, system packages and uv no). A red Uninstall
button, a confirmation, then :func:`zordon.uninstall.execute` runs in a worker
with its log on screen. Commands that need sudo run with the app suspended.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Static

from zordon import __version__, uninstall
from zordon.tui import ZORDON_THEME, TuiUnavailable
from zordon.tui.widgets import (
    BLUE,
    DIM,
    GREEN,
    RED,
    Confirm,
    Hint,
    LogPanel,
    Panel,
    TitleBar,
    needs_terminal,
    streaming_runner,
    widget_id,
)

EXIT_OK = 0
EXIT_CANCELLED = 0  # the user said no; nothing happened
EXIT_PROBLEMS = 3

Runner = Callable[..., Any]


class UninstallApp(App[str]):
    """``run()`` returns ``"done"``, ``"problems"`` or ``"cancelled"``."""

    CSS_PATH = "theme.tcss"
    TITLE = "Zordon uninstall"
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+q", "cancel", "Cancel", show=False, priority=True),
        Binding("escape,q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, plan: uninstall.Plan, *, runner: Runner | None = None) -> None:
        super().__init__()
        self.plan = plan
        self.runner = runner  # None: real subprocesses; tests pass a fake
        self.executed: list[uninstall.Item] = []
        self.problems: list[str] = []
        self.running = False
        self.finished = False

    def on_mount(self) -> None:
        self.register_theme(ZORDON_THEME)
        self.theme = "zordon"
        self.push_screen(PlanScreen())

    def action_cancel(self) -> None:
        if self.running:
            return
        if self.finished:
            self.exit("problems" if self.problems else "done", return_code=EXIT_PROBLEMS if self.problems else EXIT_OK)
            return
        self.exit("cancelled", return_code=EXIT_CANCELLED)


class PlanScreen(Screen):
    HINT = "Space toggle · Tab move · Enter press · Esc cancel"

    @property
    def un(self) -> UninstallApp:
        app = self.app
        assert isinstance(app, UninstallApp)
        return app

    def compose(self) -> ComposeResult:
        plan = self.un.plan
        yield TitleBar("Uninstall", right=f"zordon {__version__}")
        with VerticalScroll(classes="screen-body"):
            head = Text("Remove Zordon from this machine. ", style="bold")
            head.append("Everything inside its environment goes; anything it installed outside is removed only if you tick it.", style=DIM)
            yield Static(head, classes="question")
            with Vertical(id="lists"):
                yield Panel("Removed", *self._inside(plan), id="inside")
                if plan.outside:
                    yield Panel("Also remove?", *self._outside(plan), id="outside")
                else:
                    yield Panel("Also remove?", Static(Text("Nothing: Zordon installed no system packages, models or tools on request.", style=DIM)), id="outside")
                if plan.notes:
                    yield Panel("Notes", *[Static(Text("• " + n, style=DIM)) for n in plan.notes])
            yield LogPanel("Progress", id="log")
        with Horizontal(classes="row-buttons"):
            yield Button("Cancel", id="cancel")
            yield Button("Uninstall", id="uninstall", variant="error")
        yield Hint(self.HINT)

    @staticmethod
    def _inside(plan: uninstall.Plan) -> list[Static]:
        out = []
        for it in plan.inside:
            t = Text("☒ ", style=RED)
            t.append(it.label, style="bold")
            t.append(f"\n    {it.detail}", style=DIM)
            out.append(Static(t))
        return out

    @staticmethod
    def _outside(plan: uninstall.Plan) -> list[Checkbox | Static]:
        out: list[Checkbox | Static] = []
        for it in plan.outside:
            label = Text(it.label, style="bold")
            if it.recommended:
                label.append("  recommended", style=GREEN)
            out.append(Checkbox(label, value=it.recommended, id=f"item-{widget_id(it.key)}"))
            out.append(Static(Text(it.detail, style=DIM), classes="item-detail"))
        return out

    def on_mount(self) -> None:
        self.query_one(LogPanel).display = False
        self.query_one("#cancel", Button).focus()

    def chosen(self) -> list[uninstall.Item]:
        plan = self.un.plan
        items = list(plan.inside)
        for it in plan.outside:
            try:
                if self.query_one(f"#item-{widget_id(it.key)}", Checkbox).value:
                    items.append(it)
            except Exception:  # noqa: BLE001 - no checkbox means not offered
                pass
        return items

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.un.action_cancel()

    @on(Button.Pressed, "#uninstall")
    def _uninstall(self) -> None:
        if self.un.running or self.un.finished:
            return
        items = self.chosen()
        extra = [i.label for i in items if i.outside]
        body = "Zordon's data directory, the tmux session and the isolated environment are removed."
        if extra:
            body += "\nAlso: " + ", ".join(extra) + "."
        body += "\nThis cannot be undone."
        self.app.push_screen(Confirm("Uninstall Zordon?", body, yes="Uninstall", no="Keep it", danger=True), self._confirmed)

    def _confirmed(self, yes: bool | None) -> None:
        if not yes:
            return
        items = self.chosen()
        self.un.running = True
        for b in self.query(Button):
            b.disabled = True
        for cb in self.query(Checkbox):
            cb.disabled = True
        log = self.query_one(LogPanel)
        log.display = True
        log.start("removing...")
        terminal = [i for i in items if i.command and self.un.runner is None and needs_terminal(" ".join(i.command))]
        if terminal:
            # sudo needs the real tty: run those with the app suspended, the rest in a worker.
            rest = [i for i in items if i not in terminal]
            with self.app.suspend():
                print("\n  ◆ Removing packages (sudo may ask for your password)\n", flush=True)
                problems = uninstall.execute(terminal, run=subprocess.run, log=lambda s: print("  " + s, flush=True))
            for p in problems:
                log.write(f"✗ {p}", style=RED)
            self.un.problems.extend(problems)
            self.un.executed.extend(terminal)
            items = rest
        self._execute(items, log)

    @work(thread=True)
    def _execute(self, items: list[uninstall.Item], log: LogPanel) -> None:
        runner = self.un.runner or streaming_runner(log.write_from_thread)
        for it in items:
            if it.command:
                log.write_from_thread("$ " + " ".join(it.command), style=BLUE)
        problems = uninstall.execute(items, run=runner, log=lambda s: log.write_from_thread(s, style=GREEN))
        self.un.executed.extend(items)
        self.app.call_from_thread(self._finished, problems)

    def _finished(self, problems: list[str]) -> None:
        self.un.problems.extend(problems)
        self.un.running = False
        self.un.finished = True
        log = self.query_one(LogPanel)
        for p in problems:
            log.write(f"✗ {p}", style=RED)
        if self.un.problems:
            log.finish(f"done, with {len(self.un.problems)} problem(s) above", ok=False)
        else:
            log.finish("Zordon is gone. Thanks for trying it.", ok=True)
        self.query_one("#uninstall", Button).display = False
        close = self.query_one("#cancel", Button)
        close.label = "Close"
        close.disabled = False
        close.focus()


def run_uninstall_tui(plan: uninstall.Plan, *, runner: Runner | None = None) -> int:
    """Run the uninstall screen. Returns 0, or 3 when problems were reported."""
    if os.environ.get("TERM", "") in ("", "dumb"):
        raise TuiUnavailable("TERM is not set or is 'dumb'")
    app = UninstallApp(plan, runner=runner)
    app.run()
    return EXIT_PROBLEMS if app.problems else EXIT_OK


__all__ = ["EXIT_OK", "EXIT_PROBLEMS", "UninstallApp", "run_uninstall_tui"]
