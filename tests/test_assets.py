import hashlib
from pathlib import Path

import httpx
import pytest

from zordon import assets, paths


def test_registry_shape():
    for a in assets.ASSETS.values():
        assert a.url.startswith("https://")
        assert a.filename
    assert assets.path_for(assets.SILERO_VAD).parent == paths.models_dir()
    assert assets.path_for(assets.CLOUDFLARED).parent == paths.bin_dir()


def test_download_verifies_hash(monkeypatch, tmp_path: Path):
    payload = b"hello model"
    good = assets.Asset("t", "https://example.invalid/t.bin", "t.bin", hashlib.sha256(payload).hexdigest(), len(payload))
    bad = assets.Asset("b", "https://example.invalid/b.bin", "b.bin", "00" * 32, len(payload))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    transport = httpx.MockTransport(handler)
    real_stream = httpx.stream

    def fake_stream(method, url, **kw):
        kw.pop("follow_redirects", None)
        kw.pop("timeout", None)
        client = httpx.Client(transport=transport)
        return client.stream(method, url, **kw)

    monkeypatch.setattr(assets.httpx, "stream", fake_stream)
    p = assets.download(good)
    assert p.read_bytes() == payload
    assert assets.is_present(good, verify_hash=True)
    with pytest.raises(OSError):
        assets.download(bad)
    assert not assets.path_for(bad).exists()
    assert not list(assets.path_for(bad).parent.glob(".dl-*"))
    monkeypatch.setattr(assets.httpx, "stream", real_stream)
