# 0009: A per-launch Notification hook is the second signal; the pane regex stays primary

**Status:** accepted, 2026-10-02

## Question

Prompt detection by regex will never be complete (the design says so and adds the
idle watchdog as a backstop). Claude Code has a hook system. Can a hook tell Zordon
that a prompt is up, without touching the user's settings files and without any
hook ever answering on the user's behalf?

## What was verified

Claude Code 2.1.287 with an isolated `CLAUDE_CONFIG_DIR` and `--init-only`; nothing
was written to the real `~/.claude`.

* `claude --init-only --settings '{"hooks":{"SessionStart":[{"matcher":"startup","hooks":[{"type":"command","command":"cat > .../hook-fired.json","timeout":10}]}]}}'`
  exited 0 and the hook fired with the documented payload (`session_id`,
  `transcript_path`, `cwd`, `hook_event_name`, `source`). **Hooks passed inline
  through `--settings` run for that one process and write nothing to disk.**
* The same with `{"type":"http","url":"http://127.0.0.1:8799/hooks/claude"}`:
  exit 0, no request received, debug log `Skipping HTTP hook ... HTTP hooks are not
  supported for SessionStart`. Whether `Notification` supports `http` is unknown;
  `command` with `curl` is event-agnostic and works.
* Documented `Notification` matchers include `permission_prompt`, `idle_prompt`,
  `agent_needs_input`, `elicitation_dialog`; the payload adds `notification_type`,
  `message` (one line, for example "Claude needs permission to run a Bash command")
  and `title`. "Notification: No [blocking]. Exit code and stderr are ignored."
* `--settings` is applied above user, project and local settings and below managed
  settings; it "lasts one session and doesn't write to any file", and is **not
  restored on resume**, so it must be passed on every launch.
* `--bare`, `--safe-mode` and `disableAllHooks: true` disable settings hooks.
* A `Stop` hook that exits 2 or prints decision JSON blocks Claude Code from
  continuing. A `PermissionRequest` hook can `allow` or `deny` on the user's
  behalf.
* The process registry (decision 0002) also carries `status: "waiting"` while a
  session sits on a prompt.

## Decision

* On every launch Zordon writes a hooks JSON file under its own home
  (`$ZORDON_HOME/hooks/<session id prefix>.json`, mode `0600`) and passes
  `--settings <that file>`. A file rather than inline JSON so the shared secret is
  not visible in `ps` or `/proc/*/cmdline`. The file is removed when the pane ends.
* The file registers `command` handlers of the shape
  `curl -s -m 2 -X POST -H 'Content-Type: application/json' -H 'X-Zordon-Hook-Secret: <secret>' --data-binary @- http://127.0.0.1:<port>/hooks/claude >/dev/null 2>&1 || true`
  with `"async": true` and `"timeout": 5`, on `Notification` (matcher
  `permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog`),
  `UserPromptSubmit` and `Stop`. Every handler exits 0 and prints nothing, so no
  hook can ever block or alter Claude Code's behaviour.
* The secret is `AgentAPI.hook_secret`, generated per Zordon process and distinct
  from the browser token; `POST /hooks/claude` accepts only direct loopback peers
  (a request carrying `CF-Connecting-IP`/`X-Forwarded-For`, i.e. one that came
  through the tunnel, is refused), caps the body at 64 KB, checks the header with a
  constant-time compare, and forwards the payload to
  `SessionControl.hook_event()`, which matches it to a session by `session_id`.
* Hook events are hints to the state machine: `permission_prompt` makes the next
  poll re-run prompt detection and, if the regex still finds nothing, raises the
  "looks like a prompt" notice immediately instead of after the 20 s watchdog;
  `idle_prompt` and `Stop` nudge toward Idle; `UserPromptSubmit` toward Working.
  The payload's `message` is spoken when the regex has no options to offer.
* **No `PermissionRequest` hook is registered.** It is the one hook type that can
  answer a prompt, and the design forbids anything in Zordon doing that.
  *Superseded by decision 0019 (0.5.0):* the hook is registered, and it answers
  only with the user's own spoken or tapped decision; nothing in Zordon decides
  on its own. The screen reader below is the fallback when the hook has no answer.
* The pane regex stays primary because the hook says only *that* a prompt is up and
  its one-line message, not the options or which key answers which; because it does
  not fire under `disableAllHooks`; and because the `--settings` hooks vanish if the
  user resumes the session from another terminal. The registry `status` is the
  third, hook-free signal.

## Open

* Whether `Notification` accepts `type: "http"` was not tested; `command` + `curl`
  is used regardless. `curl` is a soft dependency: `zordon doctor` reports WARN
  ("hook signals are disabled") when it is missing.
* The delay before `idle_prompt` fires is undocumented.
* ~~A `PermissionRequest` observer~~ Done in 0019: the hook carries `tool_name`
  and `tool_input` (and the question tool's questions, and the plan), Zordon
  speaks them, and the user's answer is the decision.
* The `Stop` payload carries `last_assistant_message`, a clean prose source that
  could cross-check the jsonl tail; unused for now.
