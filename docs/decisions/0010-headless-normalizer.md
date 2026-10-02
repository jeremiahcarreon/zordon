# 0010: Normalize with headless Claude Code when there is no API key

**Status:** accepted, 2026-10-02

The design makes a Haiku normalizer the default and treats the Anthropic key
as the "recommended minimum". Many users have a Claude subscription and no
API account, so the zero-key path mattered.

**Measured** on Claude Code 2.1.287, Max login, `claude -p --model claude-haiku-4-5
--tools "" --setting-sources "" --no-session-persistence --max-turns 1`:

| Shape | Per request |
| --- | --- |
| One-shot process, `claude-haiku-4-5` | 6.2 s wall, 4.4 s API |
| Resident `stream-json` process, `claude-haiku-4-5`, one message per sentence | 3.8-7.2 s per sentence |
| One-shot, `claude-sonnet-5` | 5.4 s wall, 1.4 s API |

The result records showed a cached prefix of about 67k tokens on every call:
Claude Code's own system context. `--bare` would drop it but refuses OAuth
login (API key only). There is no "clear context" in print mode; a fresh
process per request is the clear, and it costs about 2-4 s of start-up.

**Decision**

- New provider `claude-cli` (`zordon/output/normalizer/claude_cli.py`). One
  short-lived process per request so nothing carries over, with one spare
  process kept warm so start-up is hidden. The parent's `CLAUDECODE` and
  `CLAUDE_CODE_*` variables are removed from the child so it never attaches to
  a parent session. The process runs in an empty private directory so no
  project instructions are loaded.
- It declares `granularity = "turn"`. The pipeline collects a turn's kept prose
  and sends it as one request when the turn ends (jsonl `end_turn`, the
  completion row, or a 6 s lull without either). The normalized text is split
  into sentences and spoken in order; on timeout or failure the pre-passed
  sentences are spoken. Prompts, errors and notices keep their priority path
  and are never delayed by the batch.
- `providers.normalizer` default becomes `auto`: Anthropic key present ->
  per-sentence `anthropic`; else `claude` on PATH -> `claude-cli`; else
  `passthrough`. Explicit names still work. `claude_cli_model` (default
  `claude-haiku-4-5`; `claude-sonnet-5` measured faster but spends more subscription quota) and
  `claude_cli_timeout_seconds` (30) are the knobs.
- The same process answers transcript questions through
  `answer_transcript_query`. It is not used as a router: 4 s per utterance is
  too slow for routing, and the keyword router already handles shim commands
  and the yes/no gate without a network call.

**What stays open**

- Per-request latency is bounded below by Claude Code's start-up and baseline
  context; if a future release offers a headless mode that keeps the login
  and drops the baseline, the per-sentence path becomes viable again.
- `claude -p` is Claude Code's documented headless mode. Extracting the login
  token to call the API directly would not be, and Zordon does not do it.
