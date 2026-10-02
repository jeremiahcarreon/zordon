# 0008: Pane capture at 100 ms with spinner masking; the alternate screen limits what is visible

**Status:** accepted, 2026-10-02

## Question

From the design: "Does `capture-pane` diffing at 100 ms hold up when Claude Code
redraws its TUI (spinners, progress bars), or does it need a settle delay before
diffing? Measure with a real session before building the state machine on it."

## What was measured

Claude Code 2.1.287 in tmux 3.4, pane 160x45, polled with
`tmux capture-pane -p -J` every 100 ms; 390 frames across five runs
(`spinner_frames*.txt`, `streaming_frames.txt`).

**The TUI runs on the alternate screen.** `#{alternate_on}` is 1 once the TUI is
up and `history_size` is 0. `capture-pane -S -` and `-S -200` return exactly the 45
visible lines; `-a` returns the normal screen underneath (the shell). Anything that
scrolls off the top is gone. A long plan box lost its top within 2 s. The trust
dialog, the settings warnings and the `claude --resume <uuid>` exit message are on
the normal screen, before and after the alternate screen.

**Redraw rate while working.**

| run | span | distinct frames | per second | spinner-only pairs |
| --- | --- | --- | --- | --- |
| `ls -la` lifecycle | 6.0 s | 36 | 6.0 | 66% |
| `touch` to permission prompt | 2.8 s | 14 | 5.0 | 54% |
| Write to permission prompt | 2.7 s | 15 | 5.5 | 79% |
| prose and code answer | 2.7 s | 16 | 5.9 | 60% |
| plan mode, 45 s of thinking | 45.8 s | 307 | 6.7 (steady 6-9) | 92% |

Response text arrives in paragraph-sized chunks, not per token. Prompts were
byte-stable 300 ms and 1.5 s after first detection.

**Spinner line.** Exactly `<glyph> <Verb>… [(<N>s[ · ↓ <tokens> tokens][ · <extra>])]`
with the glyph cycling `· ✢ * ✶ ✻ ✽ ✻ ✶ * ✢ ·` about once per 100 ms, a random verb
(Perambulating, Zesting, Thinking, ...), and an `<extra>` such as `thinking with
medium effort` whose colour shimmers every frame. The `●` bullet of the active
tool call blinks on alternate frames. A Working session can show **no spinner at
all** while streaming a long answer (`working_no_spinner.txt`).

**Capture cost.** 50 runs via `subprocess`: mean 1.54 ms, median 1.45 ms, max
2.35 ms; `-e` adds about 0.2 ms; `tmux display -p x` costs the same 1.7 ms, so the
cost is process spawn. Inside the poller under load: 3-4 ms, one outlier at 62.7 ms.

**Text details that bite.** The idle input line is `❯` followed by U+00A0 (no-break
space); the echoed user message is `❯` followed by U+0020 at column 0; menu
pointers are indented `❯ 1. Yes`. Python's `str.rstrip()` strips U+00A0. The idle
box shows dim ghost suggestions that are indistinguishable from typed text in a
plain capture. Rendered code blocks have no fences, box or colour: they are
two-space-indented lines identical to prose continuation (which is why prose comes
from the jsonl, decision 0003). tmux `window-size latest` made `new-session -x 160
-y 45` open at 169x58; `resize-window -x 160 -y 45` fixed it.

## Decision

* Poll `capture-pane -p -J` (plain, never `-e`) every `output.poll_interval_ms`
  (100 ms) with a 2 s subprocess timeout. No settle delay: content was stable at
  the first poll that showed it.
* Before diffing, mask the spinner line (the regex above) and the blinking tool
  bullet (`●` vs blank at column 0). A frame whose only change is in the masked
  region produces no `PaneLine` and does not count as "output advanced". The
  spinner's presence still sets `working`.
* `working` is spinner **or** output advancing **or** `esc to interrupt` text;
  never spinner alone. `idle` requires the input box with the no-break space and no
  spinner, and the last non-blank line above the rule being a completion line
  (`✻ <Verb> for <N>s · done <h>:<mm> <AM|PM>`), an interruption line, a rejection
  line, the effort hint or the banner.
* Strip trailing spaces with `rstrip(" ")`, not `rstrip()`, before matching; treat
  the ghost-text line as empty input when its text matches the dim suggestion
  shapes (`Try "..."`).
* Capture what is visible while it is visible: the state machine and prompt
  detection look at the last 25 lines; long content is not reconstructed from
  history, because there is none. Plan bodies longer than the viewport are
  summarised from their first line and step count (design, Deferred).
* `#{alternate_on}` flipping from 1 to 0 is the cheap signal that the TUI exited;
  the normal screen then shows `claude --resume <uuid>`, which confirms the id.
* After creating a pane, verify the size and `resize-window` if tmux chose another.

## Open

* A taller default pane (160x60 or more) would keep more of a plan visible before
  it scrolls; not measured, and the browser never sees the pane directly, so the
  only cost is memory in tmux.
* The 62.7 ms capture outlier was a single sample under load; if it recurs the
  poller should skip a tick rather than queue a late capture.
* The spinner glyph set and verb list are from 390 frames of one version; the
  regex matches any capitalised word before `…`, so new verbs are covered, but a
  new glyph would need a fixture.
