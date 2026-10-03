"""The Claude Code adapter: every Claude-Code-specific call the session layer
makes, routed through one object.

Nothing is reimplemented here. The adapter delegates to the modules that already
know Claude Code's command line (``session/discovery.py``), its screen
(``session/screen.py``, ``session/prompts.py``), its transcript
(``session/jsonl.py``), its hooks (``session/hooks.py``) and its permission
settings (``session/permissions.py``). The manager only ever sees the
``AgentAdapter`` surface, so a second agent plugs in by subclassing
``BaseAdapter`` the same way.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zordon import paths
from zordon.agents.base import (
    AgentInfo,
    BaseAdapter,
    HookRequest,
    LaunchSpec,
    SessionInfo,
    TranscriptSource,
)
from zordon.bus import PaneLine
from zordon.session import discovery, hooks, jsonl, permissions, prompts, tmux
from zordon.session.prompts import PromptMatch
from zordon.session.screen import Screen, parse_screen

log = logging.getLogger("zordon.agents.claude_code")

CLAUDE_INFO = AgentInfo(
    key="claude-code",
    display_name="Claude Code",
    binary="claude",
    install_hint="npm install -g @anthropic-ai/claude-code, then run `claude` once to log in",
    docs_url="https://code.claude.com",
)

VERSION_TIMEOUT = 5.0


class JsonlTranscript:
    """``TranscriptSource`` over ``jsonl.JsonlTail``: ``poll()`` yields PaneLines.

    ``permission_mode`` events are reported on ``last_mode`` as well as emitted
    (as ``block="permission_mode"`` lines), so the manager can track the mode
    without knowing the jsonl record shape.
    """

    def __init__(self, tail: jsonl.JsonlTail, session_id: str) -> None:
        self.tail = tail
        self.session_id = session_id
        self.last_mode: str | None = None
        self.events_seen = 0

    @property
    def path(self) -> Path:
        return self.tail.path

    def poll(self) -> list[PaneLine]:
        events = self.tail.poll()
        out: list[PaneLine] = []
        for ev in events:
            self.events_seen += 1
            if ev.kind == "permission_mode":
                self.last_mode = ev.text
            out.extend(jsonl.to_pane_lines(ev, self.session_id))
        return out


class ClaudeCodeAdapter(BaseAdapter):
    info = CLAUDE_INFO

    def __init__(
        self,
        config: Any | None = None,
        *,
        claude_home: Path | None = None,
        zordon_home: Path | None = None,
        tmux: Any | None = None,
    ) -> None:
        super().__init__(config)
        self.claude_home = Path(claude_home) if claude_home else None
        self.zordon_home = Path(zordon_home) if zordon_home else None
        self.tmux = tmux
        self._version: str | None = None
        self._version_checked = False
        self._version_lock = threading.Lock()

    def bind(self, *, tmux: Any | None = None, zordon_home: Path | None = None, claude_home: Path | None = None, **_: Any) -> None:
        super().bind(tmux=tmux, zordon_home=zordon_home)
        if claude_home is not None:
            self.claude_home = Path(claude_home)

    @property
    def home(self) -> Path:
        return self.claude_home or paths.claude_home()

    # ---- availability -------------------------------------------------------------

    def version(self) -> str | None:
        """``claude --version`` (first line), cached; None when the binary is missing or slow."""
        with self._version_lock:
            if self._version_checked:
                return self._version
            self._version_checked = True
            binary = self.available()
            if not binary:
                return None
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=VERSION_TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("claude --version failed: %s", e)
                return None
            first = (proc.stdout or proc.stderr or "").strip().splitlines()
            self._version = first[0].strip() if first else None
            return self._version

    # ---- launching ----------------------------------------------------------------

    def new_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None, *, allow_bypass: bool = False) -> LaunchSpec:
        settings, paths_ = self._hook_files(hooks)
        command = discovery.new_session_command(
            session_id, settings, permission_mode or self.default_launch_mode(), allow_bypass=allow_bypass
        )
        return LaunchSpec(command=command, cwd=cwd, settings_paths=paths_, env_scrub_names=tmux.scrub_names())

    def resume_session(self, session_id: str, cwd: str, permission_mode: str | None, hooks: HookRequest | None, *, allow_bypass: bool = False) -> LaunchSpec:
        settings, paths_ = self._hook_files(hooks)
        command = discovery.resume_command(
            session_id, settings, permission_mode or self.default_launch_mode(), allow_bypass=allow_bypass
        )
        return LaunchSpec(command=command, cwd=cwd, settings_paths=paths_, env_scrub_names=tmux.scrub_names())

    def _hook_files(self, req: HookRequest | None) -> tuple[Path | None, list[Path]]:
        """Write the per-session ``--settings`` file and its curl config (decision 0009)."""
        if req is None:
            return None, []
        path = discovery.hook_settings_path(req.zordon_home, req.session_id)
        try:
            host = discovery.hook_host(req.host)
            written = discovery.write_hook_settings(path, req.port, req.secret, host=host, scope=bool(getattr(req, "scope", False)))
        except (OSError, ValueError) as e:
            log.warning("hook settings not written (%s); launching without hooks", e)
            return None, []
        return written, [written, discovery.hook_curl_config_path(written)]

    def supports_resume(self) -> bool:
        return True

    def allowed_modes(self) -> tuple[str, ...]:
        return tuple(discovery.ALLOWED_MODES)

    def voice_switchable_modes(self) -> tuple[str, ...]:
        return tuple(permissions.VOICE_SWITCHABLE)

    def default_launch_mode(self) -> str | None:
        return "default"

    def mode_cycle_key(self) -> str | None:
        return "BTab"

    def normalize_mode(self, mode: str) -> str:
        return permissions.normalize_target_mode(mode, by_voice=False)

    def forbidden_modes(self) -> frozenset[str]:
        return permissions.FORBIDDEN_TARGET_MODES

    # ---- discovery -----------------------------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        return [_to_info(i) for i in discovery.list_sessions(self.home, self.tmux)]

    def find_session(self, session_id: str) -> SessionInfo | None:
        info = discovery.find_session(session_id, self.home, self.tmux)
        return _to_info(info) if info is not None else None

    def status_hint(self, session_id: str) -> str | None:
        """The registry ``status`` (idle | busy | waiting): the third prompt signal."""
        try:
            entry = discovery.load_registry(self.home).get(session_id)
        except Exception as e:  # noqa: BLE001
            log.debug("registry read failed: %s", e)
            return None
        if not entry:
            return None
        status = entry.get("status")
        return status if isinstance(status, str) else None

    # ---- screen --------------------------------------------------------------------

    def parse(self, lines: Sequence[str]) -> Screen:
        return parse_screen(list(lines))

    def detect_prompt(self, screen: Screen) -> PromptMatch | None:
        return prompts.detect_prompt(screen)

    def is_idle(self, screen: Screen) -> bool:
        return prompts.is_idle_prompt(screen)

    def is_working(self, screen: Screen) -> bool:
        return prompts.is_working(screen)

    def input_quiet(self, screen: Screen) -> bool:
        return prompts.input_quiet(screen)

    def exited(self, screen: Screen) -> bool:
        return prompts.exited(screen)

    def permission_mode_from_screen(self, screen: Screen) -> str | None:
        return prompts.permission_mode_from_screen(screen)

    def uses_alternate_screen(self) -> bool:
        return True

    # ---- prompt answers ---------------------------------------------------------------

    def yes_option(self, m: PromptMatch) -> int | None:
        return prompts.yes_option(m)

    def no_option(self, m: PromptMatch) -> int | None:
        return prompts.no_option(m)

    def plan_approve_option(self, m: PromptMatch) -> int | None:
        manual = prompts.plan_manual_option(m)
        if manual is None or manual == prompts.plan_auto_option(m):
            return None
        return manual

    def plan_revise_option(self, m: PromptMatch) -> int | None:
        return prompts.plan_revise_option(m)

    def question_option(self, m: PromptMatch, choice: int | str) -> int | None:
        return prompts.question_option(m, choice)

    def trust_accept_option(self, m: PromptMatch) -> int | None:
        # The trust dialog and the bypass-permissions warning share this shape; the
        # manager decides whether accepting the latter is allowed for the session.
        return next((o.index for o in m.options if o.label in ("Yes, I trust this folder", "Yes, I accept")), None)

    def trust_decline_option(self, m: PromptMatch) -> int | None:
        return next((o.index for o in m.options if o.label == "No, exit"), None)

    # ---- transcript ------------------------------------------------------------------

    def transcript_source(self, session_id: str, cwd: str, info: SessionInfo | None) -> TranscriptSource | None:
        """The session jsonl once it exists; None until then (the manager retries).

        A session Zordon just started (``info is None``) is replayed from the
        start, so nothing said before the pane was found is lost; a resumed or
        attached session (``info`` given) starts at the end of the file.
        """
        path = (info.transcript_path if info is not None else None) or discovery.jsonl_path_for(cwd, session_id, self.home)
        if not path.is_file():
            return None
        if info is None:
            tail = jsonl.JsonlTail(path, offset=0, session_id=session_id)
        else:
            tail = jsonl.JsonlTail(path, start_at_end=True, session_id=session_id)
        return JsonlTranscript(tail, session_id)

    def transcript_path(self, session_id: str, cwd: str) -> Path:
        return discovery.jsonl_path_for(cwd, session_id, self.home)

    # ---- hooks -------------------------------------------------------------------------

    def hook_hint(self, payload: dict[str, Any]) -> hooks.HookHint | None:
        if not hooks.payload_shape_ok(payload):
            return None
        return hooks.hint_for(payload)

    # ---- permissions / wording ----------------------------------------------------

    def onboarding(self, screen: Screen) -> str | None:
        return prompts.detect_onboarding(screen)

    def logged_in(self) -> bool | None:
        """Credentials on disk (``.credentials.json`` under the Claude config dir) or an
        API key in the environment. None when the binary itself is missing."""
        if not self.available():
            return None
        home = paths.claude_home()
        if (home / ".credentials.json").exists():
            return True
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        return False

    def permission_summary(self, cwd: str, active_mode: str | None) -> str:
        summary = permissions.read_settings(self.home, cwd or None)
        return permissions.summary_sentence(summary, active_mode)

    def mode_label(self, mode: str) -> str:
        return permissions.mode_label(mode)


def _to_info(i: discovery.SessionInfo) -> SessionInfo:
    return SessionInfo(
        agent=CLAUDE_INFO.key,
        session_id=i.session_id,
        cwd=i.cwd or "",
        title=i.display_title,
        last_active=i.last_active_ts or None,
        running_pid=i.running_pid if i.running else None,
        tmux_target=i.tmux_target if i.running else None,
        permission_mode=i.permission_mode,
        transcript_path=i.jsonl_path,
        extra={
            "running": "1" if i.running else "",
            "status": i.status or "",
            "git_branch": i.git_branch or "",
            "version": i.version or "",
        },
    )
