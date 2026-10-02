"""Agent adapters: everything about one coding agent's terminal UI in one place.

The rest of Zordon (tmux control, state machine, pipeline, router, transport,
client) is agent-neutral. An adapter supplies the six things that are not:

1. how to launch and resume the agent in a pane,
2. what its prompts look like (permission, plan, question, trust),
3. its screen anatomy (input box, spinner, completion row, mode row),
4. where its sessions live on disk,
5. an optional clean transcript source (a session log it writes itself),
6. permission modes, hooks and the words we use when speaking about it.

``BaseAdapter`` is the generic implementation: it knows nothing about a
specific agent, reads prose from the pane only, and recognises prompts by the
cues most terminal tools share (``(y/n)``, ``[Y/n]``, ``Allow?``, ``Do you
want ...?`` followed by a numbered or lettered Yes/No menu). Specific adapters
override what they know better. ``zordon/agents/__init__.py`` holds the registry.

How the manager answers a generic prompt is carried in ``PromptMatch.extra``:
``inline_yn`` marks an inline ``(y/n)`` question (type the letter, then Enter);
``key<n>`` gives the letter a ``[a] Approve`` style menu wants for option ``n``
(type the letter, no Enter); otherwise the pointer is moved with Up/Down and
Enter as for Claude Code's menus.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zordon.bus import PaneLine, PromptKind
from zordon.session.prompts import PromptMatch, PromptOption
from zordon.session.screen import InputBox, Screen, parse_screen


@dataclass(frozen=True, slots=True)
class AgentInfo:
    key: str  # config value, e.g. "claude-code"
    display_name: str  # spoken and shown, e.g. "Claude Code"
    binary: str  # executable name on PATH, "" for the generic adapter
    install_hint: str  # one line telling the user how to install it
    docs_url: str = ""


@dataclass(slots=True)
class SessionInfo:
    """One resumable session as the adapter sees it on disk and in the process table."""

    agent: str
    session_id: str
    cwd: str
    title: str = ""
    last_active: float | None = None  # epoch seconds
    running_pid: int | None = None
    tmux_target: str | None = None
    permission_mode: str | None = None
    transcript_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LaunchSpec:
    """What the manager needs to open a pane for this agent."""

    command: list[str]
    cwd: str
    settings_paths: list[Path] = field(default_factory=list)  # files to remove when the pane goes
    env_scrub_names: list[str] = field(default_factory=list)  # extra variables to unset in the pane


@runtime_checkable
class TranscriptSource(Protocol):
    """A clean, structured transcript the agent writes itself (optional)."""

    def poll(self) -> list[PaneLine]: ...


@runtime_checkable
class AgentAdapter(Protocol):
    info: AgentInfo

    # ---- availability -------------------------------------------------------------
    def available(self) -> str | None: ...
    def version(self) -> str | None: ...

    # ---- launching ----------------------------------------------------------------
    def new_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec: ...
    def resume_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec: ...
    def supports_resume(self) -> bool: ...
    def allowed_modes(self) -> tuple[str, ...]: ...
    def voice_switchable_modes(self) -> tuple[str, ...]: ...
    def default_launch_mode(self) -> str | None: ...
    def mode_cycle_key(self) -> str | None: ...
    def normalize_mode(self, mode: str) -> str: ...

    # ---- discovery -----------------------------------------------------------------
    def list_sessions(self) -> list[SessionInfo]: ...
    def find_session(self, session_id: str) -> SessionInfo | None: ...

    # ---- screen --------------------------------------------------------------------
    def parse(self, lines: Sequence[str]) -> Screen: ...
    def detect_prompt(self, screen: Screen) -> PromptMatch | None: ...
    def is_idle(self, screen: Screen) -> bool: ...
    def is_working(self, screen: Screen) -> bool: ...
    def input_quiet(self, screen: Screen) -> bool: ...
    def exited(self, screen: Screen) -> bool: ...
    def permission_mode_from_screen(self, screen: Screen) -> str | None: ...
    def uses_alternate_screen(self) -> bool: ...

    # ---- prompt answers ---------------------------------------------------------------
    def yes_option(self, m: PromptMatch) -> int | None: ...
    def no_option(self, m: PromptMatch) -> int | None: ...
    def plan_approve_option(self, m: PromptMatch) -> int | None: ...
    def plan_revise_option(self, m: PromptMatch) -> int | None: ...
    def question_option(self, m: PromptMatch, choice: int | str) -> int | None: ...
    def trust_accept_option(self, m: PromptMatch) -> int | None: ...
    def trust_decline_option(self, m: PromptMatch) -> int | None: ...

    # ---- transcript ------------------------------------------------------------------
    def transcript_source(self, session_id: str, cwd: str, info: SessionInfo | None) -> TranscriptSource | None: ...

    # ---- out-of-band signals (optional; BaseAdapter answers None) ---------------------
    def hook_hint(self, payload: dict[str, Any]) -> Any | None: ...
    def status_hint(self, session_id: str) -> str | None: ...

    # ---- permissions / wording ----------------------------------------------------
    def forbidden_modes(self) -> frozenset[str]: ...
    def permission_summary(self, cwd: str, active_mode: str | None) -> str: ...
    def mode_label(self, mode: str) -> str: ...


@dataclass(slots=True)
class HookRequest:
    """What the manager offers an adapter that supports out-of-band prompt signals."""

    port: int
    secret: str
    host: str
    zordon_home: Path
    session_id: str


# ---- the generic adapter ----------------------------------------------------------------

GENERIC_INFO = AgentInfo(
    key="generic",
    display_name="the agent",
    binary="",
    install_hint="attach Zordon to an existing tmux pane with `attach <session:window.pane>`",
)

# Cues most terminal tools share. Conservative on purpose: a false prompt narrows voice
# input to yes/no, so each pattern needs an explicit yes/no shape.
YN_INLINE = re.compile(r"(\(y/n\)|\[y/n\]|\[Y/n\]|\[y/N\]|\(yes/no\)|\[yes/no\])\s*:?\s*$", re.I)
QUESTION_LEAD = re.compile(
    r"^\s*(?:\?|>)?\s*(?:Do you want|Would you like|Allow|Approve|Apply|Run|Proceed|Continue|Confirm|Accept)\b.*\?\s*$",
    re.I,
)
MENU_OPTION = re.compile(r"^\s*(?P<ptr>[❯>›•\*]\s*)?(?:\(?(?P<n>\d{1,2})[.)]|\[(?P<l>[a-zA-Z])\])\s+(?P<label>\S.*?)\s*$")
YES_LABEL = re.compile(r"^(y|yes|allow|approve|accept|ok|proceed|continue|run)\b", re.I)
NO_LABEL = re.compile(r"^(n|no|deny|reject|cancel|skip|abort|don't|do not|never)\b", re.I)
UNSAFE_LABEL = re.compile(r"always|don'?t ask|do not ask|for this session|auto[- ]?approve|switch to|trust all|yolo|all future", re.I)
SHELL_PROMPT = re.compile(r"^\S+@\S+:.*[$#%] ?$|^[$#%] ?$")
SPINNERISH = re.compile(r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒·✢*✶✻✽|/\\\-]\s*\S")
MENU_TRAILING_LINES = 2  # hint lines a tool may print under its menu ("Use arrow keys...")
INPUT_BOXISH = re.compile(r"^\s*(?P<ptr>❯|>|›|»)\s?$|^\s*(?P<ptr2>❯|>|›|»)\s(?P<text>[^\n]{0,60})$")


class BaseAdapter:
    """Generic behaviour. Specific adapters subclass and override."""

    info: AgentInfo = GENERIC_INFO

    def __init__(self, config: Any | None = None) -> None:
        self.config = config
        self.tmux: Any | None = None
        self.zordon_home: Path | None = None

    def bind(self, *, tmux: Any | None = None, zordon_home: Path | None = None, **_: Any) -> None:
        """The manager hands over its tmux client and home directories after construction.

        Adapters that need more (Claude Code: ``claude_home``) accept it as a keyword;
        the base ignores what it does not know.
        """
        if tmux is not None:
            self.tmux = tmux
        if zordon_home is not None:
            self.zordon_home = Path(zordon_home)

    # ---- availability -------------------------------------------------------------
    def available(self) -> str | None:
        return shutil.which(self.info.binary) if self.info.binary else ""

    def version(self) -> str | None:
        return None

    # ---- launching ----------------------------------------------------------------
    def new_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec:
        raise NotImplementedError(f"{self.info.display_name}: attach to a running pane instead of starting one")

    def resume_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None) -> LaunchSpec:
        raise NotImplementedError(f"{self.info.display_name}: resume is not supported")

    def supports_resume(self) -> bool:
        return False

    def allowed_modes(self) -> tuple[str, ...]:
        return ()

    def voice_switchable_modes(self) -> tuple[str, ...]:
        return ()

    def default_launch_mode(self) -> str | None:
        return None

    def mode_cycle_key(self) -> str | None:
        return None

    def normalize_mode(self, mode: str) -> str:
        m = (mode or "").strip()
        if "bypass" in m.lower() or "dangerous" in m.lower() or "yolo" in m.lower():
            raise ValueError("Zordon never selects a bypass-permissions mode")
        if m not in self.allowed_modes():
            raise ValueError(f"unknown permission mode {mode!r} for {self.info.display_name}")
        return m

    # ---- discovery -----------------------------------------------------------------
    def list_sessions(self) -> list[SessionInfo]:
        return []

    def find_session(self, session_id: str) -> SessionInfo | None:
        return None

    # ---- screen --------------------------------------------------------------------
    def parse(self, lines: Sequence[str]) -> Screen:
        """``screen.parse_screen`` plus a generic input-box rule: a bare ``>``/``❯``/``›``/``»``
        prompt on the last line is the tool's input box, not conversation content."""
        raw = [ln.rstrip(" ") for ln in lines]
        idx = next((i for i in range(len(raw) - 1, -1, -1) if raw[i].strip()), None)
        if idx is None:
            return parse_screen(raw)
        m = INPUT_BOXISH.match(raw[idx])
        if m is None:
            return parse_screen(raw)
        scr = parse_screen(raw[:idx])
        scr.lines = raw
        scr.input_box = InputBox(text=(m.group("text") or "").strip())
        scr.shell_prompt = False
        scr.turn_ended = True  # the tool is waiting for input: nothing above is mid-render
        return scr

    def detect_prompt(self, screen: Screen) -> PromptMatch | None:
        lines = [ln.rstrip() for ln in screen.lines]
        while lines and not lines[-1].strip():
            lines.pop()  # a tall pane pads the bottom with blank rows; the prompt is above them
        tail = lines[-25:]
        # 1) inline (y/n) on the last non-blank line
        nb = [ln for ln in tail if ln.strip()]
        if nb and YN_INLINE.search(nb[-1]):
            q = nb[-1]
            return PromptMatch(
                kind=PromptKind.PERMISSION,
                title=_strip_cue(q),
                question=_strip_cue(q),
                options=[PromptOption(1, "Yes"), PromptOption(2, "No")],
                raw_lines=nb[-6:],
                confidence=0.8,
                extra={"inline_yn": "1"},
            )
        # 2) a question line followed by a numbered/lettered menu with a yes-ish and a no-ish entry
        #    The menu must sit at the bottom: at most MENU_TRAILING_LINES hint lines below it
        #    (none of them a shell prompt or an input box) and at most one blank between options.
        opts: list[PromptOption] = []
        letters: dict[int, str] = {}  # option index -> the key a lettered menu wants typed
        q_i = None
        trailing = 0
        blanks = 0
        for i in range(len(tail) - 1, -1, -1):
            line = tail[i]
            m = MENU_OPTION.match(line)
            if m:
                label = m.group("label")
                idx = int(m.group("n")) if m.group("n") else 0  # lettered: numbered top-down below
                opt = PromptOption(idx, label, bool(m.group("ptr")), bool(UNSAFE_LABEL.search(label)))
                if m.group("l"):
                    opt.description = m.group("l")
                opts.insert(0, opt)
                blanks = 0
                continue
            if line.strip() == "":
                if opts:
                    blanks += 1
                    if blanks > 1:
                        break
                continue
            if not opts:
                if SHELL_PROMPT.match(line) or INPUT_BOXISH.match(line):
                    return None  # a shell or an input box below anything menu-like: not a prompt
                trailing += 1
                if trailing > MENU_TRAILING_LINES:
                    return None
                continue
            q_i = i if QUESTION_LEAD.match(line) or line.strip().endswith("?") else None
            break
        for n, o in enumerate(opts, 1):
            if o.index == 0:
                o.index = n
                letters[n] = o.description
                o.description = ""
        if len(opts) >= 2 and any(YES_LABEL.match(o.label) for o in opts) and any(NO_LABEL.match(o.label) for o in opts):
            question = tail[q_i].strip() if q_i is not None else ""
            extra = {f"key{i}": k for i, k in letters.items()}
            return PromptMatch(
                kind=PromptKind.PERMISSION,
                title=question or "Permission request",
                question=question,
                options=opts,
                raw_lines=[ln for ln in tail[(q_i if q_i is not None else 0) :] if ln.strip()],
                confidence=0.85 if question else 0.6,
                extra=extra,
            )
        return None

    def is_idle(self, screen: Screen) -> bool:
        return screen.input_box is not None and not self.is_working(screen)

    def is_working(self, screen: Screen) -> bool:
        nb = [ln for ln in screen.lines if ln.strip()]
        return bool(nb) and bool(SPINNERISH.match(nb[-1])) or "esc to interrupt" in " ".join(nb[-3:]).lower()

    def input_quiet(self, screen: Screen) -> bool:
        return self.is_idle(screen)

    def exited(self, screen: Screen) -> bool:
        nb = [ln for ln in screen.lines if ln.strip()]
        return bool(nb) and bool(SHELL_PROMPT.match(nb[-1]))

    def permission_mode_from_screen(self, screen: Screen) -> str | None:
        return None

    def uses_alternate_screen(self) -> bool:
        return False

    # ---- prompt answers ---------------------------------------------------------------
    def yes_option(self, m: PromptMatch) -> int | None:
        for o in m.options:
            if YES_LABEL.match(o.label) and not o.unsafe:
                return o.index
        return None

    def no_option(self, m: PromptMatch) -> int | None:
        for o in reversed(m.options):
            if NO_LABEL.match(o.label):
                return o.index
        return None

    def plan_approve_option(self, m: PromptMatch) -> int | None:
        return None

    def plan_revise_option(self, m: PromptMatch) -> int | None:
        return None

    def question_option(self, m: PromptMatch, choice: int | str) -> int | None:
        """Resolve a 1-based number or a label (case-insensitive) against ``m.options``."""
        if isinstance(choice, int):
            return choice if m.option(choice) is not None else None
        want = choice.strip().lower().rstrip(".")
        for o in m.options:
            if o.label.lower().rstrip(".") == want:
                return o.index
        return None

    def trust_accept_option(self, m: PromptMatch) -> int | None:
        return None  # no trust dialog in the generic adapter

    def trust_decline_option(self, m: PromptMatch) -> int | None:
        return None

    # ---- transcript ------------------------------------------------------------------
    def transcript_source(self, session_id: str, cwd: str, info: SessionInfo | None) -> TranscriptSource | None:
        return None  # pane only

    # ---- out-of-band signals ------------------------------------------------------------
    def hook_hint(self, payload: dict[str, Any]) -> Any | None:
        return None  # no hooks: the screen is the only signal

    def status_hint(self, session_id: str) -> str | None:
        return None  # no registry

    # ---- permissions / wording ----------------------------------------------------
    def forbidden_modes(self) -> frozenset[str]:
        return frozenset()

    def permission_summary(self, cwd: str, active_mode: str | None) -> str:
        return f"I can't read {self.info.display_name}'s permission settings; prompts are detected from the screen only."

    def mode_label(self, mode: str) -> str:
        return mode


def _strip_cue(line: str) -> str:
    return YN_INLINE.sub("", line).strip().rstrip(":").strip()
