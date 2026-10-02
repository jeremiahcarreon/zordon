"""The 50-sample normalizer eval set and its harness.

Validates the file shape and runs the passthrough normalizer through the rubric.
Passthrough does not rewrite, so most samples miss the full lenient rubric; what
it must always do is strip markdown remnants, and it must leave already-fluent
sentences alone. The Anthropic provider is never run here (it would cost money
and need a key); run ``eval/run_normalizer_eval.py --provider anthropic`` by hand.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from zordon.output.normalizer import PassthroughNormalizer

ROOT = Path(__file__).resolve().parent.parent
EVAL_SCRIPT = ROOT / "eval" / "run_normalizer_eval.py"
EVAL_SET = ROOT / "eval" / "normalizer_set.jsonl"

# Passthrough is "readable but terse"; this is what it meets today (7/50) with margin.
PASSTHROUGH_MIN_LENIENT = 0.10


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("run_normalizer_eval", EVAL_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def samples(harness):
    return harness.load_set(EVAL_SET)


class TestSetShape:
    def test_fifty_well_formed_entries(self, harness, samples):
        assert len(samples) == 50
        assert harness.validate_set(samples) == []

    def test_unique_inputs(self, samples):
        inputs = [s.input for s in samples]
        assert len(set(inputs)) == len(inputs)

    def test_every_line_has_required_fields(self):
        for n, line in enumerate(EVAL_SET.read_text(encoding="utf-8").splitlines(), 1):
            raw = json.loads(line)
            assert set(raw) == {
                "input",
                "context",
                "expected",
                "must_include",
                "must_not_include",
            }, n
            assert isinstance(raw["context"], list) and isinstance(raw["must_include"], list)
            assert {"`", "#", "->"} <= set(raw["must_not_include"]), n

    def test_covers_the_design_categories(self, samples):
        inputs = "\n".join(s.input for s in samples)
        assert "`" in inputs  # markdown remnants
        assert "## " in inputs
        assert "->" in inputs
        assert ".py" in inputs and "/" in inputs  # paths
        assert "42/42" in inputs  # test counts
        assert "API" in inputs or "JWT" in inputs  # acronyms
        assert "Fix:" in inputs  # Caveman shorthand
        assert any(len(s.input) > 120 for s in samples)  # long sentences
        assert any(s.context for s in samples)  # at least one sample carries context
        assert any("permission" in s.input.lower() for s in samples)

    def test_expected_forms_are_speakable(self, harness, samples):
        for s in samples:
            assert harness.is_single_sentence(s.expected), s.expected
            assert not harness.has_markdown(s.expected), s.expected


class TestRubric:
    def test_exact_match_is_forgiving_about_case_and_punctuation(self, harness):
        assert harness.is_exact("  All forty-two tests pass  ", "all forty-two tests pass.")
        assert not harness.is_exact("All tests pass.", "All forty-two tests pass.")

    def test_single_sentence(self, harness):
        assert harness.is_single_sentence("I edited auth dot py, changing eight lines.")
        assert harness.is_single_sentence("Python 3.12 is required because 3.11 fails.")
        assert not harness.is_single_sentence("Fix lint. Bump deps. Done.")
        assert not harness.is_single_sentence("")
        assert not harness.is_single_sentence("One.\nTwo.")

    def test_markdown_detection(self, harness):
        assert harness.has_markdown("Edited `auth.py`")
        assert harness.has_markdown("## Summary")
        assert harness.has_markdown("**bold** text")
        assert harness.has_markdown("- a bullet")
        assert harness.has_markdown("see [doc](x.md)")
        assert not harness.has_markdown("I edited auth dot py, changing eight lines.")
        assert not harness.has_markdown("pull request number 412 is ready")

    def test_reasons(self, harness):
        sample = harness.Sample("x", [], "I edited auth dot py.", ["auth"], ["`"])
        sc = harness.score(sample, "Edited `auth.py`. Done.")
        assert not sc.lenient and not sc.exact
        assert any("contains" in r for r in sc.reasons)
        assert any("single sentence" in r for r in sc.reasons)
        assert any("markdown" in r for r in sc.reasons)
        good = harness.score(sample, "I edited auth dot py")
        assert good.exact and good.lenient and good.reasons == []


class TestPassthroughThroughHarness:
    def test_lenient_floor(self, harness, samples):
        scores = harness.run(PassthroughNormalizer(), samples)
        summary = harness.summarize(scores)
        assert summary.total == 50
        assert summary.lenient_rate >= PASSTHROUGH_MIN_LENIENT, harness.format_table(
            samples, scores, verbose=True
        )

    def test_markdown_remnants_always_removed(self, harness, samples):
        """The part of the rubric passthrough must meet on every sample."""
        p = PassthroughNormalizer()
        for s in samples:
            out = p.normalize(s.input, s.context)
            assert out, s.input
            assert not harness.has_markdown(out), (s.input, out)
            for bad in ("`", "#", "->", "**"):
                assert bad not in out, (s.input, out)

    def test_already_fluent_is_exact(self, harness, samples):
        fluent = [s for s in samples if s.input == s.expected]
        assert fluent, "the set should contain at least one already-fluent sample"
        p = PassthroughNormalizer()
        for s in fluent:
            assert harness.score(s, p.normalize(s.input, s.context)).exact

    def test_cli_runs_passthrough(self, harness, capsys):
        rc = harness.main(["--provider", "passthrough", "--min-lenient", "0"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "provider: passthrough" in out
        assert "lenient" in out and "/50" in out

    def test_cli_exit_code_on_miss(self, harness, capsys):
        assert harness.main(["--provider", "passthrough", "--min-lenient", "1.0"]) == 1
        capsys.readouterr()

    def test_cli_json(self, harness, capsys):
        assert harness.main(["--provider", "passthrough", "--min-lenient", "0", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 50 and len(data["results"]) == 50
        assert {"input", "output", "expected", "exact", "lenient", "reasons", "latency_ms"} <= set(
            data["results"][0]
        )
