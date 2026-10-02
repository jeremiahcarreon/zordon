"""TTS provider tests. Cloud providers run against ``httpx.MockTransport``; the
Kokoro test needs the real model files and runs only with ``ZORDON_TEST_MODELS``."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from zordon.config import Config
from zordon.output import tts as tts_pkg
from zordon.output.tts import ElevenLabsTTS, KokoroTTS, OpenAITTS, SilenceTTS, make_tts
from zordon.output.tts import elevenlabs as el
from zordon.output.tts import kokoro as kk
from zordon.output.tts import openai as oa
from zordon.output.tts.base import (
    BYTES_PER_SAMPLE,
    CHUNK_SAMPLES,
    SampleAligner,
    chunk_pcm,
    duration_seconds,
    float_to_int16,
)
from zordon.providers import ProviderError, ProviderNotConfigured, TTSProvider

MODELS = os.environ.get("ZORDON_TEST_MODELS", "").strip()


def _collect(provider: TTSProvider, text: str) -> list[bytes]:
    return list(provider.synthesize(text))


# ---- base helpers ---------------------------------------------------------------------


class TestBase:
    def test_float_to_int16_clips(self):
        pcm = float_to_int16(np.array([0.0, 0.5, 1.0, -1.0, 2.0, -2.0], dtype=np.float32))
        arr = np.frombuffer(pcm, dtype="<i2")
        assert arr.tolist() == [0, 16383, 32767, -32767, 32767, -32767]
        assert float_to_int16(np.zeros(0)) == b""

    def test_chunk_pcm_whole_samples(self):
        data = bytes(range(256)) * 40  # 10240 bytes = 5120 samples
        chunks = list(chunk_pcm(data, 4096))
        assert [len(c) for c in chunks] == [8192, 2048]
        assert b"".join(chunks) == data
        odd = list(chunk_pcm(b"\x00" * 5, 2))
        assert [len(c) for c in odd] == [4]

    def test_sample_aligner_carries_odd_byte(self):
        a = SampleAligner()
        out = [a.feed(b"\x01\x02\x03"), a.feed(b"\x04\x05"), a.feed(b"\x06")]
        assert out == [b"\x01\x02", b"\x03\x04", b"\x05\x06"]
        assert a.flush() == b""
        assert list(SampleAligner().iter([b"\x01", b"\x02\x03", b"\x04"])) == [
            b"\x01\x02",
            b"\x03\x04",
        ]

    def test_duration(self):
        assert duration_seconds(48000, 24000) == 1.0


# ---- silence ----------------------------------------------------------------------------


class TestSilence:
    def test_protocol(self):
        s = SilenceTTS()
        assert isinstance(s, TTSProvider)
        assert s.sample_rate == 24000 and s.name == "silence"

    def test_duration_rule(self):
        s = SilenceTTS()
        pcm = b"".join(s.synthesize("0123456789"))  # 10 chars -> 100 ms
        assert len(pcm) == int(0.1 * 24000) * BYTES_PER_SAMPLE
        assert set(pcm) == {0}
        pcm = b"".join(s.synthesize("x" * 25))  # 25 chars -> 250 ms
        assert duration_seconds(len(pcm), 24000) == pytest.approx(0.25)

    def test_chunking(self):
        s = SilenceTTS()
        chunks = _collect(s, "x" * 200)  # 2 s = 48000 samples -> 11 full chunks + 1 of 2944
        assert all(len(c) % BYTES_PER_SAMPLE == 0 for c in chunks)
        assert all(len(c) <= CHUNK_SAMPLES * BYTES_PER_SAMPLE for c in chunks)
        assert [len(c) for c in chunks[:-1]] == [CHUNK_SAMPLES * BYTES_PER_SAMPLE] * 11
        assert sum(len(c) for c in chunks) == 48000 * BYTES_PER_SAMPLE

    def test_empty(self):
        assert _collect(SilenceTTS(), "") == []
        assert _collect(SilenceTTS(), "abc") != []


# ---- cloud providers -------------------------------------------------------------------------


PCM = bytes(range(256)) * 20  # 5120 bytes = 2560 samples, deterministic content


def _stream_response(chunks: list[bytes], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        stream=httpx.ByteStream(b"".join(chunks)) if len(chunks) == 1 else _IterStream(chunks),
    )


class _IterStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self):
        yield from self._chunks


class Recorder:
    def __init__(self, response: httpx.Response | Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response if response is not None else _stream_response([PCM])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    @property
    def request(self) -> httpx.Request:
        return self.requests[-1]


class TestOpenAI:
    def test_missing_key(self):
        with pytest.raises(ProviderNotConfigured):
            OpenAITTS("")
        with pytest.raises(ProviderNotConfigured):
            OpenAITTS(None)

    def test_request_and_passthrough(self):
        rec = Recorder(_stream_response([PCM[:1000], PCM[1000:1001], PCM[1001:]]))
        t = OpenAITTS("sk-openai-test", transport=httpx.MockTransport(rec))
        assert isinstance(t, TTSProvider)
        assert t.sample_rate == 24000
        chunks = _collect(t, "Done, tests pass.")
        assert b"".join(chunks) == PCM
        # Chunks pass through as they arrive; the odd byte of the 1-byte chunk is
        # carried into the next one so every emitted chunk holds whole samples.
        assert [len(c) for c in chunks] == [1000, len(PCM) - 1000]
        assert all(len(c) % 2 == 0 for c in chunks)
        req = rec.request
        assert req.method == "POST"
        assert str(req.url) == "https://api.openai.com/v1/audio/speech"
        assert req.headers["authorization"] == "Bearer sk-openai-test"
        assert req.headers["content-type"].startswith("application/json")
        body = json.loads(req.content)
        assert body == {
            "model": "gpt-4o-mini-tts",
            "input": "Done, tests pass.",
            "voice": "alloy",
            "response_format": "pcm",
        }

    def test_options(self):
        rec = Recorder()
        t = OpenAITTS(
            "k",
            model="tts-1",
            voice="nova",
            instructions="calm",
            transport=httpx.MockTransport(rec),
        )
        _collect(t, "hi")
        body = json.loads(rec.request.content)
        assert (
            body["model"] == "tts-1" and body["voice"] == "nova" and body["instructions"] == "calm"
        )

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_http_error(self, status):
        rec = Recorder(httpx.Response(status, json={"error": {"message": "nope"}}))
        t = OpenAITTS("k", transport=httpx.MockTransport(rec))
        with pytest.raises(ProviderError) as ei:
            _collect(t, "hi")
        assert str(status) in str(ei.value)
        assert "Bearer" not in str(ei.value)

    def test_auth_error_is_not_configured(self):
        rec = Recorder(httpx.Response(401, json={"error": {"message": "bad key"}}))
        with pytest.raises(ProviderNotConfigured):
            _collect(OpenAITTS("k", transport=httpx.MockTransport(rec)), "hi")

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.ConnectTimeout("slow")],
    )
    def test_transport_errors(self, exc):
        rec = Recorder(exc)
        with pytest.raises(ProviderError):
            _collect(OpenAITTS("k", transport=httpx.MockTransport(rec)), "hi")

    def test_empty_text_makes_no_request(self):
        rec = Recorder()
        assert _collect(OpenAITTS("k", transport=httpx.MockTransport(rec)), "  ") == []
        assert rec.requests == []

    def test_key_not_in_repr_or_logs(self, caplog):
        caplog.set_level(logging.DEBUG, logger="zordon.output.tts.openai")
        rec = Recorder(httpx.Response(500, text="boom"))
        t = OpenAITTS("sk-very-secret", transport=httpx.MockTransport(rec))
        with pytest.raises(ProviderError):
            _collect(t, "hi")
        assert "sk-very-secret" not in " ".join(r.getMessage() for r in caplog.records)
        assert "sk-very-secret" not in str(t) and "sk-very-secret" not in repr(t)


class TestElevenLabs:
    def test_missing_key(self):
        with pytest.raises(ProviderNotConfigured):
            ElevenLabsTTS("", voice_id="abc")

    def test_bad_voice_id(self):
        with pytest.raises(ProviderNotConfigured):
            ElevenLabsTTS("k", voice_id="a/b")

    def test_request_and_passthrough(self):
        rec = Recorder(_stream_response([PCM[:3], PCM[3:]]))
        t = ElevenLabsTTS(
            "xi-test-key", voice_id="21m00Tcm4TlvDq8ikWAM", transport=httpx.MockTransport(rec)
        )
        assert isinstance(t, TTSProvider)
        assert t.sample_rate == 24000
        chunks = _collect(t, "Done, tests pass.")
        assert b"".join(chunks) == PCM
        assert [len(c) for c in chunks] == [2, len(PCM) - 2]
        req = rec.request
        assert req.method == "POST"
        assert str(req.url) == (
            "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM/stream?output_format=pcm_24000"
        )
        assert req.headers["xi-api-key"] == "xi-test-key"
        assert "authorization" not in req.headers
        body = json.loads(req.content)
        assert body == {"text": "Done, tests pass.", "model_id": "eleven_flash_v2_5"}

    def test_voice_settings_and_model(self):
        rec = Recorder()
        t = ElevenLabsTTS(
            "k",
            voice_id="v" * 20,
            model_id="eleven_turbo_v2",
            voice_settings={"stability": 0.5},
            transport=httpx.MockTransport(rec),
        )
        _collect(t, "hi")
        body = json.loads(rec.request.content)
        assert body["model_id"] == "eleven_turbo_v2" and body["voice_settings"] == {
            "stability": 0.5
        }

    @pytest.mark.parametrize("status", [400, 422, 429, 500])
    def test_http_error(self, status):
        rec = Recorder(httpx.Response(status, json={"detail": "nope"}))
        with pytest.raises(ProviderError):
            _collect(
                ElevenLabsTTS("k", voice_id="v" * 20, transport=httpx.MockTransport(rec)), "hi"
            )

    def test_auth_error_is_not_configured(self):
        rec = Recorder(httpx.Response(401, json={"detail": "bad key"}))
        with pytest.raises(ProviderNotConfigured):
            _collect(
                ElevenLabsTTS("k", voice_id="v" * 20, transport=httpx.MockTransport(rec)), "hi"
            )

    def test_timeout(self):
        rec = Recorder(httpx.ReadTimeout("slow"))
        with pytest.raises(ProviderError):
            _collect(
                ElevenLabsTTS("k", voice_id="v" * 20, transport=httpx.MockTransport(rec)), "hi"
            )


# ---- kokoro -------------------------------------------------------------------------------------


class TestKokoroWithoutModel:
    def test_lazy_construction_and_missing_files(self, tmp_path: Path):
        k = KokoroTTS(tmp_path / "missing.onnx", tmp_path / "missing.bin")
        assert isinstance(k, TTSProvider)
        assert k.sample_rate == 24000 and k.name == "kokoro"
        assert k.loaded is False
        with pytest.raises(ProviderNotConfigured) as ei:
            _collect(k, "hello")
        assert "zordon doctor --download" in str(ei.value)

    def test_speed_is_clamped(self):
        assert KokoroTTS("m", "v", speed=5.0).speed == kk.SPEED_MAX
        assert KokoroTTS("m", "v", speed=0.1).speed == kk.SPEED_MIN
        assert KokoroTTS("m", "v", speed=1.3).speed == 1.3

    def test_empty_text_does_not_load(self, tmp_path: Path):
        k = KokoroTTS(tmp_path / "m", tmp_path / "v")
        assert _collect(k, "") == []
        assert k.loaded is False

    def test_espeak_path_short_enough_uses_default(self, tmp_path: Path):
        assert kk.espeak_data_path(limit=10_000) is None

    def test_espeak_path_override(self):
        assert kk.espeak_data_path("/short/espeak-ng-data") == "/short/espeak-ng-data"

    def test_espeak_path_copies_when_too_long(self, tmp_path: Path, monkeypatch):
        import espeakng_loader

        bundled = Path(espeakng_loader.get_data_path())
        assert (bundled / "phontab").exists()
        home = tmp_path / "zh"
        # A limit of 1 forces the "too long" branch for the bundled path; the
        # destination must still fit, so pass a limit that the copy satisfies.
        dest = kk.espeak_data_path(None, home=home, limit=len(str(bundled)))
        assert dest == str(home / "espeak-ng-data")
        assert (home / "espeak-ng-data" / "phontab").exists()
        # Second call reuses the copy.
        assert kk.espeak_data_path(None, home=home, limit=len(str(bundled))) == dest

    def test_espeak_path_home_too_long(self, tmp_path: Path):
        import espeakng_loader

        bundled = Path(espeakng_loader.get_data_path())
        with pytest.raises(ProviderNotConfigured):
            kk.espeak_data_path(None, home=tmp_path / ("x" * 200), limit=len(str(bundled)))


def _kokoro_files() -> tuple[Path, Path]:
    model = Path(MODELS) / "kokoro-v1.0.onnx"
    voices = Path(MODELS) / "voices-v1.0.bin"
    if not (model.is_file() and voices.is_file()):
        pytest.skip("kokoro model files absent")
    return model, voices


@pytest.fixture(scope="module")
def kokoro_provider() -> KokoroTTS:
    return KokoroTTS(*_kokoro_files())


@pytest.mark.provider
@pytest.mark.skipif(not MODELS, reason="ZORDON_TEST_MODELS not set")
class TestKokoroReal:
    def test_synthesize(self, kokoro_provider: KokoroTTS, caplog):
        provider = kokoro_provider
        caplog.set_level(logging.DEBUG, logger="zordon.output.tts.kokoro")
        started = time.monotonic()
        chunks: list[bytes] = []
        first_ms = None
        for chunk in provider.synthesize("Done, tests pass."):
            if first_ms is None:
                first_ms = (time.monotonic() - started) * 1000
            chunks.append(chunk)
        assert provider.loaded and provider.load_ms is not None
        assert provider.sample_rate == 24000
        pcm = b"".join(chunks)
        assert len(pcm) % BYTES_PER_SAMPLE == 0
        arr = np.frombuffer(pcm, dtype="<i2")
        assert arr.dtype == np.int16
        assert 0.8 <= duration_seconds(len(pcm), 24000) <= 3.0
        assert int(np.abs(arr).max()) > 1000  # not silence
        assert all(len(c) <= CHUNK_SAMPLES * BYTES_PER_SAMPLE for c in chunks)
        assert provider.last_first_chunk_ms is not None
        assert any("first chunk after" in r.getMessage() for r in caplog.records)
        assert first_ms is not None and first_ms < 5000

    def test_unknown_voice(self):
        model, voices = _kokoro_files()
        with pytest.raises(ProviderNotConfigured):
            _collect(KokoroTTS(model, voices, voice="nope"), "hi")

    def test_voices_listed(self, kokoro_provider: KokoroTTS):
        voices = kokoro_provider.voices()
        assert "af_heart" in voices and "am_adam" in voices and len(voices) >= 50


# ---- factory -------------------------------------------------------------------------------------


class TestFactory:
    def test_kokoro_missing_files(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ZORDON_TEST_MODELS", raising=False)
        cfg = Config()
        assert cfg.providers.tts == "kokoro"
        with pytest.raises(ProviderNotConfigured) as ei:
            make_tts(cfg)
        assert "zordon doctor --download" in str(ei.value)
        assert str(tmp_path) in str(ei.value)  # resolved under the isolated ZORDON_HOME

    def test_kokoro_test_models_override(self, monkeypatch, tmp_path: Path):
        fake = tmp_path / "models"
        fake.mkdir()
        (fake / "kokoro-v1.0.onnx").write_bytes(b"x")
        (fake / "voices-v1.0.bin").write_bytes(b"x")
        monkeypatch.setenv("ZORDON_TEST_MODELS", str(fake))
        assert tts_pkg.models_dir() == fake
        cfg = Config()
        cfg.providers.tts_voice = "am_adam"
        cfg.providers.tts_speed = 1.2
        t = make_tts(cfg)
        assert isinstance(t, KokoroTTS)
        assert t.model_path == fake / "kokoro-v1.0.onnx"
        assert t.voice == "am_adam" and t.speed == 1.2
        assert t.loaded is False  # lazy

    def test_openai(self, monkeypatch):
        cfg = Config()
        cfg.providers.tts = "openai"
        with pytest.raises(ProviderNotConfigured):
            make_tts(cfg)
        cfg.providers.keys["openai"] = "sk-x"
        t = make_tts(cfg)
        assert isinstance(t, OpenAITTS)
        assert t.voice == oa.DEFAULT_VOICE  # af_heart is not an OpenAI voice
        cfg.providers.tts_voice = "nova"
        assert make_tts(cfg).voice == "nova"

    def test_elevenlabs(self, monkeypatch):
        cfg = Config()
        cfg.providers.tts = "elevenlabs"
        with pytest.raises(ProviderNotConfigured):
            make_tts(cfg)
        monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-x")
        t = make_tts(cfg)
        assert isinstance(t, ElevenLabsTTS)
        assert t.voice_id == el.DEFAULT_VOICE_ID
        cfg.providers.tts_voice = "pNInz6obpgDQGcFmaJgB"
        assert make_tts(cfg).voice_id == "pNInz6obpgDQGcFmaJgB"

    def test_silence_only_when_asked(self, caplog):
        caplog.set_level(logging.WARNING, logger="zordon.output.tts")
        cfg = Config()
        cfg.providers.tts = "silence"
        assert isinstance(make_tts(cfg), SilenceTTS)
        assert any("silent" in r.getMessage() for r in caplog.records)

    def test_unknown_provider(self):
        cfg = Config()
        cfg.providers.tts = "polly"
        with pytest.raises(ProviderNotConfigured):
            make_tts(cfg)
