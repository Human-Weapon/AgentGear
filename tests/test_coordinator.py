"""End-to-end tests for ExecutionWatchdog using ONLY the public coordinator
API — no direct use of ExecutionStateMachine, StallDetector, RecoveryEngine,
or LoopGuard. This is what AG-05 requires: proof that the composition
itself (not just its parts in isolation) enforces the invariants.
"""

from __future__ import annotations

import pytest

from agentgear.analysis import assess_complexity, assess_risk
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
    TaskProfile,
)
from agentgear.planning import build_execution_plan
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


def test_completion_evidence_is_retrievable_from_public_transition_history() -> None:
    """Round 2 / H3: evidence must survive, retrievable through the
    coordinator's own public surface -- not just validated and discarded."""
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="ship a fix", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    w.complete(at_seconds=2.0, evidence=("all tests pass", "reviewed by CI"))

    completed_record = w.transition_history[-1]
    assert completed_record.to_state == ExecutionState.COMPLETED
    assert completed_record.evidence == ("all tests pass", "reviewed by CI")


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


def test_m8_activity_exactly_at_progress_timestamp_is_not_resurrected() -> None:
    """Round 2 / M8/L8: coordinator-level regression for the exact-boundary
    stall detection bug. An activity recorded at exactly the same
    at_seconds as the progress event must not be treated as "since
    progress" -- otherwise a stale failure right at the progress instant
    could immediately count toward a new stall verdict, weakening AG-02.
    """
    w = ExecutionWatchdog("e1", _policy(max_identical_failures=2, no_progress_seconds=1000.0))
    w.start(task="x", at_seconds=0.0)
    w.record_activity(at_seconds=3.0, fingerprint="run-tests", succeeded=False)
    w.record_progress(
        at_seconds=3.0, kind=ProgressSignalKind.SUBTASK_COMPLETED, description="parsed input"
    )
    # Only ONE genuinely new failure after the t=3.0 boundary: must not stall.
    w.record_activity(at_seconds=4.0, fingerprint="run-tests", succeeded=False)
    assert w.state == ExecutionState.RUNNING


def test_successful_recovery_establishes_a_fresh_progress_boundary() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    w.record_activity(at_seconds=6.0, fingerprint="first", succeeded=True)
    w.record_activity(at_seconds=7.0, fingerprint="second", succeeded=True)
    assert w.state == ExecutionState.RECOVERING

    w.record_recovery_result(at_seconds=8.0, result=RecoveryResult.SUCCESS, evidence="fixed")
    w.record_activity(at_seconds=9.0, fingerprint="after-recovery", succeeded=True)

    assert w.state == ExecutionState.RUNNING
    assert w.status()["last_progress_at"] == 8.0


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


def test_advance_cannot_bypass_the_blocked_report_invariant() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    w.record_activity(at_seconds=6.0, fingerprint="first", succeeded=True)
    w.record_activity(at_seconds=7.0, fingerprint="second", succeeded=True)
    assert w.state == ExecutionState.RECOVERING

    with pytest.raises(InvalidStateTransitionError):
        w.advance(ExecutionState.BLOCKED, at_seconds=8.0)
    assert w.state == ExecutionState.RECOVERING
    assert w.blocked_report is None


def test_successful_recovery_cannot_bypass_the_blocked_report_invariant() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="x", at_seconds=0.0)
    w.record_activity(at_seconds=6.0, fingerprint="first", succeeded=True)
    w.record_activity(at_seconds=7.0, fingerprint="second", succeeded=True)
    assert w.state == ExecutionState.RECOVERING

    with pytest.raises(InvalidStateTransitionError):
        w.record_recovery_result(
            at_seconds=8.0,
            result=RecoveryResult.SUCCESS,
            resume_state=ExecutionState.BLOCKED,
            evidence="claimed recovery",
        )
    assert w.state == ExecutionState.RECOVERING
    assert w.blocked_report is None


def test_failed_second_start_does_not_charge_budget_or_rewrite_start_boundary() -> None:
    w = ExecutionWatchdog("e1", _policy())
    w.start(task="first", at_seconds=0.0, initial_tokens=100, initial_cost=0.1)

    with pytest.raises(InvalidStateTransitionError):
        w.start(task="second", at_seconds=1.0, initial_tokens=100, initial_cost=0.1)

    assert w.status()["started_at"] == 0.0
    assert w.budget.committed_tokens == 100
    assert w.budget.committed_cost == pytest.approx(0.1)


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


def test_from_plan_commits_the_full_initial_plan_charge_to_the_shared_ledger() -> None:
    p = _policy()
    profile = TaskProfile(description="rename", expected_output_tokens=10)
    execution_plan = build_execution_plan(
        profile, assess_complexity(profile), assess_risk(profile), p
    )
    w = ExecutionWatchdog.from_plan("e1", execution_plan, p)

    w.start(task=profile.description, at_seconds=0.0)

    assert w.budget.committed_tokens == (
        execution_plan.context_budget_tokens * execution_plan.strategy.agent_count
    )
    assert w.budget.committed_cost == pytest.approx(execution_plan.max_estimated_cost)


