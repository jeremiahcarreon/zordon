# Decision records

Short notes answering the open questions in `../Zordon Technical Design.md`, written
once the relevant code or measurement existed. Each record states the question, what
was measured or verified (with numbers and the Claude Code version), the decision,
and what it leaves open.

| # | Decision | Answers |
| --- | --- | --- |
| [0001](0001-single-package.md) | One package, subpackage per concern | Package layout question |
| [0002](0002-session-store-and-registry.md) | Sessions are discovered from `~/.claude/projects/*/<id>.jsonl` and `~/.claude/sessions/<pid>.json`; the directory name is lossy; a running session is never resumed | Session store question |
| [0003](0003-output-source.md) | Prose from the session jsonl, prompts and state from the pane | Output format question |
| [0004](0004-vad-placement.md) | Silero VAD runs in the agent on onnxruntime (0.08 ms per frame); in-browser VAD waits for a tunnel latency measurement | VAD placement question |
| [0005](0005-tts-default-and-streaming.md) | Kokoro fp32 is the default TTS, synthesized one sentence at a time; cloud TTS is the low-latency option | Kokoro streaming question |
| [0006](0006-router-jev-via-sdk.md) | The Jev router uses `typesafe-sdk` directly, thresholds on `probabilities[choice]`, falls back to Haiku then to keywords | Jev instruction-context question |
| [0007](0007-permission-prompt-handling.md) | Voice selects only `Yes` or `No`; plan approval picks "manually approve edits"; the trust dialog defaults to exit; the 0.95 yes/no gate | Permission safety (design, Safety section) |
| [0008](0008-pane-capture-and-diffing.md) | 100 ms `capture-pane` polling holds with the spinner masked; the alternate screen has no history | capture-pane diffing question |
| [0009](0009-notification-hook-second-signal.md) | A per-launch `--settings` Notification hook is the second prompt signal; the pane regex stays primary | Prompt detection backstop |
| [0010](0010-headless-normalizer.md) | Without an Anthropic key, headless Claude Code normalizes each finished turn under the user's own login; one fresh process per request | Zero-key default |
| [0011](0011-ollama-normalizer.md) | A local Ollama model normalizes per sentence with no key; 3b default with a padding guard; the Ollama router is opt-in only | Zero-key streaming |
| [0012](0012-guided-setup.md) | `pipx install zordon` then `zordon serve`; a terminal wizard asks four questions and does the downloads | Install path |
| [0013](0013-agent-adapters.md) | Everything agent-specific sits behind an `AgentAdapter` with six slots; a generic pane adapter attaches to any tmux pane; Claude Code stays the default | Other coding agents |
| [0014](0014-prerequisites.md) | Setup detects the package manager and installs missing system pieces (tmux, curl, Node, the agent, Ollama) one explicit yes at a time | Install path |
| [0015](0015-curl-installer.md) | `curl ... install.sh | sh` installs uv, a managed Python and zordon with no sudo, then runs the wizard; Windows via WSL2 only | Install path |

## Writing one

File name `NNNN-short-slug.md`. First lines:

```
# NNNN: One-line decision

**Status:** accepted | superseded by NNNN | rejected, YYYY-MM-DD
```

Then `## Question`, `## What was measured` (or verified), `## Decision`, `## Open`.
Numbers belong in the record, with the version they were measured against. A
record is never edited to change its decision; a new record supersedes it.
