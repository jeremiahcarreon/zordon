# Security

Zordon turns speech into keystrokes in a shell that has your full permissions, and
it can be reached from a phone. This page says what that means, what Zordon
guarantees, and what it refuses to do. The guarantees here are the invariants in
`architecture.md`; each one has a test.

## Threat model

Who the attacker is:

* **Someone who finds the URL.** A quick-tunnel URL is public once it exists;
  a LAN or Tailscale address is reachable by anything on that network.
* **Something in the transcript.** Claude Code's output is untrusted input. A tool
  result, a file it read, or a web page it fetched can contain text that looks like
  an instruction to Zordon, or a secret that should not be read aloud over a tunnel.
* **Speech recognition errors.** "Yes" heard as "yes always" is an attack on the
  permission model, even when nobody is attacking.
* **Zordon itself.** A bug that approves a prompt, widens a permission mode, or
  leaks a key is the failure the design is built around.

What is not defended: a compromised machine, a compromised Claude Code install, or
a user who deliberately configures Claude Code to bypass permissions in their own
settings. Zordon reports that last case and does not change it.

## What Zordon never does

* **Never bypasses permissions.** No code path builds a `claude` command line
  containing `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`
  or `--permission-mode bypassPermissions`. `discovery.resume_command` is the only
  constructor of that command line and `tests/test_safety.py` greps the whole
  package for the flag names. `bypassPermissions` is not a config value and is
  never written to any settings file. If your own Claude Code settings already use
  it, Zordon tells you on session start.
* **Never approves a prompt for you.** Permission prompts are spoken at every
  verbosity level, and while one is showing, voice input is narrowed to a strict
  yes or no: the router must return `yes` or `no` at 0.95 probability or higher,
  otherwise Zordon reads back what it heard and asks again. "Always allow",
  "yes to all", "don't ask again" and "switch to auto mode" are refused by voice;
  those options stay available to you in the terminal. Plan approval by voice
  selects "manually approve edits", never "use auto mode". The trust dialog that
  Claude Code shows on first launch in a directory defaults to "No, exit";
  Zordon accepts it only after you tap or speak a confirmation. No hook that could
  answer a permission request is ever registered (decision 0009).
* **Never widens the permission mode by voice.** Voice can switch between
  `default`, `acceptEdits` and `plan`. `auto` and `dontAsk` need a tap on the
  client. `bypassPermissions` is refused everywhere.
* **Never edits allow or deny rules.** `settings.json` and
  `.claude/settings.local.json` are Claude Code's. Zordon reads them to speak a
  summary and points you at the file.
* **Never interprets keystrokes.** Text is sent with `tmux send-keys -l` (literal),
  so nothing in an utterance is read as a tmux key name; C0 control characters are
  stripped first; Enter is a separate call. Named keys (Escape, Down, Enter, ...)
  come from a fixed allowlist.
* **Never constructs a command.** The router can only *select* a shim command from
  the closed set in `zordon/routing/commands.py`; the dispatcher rejects anything
  else. "Delete the session" asks for confirmation.
* **Never sends keys to the client.** Provider API keys live in
  `~/.zordon/config.toml` (mode `0600`) or in environment variables, are passed to
  the SDKs explicitly, and are never exported to child processes, logged, included
  in the `hello` or `settings` messages, or stored in a transcript row. The browser
  holds only a session cookie.
* **Never binds to the network without a token.** The server listens on
  `127.0.0.1` unless told otherwise and refuses to start on any other address
  without `server.token`. Under `--tunnel` the login rate limit (5 failures per
  minute per IP) and the 30-minute idle disconnect are forced on and the cookie is
  `Secure`. Details in `remote-access.md`.
* **Never auto-approves to hide a stall.** If a working session produces no output
  for 20 seconds and no prompt was detected, Zordon says it looks like Claude Code
  is waiting on something and shows the last pane lines. That is the backstop for
  prompt formats the regex has not seen yet. The earlier prototype's workaround of
  starting with all permissions bypassed is explicitly rejected.

## Transcript redaction

Every line from the pane or the session file passes through
`zordon/transcript/redaction.py` before the normalizer, the TTS, the transcript
database or any client sees it. Matches are replaced with `[redacted]`. The patterns
are deliberately broad; a false positive costs a masked word, a false negative reads
a key aloud over a tunnel. Covered:

| Shape | Examples |
| --- | --- |
| Provider key prefixes | `sk-ant-...`, `sk-...`, `sk-proj-...`, `xox[abprs]-...` (Slack), `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` and `github_pat_` (GitHub), `AKIA...` (AWS access key id), `AIza...` (Google), `glpat-...` (GitLab) |
| JSON Web Tokens | three base64url segments starting with `ey` |
| Authorization headers | `Bearer <token>`, `Basic <credentials>` |
| Assignments to secret-looking names | `API_KEY=...`, `TOKEN: ...`, `PASSWORD=...`, `AWS_SECRET_ACCESS_KEY=...`, anything containing `SECRET`, `TOKEN`, `PASSWORD`, `PASSWD`, `API_KEY`, `APIKEY`, `PRIVATE_KEY`, `ACCESS_KEY` or `AUTH` with a value of 6+ characters (the name is kept, the value is masked) |
| Private key blocks | `-----BEGIN ... PRIVATE KEY-----` through the matching `END`, and a bare header line |
| URLs with embedded credentials | `scheme://user:pass@host` (the credentials are masked, the scheme kept) |
| Long hex blobs | 40 or more hex characters with no spaces |

Not covered: secrets that do not look like any of the above (a plain word used as a
password, an unprefixed random string shorter than 40 hex characters). The raw
terminal is still the raw terminal; redaction changes what Zordon repeats, not what
Claude Code shows.

The transcript database (`~/.zordon/transcripts.db`) stores the redacted text only.
Uploads from the client are written to `<project>/.zordon/uploads/` with sanitized
file names; add that directory to your `.gitignore`.

## Rotating the token

The token is `server.token` in `~/.zordon/config.toml`. To replace it:

1. Stop Zordon.
2. Generate a new value, for example
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
3. Edit `server.token` in `config.toml`.
4. Start Zordon. Session cookies live in memory, so every logged-in browser is
   logged out by the restart; `zordon token show` prints the new value to type on
   the phone.

Deleting the `token` line is not a rotation: a file without a token only works on
`127.0.0.1`, and the server refuses to bind anywhere else. There is no
`zordon token rotate` command yet.

## Reporting

If you find a way to make Zordon approve a prompt, widen a permission, run a
command the router did not select from the closed set, or leak a key, open an issue
marked security on the repository's issue tracker and include the pane fixture or
transcript row that triggered it, with secrets removed.
