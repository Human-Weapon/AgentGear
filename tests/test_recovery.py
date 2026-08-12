from __future__ import annotations

import pytest

from agentgear.config import WatchdogPolicy
from agentgear.exceptions import RecoveryExhaustedError
from agentgear.watchdog.recovery import STRATEGY_LADDER, LoopGuard, RecoveryEngine


def test_first_strategy_is_re_read_error() -> None:
    engine = RecoveryEngine(WatchdogPolicy(max_recovery_attempts=10))
    strategy = engine.next_strategy(tried_strategies=(), attempt_number=1)
    assert strategy == "re_read_error"


def test_never_repeats_a_tried_strategy() -> None:
    engine = RecoveryEngine(WatchdogPolicy(max_recovery_attempts=10))
    tried: list[str] = []
    for attempt in range(1, 6):
        strategy = engine.next_strategy(tried_strategies=tuple(tried), attempt_number=attempt)
        assert strategy not in tried
        tried.append(strategy)
    assert len(tried) == len(set(tried))


def test_exceeding_max_recovery_attempts_raises() -> None:
    engine = RecoveryEngine(WatchdogPolicy(max_recovery_attempts=2))
    engine.next_strategy(tried_strategies=(), attempt_number=1)
    engine.next_strategy(tried_strategies=("re_read_error",), attempt_number=2)
    with pytest.raises(RecoveryExhaustedError):
        engine.next_strategy(
            tried_strategies=("re_read_error", "inspect_assumptions"), attempt_number=3
        )


def test_all_strategies_exhausted_raises() -> None:
    engine = RecoveryEngine(WatchdogPolicy(max_recovery_attempts=100))
    with pytest.raises(RecoveryExhaustedError):
        engine.next_strategy(
            tried_strategies=STRATEGY_LADDER, attempt_number=len(STRATEGY_LADDER) + 1
        )


def test_human_intervention_already_tried_raises() -> None:
    engine = RecoveryEngine(WatchdogPolicy(max_recovery_attempts=100))
    with pytest.raises(RecoveryExhaustedError):
        engine.next_strategy(tried_strategies=("request_human_intervention",), attempt_number=2)


def test_loop_guard_starts_clean() -> None:
    guard = LoopGuard(policy=WatchdogPolicy())
    exceeded, reasons = guard.exceeded()
    assert exceeded is False
    assert reasons == ()


def test_loop_guard_identical_failure_streak_boundary_table() -> None:
    """max_identical_failures=2 means 2 are ALLOWED (tolerated); the 3rd
    identical failure is what triggers 'exceeded' (Round 2 / C2: N
    allowed, N+1 rejected -- applied uniformly to every LoopGuard bound)."""
    guard = LoopGuard(policy=WatchdogPolicy(max_identical_failures=2))
    guard.record_identical_failure(is_repeat=True)
    assert guard.exceeded()[0] is False, "1st identical failure: still allowed"
    guard.record_identical_failure(is_repeat=True)
    assert guard.exceeded()[0] is False, "2nd identical failure: still allowed (N=2 permitted)"
    guard.record_identical_failure(is_repeat=True)
    exceeded, reasons = guard.exceeded()
    assert exceeded is True, "3rd identical failure: now exceeded (N+1 rejected)"
    assert any("identical_failure_streak" in r for r in reasons)


def test_loop_guard_progress_resets_no_progress_cycles() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(no_progress_cycles=1, max_no_progress_cycles=2))
    guard.record_no_progress_cycle()
    guard.record_no_progress_cycle()
    guard.record_progress()
    exceeded, _ = guard.exceeded()
    assert exceeded is False


def test_loop_guard_total_attempts_boundary_table() -> None:
    """max_total_attempts=3: attempts 1,2,3 are all valid/allowed; only
    the 4th attempt makes the guard report exceeded."""
    guard = LoopGuard(policy=WatchdogPolicy(max_total_attempts=3))
    for expected_attempt in range(1, 4):
        guard.record_attempt()
        exceeded, _ = guard.exceeded()
        assert exceeded is False, f"attempt {expected_attempt}/3 must still be allowed"
    guard.record_attempt()  # 4th
    exceeded, reasons = guard.exceeded()
    assert exceeded is True, "4th attempt must exceed max_total_attempts=3"
    assert any("total_attempts" in r for r in reasons)


def test_loop_guard_zero_escalations_used_is_not_flagged() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_model_escalations=0))
    exceeded, reasons = guard.exceeded()
    assert exceeded is False, reasons


def test_loop_guard_flags_escalations_when_limit_is_zero_and_one_happens() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_model_escalations=0))
    guard.record_escalation()
    exceeded, reasons = guard.exceeded()
    assert exceeded is True
    assert any("model_escalations" in r for r in reasons)


def test_loop_guard_bounds_never_auto_increase() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_total_attempts=1))
    guard.record_attempt()
    exceeded_first, _ = guard.exceeded()
    assert exceeded_first is False, "the 1st (and only allowed) attempt must not be flagged"
    guard.record_attempt()
    exceeded_second, _ = guard.exceeded()
    assert exceeded_second is True, "the 2nd attempt must exceed max_total_attempts=1"
    assert guard.policy.max_total_attempts == 1


# --- Round 2 / C2: can_start_recovery_attempt() boundary tables -----------


@pytest.mark.parametrize("max_attempts", [1, 2, 3])
def test_can_start_recovery_attempt_boundary_table(max_attempts: int) -> None:
    """max_recovery_attempts=N: attempts 1..N are allowed; attempt N+1 is
    rejected. This is the canonical C2 regression, parametrized exactly
    over the boundary table the audit specified."""
    guard = LoopGuard(policy=WatchdogPolicy(max_recovery_attempts=max_attempts))
    for attempt_index in range(1, max_attempts + 1):
        assert guard.can_start_recovery_attempt() is True, (
            f"attempt {attempt_index}/{max_attempts} must be allowed"
        )
        assert guard.recovery_attempts_remaining() == max_attempts - attempt_index + 1
        guard.record_recovery_attempt_started()
    assert guard.can_start_recovery_attempt() is False, (
        f"attempt {max_attempts + 1} must be rejected"
    )
    assert guard.recovery_attempts_remaining() == 0
    assert guard.is_recovery_exhausted() is True


def test_start_new_recovery_episode_resets_only_recovery_attempts() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_recovery_attempts=1, max_total_attempts=1000))
    guard.record_recovery_attempt_started()
    guard.record_attempt()
    guard.record_attempt()
    guard.record_escalation()
    assert guard.can_start_recovery_attempt() is False

    guard.start_new_recovery_episode()

    assert guard.can_start_recovery_attempt() is True, "episode counter must reset"
    assert guard.total_attempts == 2, "global total_attempts must NOT reset"
    assert guard.model_escalations == 1, "global model_escalations must NOT reset"


def test_recovery_attempt_admission_never_increments_on_denial() -> None:
    """Checking can_start_recovery_attempt() must be side-effect-free --
    only record_recovery_attempt_started() may advance the counter."""
    guard = LoopGuard(policy=WatchdogPolicy(max_recovery_attempts=1))
    guard.record_recovery_attempt_started()
    for _ in range(5):
        assert guard.can_start_recovery_attempt() is False
    assert guard.recovery_attempts == 1
