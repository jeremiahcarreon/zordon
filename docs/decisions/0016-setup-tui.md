# 0016: A full-screen Textual UI for setup and uninstall, with the plain wizard kept

**Status:** accepted, 2026-10-02

## Question

The question-and-answer wizard (0012, 0014) works everywhere but reads like a
form: long option texts scroll by, a typo at "Choice [1]:" re-prints the block,
and the prerequisites step hides what an install command is printing. Should
setup get a real terminal UI, and if so what happens to the plain path?

## Decision

- `zordon setup` and `zordon uninstall` open a full-screen
  [Textual](https://textual.textualize.io/) app when stdin and stdout are both
  terminals and `--plain`/`--yes` are not given (`cli.want_tui`). The plain
  wizards stay and are the fallback: an `ImportError` (Textual missing from the
  environment), a `TuiUnavailable` (`TERM` unset or `dumb`, or the app failing
  before its first screen) and `install.sh`'s no-terminal branch all land in
  the old code unchanged.
- The TUI owns no logic. `zordon/tui/setup.py` calls `setup.detect`,
  `setup.recommend`, `setup.apply`, `setup.run_actions`, `prereqs.detect`,
  `prereqs.install`, `prereqs.open_for_login` and `manifest.record`; the
  uninstall screen calls `uninstall.build_plan` and `uninstall.execute`. The
  option texts are parsed from the `SPEECH_TEXT`/`NORMALIZER_TEXT`/
  `ROUTER_TEXT`/`ACCESS_TEXT` constants (`parse_options`), so the two paths show
  the same trade-offs by construction and a test asserts it.
- One screen per step (welcome and detection, agent, speech, rewriter, routing,
  reach, prerequisites, downloads, done) with a step indicator, option cards
  that show the whole trade-off text, Back on Esc, mouse and keyboard, and a
  summary card with the token and the exact `zordon serve` command. "Start
  zordon serve now" hands the extra flags (`--tunnel`, `--bind tailscale`) back
  to the CLI, which runs `serve` after the terminal is restored.
- Nothing is written before the Downloads step: the config is loaded without
  being created, so quitting earlier leaves the machine as it was (the quit
  dialog says which case applies). Prerequisite installs run only on a click;
  their output streams into a log panel from a worker thread. Commands that
  need the terminal (anything with `sudo`, the Ollama installer, logging in to
  the agent) run with the app suspended so the real tty answers the prompt.
- `setup.run_actions` gained a `downloader=` hook so the TUI can draw a
  progress bar for the asset downloads (`assets.download` already reported
  progress); the Whisper fetch goes through `faster-whisper` and stays
  indeterminate.
- The palette is `install.sh`'s: purple 141 frames, blue 75 accents, green 114
  ok, red 203 bad, registered as a Textual `Theme` so the CSS uses `$primary`
  and friends.

## Verified

Textual 8.2 on Python 3.12. `tests/test_tui.py` drives the apps headlessly with
`App.run_test()`: the Enter-all-the-way path yields exactly `setup.recommend()`'s
choices; cloud keys, the 14b toggle, Back, the quit dialog, the prerequisites
screen (commands shown, Install calls the runner, manifest updated, login
offered), the downloads screen (problems listed, log streamed), the uninstall
screen (unchecked outside items never run, inside items do, failures exit 3) and
the CLI fallbacks. The whole file runs in about 12 s. Screens were checked at
80x24 and 110x48: the body scrolls, the button row stays put, the step
indicator collapses to dots when the names do not fit.

## Open

- The Ollama pull and the Whisper download have no byte-level progress; the
  log lines and spinner are what the user sees.
- `App.suspend()` is Unix-only; on another platform a sudo command would run
  in the worker and fail without a password prompt. Zordon is Linux/macOS
  (0015), so this is noted rather than handled.
