"""``zordon doctor`` against isolated homes and fake binaries on PATH. No download ever
reaches the network: ``assets.download`` is replaced by a fake that writes a file of
the expected size."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from zordon import assets, doctor, paths
from zordon.config import Config
from zordon.doctor import FAIL, OK, SKIP, WARN, Check, DoctorOptions


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH with only a fake ``tmux`` and a fake ``claude``."""
    b = tmp_path / "bin"
    b.mkdir()
    _script(b / "tmux", 'echo "tmux 3.4"')
    _script(b / "claude", 'echo "2.1.287 (Claude Code)"')
    monkeypatch.setenv("PATH", str(b))
    return b


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def local_cfg() -> Config:
    """A config whose providers need nothing from the network and no model files but Silero."""
    cfg = Config.default()
    cfg.providers.stt = "fake"
    cfg.providers.tts = "silence"
    cfg.providers.normalizer = "passthrough"
    cfg.providers.router = "keyword"
    # A port nothing else on the machine uses, so "port is free" does not depend on the host.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        cfg.server.port = s.getsockname()[1]
    return cfg


def by_name(report: doctor.Report) -> dict[str, Check]:
    return {c.name: c for c in report.checks}


# ---- individual checks -----------------------------------------------------------------


def test_python_check_passes_here():
    assert doctor.check_python().status == OK


def test_tmux_check_with_fake_binaries(fake_bin: Path):
    c = doctor.check_tmux()
    assert c.status == OK and c.detail == "tmux 3.4"
    _script(fake_bin / "tmux", 'echo "tmux 3.1a"')
    c = doctor.check_tmux()
    assert c.status == FAIL and "3.2" in c.fix
    (fake_bin / "tmux").unlink()
    c = doctor.check_tmux()
    assert c.status == FAIL and "not found" in c.detail


def test_tmux_check_handles_a_hanging_binary(fake_bin: Path):
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    c = doctor.check_tmux(run=run)
    assert c.status == FAIL and "did not answer" in c.detail


def test_claude_check_with_fake_binary(fake_bin: Path):
    c = doctor.check_claude()
    assert c.status == OK and c.detail.startswith("2.1.287 (Claude Code)")
    assert "prompts verified against claude-code-2.1.287" in c.detail
    (fake_bin / "claude").unlink()
    c = doctor.check_claude()
    assert c.status == FAIL and ("npm install -g @anthropic-ai/claude-code" in c.fix or "install Claude Code" in c.fix)


def test_claude_check_warns_on_prompts_version_mismatch():
    """DOC-06: doctor shows PROMPTS_VERSION next to the installed claude and warns on a mismatch."""
    c = doctor.compare_prompts_version("2.1.287 (Claude Code)", "claude-code-2.1.287")
    assert c.status == OK
    c = doctor.compare_prompts_version("2.2.0 (Claude Code)", "claude-code-2.1.287")
    assert c.status == WARN and "prompts-version.md" in c.fix
    assert doctor.compare_prompts_version("garbage", "claude-code-2.1.287").status == OK


def test_curl_and_cuda_checks():
    """DOC-08: doctor reports curl (hooks) and, with stt_device = cuda, the CUDA libraries."""
    assert doctor.check_curl(lambda n: "/usr/bin/curl").status == OK
    c = doctor.check_curl(lambda n: None)
    assert c.status == WARN and "hook" in c.detail
    cfg = Config.default()
    assert doctor.check_cuda(cfg, lambda m: None) is None  # cpu: nothing to say
    cfg.providers.stt_device = "cuda"
    assert doctor.check_cuda(cfg, lambda m: object()).status == OK
    c = doctor.check_cuda(cfg, lambda m: None)
    assert c.status == FAIL and "nvidia.cublas" in c.detail


def test_claude_home_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert doctor.check_claude_home().status == OK  # conftest creates the dir
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    c = doctor.check_claude_home()
    assert c.status == WARN and "does not exist" in c.detail


def test_config_file_check(tmp_path: Path):
    p = tmp_path / "config.toml"
    assert doctor.check_config_file(p).status == WARN
    p.write_text("x = 1\n")
    p.chmod(0o644)
    c = doctor.check_config_file(p)
    assert c.status == WARN and "chmod 600" in c.fix
    p.chmod(0o600)
    assert doctor.check_config_file(p).status == OK


def test_token_check():
    cfg = Config.default()
    assert doctor.check_token(cfg) == Check("token", OK, "set")
    cfg.server.token = ""
    assert doctor.check_token(cfg).status == WARN
    cfg.server.bind = "0.0.0.0"
    c = doctor.check_token(cfg)
    assert c.status == FAIL and "0.0.0.0" in c.detail


