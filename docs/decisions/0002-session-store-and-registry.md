# 0002: Session discovery from the Claude Code store and process registry

**Status:** accepted, 2026-10-02

## Question

From the design: "What does Claude Code's session store under `~/.claude/projects/`
actually contain in the installed version, and is the session id stable enough to
key on? Check before writing the picker."

## What was verified

Checked read-only against Claude Code 2.1.287 on 2026-10-01/02: 14 transcript files
(~80k lines, written by versions 2.1.227 to 2.1.287), every `(project dir, cwd)` pair
in the store, `~/.claude/history.jsonl` (4,546 lines), and `~/.claude/sessions/`
(9 live entries, checked against `/proc`).

**Transcript files.** `~/.claude/projects/<project>/<session uuid>.jsonl`, or under
`$CLAUDE_CONFIG_DIR` when that is set. The session id is the file stem, is repeated
as `sessionId` on every record, and is the id Claude Code prints on exit
(`claude --resume <uuid>`). It is the right key.

**The project directory name is lossy.** Every character outside `[A-Za-z0-9]`
becomes `-`, including the leading `/`, spaces, dots and underscores:

| directory name | real cwd |
| --- | --- |
| `-home-operator-Code-example-com` | `/home/operator/Code/example.com` |
| `-home-operator-Videos-Season-1-Mp4-1080p` | `/home/operator/Videos/Season 1 Mp4 1080p` |
| `-tmp-claude-1000--home-operator-Code-zordon-...` | `/tmp/claude-1000/-home-operator-Code-zordon/...` |

Names over 200 characters are truncated and get a hash suffix. The name cannot be
decoded back to a path. The real cwd has to come from a record's `cwd` field
(present on every conversation record, first found on lines 3-9 of a fresh file),
from `attachment.snapshot.workingDirectory`, or from `history.jsonl`, whose lines
carry `project` (the real path) and `sessionId`. The directory name is only the last
resort and is flagged as approximate when used.

**Record types.** Metadata records have no timestamp and are rewritten; the latest
one wins: `ai-title` (`aiTitle`), `custom-title` (`customTitle`, takes precedence),
`permission-mode` (`permissionMode`: `default`, `acceptEdits`, `plan`, `auto`,
`dontAsk`, `bypassPermissions`; never `manual`), `last-prompt` (`lastPrompt`, about
200 chars). Conversation records carry `parentUuid`, `uuid`, `timestamp` (ISO 8601),
`cwd`, `sessionId`, `version`, `gitBranch`. The two probes disagree on the assistant
record's `type`: a fresh 2.1.287 session showed `assistant`; the bulk census showed
`message` with `message.role == "assistant"` (27,643 records) and `assistant` only
twice, both synthetic. `session/jsonl.py` therefore accepts both and keys on
`message.role`. Claude Code documents the format as internal and version-unstable.

**File sizes.** 0.01 MB to 278 MB; single lines up to 1.28 MB; a forked file's first
`cwd` can be 15.6 MB in. All "latest" metadata sat within 30 KB of the end in every
file. A full scan of the 278 MB file took 0.30 s warm.

**Process registry.** `~/.claude/sessions/<pid>.json`, one per live Claude Code
process, with `pid`, `sessionId`, `cwd`, `startedAt` (epoch ms), `procStart`,
`version`, `kind` (`interactive` or `bg`), `status` (`idle`, `busy`, `waiting`),
`statusUpdatedAt`, `name`, and `tmux` when the process was started inside tmux.
`tmux` has the form `#{session_name}:#{window_id}.#{pane_id}` (for example
`zordon-probe:@1.%1`) and works directly as a `-t` target; verified with
`tmux display-message -p -t '<value>' '#{pane_pid}'`. Entries with `spare: true`
are the background daemon's pre-warmed workers, not sessions. `procStart` equals
field 22 of `/proc/<pid>/stat` (start time in clock ticks since boot) for all eight
live pids checked. Stale files persist after crashes, so `kill -0` alone is not a
liveness test.

## Decision

* The session id is the jsonl file stem. `discovery.encode_project_dir(cwd)` is used
  only to predict where a new session's file will appear; it is never decoded.
* The cwd for the picker comes from, in order: the first `cwd` in the file's head
  (first 40 lines, 2 MB cap), `history.jsonl` indexed by `sessionId`, then the
  directory name marked approximate.
* Title precedence: `custom-title`, `ai-title`, `last-prompt`, first typed user
  prompt (a `user` record with string content, not `isMeta`, not
  `isCompactSummary`, not starting with `<`).
* The tail is read in a 256 KB window that grows by 4x up to 8 MB until a line with
  a timestamp is found. Head and tail results are cached per file on
  `(size, mtime_ns)`.
* Permission mode is the latest `permission-mode` record, read about 1-2 s after
  launch; settings files are only an estimate before that.
* A session is **running** when the registry has an entry whose `pid` answers
  `kill -0` **and** whose `procStart` matches `/proc/<pid>/stat` field 22. A running
  session is never passed to `claude --resume`: two resumes of one id interleave
  into one transcript, and resuming a running background session exits 1. Instead,
  if `tmux` is set, Zordon attaches to that pane; if `kind` is `bg`, it opens
  `claude attach <id>` in a new pane; otherwise it reports "running in another
  terminal".
* New panes are created with `tmux new-window -d -P -F
  '#{session_name}:#{window_id}.#{pane_id}'` so Zordon's own pane target has the
  same shape as the registry's `tmux` field.
* The registry's `status` field (`waiting` was observed while a session sat on a
  prompt) is read as a third state signal alongside the pane regex and the hook
  (decision 0009).

## Open

* The 200-character truncation and hash suffix were not reproduced; the encoder
  returns the plain replacement and the picker falls back to `cwd` fields, so a
  wrong prediction only costs a slower jsonl lookup.
* Whether `status == "waiting"` means exactly "blocked on a prompt" is inferred from
  one observation.
* Both the pane and the jsonl formats are version-unstable by documentation. The
  jsonl parser carries its own version marker next to `PROMPTS_VERSION`
  (see `docs/prompts-version.md`).
