from __future__ import annotations

import pytest

from zordon.bus import PromptKind
from zordon.bus import SessionState as S
from zordon.session.state import Observation, next_state


def obs(**kw) -> Observation:
    base = dict(
        pane_alive=True,
        prompt_kind=None,
        idle=False,
        working=False,
        output_advanced=False,
        seconds_since_output=0.0,
        prompt_score=0.0,
    )
    base.update(kw)
    return Observation(**base)


def test_idle_stays_idle_when_nothing_changes():
    assert next_state(S.IDLE, obs(idle=True), 20)[0] is S.IDLE


def test_sending_input_does_not_change_state_by_itself():
    # The design: state comes from the screen, never from what we sent.
    assert next_state(S.IDLE, obs(idle=True, output_advanced=False), 20)[0] is S.IDLE


def test_output_moves_to_working():
    assert next_state(S.IDLE, obs(output_advanced=True), 20)[0] is S.WORKING
    assert next_state(S.IDLE, obs(working=True), 20)[0] is S.WORKING


@pytest.mark.parametrize(
    "kind,state",
    [
        (PromptKind.PERMISSION, S.AWAITING_PERMISSION),
        (PromptKind.TRUST, S.AWAITING_PERMISSION),
        (PromptKind.PLAN, S.AWAITING_PLAN_APPROVAL),
        (PromptKind.QUESTION, S.AWAITING_QUESTION),
    ],
)
def test_prompts_win_over_everything(kind, state):
    for cur in S:
        assert next_state(cur, obs(prompt_kind=kind, working=True, idle=True), 20)[0] is state


def test_watchdog_stalls_then_recovers():
    st, detail = next_state(S.WORKING, obs(seconds_since_output=25), 20)
    assert st is S.STALLED and "25" in detail
    assert next_state(S.STALLED, obs(output_advanced=True), 20)[0] is S.WORKING
    assert next_state(S.STALLED, obs(idle=True), 20)[0] is S.IDLE


def test_watchdog_with_prompt_score_second_opinion():
    st, _ = next_state(S.WORKING, obs(seconds_since_output=25, prompt_score=0.95), 20)
    assert st is S.AWAITING_PERMISSION
    st, _ = next_state(S.WORKING, obs(seconds_since_output=25, prompt_score=0.5), 20)
    assert st is S.STALLED


def test_prompt_answered_returns_to_working_or_idle():
    assert next_state(S.AWAITING_PERMISSION, obs(working=True), 20)[0] is S.WORKING
    assert next_state(S.AWAITING_PERMISSION, obs(idle=True), 20)[0] is S.IDLE
    assert next_state(S.AWAITING_PLAN_APPROVAL, obs(), 20)[0] is S.WORKING


def test_detach_and_reattach():
    assert next_state(S.WORKING, obs(pane_alive=False), 20)[0] is S.DETACHED
    assert next_state(S.DETACHED, obs(idle=True), 20)[0] is S.IDLE
    assert next_state(S.DETACHED, obs(pane_alive=False), 20)[0] is S.DETACHED
