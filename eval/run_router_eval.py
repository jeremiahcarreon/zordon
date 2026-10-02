"""Run a Router over ``eval/router_set.jsonl`` and report accuracy, confusion,
the two misroute counts that matter, and latency.

    python eval/run_router_eval.py --router keyword
    python eval/run_router_eval.py --router jev        # needs TYPESAFE_API_KEY or config
    python eval/run_router_eval.py --router anthropic  # needs ANTHROPIC_API_KEY or config
    python eval/run_router_eval.py --router fallback   # the chain make_router(config) builds

Each line of the set is one case:

    {"utterance": ..., "expected": <destination | "yes_no">, "command": <optional>,
     "argument": <optional>, "state": "idle"|"working"|"awaiting_permission",
     "yes_no": "yes"|"no"|"unclear" (when expected == "yes_no"), "notes": ...}

Routing cases are judged on the *effective* destination, i.e. after the
dispatcher's policy (``unclear`` or confidence below the threshold goes to
Claude Code). An ``unclear`` case counts as correct when the router said unclear
or the utterance would safely reach Claude Code. ``yes_no`` cases call
``router.yes_no`` and apply the 0.95 gate.

Exit status is 1 for ``jev`` / ``anthropic`` / ``fallback`` when accuracy is
below 0.95, when any transcript query reached Claude Code, or when any Claude
Code request was answered from the transcript (the unsafe failure). The keyword
router is reported but never fails the run: it is the safe default, not the
classifier under evaluation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zordon.providers import DESTINATIONS, ProviderError  # noqa: E402
from zordon.routing import commands  # noqa: E402
from zordon.routing.base import RouteContext, Router, effective_destination  # noqa: E402

SET_PATH = ROOT / "eval" / "router_set.jsonl"
MIN_CASES = 60
ACCURACY_FLOOR = 0.95

SAMPLE_TAIL = [
    "I'm adding retry logic to the upload handler.",
    "I edited auth dot py, changing eight lines.",
    "All forty-two tests pass.",
    "I committed with the message: add retry logic to the upload handler.",
]
SAMPLE_SESSIONS = ["zordon", "api", "frontend"]
STATES = ("idle", "working", "awaiting_permission")


@dataclass(slots=True)
class EvalCase:
    utterance: str
    expected: str
    state: str = "idle"
    command: str | None = None
    argument: str | None = None
    yes_no: str | None = None
    notes: str = ""


@dataclass(slots=True)
class EvalReport:
    router: str
    total: int = 0
    correct: int = 0
    confusion: Counter = field(default_factory=Counter)  # (expected, got) -> n
    tq_to_cc: int = 0  # transcript query sent to Claude Code (the safe miss the design still forbids)
    cc_to_tq: int = 0  # Claude Code request answered from the transcript (the unsafe miss)
    command_mismatches: list[str] = field(default_factory=list)
    argument_mismatches: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    model: str | None = None

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        xs = sorted(self.latencies_ms)
        k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
        return xs[k]

    def passes(self) -> bool:
        return self.accuracy >= ACCURACY_FLOOR and self.tq_to_cc == 0 and self.cc_to_tq == 0


# ---- loading / validation ----------------------------------------------------------------


def load_cases(path: Path = SET_PATH) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            d = json.loads(raw)
            try:
                cases.append(
                    EvalCase(
                        utterance=str(d["utterance"]),
                        expected=str(d["expected"]),
                        state=str(d.get("state", "idle")),
                        command=d.get("command"),
                        argument=d.get("argument"),
                        yes_no=d.get("yes_no"),
                        notes=str(d.get("notes", "")),
                    )
                )
            except KeyError as e:
                raise ValueError(f"{path}:{n}: missing field {e}") from e
    return cases


def validate_cases(cases: list[EvalCase]) -> list[str]:
    """Return a list of problems; empty means the set is well formed."""
    problems: list[str] = []
    routing = [c for c in cases if c.expected != "yes_no"]
    if len(routing) < MIN_CASES:
        problems.append(f"only {len(routing)} routing cases; need at least {MIN_CASES}")
    seen: Counter = Counter((c.utterance.strip().lower(), c.state) for c in cases)
    for (utt, state), n in seen.items():
        if n > 1:
            problems.append(f"duplicate utterance {utt!r} in state {state}")
    for c in cases:
        if c.expected not in DESTINATIONS and c.expected != "yes_no":
            problems.append(f"{c.utterance!r}: unknown expected {c.expected!r}")
        if c.state not in STATES:
            problems.append(f"{c.utterance!r}: unknown state {c.state!r}")
        if c.expected == "shim_command" and c.command not in commands.BY_NAME:
            problems.append(f"{c.utterance!r}: shim_command needs a known command, got {c.command!r}")
        if c.expected != "shim_command" and c.command:
            problems.append(f"{c.utterance!r}: command given for a non-shim case")
        if c.expected == "yes_no" and c.yes_no not in ("yes", "no", "unclear"):
            problems.append(f"{c.utterance!r}: yes_no case needs yes_no in yes/no/unclear")
        if c.expected == "yes_no" and c.state != "awaiting_permission":
            problems.append(f"{c.utterance!r}: yes_no case must be in awaiting_permission")
    covered = {c.command for c in cases if c.expected == "shim_command"}
    missing = set(commands.COMMAND_NAMES) - covered
    if missing:
        problems.append(f"commands without an example: {sorted(missing)}")
    by_dest = Counter(c.expected for c in cases)
    for dest, minimum in (("transcript_query", 15), ("shim_command", 15), ("claude_code", 25), ("unclear", 5)):
        if by_dest[dest] < minimum:
            problems.append(f"{dest}: {by_dest[dest]} cases, need at least {minimum}")
    return problems


# ---- running -------------------------------------------------------------------------------


def run_eval(
    router: Router,
    cases: list[EvalCase],
    *,
    threshold: float = 0.85,
    yes_no_threshold: float = 0.95,
    tail: list[str] | None = None,
    session_names: list[str] | None = None,
) -> EvalReport:
    tail = SAMPLE_TAIL if tail is None else tail
    names = SAMPLE_SESSIONS if session_names is None else session_names
    report = EvalReport(router=getattr(router, "name", type(router).__name__))
    for case in cases:
        report.total += 1
        t0 = time.perf_counter()
        try:
            if case.expected == "yes_no":
                _judge_yes_no(router, case, report, yes_no_threshold)
            else:
                ctx = RouteContext(
                    session_state=case.state,
                    transcript_tail=list(tail),
                    focused_session="focused",
                    session_names=list(names),
                    commands=list(commands.COMMAND_NAMES),
                )
                _judge_route(router, case, ctx, report, threshold)
        except ProviderError as e:
            report.errors.append(f"{case.utterance!r}: {e}")
            report.failures.append(f"{case.utterance!r}: provider error")
        finally:
            report.latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    report.model = getattr(router, "last_model", None)
    return report


def _judge_route(router: Router, case: EvalCase, ctx: RouteContext, report: EvalReport, threshold: float) -> None:
    r = router.route(case.utterance, ctx)
    raw = r.destination if r.destination in DESTINATIONS else "unclear"
    got = effective_destination(r, threshold)
    if raw == "unclear":
        got_label = "unclear"
    else:
        got_label = got
    report.confusion[(case.expected, got_label)] += 1
    if case.expected == "transcript_query" and got == "claude_code":
        report.tq_to_cc += 1
    if case.expected == "claude_code" and got == "transcript_query":
        report.cc_to_tq += 1

    if case.expected == "unclear":
        ok = raw == "unclear" or got == "claude_code"
    else:
        ok = got == case.expected
    if ok and case.expected == "shim_command":
        if r.command != case.command:
            ok = False
            report.command_mismatches.append(f"{case.utterance!r}: wanted {case.command}, got {r.command}")
        elif case.argument and (r.argument or "").lower() != case.argument.lower():
            report.argument_mismatches.append(
                f"{case.utterance!r}: wanted argument {case.argument!r}, got {r.argument!r}"
            )
    if ok:
        report.correct += 1
    else:
        report.failures.append(
            f"{case.utterance!r} [{case.state}]: expected {case.expected}"
            f"{'/' + case.command if case.command else ''}, got {raw} {r.confidence:.2f}"
            f"{'/' + r.command if r.command else ''} -> {got}"
        )


def _judge_yes_no(router: Router, case: EvalCase, report: EvalReport, threshold: float) -> None:
    r = router.yes_no(case.utterance)
    got = r.answer if r.answer in ("yes", "no") and r.confidence >= threshold else "unclear"
    report.confusion[(f"yes_no:{case.yes_no}", f"yes_no:{got}")] += 1
    if got == case.yes_no:
        report.correct += 1
    else:
        report.failures.append(
            f"{case.utterance!r} [awaiting_permission]: expected {case.yes_no}, got {r.answer} {r.confidence:.2f}"
        )


# ---- reporting -----------------------------------------------------------------------------


def format_report(report: EvalReport) -> str:
    lines = [
        f"router: {report.router}" + (f" (model {report.model})" if report.model else ""),
        f"cases: {report.total}  correct: {report.correct}  accuracy: {report.accuracy:.3f}",
        f"transcript_query -> claude_code (design forbids): {report.tq_to_cc}",
        f"claude_code -> transcript_query (UNSAFE): {report.cc_to_tq}",
        f"latency ms: p50 {report.percentile(0.5):.1f}  p95 {report.percentile(0.95):.1f}"
        + (f"  mean {statistics.fmean(report.latencies_ms):.1f}" if report.latencies_ms else ""),
        "",
        "confusion (expected -> got: n):",
    ]
    for (exp, got), n in sorted(report.confusion.items()):
        mark = "" if exp == got or (exp == "unclear" and got == "claude_code") else "  <--"
        lines.append(f"  {exp:>18} -> {got:<18} {n}{mark}")
    if report.command_mismatches:
        lines.append("")
        lines.append("command mismatches:")
        lines.extend(f"  {m}" for m in report.command_mismatches)
    if report.argument_mismatches:
        lines.append("")
        lines.append("argument mismatches (reported, not failing):")
        lines.extend(f"  {m}" for m in report.argument_mismatches)
    if report.failures:
        lines.append("")
        lines.append("failures:")
        lines.extend(f"  {f}" for f in report.failures)
    if report.errors:
        lines.append("")
        lines.append("provider errors:")
        lines.extend(f"  {e}" for e in report.errors[:10])
    return "\n".join(lines)


# ---- router construction -------------------------------------------------------------------


def _load_config():
    from zordon.config import Config  # noqa: PLC0415
    from zordon.paths import config_path  # noqa: PLC0415

    path = config_path()
    if path.exists():
        return Config.load(path)
    return Config()


def build_router(name: str) -> Router:
    config = _load_config()
    if name == "keyword":
        from zordon.routing.keyword import KeywordRouter  # noqa: PLC0415

        return KeywordRouter()
    if name == "jev":
        from zordon.routing.typesafe import JevRouter  # noqa: PLC0415

        return JevRouter(
            config.providers.key("typesafe"),
            confidence_threshold=config.voice.router_confidence,
            yes_no_threshold=config.voice.yes_no_confidence,
        )
    if name == "anthropic":
        from zordon.routing.anthropic import HaikuRouter  # noqa: PLC0415

        return HaikuRouter(config.providers.key("anthropic"), model=config.providers.router_model)
    if name == "ollama":
        from zordon.routing.ollama import OllamaRouter  # noqa: PLC0415

        model = os.environ.get("ZORDON_OLLAMA_MODEL") or config.providers.ollama_model
        return OllamaRouter(config.providers.ollama_url, model, timeout=10.0)
    if name == "fallback":
        from zordon.routing.select import make_router  # noqa: PLC0415

        return make_router(config)
    raise SystemExit(f"unknown router {name!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--router", choices=("keyword", "jev", "anthropic", "ollama", "fallback"), default="keyword")
    ap.add_argument("--set", type=Path, default=SET_PATH, help="path to the jsonl set")
    ap.add_argument("--threshold", type=float, default=None, help="router confidence threshold (default: config)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    cases = load_cases(args.set)
    problems = validate_cases(cases)
    if problems:
        print("eval set problems:")
        for p in problems:
            print(f"  {p}")
        return 2

    config = _load_config()
    threshold = args.threshold if args.threshold is not None else config.voice.router_confidence
    try:
        router = build_router(args.router)
    except ProviderError as e:
        print(f"cannot build router {args.router}: {e}")
        return 2
    report = run_eval(router, cases, threshold=threshold, yes_no_threshold=config.voice.yes_no_confidence)
    print(format_report(report))
    if args.router == "keyword":
        return 0
    return 0 if report.passes() else 1


if __name__ == "__main__":
    sys.exit(main())
