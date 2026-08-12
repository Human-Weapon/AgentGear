"""End-to-end tests for ExecutionWatchdog using ONLY the public coordinator
API — no direct use of ExecutionStateMachine, StallDetector, RecoveryEngine,
or LoopGuard. This is what AG-05 requires: proof that the composition
itself (not just its parts in isolation) enforces the invariants.
"""

from __future__ import annotations

import pytest

from agentgear.config import BudgetPolicy, Policy, WatchdogPolicy
from agentgear.escalation import EscalationSignals
from agentgear.exceptions import (
    InvalidObservationError,
    InvalidStateTransitionError,
    NotCompletedError,
)
from agentgear.models import (
    ExecutionState,
    ModelTier,
    ProgressSignalKind,
    ReasoningEffort,
    RecoveryResult,
)
from agentgear.watchdog import ExecutionWatchdog


def _policy(**watchdog_overrides) -> Policy:
    defaults = dict(
        no_progress_seconds=5.0,
        no_progress_cycles=2,
        max_recovery_attempts=10,
        max_total_attempts=1000,
    )
    defaults.update(watchdog_overrides)
    return Policy(watchdog=WatchdogPolicy(**defaults))


def test_start_enters_running() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="do the thing", at_seconds=0.0)
    assert w.state == ExecutionState.RUNNING


def test_happy_path_reaches_completed_with_evidence() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="ship a fix", at_seconds=0.0)
    w.record_progress(
        at_seconds=1.0, kind=ProgressSignalKind.FILE_CHANGED, description="edited foo.py"
    )
    w.advance(ExecutionState.TESTING, at_seconds=2.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=3.0)
    w.complete(at_seconds=4.0, evidence=("all tests pass",))
    assert w.state == ExecutionState.COMPLETED


def test_completed_without_evidence_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    with pytest.raises(NotCompletedError):
        w.complete(at_seconds=2.0, evidence=())
    assert w.state == ExecutionState.REVIEWING


def test_silence_can_never_be_mistaken_for_completed() -> None:
    """No calls at all after start(): the coordinator must not report
    COMPLETED just because nothing happened."""
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    assert w.state == ExecutionState.RUNNING
    assert w.state != ExecutionState.COMPLETED


# --- automatic stall -> recovery -------------------------------------------


def test_activity_without_progress_auto_transitions_to_recovering() -> None:
    """This is the AG-01/AG-02 regression at the coordinator level: 100%
    unique-fingerprint busywork, zero progress events, must still be
    caught — and the coordinator must drive the transition itself.
    """
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="busy but stuck", at_seconds=0.0)
    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    assert w.status()["recovery_attempts"] == 1


def test_progress_resets_the_stall_clock() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    for i in range(3):
        w.record_activity(at_seconds=float(i), fingerprint=f"a-{i}", succeeded=True)
    w.record_progress(
        at_seconds=3.0, kind=ProgressSignalKind.SUBTASK_COMPLETED, description="parsed input"
    )
    # Only a little more activity right after progress: must not stall.
    w.record_activity(at_seconds=3.5, fingerprint="a-after", succeeded=True)
    assert w.state == ExecutionState.RUNNING


def test_recovery_success_returns_to_resume_state() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    w.record_recovery_result(at_seconds=100.0, result=RecoveryResult.SUCCESS)
    assert w.state == ExecutionState.RUNNING


def test_recovery_failure_returns_to_stalled_for_another_attempt() -> None:
    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=10))
    w.start(task="x", at_seconds=0.0)
    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    w.record_recovery_result(at_seconds=100.0, result=RecoveryResult.FAILURE)
    assert w.state == ExecutionState.STALLED


def test_recovery_exhaustion_reaches_blocked_with_validated_report() -> None:
    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=2))
    w.start(task="x", at_seconds=0.0)
    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)

    t = 100.0
    for _ in range(10):
        if w.state == ExecutionState.STALLED:
            w.begin_recovery(at_seconds=t)
        elif w.state == ExecutionState.RECOVERING:
            w.record_recovery_result(at_seconds=t, result=RecoveryResult.FAILURE)
        else:
            break
        t += 1.0

    assert w.state == ExecutionState.BLOCKED
    report = w.blocked_report
    assert report is not None
    assert report.blocker
    assert report.root_cause
    assert report.recommended_human_action
    assert report.attempts >= 0


def test_blocked_is_never_reached_without_a_report() -> None:
    """Structural guarantee: whenever state == BLOCKED, blocked_report is
    populated. There is no coordinator code path that sets one without
    the other (the only route to BLOCKED is _transition_to_blocked)."""
    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=1))
    w.start(task="x", at_seconds=0.0)
    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)
    t = 100.0
    for _ in range(10):
        if w.state == ExecutionState.BLOCKED:
            break
        if w.state == ExecutionState.STALLED:
            w.begin_recovery(at_seconds=t)
        elif w.state == ExecutionState.RECOVERING:
            w.record_recovery_result(at_seconds=t, result=RecoveryResult.FAILURE)
        t += 1.0
    assert w.state == ExecutionState.BLOCKED
    assert w.blocked_report is not None


