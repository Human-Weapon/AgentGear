"""End-to-end adversarial watchdog scenarios (AgentGear spec principle #43).

Each scenario wires together the real StallDetector, ExecutionStateMachine,
RecoveryEngine, and LoopGuard exactly as a runtime orchestrator would, to
prove the pieces compose correctly rather than just passing in isolation.
"""

from __future__ import annotations

import pytest

from agentgear.config import WatchdogPolicy
from agentgear.exceptions import (
    InvalidStateTransitionError,
    NotCompletedError,
    RecoveryExhaustedError,
)
from agentgear.models import ExecutionState
from agentgear.watchdog.blocked import build_blocked_report
from agentgear.watchdog.recovery import LoopGuard, RecoveryEngine
from agentgear.watchdog.stall_detection import ActivityRecord, StallDetector
from agentgear.watchdog.state_machine import ExecutionStateMachine


def _policy() -> WatchdogPolicy:
    return WatchdogPolicy(
        no_progress_seconds=30.0,
        no_progress_cycles=2,
        max_identical_failures=2,
        max_recovery_attempts=3,
        trivial_command_timeout_seconds=5.0,
    )


def test_scenario_a_activity_without_progress_reaches_stalled() -> None:
    policy = _policy()
    detector = StallDetector(policy)
    sm = ExecutionStateMachine(execution_id="scenario-a", state=ExecutionState.RUNNING)

    activities = [
        ActivityRecord(at_seconds=40.0 + i, fingerprint=f"activity-{i}", succeeded=True)
        for i in range(3)
    ]
    verdict = detector.evaluate(now=100.0, last_progress_at=5.0, recent_activities=activities)

    assert verdict.is_stalled is True
    sm.transition(ExecutionState.STALLED, at_seconds=100.0, note="; ".join(verdict.reasons))
    assert sm.state == ExecutionState.STALLED


def test_scenario_b_repeated_identical_failure_stalls_then_recovers() -> None:
    policy = _policy()
    detector = StallDetector(policy)
    sm = ExecutionStateMachine(execution_id="scenario-b", state=ExecutionState.RUNNING)

    activities = [
        ActivityRecord(
            at_seconds=1.0, fingerprint="pytest -k foo", succeeded=False, error="AssertionError"
        ),
        ActivityRecord(
            at_seconds=2.0, fingerprint="pytest -k foo", succeeded=False, error="AssertionError"
        ),
    ]
    verdict = detector.evaluate(now=3.0, last_progress_at=None, recent_activities=activities)
    assert verdict.is_stalled is True

    sm.transition(ExecutionState.STALLED, at_seconds=3.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=3.5)
    assert sm.state == ExecutionState.RECOVERING


def test_scenario_c_recovery_succeeds_returns_to_running() -> None:
    sm = ExecutionStateMachine(execution_id="scenario-c", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=1.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=2.0)

    engine = RecoveryEngine(_policy())
    strategy = engine.next_strategy(tried_strategies=(), attempt_number=1)
    assert strategy == "re_read_error"
    # Recovery strategy resolved the issue:
    sm.transition(ExecutionState.RUNNING, at_seconds=3.0, note=f"recovered via {strategy}")
    assert sm.state == ExecutionState.RUNNING


def test_scenario_d_recovery_exhausted_reaches_blocked() -> None:
    policy = WatchdogPolicy(max_recovery_attempts=2)
    sm = ExecutionStateMachine(execution_id="scenario-d", state=ExecutionState.RUNNING)
    sm.transition(ExecutionState.STALLED, at_seconds=1.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=2.0)

    engine = RecoveryEngine(policy)
    guard = LoopGuard(policy=policy)
    tried: list[str] = []
    last_exc: RecoveryExhaustedError | None = None
    for attempt in range(1, 5):
        guard.record_recovery_attempt()
        try:
            strategy = engine.next_strategy(tried_strategies=tuple(tried), attempt_number=attempt)
        except RecoveryExhaustedError as exc:
            last_exc = exc
            break
        tried.append(strategy)  # every attempt "fails" in this adversarial scenario

    assert last_exc is not None
    exceeded, reasons = guard.exceeded()
    assert exceeded is True

    sm.transition(ExecutionState.BLOCKED, at_seconds=10.0, note=str(last_exc))
    assert sm.state == ExecutionState.BLOCKED

    report = build_blocked_report(
        blocker="recovery exhausted after repeated identical test failures",
        root_cause="unresolved AssertionError in pytest -k foo",
        last_successful_checkpoint=None,
        attempts=len(tried),
        strategies_tried=tuple(tried),
        evidence=("pytest -k foo failed identically on every attempt",),
    )
    assert report.attempts == len(tried)
    assert report.recommended_human_action


def test_scenario_e_silence_without_acceptance_criteria_is_never_completed() -> None:
    sm = ExecutionStateMachine(execution_id="scenario-e", state=ExecutionState.REVIEWING)
    # The agent has gone quiet: no more tool calls, no more events. That is
    # NOT evidence of completion, so asserting COMPLETED without evidence
    # must fail, and the state must remain REVIEWING (never silently done).
    with pytest.raises(NotCompletedError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=999.0, evidence=())
    assert sm.state == ExecutionState.REVIEWING
    assert sm.state != ExecutionState.COMPLETED


def test_scenario_f_trivial_command_abnormally_slow_repeatedly_signals_stall() -> None:
    policy = _policy()
    detector = StallDetector(policy)
    activities = [
        ActivityRecord(
            at_seconds=float(i * 400),
            fingerprint="python -c print('test')",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        )
        for i in range(3)
    ]
    verdict = detector.evaluate(now=1300.0, last_progress_at=None, recent_activities=activities)
    assert verdict.is_stalled is True
    assert any("trivial" in r for r in verdict.reasons)


def test_scenario_g_circular_reasoning_without_new_evidence_is_no_progress() -> None:
    policy = _policy()
    detector = StallDetector(policy)
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="re-analyze the same file", succeeded=True)
        for i in range(5)
    ]
    verdict = detector.evaluate(now=10.0, last_progress_at=None, recent_activities=activities)
    assert verdict.is_stalled is True
    assert any("circular" in r for r in verdict.reasons)


def test_invalid_transition_is_rejected_not_silently_coerced() -> None:
    sm = ExecutionStateMachine(execution_id="scenario-invalid", state=ExecutionState.PLANNING)
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(ExecutionState.COMPLETED, at_seconds=1.0, evidence=("done",))
    assert sm.state == ExecutionState.PLANNING
