"""The no-model normalizer: speaks the pre-passed text as it is.

Used when no Anthropic credentials are configured, when the config asks for it,
and as the yardstick in the eval harness. It applies only the deterministic
cleanup from ``base.clean_spoken``: markdown remnants removed, inline symbols
spoken, whitespace collapsed. It never rewrites words, so Caveman shorthand
stays terse; the design calls this "readable but terse".
"""

from __future__ import annotations

from zordon.output.normalizer.base import clean_spoken


class PassthroughNormalizer:
    name = "passthrough"

    def normalize(self, sentence: str, context: list[str]) -> str:
        """Markdown-only input (a lone fence, a rule) cleans to "" and is not spoken."""
        return clean_spoken(sentence)
