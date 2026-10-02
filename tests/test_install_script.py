"""The curl | sh installer: valid POSIX sh, does only what it says, never sudo."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"


def test_is_valid_posix_sh():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "set -eu" in text


def test_script_does_only_the_documented_steps():
    text = SCRIPT.read_text()
    assert "releases/download/${UV_VERSION}/uv-installer.sh" in text  # pinned uv release, no sudo
    assert "UV_INSTALLER_SHA256" in text and "checksum mismatch" in text  # verified before it runs
    assert "astral.sh/uv/install.sh" not in text  # never the moving target
    assert "uv python install" in text  # managed Python
    assert "uv tool install" in text  # isolated zordon
    assert "zordon setup" in text  # hands over to the wizard
    # system packages are the wizard's business, one yes at a time: the script never elevates
    code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("sudo " in ln and "no sudo" not in ln for ln in code_lines)
    assert "pip install" not in text
    assert "/dev/tty" in text  # the wizard gets the terminal back after `curl | sh`
    assert "MINGW" in text and "WSL" in text  # Windows: WSL2 yes, native no


def test_script_runs_with_no_setup_flag_in_dry_mode(tmp_path, monkeypatch):
    """With a fake uv on PATH the script completes its steps without touching the network."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fake = bindir / "uv"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"uv $*\" >> {log}\n"
        'case "$1" in\n'
        "  --version) echo 'uv 0.9.9';;\n"
        "  tool) case \"$2\" in list) echo '';; dir) echo " + f"'{bindir}'" + ";; *) ;; esac;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    zordon = bindir / "zordon"
    zordon.write_text(f"#!/bin/sh\necho \"zordon $*\" >> {log}\n[ \"$1\" = --version ] && echo 'zordon 0.1.0'\nexit 0\n")
    zordon.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "ZORDON_NO_SETUP": "1", "UV_INSTALL_DIR": str(bindir)}
    out = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    calls = log.read_text()
    assert "uv python install 3.12" in calls
    assert "uv tool install --python 3.12 zordon @ git+https://github.com/jeremiahcarreon/zordon@main" in calls
    assert "Skipping setup" in out.stdout


def test_pinned_checksum_is_a_sha256_and_mismatch_refuses(tmp_path):
    import re

    text = SCRIPT.read_text()
    m = re.search(r'UV_INSTALLER_SHA256="\$\{ZORDON_UV_INSTALLER_SHA256:-([0-9a-f]{64})\}"', text)
    assert m, "the pinned checksum must be a 64-hex sha256"
    # A fake fetch (curl) serves a different installer; the script must refuse to run it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\necho 'echo tampered'\n")
    curl.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "UV_INSTALL_DIR": str(tmp_path / "uvbin"), "ZORDON_NO_SETUP": "1"}
    out = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and "checksum mismatch" in out.stderr
    assert not (tmp_path / "uvbin").exists()
