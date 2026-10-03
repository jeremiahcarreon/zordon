# 0011: Local Ollama models for zero-key, per-sentence normalization

**Status:** accepted, 2026-10-02

Decision 0010 gave the zero-key path to headless Claude Code at 4-15 s per
request, which forced whole-turn normalization. A local small model can hold
the design's per-sentence budget without a key.

## What was measured

RTX 4090, Ollama 0.31.1, `qwen2.5` instruct models, temperature 0, four
hand samples then the 50-sample `eval/normalizer_set.jsonl`:

| Model | Size | Mean latency | Exact | Lenient | Padding guard fired |
| --- | --- | --- | --- | --- | --- |
| qwen2.5:1.5b-instruct | 1 GB | 208 ms | 4% | 24% | 0 |
| qwen2.5:3b-instruct | 2 GB | 219 ms | 6% | 40% | 1 |
| qwen2.5:14b-instruct | 9 GB | 565 ms | 10% | 44% | 0 |

The lenient rubric is strict (exact substrings, no shell commands, one
sentence). Reading the failures: the dominant miss for every size is a
dropped or compressed fact ("Done, tests pass." -> "All tests pass."), not
padding. 1.5b ignores formatting rules (keeps backticks and digits). 3b and
14b are close on this set; 14b is better on spoken forms of paths and numbers.

Router use, `eval/run_router_eval.py --router ollama`: 3b 50% accuracy with
12 unsafe misroutes (a work request answered from the transcript); 14b 87%
with 4 unsafe. The design requires 95% and zero unsafe.

## Decision

- New `ollama` normalizer (`zordon/output/normalizer/ollama.py`), per-sentence
  over `/api/chat`, temperature 0, 1.5 s timeout, `keep_alive` -1 (see below). The
  prompt demands the same facts, same order, about the same length. A length
  guard retries once when the rewrite grows past 1.6x the input words and then
  speaks the pre-passed sentence; padding is a fact-invention risk for a
  coding agent's output, not a style choice. The same object answers
  transcript questions.
- Default model `qwen2.5:3b-instruct`: runs without a GPU, 220 ms, and the
  eval does not separate it from 14b enough to justify four times the memory.
  `ollama_model` is a one-line switch; 14b is the documented upgrade.
- `auto` order: Anthropic key -> `anthropic`; else Ollama reachable with the
  model pulled -> `ollama`; else `claude-cli` (per turn); else `passthrough`.
- `ollama` router exists but is explicit opt-in only and is never in the
  automatic chain, because it misses the accuracy bar and misroutes unsafely.
  The keyword fast path still handles shim commands and yes/no without it.
- `zordon doctor` reports the server and the model, with the `ollama pull`
  line to run.

- Model residency (0.3.5). Ollama unloads an idle model after five minutes
  by default and a `keep_alive` of 30 minutes only stretched that. The first
  answer after a longer pause then hit a 10-20 s cold load, every sentence of
  it missed the 1.5 s deadline and was spoken as pre-passed text, which is
  what "the output is not normalized" looked like in use. The requests now
  pin the model for the life of the Ollama server (`keep_alive: -1`) and the
  normalizer preloads it in the background when serve starts. The cost is
  about 2 GB of GPU or system memory held while Ollama runs, which is the
  price of a voice loop that answers in time.

## Open

- The eval rubric rewards exact substrings; a fact-retention judge would
  grade these models more fairly. Numbers above are comparable to each other,
  not to a human bar.
- Other families (Llama 3.2 3B, Gemma 3 4B, Phi-4-mini) were not measured.
- Whether a small model can route safely with a better prompt or a
  two-stage design (classify, then confirm) is untested.
