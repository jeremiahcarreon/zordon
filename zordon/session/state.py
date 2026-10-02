"""Session state machine. Pure: no I/O, no threads.

State is derived from what the pane shows, never from what we sent. The design's
rule: if we send a message and nothing changes on screen, the state stays Idle
and the client is told so.

    Idle -> Working                    output advanced, or spinner visible
    Working -> AwaitingPermission      permission prompt detected
    Working -> AwaitingPlanApproval    plan approval prompt detected
    Working -> AwaitingQuestion        option-pick prompt detected
    Working -> Idle                    idle prompt detected (input box, no spinner)
    Working -> Stalled                 no output for idle_watchdog_seconds and no prompt detected
    Stalled -> Working                 any output
    Stalled -> Idle                    idle prompt detected
    Awaiting* -> Working | Idle        prompt gone (answered in the pane or by voice)
    any -> Detached                    pane gone
    Detached -> Idle                   pane recreated (resume)
"""

from __future__ import annotations

from dataclasses import dataclass

from zordon.bus import PromptKind, SessionState


@dataclass(slots=True)
class Observation:
    """One poll's worth of facts about the pane."""

    pane_alive: bool
    prompt_kind: PromptKind | None  # from prompts.detect_prompt
    idle: bool  # prompts.is_idle_prompt
    working: bool  # prompts.is_working (spinner / esc to interrupt)
    output_advanced: bool  # new lines since last poll (pane or jsonl)
    seconds_since_output: float
    prompt_score: float = 0.0  # router second opinion, 0..1; used when regex is silent


PROMPT_STATES = {
    PromptKind.PERMISSION: SessionState.AWAITING_PERMISSION,
    PromptKind.TRUST: SessionState.AWAITING_PERMISSION,
    PromptKind.PLAN: SessionState.AWAITING_PLAN_APPROVAL,
    PromptKind.QUESTION: SessionState.AWAITING_QUESTION,
}

AWAITING = {
    SessionState.AWAITING_PERMISSION,
    SessionState.AWAITING_PLAN_APPROVAL,
    SessionState.AWAITING_QUESTION,
}


def next_state(
    current: SessionState,
    obs: Observation,
    watchdog_seconds: float,
    prompt_score_threshold: float = 0.9,
) -> tuple[SessionState, str]:
    """Return (new_state, detail). ``detail`` is a short human reason for the client."""
    if not obs.pane_alive:
        return SessionState.DETACHED, "pane is gone"

    if current is SessionState.DETACHED:
        # Pane came back. Fall through to classify it like a fresh poll.
        current = SessionState.IDLE

    if obs.prompt_kind is not None:
        return PROMPT_STATES[obs.prompt_kind], f"{obs.prompt_kind.value} prompt detected"

    if current in AWAITING:
        # Prompt is no longer on screen: it was answered somewhere.
        if obs.idle and not obs.working:
            return SessionState.IDLE, "prompt answered, back at the input prompt"
        return SessionState.WORKING, "prompt answered"

    if obs.working:
        if current is SessionState.STALLED and not obs.output_advanced:
            return SessionState.STALLED, "still no output"
        return SessionState.WORKING, "producing output"

    if obs.idle:
        return SessionState.IDLE, "at the input prompt"

    if obs.output_advanced:
        return SessionState.WORKING, "producing output"

    if current in (SessionState.WORKING, SessionState.STALLED):
        if obs.seconds_since_output >= watchdog_seconds:
            if obs.prompt_score >= prompt_score_threshold:
                return (
                    SessionState.AWAITING_PERMISSION,
                    "no output and the screen looks like a prompt waiting for input",
                )
            return SessionState.STALLED, f"no output for {int(obs.seconds_since_output)} seconds"
        return SessionState.WORKING, "waiting for output"

    return current, ""


def is_awaiting(state: SessionState) -> bool:
    return state in AWAITING
