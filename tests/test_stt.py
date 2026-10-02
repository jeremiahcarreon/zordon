"""STT providers: OpenAI / Groq over httpx.MockTransport (request shape, auth,
parsing, errors), the factory, the WAV helper and, when the test models are
present, FasterWhisperSTT on the Kokoro clip."""

from __future__ import annotations

import io
import json
import math
import os
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from zordon.config import Config
from zordon.providers import ProviderError, ProviderNotConfigured, STTProvider, STTResult
from zordon.speech.resample import pcm16_to_float32, resample_int16
from zordon.speech.stt import FakeSTT, FasterWhisperSTT, GroqSTT, OpenAISTT, make_stt
from zordon.speech.stt.base import confidence_from_logprobs, pcm_to_wav_bytes

ONE_SECOND = np.zeros(16000, dtype=np.float32)
TONE = (0.3 * np.sin(np.arange(16000) * 2 * np.pi * 440 / 16000)).astype(np.float32)


class Recorder:
    """MockTransport handler that records the request and replies with ``response``."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def multipart_fields(request: httpx.Request) -> dict[str, tuple[dict[str, str], bytes]]:
    """Parse a multipart body into name -> (headers-ish, content)."""
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1].encode()
    fields: dict[str, tuple[dict[str, str], bytes]] = {}
    for part in request.content.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        headers: dict[str, str] = {}
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers["content-disposition"]
        name = disp.split('name="', 1)[1].split('"', 1)[0]
        fields[name] = (headers, body)
    return fields


VERBOSE = {
    "task": "transcribe",
    "language": "english",
    "duration": 3.88,
    "text": " The upload handler now retries three times before it gives up.",
    "segments": [
        {
            "id": 0,
            "start": 0.0,
            "end": 3.6,
            "text": " The upload handler now retries three times before it gives up.",
            "avg_logprob": -0.244,
            "no_speech_prob": 0.0011,
        },
    ],
}


# ---- helpers ----------------------------------------------------------------------


def test_pcm_to_wav_bytes_is_a_valid_16bit_mono_wav():
    wav = pcm_to_wav_bytes(TONE)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (
            1,
            2,
            16000,
            16000,
        )
        back = pcm16_to_float32(w.readframes(w.getnframes()))
    assert np.max(np.abs(back - TONE)) < 1e-4


def test_confidence_mapping():
    assert confidence_from_logprobs([]) is None
    assert confidence_from_logprobs([-0.244]) == pytest.approx(math.exp(-0.244))
    assert confidence_from_logprobs([0.5]) == 1.0
    assert confidence_from_logprobs([-50.0]) == pytest.approx(0.0, abs=1e-6)
    assert confidence_from_logprobs([float("nan"), -1.0]) == pytest.approx(math.exp(-1.0))


def test_resample_24k_to_16k_keeps_duration_and_tone():
    t = np.arange(24000) / 24000
    pcm24 = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()
    pcm16 = resample_int16(pcm24, 24000, 16000)
    assert len(pcm16) == 16000 * 2
    x = pcm16_to_float32(pcm16)
    # Dominant frequency survives.
    spectrum = np.abs(np.fft.rfft(x))
    assert abs(int(np.argmax(spectrum)) - 440) <= 2


# ---- OpenAI -----------------------------------------------------------------------


def test_openai_request_shape_and_parsing():
    rec = Recorder(httpx.Response(200, json=VERBOSE))
    stt = OpenAISTT("sk-test-key", transport=httpx.MockTransport(rec))
    assert isinstance(stt, STTProvider)
    result = stt.transcribe(TONE)

    req = rec.last
    assert req.method == "POST"
    assert str(req.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer sk-test-key"
    fields = multipart_fields(req)
    assert set(fields) == {"file", "model", "response_format", "language"}
    assert fields["model"][1] == b"whisper-1"
    assert fields["response_format"][1] == b"verbose_json"
    assert fields["language"][1] == b"en"
    file_headers, body = fields["file"]
    assert 'filename="audio.wav"' in file_headers["content-disposition"]
    assert file_headers["content-type"] == "audio/wav"
    assert body.startswith(b"RIFF") and body[8:12] == b"WAVE"
    with wave.open(io.BytesIO(body), "rb") as w:
        assert w.getframerate() == 16000 and w.getnframes() == 16000

    assert result.text == "The upload handler now retries three times before it gives up."
    assert result.confidence == pytest.approx(math.exp(-0.244))
    assert result.duration_s == pytest.approx(3.88)
    assert result.language == "english"


def test_openai_gpt4o_mini_uses_json_and_no_confidence():
    rec = Recorder(httpx.Response(200, json={"text": "hello there"}))
    stt = OpenAISTT("sk-test", model="gpt-4o-mini-transcribe", transport=httpx.MockTransport(rec))
    result = stt.transcribe(TONE)
    fields = multipart_fields(rec.last)
    assert fields["model"][1] == b"gpt-4o-mini-transcribe"
    assert fields["response_format"][1] == b"json"
    assert result.text == "hello there"
    assert result.confidence is None
    assert result.duration_s == pytest.approx(1.0)


def test_hallucinated_segments_are_dropped():
    payload = {
        "text": " You",
        "segments": [{"text": " You", "avg_logprob": -0.645, "no_speech_prob": 0.864}],
    }
    rec = Recorder(httpx.Response(200, json=payload))
    stt = OpenAISTT("sk-test", transport=httpx.MockTransport(rec))
    result = stt.transcribe(ONE_SECOND)
    assert result.text == ""
    assert result.confidence is None


def test_empty_audio_makes_no_request():
    rec = Recorder(httpx.Response(200, json=VERBOSE))
    stt = OpenAISTT("sk-test", transport=httpx.MockTransport(rec))
    assert stt.transcribe(np.zeros(0, dtype=np.float32)).text == ""
    assert rec.requests == []


def test_missing_key_is_not_configured():
    with pytest.raises(ProviderNotConfigured):
        OpenAISTT("")
    with pytest.raises(ProviderNotConfigured):
        GroqSTT("")


@pytest.mark.parametrize(
    "status,body",
    [
        (
            401,
            {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
        ),
        (429, {"error": {"message": "Rate limit reached"}}),
        (500, "internal error"),
    ],
)
def test_http_errors_become_provider_error(status, body):
    resp = (
        httpx.Response(status, json=body)
        if isinstance(body, dict)
        else httpx.Response(status, text=body)
    )
    stt = OpenAISTT("sk-secret-key-value", transport=httpx.MockTransport(Recorder(resp)))
    with pytest.raises(ProviderError) as e:
        stt.transcribe(TONE)
    msg = str(e.value)
    assert f"HTTP {status}" in msg
    assert "sk-secret" not in msg
    if isinstance(body, dict):
        assert body["error"]["message"] in msg


def test_timeout_and_network_errors_become_provider_error():
    stt = OpenAISTT("sk-test", transport=httpx.MockTransport(Recorder(httpx.ReadTimeout("slow"))))
    with pytest.raises(ProviderError) as e:
        stt.transcribe(TONE)
    assert "timed out" in str(e.value)
    stt = OpenAISTT(
        "sk-test", transport=httpx.MockTransport(Recorder(httpx.ConnectError("refused")))
    )
    with pytest.raises(ProviderError):
        stt.transcribe(TONE)


def test_invalid_json_is_provider_error():
    stt = OpenAISTT(
        "sk-test", transport=httpx.MockTransport(Recorder(httpx.Response(200, text="<html>")))
    )
    with pytest.raises(ProviderError):
        stt.transcribe(TONE)


def test_default_timeout_is_ten_seconds():
    stt = OpenAISTT(
        "sk-test", transport=httpx.MockTransport(Recorder(httpx.Response(200, json={"text": ""})))
    )
    assert stt.timeout == 10.0
    assert stt._client.timeout == httpx.Timeout(10.0)


# ---- Groq -------------------------------------------------------------------------


def test_groq_endpoint_and_model():
    rec = Recorder(httpx.Response(200, json=VERBOSE))
    stt = GroqSTT("gsk_test", transport=httpx.MockTransport(rec))
    result = stt.transcribe(TONE)
    assert str(rec.last.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert rec.last.headers["authorization"] == "Bearer gsk_test"
    fields = multipart_fields(rec.last)
    assert fields["model"][1] == b"whisper-large-v3-turbo"
    assert fields["response_format"][1] == b"verbose_json"
    assert "upload handler" in result.text
    assert result.confidence is not None and result.confidence > 0.5


# ---- fake + factory -----------------------------------------------------------------


def test_fake_stt_text_and_callable():
    f = FakeSTT("add retry logic")
    r = f.transcribe(TONE)
    assert (
        r.text == "add retry logic" and r.confidence == 0.9 and r.duration_s == pytest.approx(1.0)
    )
    assert f.call_count == 1
    g = FakeSTT(lambda pcm: STTResult(text=f"{len(pcm)} samples", confidence=0.1))
    assert g.transcribe(TONE).text == "16000 samples"
    assert isinstance(g, STTProvider)


def test_make_stt_openai_and_groq(monkeypatch: pytest.MonkeyPatch):
    cfg = Config()
    cfg.providers.stt = "openai"
    with pytest.raises(ProviderNotConfigured):
        make_stt(cfg)
    cfg.providers.keys["openai"] = "sk-x"
    stt = make_stt(cfg)
    assert (
        isinstance(stt, OpenAISTT) and stt.model == "whisper-1"
    )  # small.en is not an OpenAI model

    cfg.providers.stt = "groq"
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    stt = make_stt(cfg)
    assert isinstance(stt, GroqSTT) and stt.model == "whisper-large-v3-turbo"

    cfg.providers.stt_model = "whisper-large-v3"
    assert make_stt(cfg).model == "whisper-large-v3"


def test_make_stt_unknown_and_fake():
    cfg = Config()
    cfg.providers.stt = "nope"
    with pytest.raises(ProviderNotConfigured):
        make_stt(cfg)
    cfg.providers.stt = "fake"
    assert isinstance(make_stt(cfg), FakeSTT)


def test_make_stt_faster_whisper_is_lazy_and_honours_test_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = Config()  # stt = faster-whisper by default
    monkeypatch.delenv("ZORDON_TEST_MODELS", raising=False)
    stt = make_stt(cfg)
    assert isinstance(stt, FasterWhisperSTT)
    assert not stt.loaded
    assert stt.model_spec == "small.en"
    assert stt.models_dir == Path(os.environ["ZORDON_HOME"]) / "models"
    assert stt.device == "cpu" and stt.compute_type == "int8"

    (tmp_path / "faster-whisper-small.en").mkdir()
    monkeypatch.setenv("ZORDON_TEST_MODELS", str(tmp_path))
    stt = make_stt(cfg)
    assert stt.model_spec == str(tmp_path / "faster-whisper-small.en")

    cfg.providers.stt_device = "cuda"
    stt = make_stt(cfg)
    assert stt.device == "cuda" and stt.compute_type == "float16"


def test_faster_whisper_empty_audio_does_not_load():
    stt = FasterWhisperSTT("/nonexistent/dir")
    assert stt.transcribe(np.zeros(0, dtype=np.float32)).text == ""
    assert not stt.loaded


# ---- faster-whisper on real audio -------------------------------------------------------


def _models_dir() -> Path:
    env = os.environ.get("ZORDON_TEST_MODELS")
    if not env:
        pytest.skip("ZORDON_TEST_MODELS not set")
    d = Path(env)
    if (
        not (d / "faster-whisper-small.en" / "model.bin").is_file()
        or not (d / "kokoro_test_16k.wav").is_file()
    ):
        pytest.skip(f"faster-whisper test model missing under {d}")
    return d


@pytest.mark.provider
def test_faster_whisper_transcribes_kokoro_clip_and_ignores_silence():
    d = _models_dir()
    stt = FasterWhisperSTT(d / "faster-whisper-small.en", device="cpu", compute_type="int8")
    with wave.open(str(d / "kokoro_test_16k.wav"), "rb") as w:
        audio = pcm16_to_float32(w.readframes(w.getnframes()))
    result = stt.transcribe(audio)
    print(f"faster-whisper: {result!r}")
    assert "upload handler" in result.text.lower()
    assert result.confidence is not None and result.confidence > 0.5
    assert result.duration_s == pytest.approx(3.88, abs=0.01)
    assert stt.loaded

    silence = stt.transcribe(ONE_SECOND)
    assert silence.text == ""
    assert json.dumps(silence.text) == '""'
