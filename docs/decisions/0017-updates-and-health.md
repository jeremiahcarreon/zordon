# 0017: Updates from the repository channel, and a runtime health strip

**Status:** accepted, 2026-10-02

## Question

People who install Zordon and leave it running need two things the MVP did not
have: a way to learn that a newer version exists and get it, and a way to see
that every part the product depends on is actually working right now, not just
at `zordon doctor` time.

## Decision

**Updates.** The install tracks a git channel (`main` by default; a tag once
releases exist) rather than PyPI, so the version source of truth is the
`__version__` string published on that channel. `zordon/update.py` fetches it
(3 s timeout), compares, caches the answer for 6 hours in
`~/.zordon/update-check.json`, and never raises. `zordon serve` runs the check
in a background thread after the agent starts. With `[update] auto = true`
(default) it reinstalls through whatever installed Zordon (`uv tool install
--force --reinstall` from the channel tarball, or `pipx install --force`) and
publishes an `update` message with `auto: true`, so the terminal and the web
client say "restart zordon serve". With `auto = false` it only announces.
`zordon update [--check]` does the same on demand. Opt-outs: `--no-update`,
`ZORDON_NO_UPDATE_CHECK=1`, `[update] check = false`.

A running process is never swapped under itself: the new version is on disk,
the old one keeps serving until the user restarts. Versions are bumped in
`zordon/__init__.py` whenever a change ships; the check is only as good as
that discipline.

**Health.** `zordon/health.py` collects, with every probe bounded to about a
second and run off the event loop: tmux reachable, the agent binary present,
a focused session, the rewriter reachable (Ollama server and model, Anthropic
credentials, the headless binary, or passthrough as a warning), TTS/STT/VAD
backing files or keys (the silent fallbacks are failures: nothing will be
spoken or heard), the router chain (keyword-only is a warning), the four
worker threads alive, curl for hooks, the update status, the tunnel. The
agent re-collects every 30 s and publishes on change and at least every 60 s;
the client renders a dot strip with the detail and fix on tap, a persistent
banner for failures, and "status" by voice appends the degraded items.
`GET /health` (cookie required) returns the same report.

**Rechecking (added 0.3.5).** The first version checked once, when serve
started. A server that was left running for days (the normal case with
`zordon start` or the login service) therefore never saw a build pushed an
hour after it came up, while its cache file said "latest" for six hours and
then nobody asked again. The check now repeats on the cache interval for the
life of the process, announces each new version once (banner, terminal line
and one spoken notice) and installs it when `auto` is on. It still never
restarts the running process: a restart in the middle of an answer or a
permission prompt is worse than running yesterday's version until the user
says `zordon restart`.

## Open

- Health does not yet probe cloud providers with a real request (that costs
  money); it checks keys and reachability only.
- The update check cannot distinguish two installs from the same `main` at
  different commits with the same version string; bump the version on every
  shipped change, or move to tags.
