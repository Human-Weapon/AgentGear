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


def test_loop_guard_flags_identical_failure_streak() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_identical_failures=2))
    guard.record_identical_failure(is_repeat=True)
    guard.record_identical_failure(is_repeat=True)
    exceeded, reasons = guard.exceeded()
    assert exceeded is True
    assert any("identical_failure_streak" in r for r in reasons)


def test_loop_guard_progress_resets_no_progress_cycles() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(no_progress_cycles=1, max_no_progress_cycles=2))
    guard.record_no_progress_cycle()
    guard.record_no_progress_cycle()
    guard.record_progress()
    exceeded, _ = guard.exceeded()
    assert exceeded is False


def test_loop_guard_total_attempts_bounded() -> None:
    guard = LoopGuard(policy=WatchdogPolicy(max_total_attempts=3))
    for _ in range(3):
        guard.record_attempt()
    exceeded, reasons = guard.exceeded()
    assert exceeded is True
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
    guard.record_attempt()
    exceeded_second, _ = guard.exceeded()
    assert exceeded_first is True
    assert exceeded_second is True
    assert guard.policy.max_total_attempts == 1