def test_recovery_attempts_share_and_enforce_the_execution_budget() -> None:
    p = Policy(
        budget=BudgetPolicy(max_estimated_cost=0.0015, max_estimated_tokens=10_000),
        watchdog=WatchdogPolicy(
            no_progress_seconds=1.0,
            no_progress_cycles=2,
            max_recovery_attempts=5,
            max_total_attempts=100,
        ),
    )
    w = ExecutionWatchdog("e1", p, context_budget_tokens=1000)
    w.start(task="x", at_seconds=0.0)
    w.record_activity(at_seconds=2.0, fingerprint="a", succeeded=True)
    w.record_activity(at_seconds=3.0, fingerprint="b", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    assert w.budget.committed_cost == pytest.approx(0.001)

    w.record_recovery_result(at_seconds=4.0, result=RecoveryResult.FAILURE)
    assert w.state == ExecutionState.STALLED
    assert w.begin_recovery(at_seconds=5.0) is None
    assert w.state == ExecutionState.BLOCKED
    assert w.budget.committed_cost == pytest.approx(0.001)


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


# --- Round 2 / section 42: recovery episode E2E (public coordinator only) --


def _drive_one_episode_to_success(w: ExecutionWatchdog, *, start_t: float) -> float:
    """Busywork -> STALLED -> RECOVERING -> SUCCESS, using ONLY the public
    coordinator. Returns the time cursor after the episode closes."""
    t = start_t
    for i in range(6):
        t += 1.0
        w.record_activity(at_seconds=t, fingerprint=f"unique-{start_t}-{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    t += 1.0
    w.record_recovery_result(at_seconds=t, result=RecoveryResult.SUCCESS)
    assert w.state == ExecutionState.RUNNING
    return t


def test_five_successful_recovery_episodes_each_start_fresh() -> None:
    """Section 42: STALL -> recover (strategy #1) -> SUCCESS -> real
    progress -> busywork -> STALL again, repeated 5 times. No premature
    BLOCKED; each episode gets its own fresh attempt count and its own
    're_read_error' first strategy; cumulative execution history/budget
    keep accumulating across all 5.
    """
    w = ExecutionWatchdog(
        "e1", _policy(no_progress_cycles=2, max_recovery_attempts=10, max_total_attempts=10_000)
    )
    w.start(task="self-healing task", at_seconds=0.0)

    t = 0.0
    for episode_index in range(1, 6):
        t = _drive_one_episode_to_success(w, start_t=t + 100.0)
        status = w.status()
        assert status["recovery_episode_number"] == episode_index
        assert status["recovery_episodes_completed"] == episode_index
        # Fresh episode -> exactly one attempt, using the FIRST strategy
        # ('re_read_error') again, never an accumulated attempt count.
        episode = w.recovery_history[-1]
        assert len(episode.attempts) == 1
        assert episode.attempts[0].strategy == "re_read_error"
        assert episode.outcome.value == "success"

    assert w.state == ExecutionState.RUNNING
    assert len(w.recovery_history) == 5
    # Global attempt count accumulated across every episode's busywork.
    assert w.status()["total_attempts"] == 30


def test_budget_exhausted_on_third_episode_blocks_correctly() -> None:
    """Section 43: budget sized for exactly initial + 2 recovery episodes,
    not a 3rd. Episodes 1/2 must succeed; episode 3's reservation must be
    denied, driving a correct BLOCKED with a validated report -- and no
    committed cost from episodes 1/2 may be released to make room."""
    tokens_per_episode = 1000
    # Rough per-episode relative cost at FAST tier (see routing.py) is
    # tokens/1000 * RELATIVE_COST_PER_1K_TOKENS[FAST]; size the ceiling for
    # exactly two recovery reservations plus a token-only initial charge.
    from agentgear.routing import estimate_cost

    per_recovery_cost = estimate_cost(ModelTier.FAST, tokens_per_episode)
    budget_ceiling = per_recovery_cost * 2 + 1e-9

    p = Policy(
        budget=BudgetPolicy(max_estimated_cost=budget_ceiling, max_estimated_tokens=10_000_000),
        watchdog=WatchdogPolicy(
            no_progress_seconds=1.0,
            no_progress_cycles=2,
            max_recovery_attempts=10,
            max_total_attempts=10_000,
        ),
    )
    w = ExecutionWatchdog("e1", p, context_budget_tokens=tokens_per_episode)
    w.start(task="x", at_seconds=0.0)

    t = 0.0
    for _episode in range(2):
        t = _drive_one_episode_to_success(w, start_t=t + 100.0)
    assert w.state == ExecutionState.RUNNING
    assert len(w.recovery_history) == 2
    committed_after_two = w.budget.committed_cost

    # Episode 3: busywork stalls again, but the recovery reservation must
    # now be denied by the ledger -> BLOCKED with a report, not silently
    # granted, and not by releasing anything already committed.
    t += 100.0
    for i in range(6):
        t += 1.0
        w.record_activity(at_seconds=t, fingerprint=f"ep3-{i}", succeeded=True)
    assert w.state == ExecutionState.BLOCKED
    assert w.blocked_report is not None
    assert "budget" in w.blocked_report.blocker.lower() or "budget" in (
        w.blocked_report.root_cause.lower()
    )
    assert w.budget.committed_cost == pytest.approx(committed_after_two), (
        "no previously committed recovery cost may be released to make room for episode 3"
    )
    assert len(w.recovery_history) == 3
    assert w.recovery_history[-1].outcome.value == "blocked"


def test_max_recovery_attempts_one_canonical_regression() -> None:
    """Section 44: the canonical max_recovery_attempts=1 regression.
    Exactly one attempt executes; the report must show attempts=1 and the
    attempted strategy, and must NEVER claim 'no automated recovery was
    attempted' when one genuinely was."""
    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=1))
    w.start(task="x", at_seconds=0.0)
    for i in range(6):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"u-{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING

    w.record_recovery_result(at_seconds=100.0, result=RecoveryResult.FAILURE)
    assert w.state == ExecutionState.STALLED

    result = w.begin_recovery(at_seconds=101.0)
    assert result is None
    assert w.state == ExecutionState.BLOCKED

    report = w.blocked_report
    assert report is not None
    assert report.attempts == 1
    assert report.strategies_tried == ("re_read_error",)
    assert "no automated recovery was attempted" not in report.recommended_human_action.lower()


