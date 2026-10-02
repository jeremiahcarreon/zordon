from __future__ import annotations

import pytest

from zordon.transcript.redaction import MASK, redact


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