def test_begin_recovery_requires_stalled_state() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    with pytest.raises(InvalidStateTransitionError):
        w.begin_recovery(at_seconds=1.0)


def test_record_recovery_result_requires_recovering_state() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    with pytest.raises(InvalidStateTransitionError):
        w.record_recovery_result(at_seconds=1.0, result=RecoveryResult.SUCCESS)


# --- escalation / cumulative budget ----------------------------------------


def test_escalation_updates_tier_and_reasoning() -> None:
    w = ExecutionWatchdog(
        "e1",
        _policy(max_model_escalations=3),
        initial_tier=ModelTier.FAST,
        initial_reasoning=ReasoningEffort.LOW,
    )
    w.start(task="x", at_seconds=0.0)
    decision = w.record_escalation(at_seconds=1.0, signals=EscalationSignals(repeated_failures=5))
    assert decision.should_escalate is True
    assert w.tier == decision.next_tier
    assert w.reasoning == decision.next_reasoning
    assert w.escalations_used == 1


def test_ag04_coordinator_denies_second_escalation_once_cumulative_budget_exceeded() -> None:
    p = Policy(
        budget=BudgetPolicy(max_estimated_cost=0.02, max_estimated_tokens=10_000_000),
        watchdog=WatchdogPolicy(max_model_escalations=5),
    )
    w = ExecutionWatchdog("e1", p, context_budget_tokens=1000)
    w.start(task="x", at_seconds=0.0, initial_tokens=1000, initial_cost=0.001)

    decision_1 = w.record_escalation(at_seconds=1.0, signals=EscalationSignals(repeated_failures=5))
    assert decision_1.should_escalate is True

    decision_2 = w.record_escalation(at_seconds=2.0, signals=EscalationSignals(repeated_failures=5))
    assert decision_2.should_escalate is False
    assert decision_2.reason == "cost_budget_exceeded"
    # Denial must not have advanced tier/reasoning/escalations_used.
    assert w.tier == decision_1.next_tier
    assert w.escalations_used == 1


# --- validation (AG-06 at the coordinator boundary) -------------------------


def test_backwards_timestamp_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=10.0)
    with pytest.raises(InvalidObservationError):
        w.record_activity(at_seconds=5.0, fingerprint="a", succeeded=True)


def test_nan_timestamp_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    with pytest.raises(InvalidObservationError):
        w.start(task="x", at_seconds=float("nan"))


def test_negative_timestamp_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    with pytest.raises(InvalidObservationError):
        w.start(task="x", at_seconds=-1.0)


def test_blank_task_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    with pytest.raises(InvalidObservationError):
        w.start(task="   ", at_seconds=0.0)


def test_blank_fingerprint_is_rejected() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    with pytest.raises(InvalidObservationError):
        w.record_activity(at_seconds=1.0, fingerprint="", succeeded=True)


# --- checkpoints / status ---------------------------------------------------


def test_checkpoint_appears_in_status_and_blocked_report() -> None:
    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=1))
    w.start(task="x", at_seconds=0.0)
    w.checkpoint(at_seconds=1.0, phase="implementation", completed=("parser",), pending=("tests",))
    assert w.status()["latest_checkpoint"].phase == "implementation"

    for i in range(10):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"unique-{i}", succeeded=True)
    t = 100.0
    for _ in range(10):
        if w.state == ExecutionState.BLOCKED:
            break
        if w.state == ExecutionState.STALLED:
            w.begin_recovery(at_seconds=t)
        elif w.state == ExecutionState.RECOVERING:
            w.record_recovery_result(at_seconds=t, result=RecoveryResult.FAILURE)
        t += 1.0
    assert w.blocked_report.last_successful_checkpoint.phase == "implementation"


def test_status_reports_consistent_snapshot() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="ship it", at_seconds=0.0)
    status = w.status()
    assert status["execution_id"] == "e1"
    assert status["state"] == "running"
    assert status["current_task"] == "ship it"
    assert "budget" in status


# --- persistence (heartbeat/checkpoint) via a real state_dir ---------------


def test_heartbeat_and_checkpoint_are_persisted_when_state_dir_given(tmp_path) -> None:
    from agentgear.checkpoints import CheckpointStore
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _policy(), state_dir=str(tmp_path))
    w.start(task="x", at_seconds=0.0)
    w.checkpoint(at_seconds=1.0, phase="build", completed=(), pending=("tests",))

    heartbeat = HeartbeatWriter(tmp_path).read("e1")
    assert heartbeat is not None
    assert heartbeat.state == ExecutionState.RUNNING

    checkpoints = CheckpointStore(tmp_path).all("e1")
    assert len(checkpoints) == 1
    assert checkpoints[0].phase == "build"
