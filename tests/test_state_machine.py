from __future__ import annotations

import pytest

from agentgear.exceptions import (
    InvalidObservationError,
    InvalidStateTransitionError,
    NotCompletedError,
)
from agentgear.models import ExecutionState
from agentgear.watchdog.state_machine import ExecutionStateMachine


def test_initial_state_is_planning() -> None:
    sm = ExecutionStateMachine(execution_id="e1")
    assert sm.state == ExecutionState.PLANNING


def test_planning_to_running_is_legal() -> None:
    sm = ExecutionStateMachine(execution_id="e1")
    sm.transition(ExecutionState.RUNNING, at_seconds=1.0)
    assert sm.state == ExecutionState.RUNNING


def test_running_to_completed_is_illegal_directly() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0)


def test_full_recovery_cycle_running_stalled_recovering_running() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=1.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=2.0)
    sm.transition(ExecutionState.RUNNING, at_seconds=3.0)
    assert sm.state == ExecutionState.RUNNING
    assert [r.to_state for r in sm.history] == [
        ExecutionState.STALLED,
        ExecutionState.RECOVERING,
        ExecutionState.RUNNING,
    ]


def test_recovery_exhausted_leads_to_blocked() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=1.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=2.0)
    sm.transition(ExecutionState.BLOCKED, at_seconds=3.0)
    assert sm.state == ExecutionState.BLOCKED


def test_blocked_can_resume_recovery_via_human_intervention() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.BLOCKED)
    sm.transition(ExecutionState.RECOVERING, at_seconds=1.0)
    assert sm.state == ExecutionState.RECOVERING


def test_completed_requires_evidence() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    with pytest.raises(NotCompletedError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0)
    assert sm.state == ExecutionState.REVIEWING  # rejected transition leaves state unchanged


def test_completed_requires_non_blank_evidence_strings() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    with pytest.raises(NotCompletedError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0, evidence=("   ", ""))


def test_completed_with_real_evidence_succeeds() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    sm.transition(ExecutionState.COMPLETED, at_seconds=1.0, evidence=("all tests pass",))
    assert sm.state == ExecutionState.COMPLETED


def test_completed_is_terminal() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    sm.transition(ExecutionState.COMPLETED, at_seconds=1.0, evidence=("done",))
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(ExecutionState.RUNNING, at_seconds=2.0)


def test_idle_or_silence_can_never_reach_completed_without_a_call() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    # No transitions at all: state must remain RUNNING, never silently COMPLETED.
    assert sm.state == ExecutionState.RUNNING
    assert sm.history == []


def test_stalled_cannot_skip_directly_to_running() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.STALLED)
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(ExecutionState.RUNNING, at_seconds=1.0)


def test_error_message_lists_legal_targets() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.STALLED)
    with pytest.raises(InvalidStateTransitionError, match="recovering"):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0)


def test_transitions_reject_time_going_backwards() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=10.0)
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(ExecutionState.RECOVERING, at_seconds=5.0)
    # the rejected transition must not have mutated state or history
    assert sm.state == ExecutionState.STALLED
    assert len(sm.history) == 1


def test_transitions_allow_equal_timestamps() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=10.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=10.0)
    assert sm.state == ExecutionState.RECOVERING


# --- Round 2 / H3: evidence must survive in the transition history --------


def test_completed_evidence_is_preserved_in_history() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    sm.transition(
        ExecutionState.COMPLETED, at_seconds=1.0, evidence=("all tests pass", "coverage 95%")
    )
    record = sm.history[-1]
    assert record.evidence == ("all tests pass", "coverage 95%")
    assert record.to_state == ExecutionState.COMPLETED


def test_evidence_blank_and_non_string_entries_are_filtered_not_stored() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(
        ExecutionState.TESTING,
        at_seconds=1.0,
        evidence=("real evidence", "   ", "", "also real"),
    )
    record = sm.history[-1]
    assert record.evidence == ("real evidence", "also real")


def test_evidence_defaults_to_empty_tuple_when_not_supplied() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.TESTING, at_seconds=1.0)
    assert sm.history[-1].evidence == ()


def test_evidence_must_be_a_tuple() -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.RUNNING)
    with pytest.raises(InvalidObservationError):
        sm.transition(ExecutionState.TESTING, at_seconds=1.0, evidence=["not", "a", "tuple"])  # type: ignore[arg-type]


# --- Round 2 / section 12: COMPLETED must not become easier to forge ------


@pytest.mark.parametrize("bad_evidence", [(), ("",), ("   ",), (42,)])
def test_completed_rejects_every_kind_of_meaningless_evidence(bad_evidence) -> None:
    sm = ExecutionStateMachine(execution_id="e1", state=ExecutionState.REVIEWING)
    with pytest.raises(NotCompletedError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0, evidence=bad_evidence)
    assert sm.state == ExecutionState.REVIEWING
