from __future__ import annotations

import pytest

from zordon.transcript.redaction import MASK, redact, redact_pair


@pytest.mark.parametrize(
    "text",
    [
        "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "export OPENAI_API_KEY='sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----",
        "postgres://admin:hunter2@db.internal:5432/app",
        "token: 0123456789abcdef0123456789abcdef01234567",
        "DB_PASSWORD=correct-horse-battery",
    ],
)
def test_masks_secrets(text):
    out, hit = redact(text)
    assert hit
    assert MASK in out
    for secret in ("hunter2", "sk-ant-api03", "wJalrXUtnFEMI", "correct-horse", "ghp_ABC", "EXAMPLEKEY"):
        assert secret not in out


@pytest.mark.parametrize(
    "text",
    [
        "edited auth.py, 8 lines changed",
        "The token count is 1200.",
        "Set TIMEOUT=30 in config",  # not a secret name
        "running the tests again",
        "https://github.com/user/repo/pull/12",
    ],
)
def test_leaves_normal_text_alone(text):
    out, hit = redact(text)
    assert not hit
    assert out == text


def test_assignment_keeps_variable_name():
    out, _ = redact("API_KEY=abcdef123456")
    assert out == f"API_KEY={MASK}"


# ---- SEC-4: JSON-quoted names, provider key shapes, config.toml lines -----------------


@pytest.mark.parametrize(
    "text, secret",
    [
        ('{"password": "hunter2hunter"}', "hunter2hunter"),
        ('"aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"', "wJalrXUtnFEMI"),
        ('  "api_key": "abcdef1234567890",', "abcdef1234567890"),
        ("GROQ key gsk_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghijkl", "gsk_AbCd"),
        ("elevenlabs = \"sk_0123456789abcdef0123456789abcdef0123456789abcdef\"", "sk_0123"),
        ("xi-api-key: 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("ELEVENLABS_API_KEY 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("typesafe = \"tsk_AbCdEfGhIjKlMnOpQrStUv\"", "tsk_AbCd"),
        ("typesafe = \"QwErTyUiOpAsDfGh\"", "QwErTyUiOpAsDfGh"),
        ("groq = \"QwErTyUiOpAsDfGh\"", "QwErTyUiOpAsDfGh"),
        ("anthropic = \"sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\"", "sk-ant-api03"),
        ("Your session token is: Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56", "Ab12Cd34"),
        ("Cookie: zordon_session=Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56; other=1", "Ab12Cd34"),
        ("npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "npm_AbCd"),
        ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "hf_AbCd"),
    ],
)
def test_masks_more_secret_shapes(text, secret):
    out, hit = redact(text)
    assert hit, text
    assert MASK in out
    assert secret not in out


def test_context_hex_keeps_context_word():
    out, _ = redact("xi-api-key: 0123456789abcdef0123456789abcdef")
    assert out == f"xi-api-key: {MASK}"
    # a bare 32-hex (a git short-ish hash, an md5) is not a secret without context
    out, hit = redact("md5 0123456789abcdef0123456789abcdef")
    assert not hit


def test_config_model_values_are_left_alone():
    out, hit = redact('stt = "openai"')
    assert not hit and out == 'stt = "openai"'


# ---- SEC-5: a key hard-wrapped by the TUI across two pane lines -----------------------

KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL"
TAIL = "MNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz01234567890123456789abcdefghijklmnopqrstuvwxyzAA"


def test_redact_pair_masks_wrapped_tail():
    first = f"  export ANTHROPIC_API_KEY={KEY}"
    # On its own the tail is plain alphanumerics and leaks.
    assert redact(TAIL) == (TAIL, False)
    out, hit = redact_pair(first, TAIL)
    assert hit
    assert out == MASK
    assert "MNOPQRSTUV" not in out


def test_redact_pair_masks_only_the_wrapped_part():
    out, hit = redact_pair(f"token={KEY}", TAIL + " and then prose continues")
    assert hit
    assert out == f"{MASK} and then prose continues"


def test_redact_pair_without_a_spanning_match_is_plain_redact():
    assert redact_pair("edited auth.py", "running the tests again") == ("running the tests again", False)
    assert redact_pair(None, "running the tests again") == ("running the tests again", False)
    assert redact_pair("", "DB_PASSWORD=correct-horse-battery")[0] == f"DB_PASSWORD={MASK}"
    # A secret inside curr itself is still masked when prev is unrelated.
    out, hit = redact_pair("some prose", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij done")
    assert hit and "ghp_ABC" not in out