def test_key_checks_say_set_and_never_print_the_value(monkeypatch: pytest.MonkeyPatch):
    cfg = Config.default()  # anthropic normalizer, jev router
    checks = {c.name: c for c in doctor.check_keys(cfg)}
    assert checks["key anthropic"].status == WARN and "ANTHROPIC_API_KEY" in checks["key anthropic"].fix
    assert checks["key typesafe"].status == WARN
    cfg.providers.keys["anthropic"] = "sk-ant-secret-value-000000000000"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-value-1234567890")
    checks = {c.name: c for c in doctor.check_keys(cfg)}
    assert checks["key anthropic"].status == OK and checks["key anthropic"].detail.startswith("set")
    assert checks["key typesafe"].status == OK
    blob = json.dumps([asdict(c) for c in checks.values()])
    assert "sk-ant" not in blob and "ts-secret" not in blob


def test_required_keys_follow_the_providers():
    cfg = Config.default()
    cfg.providers.stt, cfg.providers.tts = "groq", "elevenlabs"
    cfg.providers.normalizer, cfg.providers.router = "passthrough", "anthropic"
    needs = doctor.required_keys(cfg)
    assert set(needs) == {"groq", "elevenlabs", "anthropic"}
    cfg.providers.stt = cfg.providers.tts = "openai"
    assert doctor.required_keys(cfg)["openai"] == "stt tts"


def test_module_checks(local_cfg: Config):
    cfg = Config.default()
    found = {c.name: c.status for c in doctor.check_modules(cfg, find_spec=lambda m: object())}
    assert found == {
        "module onnxruntime": OK,
        "module faster-whisper": OK,
        "module kokoro-onnx": OK,
        "module typesafe-sdk": OK,
        "module anthropic": OK,
    }
    missing = {c.name: c for c in doctor.check_modules(cfg, find_spec=lambda m: None)}
    assert missing["module faster-whisper"].status == FAIL and "reinstall" in missing["module faster-whisper"].fix
    assert missing["module typesafe-sdk"].status == WARN  # the jev chain degrades to keyword
    assert [c.name for c in doctor.check_modules(local_cfg, find_spec=lambda m: object())] == ["module onnxruntime"]
    # The real find_spec works too (every dependency is installed in the venv).
    assert all(c.status == OK for c in doctor.check_modules(cfg, importlib.util.find_spec))


def test_model_checks_report_missing_files(local_cfg: Config):
    checks = doctor.model_checks(local_cfg, DoctorOptions())
    assert [c.name for c in checks] == ["model silero_vad.onnx"]
    assert checks[0].status == FAIL and "--download" in checks[0].fix
    cfg = Config.default()
    names = [c.name for c in doctor.model_checks(cfg, DoctorOptions())]
    assert names == [
        "model silero_vad.onnx",
        "model kokoro-v1.0.onnx",
        "model voices-v1.0.bin",
        "model faster-whisper-small.en",
    ]


def test_download_uses_the_asset_downloader(local_cfg: Config):
    calls: list[str] = []

    def fake_download(asset: assets.Asset, progress: Any = None, timeout: float = 60.0) -> Path:
        calls.append(asset.name)
        dest = assets.path_for(asset)
        paths.ensure_private_dir(dest.parent)
        dest.write_bytes(b"\0" * (asset.size or 1))
        return dest

    checks = doctor.model_checks(local_cfg, DoctorOptions(download=True), downloader=fake_download)
    assert calls == ["silero_vad"]
    assert checks[0].status == OK and str(paths.models_dir()) in checks[0].detail
    # Present now: a second run downloads nothing.
    calls.clear()
    doctor.model_checks(local_cfg, DoctorOptions(download=True), downloader=fake_download)
    assert calls == []


def test_download_failure_is_reported_not_raised(local_cfg: Config):
    def boom(asset: assets.Asset, **kw: Any) -> Path:
        raise OSError("network down")

    checks = doctor.model_checks(local_cfg, DoctorOptions(download=True), downloader=boom)
    assert checks[0].status == FAIL and "network down" in checks[0].detail


def test_wrong_size_model_is_a_failure(local_cfg: Config):
    dest = assets.path_for(assets.SILERO_VAD)
    paths.ensure_private_dir(dest.parent)
    dest.write_bytes(b"short")
    c = doctor.model_checks(local_cfg, DoctorOptions())[0]
    assert c.status == FAIL and "wrong size" in c.detail


