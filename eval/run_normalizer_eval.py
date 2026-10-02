#!/usr/bin/env python3
"""Run a Normalizer over ``eval/normalizer_set.jsonl`` and score it.

Two scores per sample:

* **exact**: the output equals ``expected`` after case, whitespace and trailing
  punctuation are normalized. Strict; a wording change fails it.
* **lenient**: the rubric a spoken sentence must meet regardless of wording:
  every ``must_include`` word is present (case-insensitive), no
  ``must_not_include`` string is present, the output is a single sentence, and
  it carries no markdown.

Usage::

    python eval/run_normalizer_eval.py                      # passthrough (no network)
    python eval/run_normalizer_eval.py --provider anthropic # real key, real calls, costs money
    python eval/run_normalizer_eval.py --verbose --min-lenient 0.9

Exit status is 0 when the lenient and exact pass rates meet ``--min-lenient``
and ``--min-exact``, 1 otherwise. The test suite only ever runs the passthrough
provider through this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zordon.output.sentences import split_sentences  # noqa: E402
from zordon.providers import Normalizer  # noqa: E402

DEFAULT_SET = Path(__file__).with_name("normalizer_set.jsonl")
EXPECTED_COUNT = 50
REQUIRED_FIELDS = ("input", "context", "expected", "must_include", "must_not_include")

_MARKDOWN = (
    re.compile(r"`"),
    re.compile(r"(^|\s)#{1,6}(\s|$)"),
    re.compile(r"\*\*"),
    re.compile(r"(^|\n)\s*[-*+]\s"),
    re.compile(r"\[[^\]]+\]\([^)]*\)"),
    re.compile(r"```"),
)
_TRAILING_PUNCT = re.compile(r"[\s.!?;:,]+$")
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class Sample:
    input: str
    context: list[str]
    expected: str
    must_include: list[str]
    must_not_include: list[str]
    line_no: int = 0


@dataclass(slots=True)
class Score:
    output: str
    exact: bool
    lenient: bool
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


# ---- the set --------------------------------------------------------------------


def load_set(path: Path = DEFAULT_SET) -> list[Sample]:
    samples: list[Sample] = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            samples.append(
                Sample(
                    input=str(raw["input"]),
                    context=[str(c) for c in raw.get("context", [])],
                    expected=str(raw["expected"]),
                    must_include=[str(w) for w in raw.get("must_include", [])],
                    must_not_include=[str(w) for w in raw.get("must_not_include", [])],
                    line_no=n,
                )
            )
    return samples


def validate_set(samples: list[Sample], expected_count: int = EXPECTED_COUNT) -> list[str]:
    """Return a list of problems (empty when the file is well formed)."""
    problems: list[str] = []
    if len(samples) != expected_count:
        problems.append(f"expected {expected_count} samples, found {len(samples)}")
    seen: dict[str, int] = {}
    for s in samples:
        where = f"line {s.line_no}"
        if not s.input.strip():
            problems.append(f"{where}: empty input")
        if not s.expected.strip():
            problems.append(f"{where}: empty expected")
        if s.input in seen:
            problems.append(f"{where}: duplicate input (also line {seen[s.input]})")
        seen.setdefault(s.input, s.line_no)
        if not s.must_include:
            problems.append(f"{where}: must_include is empty")
        low = s.expected.lower()
        for w in s.must_include:
            if w.lower() not in low:
                problems.append(f"{where}: expected lacks must_include {w!r}")
        for w in s.must_not_include:
            if w in s.expected:
                problems.append(f"{where}: expected contains must_not_include {w!r}")
        if not is_single_sentence(s.expected):
            problems.append(f"{where}: expected is not a single sentence")
        if has_markdown(s.expected):
            problems.append(f"{where}: expected contains markdown")
    return problems


# ---- scoring ---------------------------------------------------------------------


def canonical(text: str) -> str:
    out = (text or "").strip().lower()
    out = out.replace("’", "'").replace("“", '"').replace("”", '"')
    out = _WS.sub(" ", out)
    return _TRAILING_PUNCT.sub("", out)


def is_exact(output: str, expected: str) -> bool:
    return canonical(output) == canonical(expected)


def is_single_sentence(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if "\n" in text:
        return False
    return len(split_sentences(text)) <= 1


def has_markdown(text: str) -> bool:
    return any(p.search(text or "") for p in _MARKDOWN)


def lenient_reasons(sample: Sample, output: str) -> list[str]:
    reasons: list[str] = []
    low = (output or "").lower()
    missing = [w for w in sample.must_include if w.lower() not in low]
    if missing:
        reasons.append("missing " + ", ".join(repr(w) for w in missing))
    present = [w for w in sample.must_not_include if w in (output or "")]
    if present:
        reasons.append("contains " + ", ".join(repr(w) for w in present))
    if not is_single_sentence(output):
        reasons.append("not a single sentence")
    if has_markdown(output):
        reasons.append("markdown")
    n_in, n_out = _words(sample.input), _words(output)
    if n_in >= 4 and n_out > 1.6 * n_in + 2:
        reasons.append(f"padded ({n_in} -> {n_out} words)")
    return reasons


_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def score(sample: Sample, output: str, latency_ms: float = 0.0) -> Score:
    reasons = lenient_reasons(sample, output)
    return Score(
        output=output,
        exact=is_exact(output, sample.expected),
        lenient=not reasons,
        reasons=reasons,
        latency_ms=latency_ms,
    )


def run(normalizer: Normalizer, samples: Iterable[Sample]) -> list[Score]:
    scores: list[Score] = []
    for s in samples:
        started = time.monotonic()
        try:
            out = normalizer.normalize(s.input, list(s.context))
        except Exception as exc:  # noqa: BLE001 - the harness must finish the table
            out = f"<error: {type(exc).__name__}: {exc}>"
        scores.append(score(s, out, (time.monotonic() - started) * 1000))
    return scores


@dataclass(slots=True)
class Summary:
    total: int
    exact: int
    lenient: int
    mean_latency_ms: float
    max_latency_ms: float

    @property
    def exact_rate(self) -> float:
        return self.exact / self.total if self.total else 0.0

    @property
    def lenient_rate(self) -> float:
        return self.lenient / self.total if self.total else 0.0


def summarize(scores: list[Score]) -> Summary:
    lat = [s.latency_ms for s in scores] or [0.0]
    return Summary(
        total=len(scores),
        exact=sum(1 for s in scores if s.exact),
        lenient=sum(1 for s in scores if s.lenient),
        mean_latency_ms=sum(lat) / len(lat),
        max_latency_ms=max(lat),
    )


# ---- output -----------------------------------------------------------------------


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def format_table(samples: list[Sample], scores: list[Score], verbose: bool = False) -> str:
    lines = [f"{'#':>3} {'exact':5} {'lenient':7} {'ms':>6}  input -> output"]
    for i, (s, sc) in enumerate(zip(samples, scores, strict=True), 1):
        flag_e = "yes" if sc.exact else "no"
        flag_l = "yes" if sc.lenient else "no"
        lines.append(
            f"{i:>3} {flag_e:5} {flag_l:7} {sc.latency_ms:6.0f}  {_clip(s.input, 48)} -> {_clip(sc.output, 60)}"
        )
        if verbose and not sc.lenient:
            lines.append(f"{'':3} {'':5} {'':7} {'':6}  reasons: {'; '.join(sc.reasons)}")
        if verbose and not sc.exact:
            lines.append(f"{'':3} {'':5} {'':7} {'':6}  expected: {_clip(s.expected, 100)}")
    summ = summarize(scores)
    lines.append("")
    lines.append(
        f"exact {summ.exact}/{summ.total} ({summ.exact_rate:.0%})  "
        f"lenient {summ.lenient}/{summ.total} ({summ.lenient_rate:.0%})  "
        f"latency mean {summ.mean_latency_ms:.0f} ms, max {summ.max_latency_ms:.0f} ms"
    )
    return "\n".join(lines)


# ---- providers ----------------------------------------------------------------------


def make_provider(name: str, model: str | None, timeout: float | None) -> Normalizer:
    name = name.lower()
    if name == "passthrough":
        from zordon.output.normalizer.passthrough import PassthroughNormalizer

        return PassthroughNormalizer()
    if name == "anthropic":
        from zordon import paths
        from zordon.config import Config
        from zordon.output.normalizer.anthropic import AnthropicNormalizer

        cfg_path = paths.config_path()
        cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
        return AnthropicNormalizer(
            cfg.providers.key("anthropic"),
            model=model or cfg.providers.normalizer_model,
            timeout=timeout or cfg.providers.normalizer_timeout_seconds,
        )
    if name == "ollama":
        from zordon.output.normalizer.ollama import DEFAULT_MODEL, DEFAULT_URL, OllamaNormalizer

        return OllamaNormalizer(DEFAULT_URL, model or DEFAULT_MODEL, timeout=timeout or 5.0)
    if name == "claude-cli":
        from zordon.output.normalizer.claude_cli import ClaudeCliNormalizer

        return ClaudeCliNormalizer(model=model or "claude-haiku-4-5", timeout=timeout or 60.0)
    raise SystemExit(f"unknown provider {name!r}; use passthrough, anthropic, ollama or claude-cli")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--provider", default="passthrough", help="passthrough (default), anthropic, ollama or claude-cli")
    ap.add_argument("--model", default=None, help="model id for --provider anthropic")
    ap.add_argument("--timeout", type=float, default=None, help="per-call timeout in seconds")
    ap.add_argument("--set", type=Path, default=DEFAULT_SET, help="path to the jsonl set")
    ap.add_argument(
        "--min-lenient", type=float, default=1.0, help="required lenient pass rate (0-1)"
    )
    ap.add_argument("--min-exact", type=float, default=0.0, help="required exact pass rate (0-1)")
    ap.add_argument(
        "--verbose", "-v", action="store_true", help="show reasons and expected text for misses"
    )
    ap.add_argument(
        "--json", action="store_true", help="print machine-readable results instead of a table"
    )
    args = ap.parse_args(argv)

    samples = load_set(args.set)
    problems = validate_set(samples)
    if problems:
        print("eval set problems:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 2

    normalizer = make_provider(args.provider, args.model, args.timeout)
    scores = run(normalizer, samples)
    summ = summarize(scores)
    if args.json:
        print(
            json.dumps(
                {
                    "provider": normalizer.name,
                    "exact": summ.exact,
                    "lenient": summ.lenient,
                    "total": summ.total,
                    "results": [
                        {
                            "input": s.input,
                            "output": sc.output,
                            "expected": s.expected,
                            "exact": sc.exact,
                            "lenient": sc.lenient,
                            "reasons": sc.reasons,
                            "latency_ms": round(sc.latency_ms, 1),
                        }
                        for s, sc in zip(samples, scores, strict=True)
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"provider: {normalizer.name}")
        print(format_table(samples, scores, verbose=args.verbose))
    ok = summ.lenient_rate >= args.min_lenient and summ.exact_rate >= args.min_exact
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
