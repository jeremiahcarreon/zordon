"""Normalizer implementations and the factory that picks one from config.

``make_normalizer`` never raises at startup: a missing Anthropic key logs a
warning and returns the passthrough normalizer, so the pipeline still speaks.
"""

from __future__ import annotations

import logging

from zordon.config import Config
from zordon.output.normalizer.anthropic import AnthropicNormalizer, answer_transcript_query
from zordon.output.normalizer.base import Normalizer, clean_spoken
from zordon.output.normalizer.passthrough import PassthroughNormalizer
from zordon.providers import ProviderError

__all__ = [
    "AnthropicNormalizer",
    "Normalizer",
    "PassthroughNormalizer",
    "answer_transcript_query",
    "clean_spoken",
    "make_normalizer",
]

log = logging.getLogger("zordon.output.normalizer")

NORMALIZERS = ("anthropic", "passthrough")


def make_normalizer(config: Config) -> Normalizer:
    """``providers.normalizer`` = ``anthropic`` with credentials -> AnthropicNormalizer;
    anything else -> PassthroughNormalizer (with a warning when that was not asked for)."""
    name = (config.providers.normalizer or "passthrough").strip().lower()
    if name == "passthrough":
        log.info("normalizer: passthrough (configured)")
        return PassthroughNormalizer()
    if name == "anthropic":
        key = config.providers.key("anthropic")
        try:
            return AnthropicNormalizer(
                key,
                model=config.providers.normalizer_model,
                timeout=config.providers.normalizer_timeout_seconds,
            )
        except ProviderError as exc:
            log.warning("normalizer: %s; falling back to passthrough (output will be terse)", exc)
            return PassthroughNormalizer()
        except OSError as exc:
            log.warning("normalizer: cannot load prompt (%s); falling back to passthrough", exc)
            return PassthroughNormalizer()
    log.warning(
        "normalizer: unknown provider %r (expected one of %s); using passthrough",
        name,
        ", ".join(NORMALIZERS),
    )
    return PassthroughNormalizer()
