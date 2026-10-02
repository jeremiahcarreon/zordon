"""Normalizer implementations and the factory that picks one from config.

``make_normalizer`` never raises at startup: a missing Anthropic key logs a
warning and returns the passthrough normalizer, so the pipeline still speaks.
"""

from __future__ import annotations

import logging

from zordon.config import Config
from zordon.output.normalizer.anthropic import AnthropicNormalizer, answer_transcript_query
from zordon.output.normalizer.base import Normalizer, clean_spoken
from zordon.output.normalizer.claude_cli import ClaudeCliNormalizer, claude_binary
from zordon.output.normalizer.ollama import OllamaNormalizer
from zordon.output.normalizer.passthrough import PassthroughNormalizer
from zordon.providers import ProviderError

__all__ = [
    "AnthropicNormalizer",
    "ClaudeCliNormalizer",
    "OllamaNormalizer",
    "Normalizer",
    "PassthroughNormalizer",
    "answer_transcript_query",
    "claude_binary",
    "clean_spoken",
    "make_normalizer",
]

log = logging.getLogger("zordon.output.normalizer")

NORMALIZERS = ("auto", "anthropic", "ollama", "claude-cli", "passthrough")


def make_normalizer(config: Config) -> Normalizer:
    """Pick the normalizer from ``providers.normalizer``.

    ``auto`` (the default): the Anthropic API when a key is configured (per-sentence),
    else a local Ollama server with the configured model (per-sentence, no key),
    else headless Claude Code under the user's login (per-turn, no key), else
    passthrough. An explicit name is honoured and falls back to passthrough
    with a warning when it cannot be built. Never raises.
    """
    name = (config.providers.normalizer or "auto").strip().lower()
    if name == "passthrough":
        log.info("normalizer: passthrough (configured)")
        return PassthroughNormalizer()
    if name not in NORMALIZERS:
        log.warning(
            "normalizer: unknown provider %r (expected one of %s); using passthrough",
            name,
            ", ".join(NORMALIZERS),
        )
        return PassthroughNormalizer()

    if name in ("auto", "anthropic"):
        key = config.providers.key("anthropic")
        if key or name == "anthropic":
            try:
                return AnthropicNormalizer(
                    key,
                    model=config.providers.normalizer_model,
                    timeout=config.providers.normalizer_timeout_seconds,
                )
            except ProviderError as exc:
                if name == "anthropic":
                    log.warning("normalizer: %s; falling back to passthrough (output will be terse)", exc)
                    return PassthroughNormalizer()
                log.info("normalizer: anthropic not usable (%s); trying claude-cli", exc)
            except OSError as exc:
                log.warning("normalizer: cannot load prompt (%s); falling back to passthrough", exc)
                return PassthroughNormalizer()

    if name in ("auto", "ollama"):
        try:
            o = OllamaNormalizer(
                config.providers.ollama_url,
                config.providers.ollama_model,
                timeout=config.providers.normalizer_timeout_seconds,
            )
            log.info("normalizer: ollama (%s at %s)", o.model, o.url)
            return o
        except ProviderError as exc:
            if name == "ollama":
                log.warning("normalizer: %s; falling back to passthrough (output will be terse)", exc)
                return PassthroughNormalizer()
            log.info("normalizer: ollama not usable (%s); trying claude-cli", exc)

    if name in ("auto", "claude-cli"):
        try:
            n = ClaudeCliNormalizer(
                model=config.providers.claude_cli_model,
                timeout=config.providers.claude_cli_timeout_seconds,
            )
            log.info(
                "normalizer: claude-cli (%s, whole turns are normalized once they finish; "
                "set providers.keys.anthropic for per-sentence normalization)",
                n.model,
            )
            return n
        except ProviderError as exc:
            if name == "claude-cli":
                log.warning("normalizer: %s; falling back to passthrough (output will be terse)", exc)
            else:
                log.warning(
                    "normalizer: no Anthropic key, no Ollama server and %s; using passthrough "
                    "(output will be terse)",
                    exc,
                )
            return PassthroughNormalizer()
    return PassthroughNormalizer()