@pytest.mark.provider
def test_model_checks_pass_with_the_test_models(local_cfg: Config):
    models = os.environ.get("ZORDON_TEST_MODELS")
    if not models or not (Path(models) / "silero_vad.onnx").is_file():
        pytest.skip("ZORDON_TEST_MODELS not set")
    cfg = Config.default()
    checks = {c.name: c for c in doctor.model_checks(cfg, DoctorOptions(verify_hashes=True))}
    assert checks["model silero_vad.onnx"].status == OK and "sha256 verified" in checks["model silero_vad.onnx"].detail
    assert checks["model kokoro-v1.0.onnx"].status == OK
    assert checks["model voices-v1.0.bin"].status == OK
    assert checks["model faster-whisper-small.en"].status == OK


def test_espeak_check(local_cfg: Config):
    assert doctor.check_espeak(local_cfg).status == SKIP
    cfg = Config.default()
    c = doctor.check_espeak(cfg, find_spec=lambda m: None)
    assert c.status == WARN and "espeakng_loader" in c.detail
    c = doctor.check_espeak(cfg)  # the real loader is installed in the venv
    assert c.status in (OK, WARN)
    if c.status == OK:
        assert "characters" in c.detail


def test_tunnel_binary_check(monkeypatch: pytest.MonkeyPatch):
    cfg = Config.default()
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    assert doctor.check_tunnel_binary(cfg, DoctorOptions()).status == WARN
    assert doctor.check_tunnel_binary(cfg, DoctorOptions(tunnel=True)).status == FAIL
    monkeypatch.setattr(assets, "find_binary", lambda name: "/opt/bin/cloudflared")
    c = doctor.check_tunnel_binary(cfg, DoctorOptions())
    assert c.status == OK and c.detail == "/opt/bin/cloudflared"
    cfg.tunnel.provider = "ngrok"
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    c = doctor.check_tunnel_binary(cfg, DoctorOptions(tunnel=True))
    assert c.status == FAIL and c.name == "ngrok"


def test_tunnel_download_only_with_tunnel_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = Config.default()
    calls: list[str] = []
    binary = tmp_path / "cloudflared"

    def fake_download(asset: assets.Asset, **kw: Any) -> Path:
        calls.append(asset.name)
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        return binary

    monkeypatch.setattr(assets, "find_binary", lambda name: str(binary) if binary.exists() else None)
    c = doctor.check_tunnel_binary(cfg, DoctorOptions(download=True), downloader=fake_download)
    assert calls == [] and c.status == WARN
    c = doctor.check_tunnel_binary(cfg, DoctorOptions(download=True, tunnel=True), downloader=fake_download)
    assert calls == ["cloudflared"] and c.status == OK and c.detail == str(binary)


def test_download_cloudflared_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    binary = tmp_path / "cloudflared"

    def fake_download(asset: assets.Asset, **kw: Any) -> Path:
        binary.write_bytes(b"#!/bin/sh\n")
        return binary

    monkeypatch.setattr(assets, "find_binary", lambda name: str(binary) if binary.exists() else None)
    assert doctor.download_cloudflared(fake_download) == str(binary)
    binary.unlink()
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    with pytest.raises(OSError, match="not runnable"):
        doctor.download_cloudflared(lambda asset, **kw: tmp_path / "elsewhere")


def test_install_hints_follow_the_installer(monkeypatch: pytest.MonkeyPatch):
    """PKG-4: a pipx-managed interpreter gets `pipx inject`, anything else `pip install`."""
    assert doctor.install_hint("local", pipx=False) == "pip install --upgrade --force-reinstall zordon"
    assert doctor.install_hint("local", pipx=True) == "pipx reinstall zordon"
    assert doctor.install_hint("jev", pipx=True) == "pipx inject zordon typesafe-sdk"
    assert doctor.install_hint("", "anthropic", pipx=False) == "pip install anthropic"
    assert doctor.install_hint("", "anthropic", pipx=True) == "pipx inject zordon anthropic"
    assert doctor.installed_with_pipx("/home/u/.local/pipx/venvs/zordon", {})
    assert doctor.installed_with_pipx("/opt/px/venvs/zordon", {"PIPX_HOME": "/opt/px"})
    assert not doctor.installed_with_pipx("/home/u/Code/zordon/.venv", {})
    monkeypatch.setattr(doctor.sys, "prefix", "/home/u/.local/pipx/venvs/zordon")
    cfg = Config.default()
    missing = {c.name: c for c in doctor.check_modules(cfg, find_spec=lambda m: None)}
    assert missing["module faster-whisper"].fix == "pipx reinstall zordon"
    assert missing["module typesafe-sdk"].fix == "pipx inject zordon typesafe-sdk"


