"""The closed set of shim commands the router may select.

The router returns a *name* from this table; it cannot construct a command. The
dispatcher looks the name up here and calls the matching handler on the agent.
Destructive commands require spoken (or tapped) confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ShimCommand:
    name: str
    description: str  # shown to the router as the meaning of the command
    examples: tuple[str, ...]
    takes_argument: str | None = None  # "session" | "verbosity" | "mode" | "text" | None
    confirm: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


SHIM_COMMANDS: tuple[ShimCommand, ...] = (
    ShimCommand("mute", "stop speaking output until unmuted", ("mute", "be quiet", "shut up for now")),
    ShimCommand("unmute", "resume speaking output", ("unmute", "you can talk again")),
    ShimCommand(
        "stop",
        "interrupt Claude Code itself by sending Escape to its pane",
        ("stop", "cancel that", "abort", "stop what you're doing"),
    ),
    ShimCommand(
        "repeat",
        "say the last spoken sentence again",
        ("repeat that", "say that again", "what did you say"),
    ),
    ShimCommand(
        "status",
        "say the focused session's state and what it is doing",
        ("status", "what are you doing", "are you still working", "where are we"),
    ),
    ShimCommand(
        "set_verbosity",
        "change how much output is spoken: minimal, normal or technical",
        ("set verbosity to technical", "be more verbose", "minimal verbosity", "less detail"),
        takes_argument="verbosity",
    ),
    ShimCommand(
        "set_tool_chatter",
        "turn tool-call and progress narration on or off",
        ("turn on tool chatter", "stop telling me about tool calls"),
        takes_argument="text",
    ),
    ShimCommand(
        "focus",
        "switch voice to a different session by name or directory",
        ("switch to the API session", "focus the zordon session", "go to the other project"),
        takes_argument="session",
    ),
    ShimCommand(
        "list_sessions",
        "read out the available sessions",
        ("what sessions are there", "list sessions"),
    ),
    ShimCommand(
        "set_permission_mode",
        "switch Claude Code's permission mode: default, accept edits, or plan",
        ("switch to plan mode", "accept edits mode", "back to default permissions"),
        takes_argument="mode",
    ),
    ShimCommand(
        "delete",
        "delete the focused session's pane (asks for confirmation)",
        ("delete the session", "kill this session"),
        confirm=True,
    ),
    ShimCommand(
        "detach",
        "stop following the focused session but leave it running",
        ("detach", "leave this session running"),
    ),
    ShimCommand(
        "hush",
        "stop reading the current answer aloud; the agent keeps working and nothing is sent",
        ("got it", "say no more", "that's enough", "okay okay", "shut up"),
    ),
    ShimCommand(
        "set_model",
        "switch the agent's model for this session: sonnet, opus, fable, haiku or a full model id",
        ("switch to <model>", "use <model>", "change the model to <model>"),
        takes_argument="model",
    ),
    ShimCommand(
        "set_effort",
        "set the agent's effort level for this session: low, medium, high, x high or max",
        ("set effort to <level>", "effort <level>", "<level> effort"),
        takes_argument="effort",
    ),
    # deferred submit (decision 0019)
    ShimCommand(
        "send",
        "send what has been said so far to the agent now, instead of waiting for the quiet",
        ("go ahead", "send it", "send that", "that's all", "over"),
    ),
    ShimCommand(
        "scratch",
        "throw away what has been said so far before it is sent",
        ("scratch that", "never mind", "clear that", "start over"),
    ),
    # projects (decision 0018)
    ShimCommand(
        "open_project",
        "continue a saved project by name: reconnects to it or starts it again",
        ("open the <name> project", "continue <name>", "work on <name>"),
        takes_argument="project",
    ),
    ShimCommand(
        "list_projects",
        "read out the saved projects",
        ("what projects do I have", "list projects"),
    ),
    ShimCommand(
        "new_project",
        "start a new project (the app walks through folder and settings)",
        ("start a new project", "new project"),
    ),
    ShimCommand(
        "admin",
        "pause work: leave the current project focused on nothing, every pane keeps running",
        ("pause", "admin mode", "back to projects", "leave the project"),
    ),
)

BY_NAME = {c.name: c for c in SHIM_COMMANDS}
COMMAND_NAMES = tuple(c.name for c in SHIM_COMMANDS)

# Words that must never be accepted as a permission answer by voice.
FORBIDDEN_PERMISSION_PHRASES = (
    "always allow",
    "always",
    "don't ask again",
    "do not ask again",
    "yes to all",
    "allow all",
    "skip permissions",
)


def describe_for_router() -> str:
    """Compact description of the command set for a router's instructions."""
    lines = []
    for c in SHIM_COMMANDS:
        ex = "; ".join(c.examples[:3])
        lines.append(f"- {c.name}: {c.description}. e.g. {ex}")
    return "\n".join(lines)
