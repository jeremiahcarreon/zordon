"""The system prompt appended to Claude Code sessions Zordon launches (decision 0019).

Claude writes for a terminal by default: headers, lists, paths, code, a report at
the end. Read aloud, that is slow and hard to follow. This text, passed with
``--append-system-prompt``, tells Claude it is on a call and asks for the shape a
listener needs. The talk-first paragraph is added for projects that want questions
and a stated plan before anything changes.

The user owns the wording: ``~/.zordon/voice_prompt.md`` overrides the default
below (the Settings sheet edits it); a missing or empty file means the default.
Changes apply to sessions opened afterwards.

Kept short on purpose: it rides on every request of the session.
"""

from __future__ import annotations

import os
from pathlib import Path

from zordon import paths

VOICE_MODE = """\
You are on a phone call. The user talks to you by voice through Zordon and hears every \
word you write read aloud by a text-to-speech voice; they cannot see a screen. Write only \
what a person would say on a call:
- Short turns: two or three plain sentences, then stop and let them answer. No markdown, \
headers, bullet lists, tables, code blocks, URLs or file paths. Say file and function names \
in words ("the upload handler", "the settings file").
- Keep them in the loop like a colleague on a call: say what you are about to do in one \
sentence, do it, then say what happened in one or two sentences. Do not narrate every file \
or command, and do not give an overview or a summary unless asked.
- One question at a time, and only when you need the answer. For a choice between options \
use the AskUserQuestion tool with short labels; Zordon reads the options aloud and returns \
the pick. Never list several questions in one turn.
- When they ask a question, answer it first, in one sentence, then add at most one \
suggestion. No detail unless they ask for more.
- Never write "waiting for your confirmation", "let me know", or a sign-off; just stop."""

TALK_FIRST = """\
Before changing anything that is not trivial, talk it through: ask the clarifying questions \
you need (one at a time), then say your plan in one or two sentences and wait for the user to \
say "go ahead". Treat "go ahead", "do it" or "yes" as approval of the plan you just said."""

OVERRIDE_FILE = "voice_prompt.md"


def override_path() -> Path:
    return paths.zordon_home() / OVERRIDE_FILE


def default_prompt(*, talk_first: bool = True) -> str:
    return VOICE_MODE + ("\n\n" + TALK_FIRST if talk_first else "")


def custom_prompt() -> str | None:
    """The user's own text, or None when there is none (missing or empty file)."""
    try:
        text = override_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def set_custom_prompt(text: str | None) -> None:
    """Write the user's text (0600), or remove the override for an empty/None text."""
    p = override_path()
    clean = (text or "").strip()
    if not clean:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    paths.ensure_private_dir(p.parent)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(clean + "\n")


def system_prompt(*, talk_first: bool = True) -> str:
    """What a session gets: the user's text when they wrote one (talk-first paragraph
    appended unless their text already mentions "go ahead"), else the default."""
    custom = custom_prompt()
    if custom is None:
        return default_prompt(talk_first=talk_first)
    if talk_first and "go ahead" not in custom.lower():
        return custom + "\n\n" + TALK_FIRST
    return custom
