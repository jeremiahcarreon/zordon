"""The 60-utterance router eval set and its harness.

Validates ``eval/router_set.jsonl`` (size, uniqueness, every shim command covered,
destination minimums) and runs the KeywordRouter over the shim-command and yes/no
subsets, where it must be perfect: those are the fast path that never costs a
network call. Jev and Haiku runs need keys and are exercised by hand with
``python eval/run_router_eval.py --router jev|anthropic``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from zordon.providers import DESTINATIONS, ProviderError
from zordon.routing import commands
from zordon.routing.base import RouteResult, YesNoResult
from zordon.routing.keyword import KeywordRouter

ROOT = Path(__file__).resolve().parent.parent
SET_PATH = ROOT / "eval" / "router_set.jsonl"
RUNNER_PATH = ROOT / "eval" / "run_router_eval.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_router_eval", RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_router_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture(scope="module")
def cases(runner):
    return runner.load_cases(SET_PATH)


# ---- the set itself ------------------------------------------------------------------------


def test_every_line_is_a_well_formed_case():
    with open(SET_PATH, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            d = json.loads(line)
            assert set(d) >= {"utterance", "expected", "state"}, f"line {n}"
            assert d["utterance"].strip(), f"line {n}: empty utterance"
            assert d["expected"] in (*DESTINATIONS, "yes_no"), f"line {n}"
            assert d["state"] in ("idle", "working", "awaiting_permission"), f"line {n}"
            if d["expected"] == "shim_command":
                assert d["command"] in commands.BY_NAME, f"line {n}"


def test_set_is_large_unique_and_covers_every_command(runner, cases):
    assert runner.validate_cases(cases) == []
    routing = [c for c in cases if c.expected != "yes_no"]
    assert len(routing) >= 60
    assert len({(c.utterance.lower(), c.state) for c in cases}) == len(cases)
    assert {c.command for c in cases if c.expected == "shim_command"} == set(commands.COMMAND_NAMES)
    by = Counter(c.expected for c in cases)
    assert by["transcript_query"] >= 15
    assert by["shim_command"] >= 15
    assert by["claude_code"] >= 25
    assert by["unclear"] >= 5
    assert by["yes_no"] >= 8


def test_set_contains_the_tricky_pairs(cases):
    by_utt = {c.utterance.lower(): c for c in cases if c.expected != "yes_no"}
    assert by_utt["what did you just change"].expected == "transcript_query"
    assert by_utt["change what you did"].expected == "claude_code"
    assert by_utt["stop"].expected == "shim_command" and by_utt["stop"].command == "stop"
    assert by_utt["stop retrying on 500s"].expected == "claude_code"
    assert by_utt["yes"].expected == "claude_code" and by_utt["yes"].state == "idle"


def test_validate_cases_reports_problems(runner):
    EvalCase = runner.EvalCase
    bad = [EvalCase("mute", "shim_command", command="nonsense"), EvalCase("mute", "shim_command", command="mute")]
    problems = runner.validate_cases(bad)
    assert any("duplicate" in p for p in problems)
    assert any("known command" in p for p in problems)
    assert any("commands without an example" in p for p in problems)
    assert any("need at least" in p for p in problems)


# ---- keyword router over the fast-path subset -------------------------------------------------


def test_keyword_router_is_perfect_on_shim_commands_and_yes_no(runner, cases):
    subset = [c for c in cases if c.expected in ("shim_command", "yes_no")]
    assert len(subset) >= 25
    report = runner.run_eval(KeywordRouter(), subset)
    assert report.failures == []
    assert report.command_mismatches == []
    assert report.argument_mismatches == []
    assert report.accuracy == 1.0
    assert report.percentile(0.95) < 50.0  # ms; pure string matching


def test_keyword_router_never_misroutes_claude_code_to_transcript(runner, cases):
    report = runner.run_eval(KeywordRouter(), cases)
    assert report.cc_to_tq == 0
    # Everything the keyword router does not recognise is Claude Code at 0.6, i.e. safe.
    assert report.confusion[("claude_code", "claude_code")] == sum(1 for c in cases if c.expected == "claude_code")


# ---- the harness itself ---------------------------------------------------------------------


class Canned:
    name = "canned"
    last_model = "canned-1"

    def __init__(self, dest: str, conf: float = 0.99, fail: bool = False):
        self.dest, self.conf, self.fail = dest, conf, fail

    def route(self, utterance, ctx):
        if self.fail:
            raise ProviderError("down")
        return RouteResult(self.dest, self.conf, command="mute" if self.dest == "shim_command" else None)

    def yes_no(self, utterance):
        return YesNoResult("yes", 0.99)

    def prompt_score(self, lines):
        return 0.0


def test_harness_counts_both_misroute_directions(runner):
    EvalCase = runner.EvalCase
    cases = [
        EvalCase("what did you change", "transcript_query"),
        EvalCase("add retries", "claude_code"),
        EvalCase("um", "unclear"),
        EvalCase("yes", "yes_no", state="awaiting_permission", yes_no="yes"),
    ]
    always_cc = runner.run_eval(Canned("claude_code"), cases)
    assert always_cc.tq_to_cc == 1 and always_cc.cc_to_tq == 0
    assert always_cc.correct == 3  # claude_code, unclear (safe), yes
    assert not always_cc.passes()

    always_tq = runner.run_eval(Canned("transcript_query"), cases)
    assert always_tq.cc_to_tq == 1 and always_tq.tq_to_cc == 0
    assert not always_tq.passes()

    low = runner.run_eval(Canned("transcript_query", conf=0.5), cases)
    assert low.tq_to_cc == 1  # below threshold the dispatcher sends it to Claude Code

    failing = runner.run_eval(Canned("claude_code", fail=True), cases)
    assert len(failing.errors) == 3
    assert failing.model == "canned-1"
    text = runner.format_report(failing)
    assert "provider errors" in text and "accuracy" in text


def test_main_with_keyword_router_exits_zero(runner, capsys):
    assert runner.main(["--router", "keyword"]) == 0
    out = capsys.readouterr().out
    assert "router: keyword" in out
    assert "UNSAFE): 0" in out
