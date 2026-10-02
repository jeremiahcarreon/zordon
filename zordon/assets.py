"""Downloadable assets: local models and the cloudflared binary.

Everything lands under ``paths.models_dir()`` / ``paths.bin_dir()``. Hashes and
sizes were recorded on 2026-10-01 against the upstream files; ``zordon doctor``
verifies them and re-downloads on mismatch.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from zordon import paths

log = logging.getLogger("zordon.assets")


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    url: str
    filename: str
    sha256: str | None
    size: int | None
    kind: str = "model"  # model | binary
    description: str = ""
    executable: bool = False


SILERO_VAD = Asset(
    name="silero_vad",
    url="https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
    filename="silero_vad.onnx",
    sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
    size=2_327_524,
    description="Silero VAD v6 (ONNX), used for speech onset and barge-in",
)

KOKORO_MODEL = Asset(
    name="kokoro_model",
    url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    filename="kokoro-v1.0.onnx",
    sha256="beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
    size=325_505_369,
    description="Kokoro v1.0 TTS (fp32 ONNX)",
)

KOKORO_VOICES = Asset(
    name="kokoro_voices",
    url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
    filename="voices-v1.0.bin",
    sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    size=28_214_398,
    description="Kokoro voice styles",
)

# faster-whisper models are folders fetched through huggingface_hub; see speech/stt/faster_whisper.py.
WHISPER_DIRNAME = "faster-whisper-small.en"
WHISPER_REPO = "Systran/faster-whisper-small.en"


def _cloudflared_asset() -> Asset:
    arch = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        suffix = "darwin-arm64.tgz" if arch in ("arm64", "aarch64") else "darwin-amd64.tgz"
    else:
        suffix = "linux-arm64" if arch in ("arm64", "aarch64") else "linux-amd64"
    return Asset(
        name="cloudflared",
        url=f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-{suffix}",
        filename="cloudflared" + (".tgz" if suffix.endswith(".tgz") else ""),
        sha256=None,
        size=None,
        kind="binary",
        description="cloudflared, for `zordon serve --tunnel`",
        executable=True,
    )


CLOUDFLARED = _cloudflared_asset()

ASSETS: dict[str, Asset] = {
    a.name: a for a in (SILERO_VAD, KOKORO_MODEL, KOKORO_VOICES, CLOUDFLARED)
}


def path_for(asset: Asset) -> Path:
    base = paths.bin_dir() if asset.kind == "binary" else paths.models_dir()
    return base / asset.filename


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_present(asset: Asset, verify_hash: bool = False) -> bool:
    p = path_for(asset)
    if not p.exists():
        return False
    if asset.size is not None and p.stat().st_size != asset.size:
        return False
    if verify_hash and asset.sha256 and sha256_of(p) != asset.sha256:
        return False
    return True


def download(
    asset: Asset,
    progress: Callable[[int, int | None], None] | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download ``asset`` to its path atomically. Verifies size and hash when known."""
    dest = path_for(asset)
    paths.ensure_private_dir(dest.parent)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".dl-")
    tmp = Path(tmp_name)
    done = 0
    try:
        with os.fdopen(tmp_fd, "wb") as out, httpx.stream(
            "GET", asset.url, follow_redirects=True, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0) or asset.size
            for chunk in resp.iter_bytes(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if asset.size is not None and tmp.stat().st_size != asset.size:
            raise OSError(f"{asset.name}: size {tmp.stat().st_size} != expected {asset.size}")
        if asset.sha256 and sha256_of(tmp) != asset.sha256:
            raise OSError(f"{asset.name}: sha256 mismatch")
        if asset.executable:
            os.chmod(tmp, 0o755)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        log.info("downloaded %s (%d bytes)", asset.name, done)
        return dest
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def find_binary(name: str) -> str | None:
    """Prefer our own ``bin_dir`` copy, then PATH."""
    own = paths.bin_dir() / name
    if own.exists() and os.access(own, os.X_OK):
        return str(own)
    return shutil.which(name)
