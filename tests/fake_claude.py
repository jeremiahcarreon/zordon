#!/usr/bin/env python3
"""A stand-in for the Claude Code terminal UI, close enough for the session manager.

Run it in a tmux pane: ``python tests/fake_claude.py <session jsonl path>``. It
enters the alternate screen, draws the banner, an input box (``❯`` + U+00A0) and
the ``⏸ manual mode on`` status row, then reads single keys from the tty.

Commands (typed, then Enter):

    say <text>   300 ms spinner, then ``● <text>`` and a done line; also appends
                 user + assistant records to the jsonl
    perm         the Bash permission block from eval/fixtures/pane/bash_permission.txt
                 (Enter on 1 -> ``● Ran 1 shell command``; 4 or Escape -> Interrupted)
    plan         the plan approval block from plan_approval.txt
                 (Enter on n -> ``● Approved: auto|manual`` / ``● Revising plan``)
    ask          the AskUserQuestion block from ask_user_question.txt
    hang         a spinner forever (until Escape)
    /exit        leaves the alternate screen, prints the resume line and exits

Keys: Up/Down move the menu pointer, Enter selects, Escape cancels, Shift+Tab
(``\\x1b[Z``) cycles the status row manual -> accept edits -> plan -> manual and
writes a ``permission-mode`` record, C-u clears the input line. Any C0 control
byte that reaches the script is reported on screen so a test can prove it was
stripped upstream.

Only the standard library is used; the script must run from a bare interpreter.
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
import termios
import time
import tty
from datetime import UTC, datetime
from pathlib import Path

NBSP = " "
GLYPHS = "·✢*✶✻✽"
MODES = ["manual", "accept edits", "plan"]
MODE_NAMES = {"manual": "default", "accept edits": "acceptEdits", "plan": "plan"}
FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "pane"
RULE = re.compile(r"^\s*─{20,}\s*$")
OPTION = re.compile(r"^\s*❯?\s*(?P<n>\d)\. (?P<label>\S.*)$")
BANNER = [
    " ▐▛███▛█   Claude Code v2.1.287 (fake)",
    "▝▜██████▀  Test model with medium effort · Local",
    " ▝▝   ▝▝   {cwd}",
    "",
]
ESC = "\x1b"


def now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clock() -> str:
    return datetime.now().strftime("%-I:%M %p")


class Jsonl:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.session_id = self.path.stem if self.path else "fake-session"
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()

    def write(self, rec: dict) -> None:
        if not self.path:
            return
        rec.setdefault("sessionId", self.session_id)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def mode(self, mode: str) -> None:
        self.write({"type": "permission-mode", "permissionMode": mode})

    def user(self, text: str) -> None:
        self.write(
            {
                "type": "user",
                "timestamp": now_iso(),
                "cwd": os.getcwd(),
                "message": {"role": "user", "content": text},
            }
        )

    def assistant(self, text: str) -> None:
        self.write(
            {
                "type": "assistant",
                "timestamp": now_iso(),
                "cwd": os.getcwd(),
                "message": {
                    "role": "assistant",
                    "model": "fake",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                },
            }
        )


def load_block(name: str, header: str) -> list[str]:
    """The prompt block of a fixture: from the rule above ``header`` to the end."""
    lines = (FIXTURES / name).read_text(encoding="utf-8").split("\n")
    lines = [line.rstrip(" ") for line in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    for i, line in enumerate(lines):
        if line.strip() == header and i > 0 and RULE.match(lines[i - 1]):
            return lines[i - 1 :]
    raise SystemExit(f"fixture {name} has no block headed {header!r}")


def option_lines(block: list[str]) -> list[int]:
    return [i for i, line in enumerate(block) if OPTION.match(line)]


def point(block: list[str], selected: int) -> list[str]:
    """Re-render the pointer so option ``selected`` (1-based) carries ``❯``.

    The number keeps its column; the pointer sits two columns to its left, which
    is where Claude Code draws it for every prompt kind.
    """
    out = []
    for line in block:
        m = OPTION.match(line)
        if not m:
            out.append(line)
            continue
        n = int(m.group("n"))
        idx = line.index(f"{n}. ")
        marker = "❯ " if n == selected else "  "
        out.append(" " * max(0, idx - 2) + marker + line[idx:])
    return out


class Fake:
    def __init__(self, jsonl_path: str | None) -> None:
        self.jsonl = Jsonl(jsonl_path)
        self.out = sys.stdout
        size = os.get_terminal_size()
        self.width, self.height = size.columns, size.lines
        self.content: list[str] = []
        self.input = ""
        self.mode_i = 0
        self.phase = "idle"  # idle | spinner | hang | prompt | revise
        self.spinner_until = 0.0
        self.spinner_verb = "Thinking"
        self.spinner_started = 0.0
        self.pending_say = ""
        self.block: list[str] = []
        self.block_kind = ""
        self.selected = 1
        self.frame = 0
        self.dirty = True
        self.alt = False
        self.jsonl.mode("default")

    # ---- drawing ---------------------------------------------------------------------

    def write(self, s: str) -> None:
        self.out.write(s)

    def at(self, row: int, text: str) -> None:
        self.write(f"{ESC}[{row};1H{text[: self.width]}{ESC}[K")

    def draw(self) -> None:
        self.frame += 1
        self.write(f"{ESC}[?25l")
        banner = [b.replace("{cwd}", os.getcwd()) for b in BANNER]
        bottom = self.bottom_block()
        rows_for_content = self.height - len(banner) - len(bottom)
        body = list(self.content)
        if self.phase in ("spinner", "hang"):
            glyph = GLYPHS[(self.frame // 1) % len(GLYPHS)]
            secs = int(time.monotonic() - self.spinner_started)
            body.append("")
            body.append(f"{glyph} {self.spinner_verb}… ({secs}s)")
        body = body[-rows_for_content:] if rows_for_content > 0 else []
        row = 1
        for line in banner + body:
            self.at(row, line)
            row += 1
        while row <= self.height - len(bottom):
            self.at(row, "")
            row += 1
        for line in bottom:
            self.at(row, line)
            row += 1
        self.write(f"{ESC}[{self.height};1H")
        self.out.flush()
        self.dirty = False

    def bottom_block(self) -> list[str]:
        if self.phase == "prompt":
            block = point(self.block, self.selected)
            limit = self.height - len(BANNER) - 2
            if len(block) > limit:
                block = self.trim_block(block, limit)
            return block
        rule = "─" * self.width
        return [rule, f"❯{NBSP}{self.input}", rule, f"  ⏸ {MODES[self.mode_i]} mode on"]

    def trim_block(self, block: list[str], limit: int) -> list[str]:
        """Keep the head (header + first plan line) and the tail (question + options)."""
        head = 6
        tail = limit - head
        return block[:head] + block[-tail:]

    # ---- phases ----------------------------------------------------------------------

    def echo(self, text: str) -> None:
        if self.content:
            self.content.append("")
        self.content.append(f"❯ {text}")

    def done(self, verb: str = "Worked", secs: int = 1) -> None:
        self.content.append("")
        self.content.append(f"✻ {verb} for {secs}s · done {clock()}")

    def start_spinner(self, verb: str, duration: float) -> None:
        self.phase = "spinner"
        self.spinner_verb = verb
        self.spinner_started = time.monotonic()
        self.spinner_until = self.spinner_started + duration

    def submit(self) -> None:
        text = self.input.strip()
        self.input = ""
        if not text:
            return
        if self.phase == "revise":
            self.echo(text)
            self.content.append("")
            self.content.append(f"● Feedback: {text}")
            self.done("Cogitated")
            self.phase = "idle"
            return
        self.echo(text)
        if text == "/exit":
            self.exit()
            return
        self.jsonl.user(text)
        cmd, _, arg = text.partition(" ")
        if cmd == "say":
            self.pending_say = arg.strip() or "nothing"
            self.start_spinner("Thinking", 0.3)
        elif cmd == "perm":
            self.block = load_block("bash_permission.txt", "Bash command")
            self.block_kind = "perm"
            self.open_prompt()
        elif cmd == "plan":
            self.block = load_block("plan_approval.txt", "Ready to code?")
            self.block_kind = "plan"
            self.open_prompt()
        elif cmd == "ask":
            self.block = load_block("ask_user_question.txt", "☐ Indentation")
            self.block_kind = "ask"
            self.open_prompt()
        elif cmd == "hang":
            self.phase = "hang"
            self.spinner_verb = "Hanging"
            self.spinner_started = time.monotonic()
        else:
            self.content.append("")
            self.content.append(f"● Unknown command: {text}")
            self.done()

    def open_prompt(self) -> None:
        self.phase = "prompt"
        self.selected = 1
        self.content.append("")
        if self.block_kind == "perm":
            self.content.append("  Creating probe marker file")
            self.content.append("  ⎿  $ touch probe_marker && echo done")

    def options(self) -> list[int]:
        return [int(OPTION.match(self.block[i]).group("n")) for i in option_lines(self.block)]

    def finish_spinner(self) -> None:
        self.phase = "idle"
        self.content.append("")
        self.content.append(f"● {self.pending_say}")
        self.jsonl.assistant(self.pending_say)
        self.done()

    def interrupt(self, with_done: bool) -> None:
        self.content.append("  ⎿  Interrupted · What should Claude do instead?")
        if with_done:
            self.done("Sautéed")
        self.phase = "idle"

    def choose(self) -> None:
        n = self.selected
        kind = self.block_kind
        self.phase = "idle"
        if kind == "perm":
            if n == 4:
                self.content.append("  Ran 1 shell command")
                self.interrupt(with_done=True)
            else:
                self.content.append(f"● Ran 1 shell command (option {n})" if n != 1 else "● Ran 1 shell command")
                self.content.append("  ⎿  done")
                self.done()
        elif kind == "plan":
            if n == 1:
                self.content.append("● Approved: auto")
                self.done()
            elif n == 2:
                self.content.append("● Approved: manual")
                self.done()
            else:
                self.content.append("● Revising plan")
                self.phase = "revise"
        elif kind == "ask":
            labels = {}
            for i in option_lines(self.block):
                m = OPTION.match(self.block[i])
                labels[int(m.group("n"))] = m.group("label").strip()
            self.content.append("● User answered Claude's questions:")
            self.content.append(f"  ⎿  · Do you prefer tabs or spaces for indentation? → {labels.get(n, n)}")
            self.done()

    def cancel_prompt(self) -> None:
        kind = self.block_kind
        if kind == "plan":
            self.content.append("● Plan rejected")
            self.done("Cogitated")
            self.phase = "idle"
        else:
            self.interrupt(with_done=True)

    def cycle_mode(self) -> None:
        self.mode_i = (self.mode_i + 1) % len(MODES)
        self.jsonl.mode(MODE_NAMES[MODES[self.mode_i]])

    def exit(self) -> None:
        self.leave_alt()
        self.write("Resume this session with:\n")
        self.write(f"claude --resume {self.jsonl.session_id}\n")
        self.out.flush()
        time.sleep(2.0)  # long enough for a poller to see the resume line
        raise SystemExit(0)

    # ---- keys --------------------------------------------------------------------------

    def key(self, k: str) -> None:
        self.dirty = True
        if self.phase == "prompt":
            if k == "Down":
                self.selected = min(self.selected + 1, max(self.options() or [1]))
            elif k == "Up":
                self.selected = max(self.selected - 1, 1)
            elif k == "Enter":
                self.choose()
            elif k == "Escape":
                self.cancel_prompt()
            elif k == "BTab" and self.block_kind == "plan":
                self.content.append("● Approved with feedback (shift+tab)")
                self.done()
                self.phase = "idle"
            return
        if self.phase in ("spinner", "hang"):
            if k == "Escape":
                self.interrupt(with_done=False)
            return
        if k == "Enter":
            self.submit()
        elif k == "Escape":
            pass
        elif k == "BTab":
            self.cycle_mode()
        elif k == "C-u":
            self.input = ""
        elif k == "Backspace":
            self.input = self.input[:-1]
        elif k in ("Up", "Down", "Left", "Right", "Tab"):
            pass
        elif k.startswith("C0:"):
            self.content.append("")
            self.content.append(f"● control character received: {k[3:]}")
        else:
            self.input += k

    # ---- terminal -----------------------------------------------------------------------

    def enter_alt(self) -> None:
        self.write(f"{ESC}[?1049h{ESC}[2J{ESC}[H")
        self.alt = True

    def leave_alt(self) -> None:
        self.write(f"{ESC}[?1049l{ESC}[?25h")
        self.out.flush()
        self.alt = False

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ISIG  # deliver C-c as a byte so a leak is visible on screen
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            self.enter_alt()
            self.loop(fd)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if self.alt:
                self.leave_alt()

    def loop(self, fd: int) -> None:
        buf = b""
        while True:
            now = time.monotonic()
            if self.phase == "spinner" and now >= self.spinner_until:
                self.finish_spinner()
                self.dirty = True
            if self.phase in ("spinner", "hang"):
                self.dirty = True
            if self.dirty:
                self.draw()
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                if buf == ESC.encode():
                    self.key("Escape")
                    buf = b""
                continue
            buf += os.read(fd, 1024)
            buf = self.decode(buf)

    def decode(self, buf: bytes) -> bytes:
        while buf:
            if buf.startswith(b"\x1b"):
                if len(buf) == 1:
                    return buf  # wait: lone Escape or the start of a sequence
                if buf[1:2] == b"[":
                    m = re.match(rb"\x1b\[([0-9;]*)([A-Za-z~])", buf)
                    if not m:
                        if len(buf) < 8:
                            return buf
                        buf = buf[2:]
                        continue
                    seq = m.group(0)
                    buf = buf[len(seq) :]
                    self.key(
                        {b"A": "Up", b"B": "Down", b"C": "Right", b"D": "Left", b"Z": "BTab"}.get(
                            m.group(2), "Unknown"
                        )
                    )
                    continue
                self.key("Escape")
                buf = buf[1:]
                continue
            b = buf[:1]
            buf = buf[1:]
            if b in (b"\r", b"\n"):
                self.key("Enter")
            elif b == b"\x7f" or b == b"\x08":
                self.key("Backspace")
            elif b == b"\x15":
                self.key("C-u")
            elif b == b"\t":
                self.key("Tab")
            elif b < b" ":
                self.key(f"C0:{b.hex()}")
            else:
                # UTF-8: gather continuation bytes
                n = 1
                first = b[0]
                if first >= 0xF0:
                    n = 4
                elif first >= 0xE0:
                    n = 3
                elif first >= 0xC0:
                    n = 2
                if n > 1:
                    if len(buf) < n - 1:
                        return b + buf
                    b += buf[: n - 1]
                    buf = buf[n - 1 :]
                self.key(b.decode("utf-8", errors="replace"))
        return buf


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    Fake(path).run()


if __name__ == "__main__":
    main()