def test_blocked_state_report_and_heartbeat_agree(tmp_path) -> None:
    """Section 45: whenever state == BLOCKED, the report and the
    persisted heartbeat must agree with it -- never a heartbeat still
    claiming RECOVERING/RUNNING once the coordinator itself is BLOCKED."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _policy(max_recovery_attempts=1), state_dir=str(tmp_path))
    w.start(task="x", at_seconds=0.0)
    for i in range(6):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"u-{i}", succeeded=True)
    w.record_recovery_result(at_seconds=100.0, result=RecoveryResult.FAILURE)
    w.begin_recovery(at_seconds=101.0)

    assert w.state == ExecutionState.BLOCKED
    assert w.blocked_report is not None
    heartbeat = HeartbeatWriter(tmp_path).read("e1")
    assert heartbeat is not None
    assert heartbeat.state == ExecutionState.BLOCKED


def test_completed_state_and_heartbeat_agree_with_no_pending_recovery(tmp_path) -> None:
    """Section 46: on COMPLETED, the transition record carries evidence,
    the heartbeat (if persisted) reflects COMPLETED, and no recovery
    episode is left dangling open."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _policy(), state_dir=str(tmp_path))
    w.start(task="x", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    w.complete(at_seconds=2.0, evidence=("all tests pass",))

    assert w.state == ExecutionState.COMPLETED
    heartbeat = HeartbeatWriter(tmp_path).read("e1")
    assert heartbeat is not None
    assert heartbeat.state == ExecutionState.COMPLETED
    assert w.recovery_history == ()  # no episode was ever opened


def test_start_called_twice_does_not_double_charge_or_restart(tmp_path) -> None:
    """Section 47: start() is a one-shot atomic operation. A second call
    must fail predictably and must not alter the budget or the recorded
    start boundary."""
    w = ExecutionWatchdog("e1", _policy(), context_budget_tokens=1000)
    w.start(task="first", at_seconds=0.0, initial_tokens=100, initial_cost=0.01)
    committed_tokens = w.budget.committed_tokens
    committed_cost = w.budget.committed_cost

    with pytest.raises(InvalidStateTransitionError):
        w.start(task="second", at_seconds=1.0, initial_tokens=999, initial_cost=99.0)

    assert w.budget.committed_tokens == committed_tokens
    assert w.budget.committed_cost == committed_cost
    assert w.status()["current_task"] == "first"
    assert w.status()["started_at"] == 0.0


def test_start_with_invalid_input_does_not_leave_a_dangling_reservation() -> None:
    """A start() call that fails validation before any reservation is
    made must leave the budget completely untouched."""
    w = ExecutionWatchdog("e1", _policy())
    with pytest.raises(InvalidObservationError):
        w.start(task="   ", at_seconds=0.0, initial_tokens=100, initial_cost=0.01)
    assert w.budget.committed_tokens == 0
    assert w.budget.reserved_tokens == 0
    assert w.state == ExecutionState.PLANNING
