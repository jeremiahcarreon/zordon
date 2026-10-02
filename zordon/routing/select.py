"""Router selection: the fallback chain and the config-driven factory.

``FallbackRouter`` always runs the ``KeywordRouter`` first. Its answer is final
only when it is certain and cheap to be certain about: an exact shim command, or
a strict yes/no, at confidence 0.95 or higher. "mute" never costs a network
call. Anything else goes to the next router in the chain; a ``ProviderError``
moves on to the one after; the keyword router's own answer is the last resort.

``make_router(config)`` builds the chain from ``config.providers.router``:

    jev        Keyword -> Jev (if a TypeSafe key resolves) -> Haiku (if Anthropic credentials)
    anthropic  Keyword -> Haiku
    keyword    Keyword only

It logs which routers are active and never logs a key.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from zordon.config import Config
from zordon.routing.base import (
    ProviderError,
    ProviderNotConfigured,
    RouteContext,
    Router,
    RouteResult,
    YesNoResult,
)
from zordon.routing.keyword import KeywordRouter

log = logging.getLogger("zordon.routing.select")

FAST_PATH_CONFIDENCE = 0.95
# Below this the regex prompt score is "unsure" and a smarter router is consulted.
PROMPT_SCORE_SURE = 0.95


class FallbackRouter:
    """Tries each router in order; the keyword router is both fast path and last resort."""

    def __init__(self, chain: Sequence[Router], confidence_threshold: float = 0.85) -> None:
        if not chain:
            chain = [KeywordRouter()]
        self.chain: list[Router] = list(chain)
        self.confidence_threshold = confidence_threshold
        self.keyword: Router = next(
            (r for r in self.chain if isinstance(r, KeywordRouter)), None
        ) or KeywordRouter()
        self.smart: list[Router] = [r for r in self.chain if r is not self.keyword]
        self.name = "fallback(" + ",".join(r.name for r in [self.keyword, *self.smart]) + ")"

    # ---- Router ------------------------------------------------------------------------

    def route(self, utterance: str, ctx: RouteContext) -> RouteResult:
        kw = self.keyword.route(utterance, ctx)
        if kw.destination == "shim_command" and kw.confidence >= FAST_PATH_CONFIDENCE:
            return kw
        if kw.destination == "unclear" and kw.confidence >= FAST_PATH_CONFIDENCE:
            # Empty or filler-only utterance: nothing for a model to decide.
            return kw
        for router in self.smart:
            try:
                return router.route(utterance, ctx)
            except ProviderError as e:
                log.warning("router %s failed, trying next: %s", router.name, e)
        return kw

    def yes_no(self, utterance: str) -> YesNoResult:
        kw = self.keyword.yes_no(utterance)
        if kw.answer in ("yes", "no") and kw.confidence >= FAST_PATH_CONFIDENCE:
            return kw
        if kw.answer == "unclear" and getattr(kw, "reason", "") == "always":
            # A forbidden phrase is refused without asking anyone.
            return kw
        for router in self.smart:
            try:
                return router.yes_no(utterance)
            except ProviderError as e:
                log.warning("router %s yes_no failed, trying next: %s", router.name, e)
        return kw

    def prompt_score(self, lines: list[str]) -> float:
        kw = self.keyword.prompt_score(lines)
        if kw >= PROMPT_SCORE_SURE:
            return kw
        for router in self.smart:
            try:
                return router.prompt_score(lines)
            except ProviderError as e:
                log.warning("router %s prompt_score failed, trying next: %s", router.name, e)
        return kw


def make_router(config: Config) -> Router:
    """Build the router chain the config asks for. Never raises for a missing key."""
    pref = (config.providers.router or "jev").lower()
    if pref not in ("jev", "anthropic", "keyword"):
        log.warning("unknown providers.router=%r; using the jev chain", pref)
        pref = "jev"
    chain: list[Router] = [KeywordRouter()]

    if pref == "jev":
        jev = _try_jev(config)
        if jev is not None:
            chain.append(jev)
    if pref in ("jev", "anthropic"):
        haiku = _try_haiku(config)
        if haiku is not None:
            chain.append(haiku)
    if pref != "keyword" and len(chain) == 1:
        log.warning(
            "providers.router=%s but no provider is configured; shim commands and yes/no still work, "
            "everything else goes to Claude Code",
            pref,
        )
    log.info("active routers: %s", ", ".join(r.name for r in chain))
    return FallbackRouter(chain, confidence_threshold=config.voice.router_confidence)


def _try_jev(config: Config) -> Router | None:
    try:
        from zordon.routing.typesafe import JevRouter  # noqa: PLC0415

        return JevRouter(
            config.providers.key("typesafe"),
            confidence_threshold=config.voice.router_confidence,
            yes_no_threshold=config.voice.yes_no_confidence,
        )
    except ProviderNotConfigured as e:
        log.info("jev router not configured: %s", e)
        return None


def _try_haiku(config: Config) -> Router | None:
    try:
        from zordon.routing.anthropic import HaikuRouter  # noqa: PLC0415

        return HaikuRouter(
            config.providers.key("anthropic"),
            model=config.providers.router_model or "claude-haiku-4-5",
        )
    except ProviderNotConfigured as e:
        log.info("anthropic router not configured: %s", e)
        return None