def test_port_check():
    cfg = Config.default()
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        cfg.server.port = s.getsockname()[1]
        c = doctor.check_port(cfg)
        assert c.status == FAIL and "not free" in c.detail
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    cfg.server.port = free
    assert doctor.check_port(cfg).status == OK


def test_parse_version():
    assert doctor.parse_version("tmux 3.4") == (3, 4)
    assert doctor.parse_version("tmux 3.3a") == (3, 3)
    assert doctor.parse_version("2.1.287 (Claude Code)") == (2, 1, 287)
    assert doctor.parse_version("nope") is None


def test_probe_is_off_by_default_and_skips_without_keys(local_cfg: Config, fake_bin: Path):
    report = doctor.run_checks(local_cfg)
    assert not any(c.name.startswith("probe") for c in report.checks)
    cfg = Config.default()
    probes = {c.name: c for c in doctor.probe_checks(cfg)}
    assert probes["probe anthropic"].status == SKIP and probes["probe typesafe"].status == SKIP


# ---- the whole run ---------------------------------------------------------------------


def test_run_checks_table_and_exit_code(local_cfg: Config, fake_bin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    report = doctor.run_checks(local_cfg)
    names = by_name(report)
    assert names["python"].status == OK
    assert names["tmux"].status == OK and names["claude"].status == OK
    assert names["token"].status == OK
    assert names["providers"].detail == "stt=fake, tts=silence, normalizer=passthrough, router=keyword"
    assert names["model silero_vad.onnx"].status == FAIL
    assert names["cloudflared"].status == WARN
    assert names["port"].status == OK
    assert report.failures == [names["model silero_vad.onnx"]] and not report.ok
    table = doctor.format_table(report)
    assert "FAIL model silero_vad.onnx" in table and "fix: `zordon doctor --download`" in table
    assert "1 problem to fix." in table


def test_main_json_shape(local_cfg: Config, fake_bin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    local_cfg.save(paths.config_path())
    monkeypatch.setattr(assets, "find_binary", lambda name: None)
    code = doctor.main(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == doctor.EXIT_MISSING
    assert set(data) == {"version", "config_path", "ok", "checks"}
    assert data["ok"] is False and data["config_path"] == str(paths.config_path())
    assert all(set(c) == {"name", "status", "detail", "fix"} for c in data["checks"])
    assert {c["status"] for c in data["checks"]} <= {OK, WARN, FAIL, SKIP}
    assert {"python", "tmux", "claude", "config", "token", "port"} <= {c["name"] for c in data["checks"]}


def test_main_exit_zero_when_everything_is_in_place(local_cfg: Config, fake_bin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    local_cfg.save(paths.config_path())
    monkeypatch.setattr(assets, "find_binary", lambda name: "/opt/bin/cloudflared")
    monkeypatch.setattr(doctor, "model_checks", lambda cfg, opts, downloader=None: [Check("model silero_vad.onnx", OK, "fake")])
    assert doctor.main([]) == doctor.EXIT_OK
    out = capsys.readouterr().out
    assert "Everything Zordon needs is in place." in out
    assert "OK   config" in out


def test_main_download_flag_reaches_the_downloader(local_cfg: Config, fake_bin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    local_cfg.save(paths.config_path())
    calls: list[str] = []

    def fake_download(asset: assets.Asset, progress: Any = None, timeout: float = 60.0) -> Path:
        calls.append(asset.name)
        dest = assets.path_for(asset)
        paths.ensure_private_dir(dest.parent)
        dest.write_bytes(b"\0" * (asset.size or 1))
        return dest

    monkeypatch.setattr(assets, "download", fake_download)
    monkeypatch.setattr(assets, "find_binary", lambda name: "/opt/bin/cloudflared")
    assert doctor.main(["--download"]) == doctor.EXIT_OK
    assert calls == ["silero_vad"]
    assert stat.S_IMODE(paths.models_dir().stat().st_mode) == 0o700


def test_main_reports_a_broken_config(capsys: pytest.CaptureFixture[str]):
    paths.ensure_private_dir(paths.zordon_home())
    paths.config_path().write_text("[voice]\nverbosity = 'shouting'\n")
    assert doctor.main([]) == doctor.EXIT_CONFIG
    assert "config error" in capsys.readouterr().err
    assert doctor.main(["--json"]) == doctor.EXIT_CONFIG
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_load_config_defaults_without_a_file():
    cfg = doctor.load_config(None)
    assert cfg.server.token == "" and cfg.path == paths.config_path()


def test_doctor_never_runs_the_real_claude_or_tmux_when_absent(local_cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    names = by_name(doctor.run_checks(local_cfg))
    assert names["tmux"].status == FAIL and names["claude"].status == FAIL
