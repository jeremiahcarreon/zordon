"""tmux.py: argument construction against a fake subprocess, plus a private-server
integration test (marked) that types hostile literal text into a real bash pane."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import pytest

from zordon.session import tmux as T
from zordon.session.tmux import KEY_ALLOWLIST, PaneInfo, Tmux, TmuxError, strip_control

# ---- fake subprocess -----------------------------------------------------------------


@dataclass
class Completed:
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass
class FakeRun:
    """Records every argv and answers from a queue of (returncode, stdout) replies."""

    replies: list[Completed] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)

    def __call__(self, cmd, capture_output, timeout, check, env=None):  # noqa: ANN001
        self.calls.append(list(cmd))
        self.timeouts.append(timeout)
        self.envs.append(env)
        if self.replies:
            return self.replies.pop(0)
        return Completed()

    def last(self) -> list[str]:
        return self.calls[-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    f = FakeRun()
    monkeypatch.setattr(T.subprocess, "run", f)
    return f


def test_run_prefixes_socket_and_decodes_with_replace(fake: FakeRun):
    fake.replies.append(Completed(stdout=b"ok \xff\n"))
    out = Tmux(socket="zordon-x").run("list-sessions", timeout=1.5)
    assert fake.last() == ["tmux", "-L", "zordon-x", "list-sessions"]
    assert fake.timeouts[-1] == 1.5
    assert out == "ok �\n"


def test_run_without_socket_and_custom_binary(fake: FakeRun):
    Tmux(binary="/opt/bin/tmux").run("list-sessions")
    assert fake.last() == ["/opt/bin/tmux", "list-sessions"]


# ---- environment scrubbing (SEC-2 / RR-7) ------------------------------------------------


def test_scrub_names_covers_claude_markers_and_secret_shapes():
    environ = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "OPENAI_API_KEY": "sk-x",
        "GITHUB_TOKEN": "ghp_x",
        "MY_APP_SECRET": "s",
        "CLAUDE_CODE_EXECPATH": "/x",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_CONFIG_DIR": "/home/u/.claude-alt",
        "TMUX_TMPDIR": "/tmp",
        "SECRET_SAUCE": "no match: SECRET is not a suffix",
    }
    names = T.scrub_names(environ)
    for fixed in T.SCRUB_NAMES:
        assert fixed in names
    for dyn in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "MY_APP_SECRET", "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT"):
        assert dyn in names
    assert "CLAUDE_CONFIG_DIR" not in names  # claude must still find the store Zordon watches
    assert "PATH" not in names and "HOME" not in names and "TMUX_TMPDIR" not in names
    assert "SECRET_SAUCE" not in names
    assert names == sorted(set(names))
    clean = T.scrubbed_environ(environ)
    assert set(clean) == {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "TMUX_TMPDIR", "SECRET_SAUCE"}
    assert not any(k.endswith("_API_KEY") or k.startswith("CLAUDE_CODE") for k in clean)


def test_run_starts_tmux_with_a_scrubbed_environment(fake: FakeRun, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("KEEP_ME", "yes")
    Tmux().run("list-sessions")
    env = fake.envs[-1]
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["KEEP_ME"] == "yes" and "PATH" in env


def test_scrub_environment_marks_every_name_removed_in_one_call(fake: FakeRun):
    t = Tmux()
    removed = t.scrub_environment("zordon", ["ANTHROPIC_API_KEY", "CLAUDECODE"])
    assert removed == ["ANTHROPIC_API_KEY", "CLAUDECODE"]
    assert fake.last() == [
        "tmux",
        "set-environment", "-t", "=zordon", "-r", "ANTHROPIC_API_KEY",
        ";",
        "set-environment", "-t", "=zordon", "-r", "CLAUDECODE",
    ]
    # Once per session per instance when the names are not given explicitly.
    fake.calls.clear()
    assert t.scrub_environment("zordon") == []
    assert fake.calls == []
    assert t.scrub_environment("other")  # a different session is scrubbed with the live names
    assert fake.last()[1] == "set-environment" and "-r" in fake.last()


def test_run_raises_tmux_error_on_failure(fake: FakeRun):
    fake.replies.append(Completed(returncode=1, stderr=b"no server running on /tmp/x"))
    with pytest.raises(TmuxError, match="no server running"):
        Tmux().run("list-sessions")


def test_run_maps_missing_binary_and_timeout(monkeypatch: pytest.MonkeyPatch):
    def missing(*a, **k):  # noqa: ANN001
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(T.subprocess, "run", missing)
    with pytest.raises(TmuxError, match="not found"):
        Tmux(binary="nope-tmux").run("ls")

    def slow(*a, **k):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=2.0)

    monkeypatch.setattr(T.subprocess, "run", slow)
    with pytest.raises(TmuxError, match="timed out"):
        Tmux().run("capture-pane")


def test_server_alive_and_pane_exists(fake: FakeRun):
    t = Tmux()
    assert t.server_alive()
    fake.replies.append(Completed(returncode=1, stderr=b"no server running"))
    assert not t.server_alive()
    fake.replies.append(Completed(stdout=b"%3\t%3\n"))
    assert t.pane_exists("s:@1.%3")
    assert fake.last() == ["tmux", "display-message", "-p", "-t", "s:@1.%3", "#{pane_id}\t#{pane_id}"]
    fake.replies.append(Completed(returncode=1, stderr=b"can't find pane"))
    assert not t.pane_exists("s:@1.%9")
    # tmux resolves a dead window's target to the current pane: that is "gone", not "exists".
    fake.replies.append(Completed(stdout=b"%0\t%0\n"))
    assert not t.pane_exists("s:@1.%9")
    # An unknown bare pane id prints an empty line with exit 0.
    fake.replies.append(Completed(stdout=b"\n"))
    assert not t.pane_exists("%9")


def test_list_panes_parses_fields_and_builds_targets(fake: FakeRun):
    row = "\t".join(["zordon", "@3", "%7", "4242", "/home/u/proj", "node", "1", "160", "45"])
    fake.replies.append(Completed(stdout=(row + "\nbad line\n").encode()))
    panes = Tmux().list_panes()
    assert panes == [
        PaneInfo(
            target="zordon:@3.%7",
            session="zordon",
            window_id="@3",
            pane_id="%7",
            pid=4242,
            cwd="/home/u/proj",
            command="node",
            alternate_on=True,
            width=160,
            height=45,
        )
    ]
    assert fake.last()[:4] == ["tmux", "list-panes", "-a", "-F"]
    assert "#{alternate_on}" in fake.last()[4]


def test_list_panes_without_server_is_empty(fake: FakeRun):
    fake.replies.append(Completed(returncode=1, stderr=b"no server running on /tmp/tmux-1000/default"))
    assert Tmux().list_panes() == []


def test_ensure_session_creates_detached_when_missing(fake: FakeRun):
    fake.replies.append(Completed(returncode=1, stderr=b"can't find session: zordon"))
    Tmux().ensure_session("zordon", cwd="/tmp", width=160, height=45)
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=zordon"]
    assert fake.calls[1] == ["tmux", "new-session", "-d", "-s", "zordon", "-x", "160", "-y", "45", "-c", "/tmp"]
    assert fake.calls[2][1:5] == ["set-environment", "-t", "=zordon", "-r"]  # scrubbed right away
    assert len(fake.calls) == 3
    fake.calls.clear()
    Tmux().ensure_session("zordon")
    assert len(fake.calls) == 1  # exists: nothing created


def test_new_window_uses_target_format_resizes_and_verifies(fake: FakeRun):
    fake.replies.append(Completed())  # has-session: exists
    fake.replies.append(Completed())  # set-environment (scrub)
    fake.replies.append(Completed(stdout=b"zordon:@4.%9\n"))  # new-window -P
    fake.replies.append(Completed())  # resize-window
    fake.replies.append(Completed(stdout=b"%9\t160x45\n"))  # display-message size
    target = Tmux().new_window("zordon", "z-abc", "/home/u/proj", ["claude", "--resume", "abc def"], 160, 45)
    assert target == "zordon:@4.%9"
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=zordon"]
    assert fake.calls[1][1:5] == ["set-environment", "-t", "=zordon", "-r"]
    nw = fake.calls[2]
    assert nw[:3] == ["tmux", "new-window", "-d"]
    assert nw[nw.index("-t") + 1] == "=zordon"
    assert nw[nw.index("-n") + 1] == "z-abc"
    assert nw[nw.index("-c") + 1] == "/home/u/proj"
    assert "-P" in nw and nw[nw.index("-F") + 1] == "#{session_name}:#{window_id}.#{pane_id}"
    assert nw[-1] == "claude --resume 'abc def'"  # one shell-safe string
    assert fake.calls[3] == ["tmux", "resize-window", "-t", "zordon:@4.%9", "-x", "160", "-y", "45"]
    assert fake.calls[4][-1] == "#{pane_id}\t#{window_width}x#{window_height}"


def test_new_window_creates_the_session_with_itself_as_first_window(fake: FakeRun):
    """RR-8: no idle shell window is created ahead of the Claude Code window."""
    fake.replies.append(Completed(returncode=1, stderr=b"can't find session: zordon"))  # has-session
    fake.replies.append(Completed(stdout=b"zordon:@0.%0\n"))  # new-session -P
    fake.replies.append(Completed())  # set-environment (scrub)
    fake.replies.append(Completed())  # resize-window
    fake.replies.append(Completed(stdout=b"%0\t160x45\n"))  # display-message size
    target = Tmux().new_window("zordon", "proj", "/home/u/proj", ["claude", "--session-id", "x"], 160, 45)
    assert target == "zordon:@0.%0"
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=zordon"]
    ns = fake.calls[1]
    assert ns[:5] == ["tmux", "new-session", "-d", "-s", "zordon"]
    assert ns[ns.index("-c") + 1] == "/home/u/proj"
    assert ns[ns.index("-n") + 1] == "proj"
    assert ns[-1] == "claude --session-id x"
    assert "-P" in ns and ns[ns.index("-F") + 1] == "#{session_name}:#{window_id}.#{pane_id}"
    assert not any(c[1] == "new-window" for c in fake.calls)
    assert fake.calls[2][1:5] == ["set-environment", "-t", "=zordon", "-r"]
    assert fake.calls[3][:3] == ["tmux", "resize-window", "-t"]


def test_new_window_rejects_garbage_output_and_empty_command(fake: FakeRun):
    fake.replies.append(Completed())  # has-session
    fake.replies.append(Completed())  # scrub
    fake.replies.append(Completed(stdout=b"something odd\n"))
    with pytest.raises(TmuxError, match="unexpected"):
        Tmux().new_window("z", "w", "/tmp", ["true"])
    with pytest.raises(ValueError):
        Tmux().new_window("z", "w", "/tmp", [])


def test_capture_strips_spaces_but_keeps_nbsp(fake: FakeRun):
    fake.replies.append(Completed(stdout="❯    \nline two   \n\n".encode()))
    lines = Tmux().capture("s:@1.%1")
    assert lines == ["❯ ", "line two", ""]
    assert fake.last() == ["tmux", "capture-pane", "-p", "-J", "-t", "s:@1.%1"]
    fake.replies.append(Completed(stdout=b"x\n"))
    Tmux().capture("s:@1.%1", ansi=True, history_lines=50)
    assert fake.last() == ["tmux", "capture-pane", "-p", "-J", "-t", "s:@1.%1", "-e", "-S", "-50"]
    fake.replies.append(Completed(stdout=b"x\n"))
    Tmux().capture("s:@1.%1", with_ansi=True)
    assert fake.last()[-1] == "-e"


def test_alternate_on_and_pane_pid(fake: FakeRun):
    fake.replies.append(Completed(stdout=b"%1\t1\n"))
    assert Tmux().alternate_on("s:@1.%1") is True
    assert fake.last()[-1] == "#{pane_id}\t#{alternate_on}"
    fake.replies.append(Completed(stdout=b"%1\t0\n"))
    assert Tmux().alternate_on("s:@1.%1") is False
    fake.replies.append(Completed(stdout=b"%1\t78572\n"))
    assert Tmux().pane_pid("s:@1.%1") == 78572
    fake.replies.append(Completed(returncode=1, stderr=b"can't find pane"))
    assert Tmux().pane_pid("s:@1.%1") is None
    fake.replies.append(Completed(stdout=b"%0\t4242\n"))  # resolved to another pane
    assert Tmux().pane_pid("s:@1.%1") is None
    fake.replies.append(Completed(stdout=b"%0\t1\n"))
    with pytest.raises(TmuxError, match="gone"):
        Tmux().alternate_on("s:@1.%1")
    assert T.pane_id_of("zordon:@3.%7") == "%7" and T.pane_id_of("%7") == "%7" and T.pane_id_of("zordon") is None


def test_send_literal_is_always_dash_l_and_strips_controls(fake: FakeRun):
    Tmux().send_literal("s:@1.%1", "echo hi\x1b[A\x03\nnext\tcol\x7f")
    assert fake.last() == ["tmux", "send-keys", "-t", "s:@1.%1", "-l", "--", "echo hi[A next col"]
    with pytest.raises(ValueError):
        Tmux().send_literal("s:@1.%1", "\x1b\x03\n")
    with pytest.raises(ValueError):
        Tmux().send_literal("s:@1.%1", "   ")


def test_strip_control_keeps_text_that_looks_like_key_names():
    hostile = 'Enter;C-c;$(whoami);`id`;-l;--;Escape'
    assert strip_control(hostile) == hostile


def test_send_enter_is_separate_and_send_key_is_allowlisted(fake: FakeRun):
    t = Tmux()
    t.send_enter("s:@1.%1")
    assert fake.last() == ["tmux", "send-keys", "-t", "s:@1.%1", "Enter"]
    for key in ("Escape", "Down", "BTab", "C-u", "C-c", "Space", "1", "9"):
        t.send_key("s:@1.%1", key)
        assert fake.last() == ["tmux", "send-keys", "-t", "s:@1.%1", key]
    for bad in ("C-d", "M-x", "F1", "0", "10", "echo hi", "Enter Enter", ""):
        with pytest.raises(ValueError):
            t.send_key("s:@1.%1", bad)
    assert {"Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "C-u", "C-c", "Space"} <= KEY_ALLOWLIST
    assert {str(n) for n in range(1, 10)} <= KEY_ALLOWLIST
    assert len(KEY_ALLOWLIST) == 20


def test_kill_window_and_session(fake: FakeRun):
    Tmux().kill_window("s:@1.%1")
    assert fake.last() == ["tmux", "kill-window", "-t", "s:@1.%1"]
    Tmux().kill_session("zordon")
    assert fake.last() == ["tmux", "kill-session", "-t", "=zordon"]


# ---- integration: a real private tmux server ------------------------------------------


def _socket_path(name: str) -> str:
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.join(base, f"tmux-{os.getuid()}", name)


@pytest.fixture
def private_tmux():
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    name = f"zordon-test-{os.getpid()}"
    t = Tmux(socket=name)
    try:
        yield t
    finally:
        t.kill_server()
        try:
            os.unlink(_socket_path(name))  # tmux leaves the dead socket file behind
        except OSError:
            pass


def _wait_for(pred, timeout: float = 5.0):  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@pytest.mark.integration
def test_private_server_literal_text_is_not_interpreted(private_tmux: Tmux, tmp_path):
    t = private_tmux
    assert not t.server_alive()
    t.ensure_session("zt", cwd=str(tmp_path))
    assert t.server_alive() and t.has_session("zt")
    # A plain bash pane with no rc files, so the prompt is predictable.
    target = t.new_window("zt", "w", str(tmp_path), ["bash", "--noprofile", "--norc"], width=120, height=30)
    assert T.is_pane_target(target)
    assert t.pane_exists(target)
    assert t.window_size(target) == (120, 30)
    assert _wait_for(lambda t=t, target=target: t.pane_pid(target) is not None)
    assert t.alternate_on(target) is False

    hostile = 'echo "Enter;C-c;$(whoami);`id`;Escape;-l;--"'
    t.send_literal(target, hostile)
    assert _wait_for(lambda: any(hostile in line for line in t.capture(target)))
    # Nothing ran: no Enter was sent.
    assert not any(line.startswith("Enter;C-c;") for line in t.capture(target))
    before = t.capture(target)
    t.send_key(target, "C-u")  # clears the typed line
    assert _wait_for(lambda: not any(hostile in line for line in t.capture(target)))
    assert before != t.capture(target)

    t.send_literal(target, "printf '%s\\n' ok-marker")
    t.send_enter(target)
    assert _wait_for(lambda: "ok-marker" in t.capture(target))
    env = t.pane_environment(target)
    assert env.get("PATH")  # the pane shell is readable through /proc
    panes = t.list_panes()
    assert any(p.target == target and p.cwd == str(tmp_path) for p in panes)
    assert all(p.session == "zt" for p in panes)

    t.kill_window(target)
    assert _wait_for(lambda: not t.pane_exists(target))
    t.kill_session("zt")
    assert not t.has_session("zt")


@pytest.mark.integration
def test_private_server_capture_preserves_nbsp(private_tmux: Tmux, tmp_path):
    t = private_tmux
    t.ensure_session("zn", cwd=str(tmp_path))
    target = t.new_window("zn", "w", str(tmp_path), ["bash", "--noprofile", "--norc"])
    assert _wait_for(lambda: t.pane_pid(target) is not None)
    t.send_literal(target, "printf '\\u276f\\u00a0ghost   \\n'")
    t.send_enter(target)
    assert _wait_for(lambda: any(line.startswith("❯ ghost") for line in t.capture(target)))
    line = next(line for line in t.capture(target) if line.startswith("❯ ghost"))
    assert line == "❯ ghost"  # trailing U+0020 gone, U+00A0 kept
