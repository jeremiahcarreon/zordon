# 0006: The Jev router calls typesafe-sdk directly; thresholds use probabilities

**Status:** accepted, 2026-10-02

## Question

From the design: "Does Jev's Choice primitive accept enough instruction context to
distinguish 'what did you change' (transcript) from 'change what you did' (Claude
Code)? If not, the fallback router becomes the default and Jev handles only the
yes/no gate."

## What was verified

`typesafe-sdk` 0.7.2 (MIT, Python >= 3.10) read from source and exercised against an
`httpx2.MockTransport`; the TypeSafe docs fetched 2026-10-01. No live call was made.

**Capacity: yes.** `instructions` and every `criteria` value accept a string, a dict
or a list, nested arbitrarily. The docs recommend structured criteria with `what`,
`not_for` and `examples` fields. Limits: 64k tokens per request, 32k for `state`
plus the longest single question; Choice up to 255 options; Score 2-10 levels; text
only, English primary. A four-way route request with full few-shot criteria is
about 2.9 KB on the wire. Whether Jev *discriminates* well is an accuracy question,
answered only by `eval/router_set.jsonl`.

**`jev` package.** `jev` 0.3.0 on PyPI requires Python 3.14 and is a `@jev.fn`
decorator layer over `typesafe-sdk >= 0.6`; it compiles a Pydantic return type into
the same `state` + `questions` request. Zordon is Python 3.12, so it cannot use
`jev`, and loses nothing by calling `TypeSafeClient.system_one` itself.

**Answer shapes.** `ChoiceAnswer.choice` is the top label; `.probabilities` is a
dict over labels summing to about 1; `.confidence` is a spread statistic, not the
top probability. The docs' three-option formula is `(3 x p_max - 1) / 2`; the
general `(N x p_max - 1) / (N - 1)` is inferred. For N = 4, a probability of 0.85 is
a confidence of about 0.80. `NoulAnswer.noul` is P(statement true) with no
confidence field. `ScoreAnswer.score` is the expected level and `.probabilities`
is per level.

**Failure modes.** Missing or blank key raises `TypeSafeError` at construction with
no network access; a wrong key surfaces only on the first request as
`TypeSafeAuthenticationError` (401). The default `RetryPolicy` (2 retries, 0.5 s
initial backoff, 30 s total budget) is far too slow for voice. All SDK failures
derive from `TypeSafeError`; Pydantic `ValidationError` from a malformed `Choice`
is not covered.

**Pricing and limits (docs).** $0.042 per million input tokens, output free; a
700-token route call is about $0.00003. Rate limits 100K tokens/s and 40
requests/s, "adjusting dynamically". Models: `jev-1.13.0`, aliases `jev-latest`
and `jev-preview`. **Latency is not documented anywhere fetched.**

**Haiku fallback (anthropic 1.11).** Structured outputs work on `claude-haiku-4-5`
via `output_config={"format": {"type": "json_schema", "schema": ...}}`; the schema
may not use `minimum`/`maximum`, so the 0-1 range goes in `description` and is
clamped client-side. `temperature` is no longer a `messages.create` keyword and
must be sent as `extra_body={"temperature": 0}`. `max_retries=0` is required
(a 529 made three attempts under the default). Prompt caching does not engage: the
minimum cacheable prefix on Haiku 4.5 is 4,096 tokens and the router prompt is a
few hundred. A client built with `api_key=""` treats it as an explicit empty key,
skips environment discovery, and raises a bare `TypeError` on the first request;
blank keys are mapped to `None`.

## Decision

* `routing/typesafe.py` depends on `typesafe-sdk>=0.7.2,<0.8` and calls
  `system_one` directly. One sync client per process, built with the key passed
  explicitly (never exported to the environment, so child processes such as
  cloudflared cannot see it), `timeout=1.5`, and
  `RetryPolicy(max_retries=1, backoff_initial=0.1, backoff_max=0.2, timeout=2.0)`.
* One request per utterance carries a four-way `Choice` (`claude_code`,
  `transcript_query`, `shim_command`, `unclear`) and a `Noul` "is this a question
  about the past". The decision threshold is `probabilities[choice] >=
  voice.router_confidence` (0.85), **not** `confidence`, because the design states
  the threshold as a probability. `confidence` is logged next to it. `unclear` or a
  low probability routes to Claude Code (the safe failure). `transcript_query` also
  requires the Noul to agree; disagreement routes to Claude Code.
* The permission gate is three Nouls in one call: `yes`, `no`, `always`. Accept
  `yes` only when P(yes) >= `voice.yes_no_confidence` (0.95) and P(no) <= 0.05;
  `no` symmetrically; P(always) >= 0.5 is refused with a spoken explanation.
  Two Nouls because P(yes) = 0.03 means "not yes", which an off-topic utterance also
  scores; it does not mean "no".
* `prompt_score` is a three-level `Score` over the last 10 pane lines (working,
  idle, blocked). P(level 2) is the design's "how likely is this a prompt".
* Fallback chain, per utterance: Jev; on any `TypeSafeError`, the Haiku router
  (`routing/anthropic.py`, structured output, 1.5 s, no retries); on any Anthropic
  failure or no key, the keyword router (`routing/keyword.py`, deterministic, no
  network: shim commands by phrase table, yes/no by word list, everything else to
  Claude Code). At startup a wrong TypeSafe key is detected with
  `client.models.list(timeout=3.0, retry=RetryPolicy(max_retries=0))` and the
  Haiku router becomes primary for the process.
* Whatever the router answers, `dispatcher` only executes a command whose name is
  in `routing/commands.py`; a `shim_command` with an unknown name becomes `unclear`.

## Open

* **Latency.** The design assumed sub-100 ms. The eval harness records p50 and p95
  wall-clock for Jev and for Haiku on the same 60 utterances, and the default
  router is chosen from that measurement. Until then `providers.router = "jev"`
  is the configured default and the chain above covers its absence.
* **Accuracy.** The 95% target and the zero-misrouted-transcript-query rule are
  tested by `eval/router_set.jsonl`, not assumed.
* The confidence formula for N > 3 is inferred and unused for decisions.
* `jev-latest` is a moving alias. `resp.model` reports the concrete version and is
  logged by the eval so a regression can be tied to a model change; pinning
  `jev-1.13.0` is an option once the eval passes.
* Whether Jev's `Score` second opinion improves prompt detection over the regex
  alone is unmeasured; the idle watchdog remains the backstop regardless.
