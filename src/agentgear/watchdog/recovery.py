"""Bounded recovery: what to try after STALLED, and when to give up.

``RecoveryEngine`` never proposes the same strategy twice in a row for the
same execution (principle #17: "no repetir exactamente la misma accion").
It walks a fixed, documented strategy ladder and raises
``RecoveryExhaustedError`` once every strategy has been tried or the
configured attempt bound is reached — the caller is expected to catch
that and transition the state machine to BLOCKED.

``LoopGuard`` implements the broader loop-protection bounds (principle
#18): identical failures, recovery attempts, no-progress cycles, total
attempts, and model escalations are all tracked and bounded independently,
and none of these limits auto-increases itself to hide a stuck execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import WatchdogPolicy
from ..exceptions import RecoveryExhaustedError

STRATEGY_LADDER: tuple[str, ...] = (
    "re_read_error",
    "inspect_assumptions",
    "split_task",
    "change_approach",
    "restore_checkpoint",
    "restart_tool",
    "use_another_agent",
    "increase_reasoning",
    "change_model_tier",
    "request_human_intervention",
)


class RecoveryEngine:
    def __init__(self, policy: WatchdogPolicy) -> None:
        self.policy = policy

    def next_strategy(self, *, tried_strategies: tuple[str, ...], attempt_number: int) -> str:
        """Return the next recovery strategy to try.

        ``attempt_number`` is 1-indexed: the attempt about to be made.
        Raises ``RecoveryExhaustedError`` if the attempt bound is exceeded
        or every strategy (including the last-resort
        ``request_human_intervention``) has already been tried.
        """
        if attempt_number > self.policy.max_recovery_attempts:
            raise RecoveryExhaustedError(
                f"recovery attempt {attempt_number} exceeds "
                f"max_recovery_attempts={self.policy.max_recovery_attempts}"
            )

        if tried_strategies and tried_strategies[-1] == STRATEGY_LADDER[-1]:
            raise RecoveryExhaustedError(
                "last-resort strategy 'request_human_intervention' was already tried; "
                "no further automated recovery is possible"
            )

        for strategy in STRATEGY_LADDER:
            if strategy not in tried_strategies:
                return strategy

        raise RecoveryExhaustedError(
            f"all {len(STRATEGY_LADDER)} recovery strategies have been tried without success"
        )


@dataclass
class LoopGuard:
    """Aggregates the loop-protection counters from ``WatchdogPolicy`` and
    reports which bound(s), if any, have been exceeded.
    """

    policy: WatchdogPolicy
    identical_failure_streak: int = 0
    recovery_attempts: int = 0
    no_progress_cycles: int = 0
    total_attempts: int = 0
    model_escalations: int = 0
    _violations: list[str] = field(default_factory=list, repr=False)

    def record_attempt(self) -> None:
        self.total_attempts += 1

    def record_identical_failure(self, *, is_repeat: bool) -> None:
        self.identical_failure_streak = self.identical_failure_streak + 1 if is_repeat else 0

    def record_recovery_attempt(self) -> None:
        self.recovery_attempts += 1

    def record_no_progress_cycle(self) -> None:
        self.no_progress_cycles += 1

    def record_progress(self) -> None:
        self.no_progress_cycles = 0

    def record_escalation(self) -> None:
        self.model_escalations += 1

    def exceeded(self) -> tuple[bool, tuple[str, ...]]:
        p = self.policy
        reasons: list[str] = []
        if self.identical_failure_streak >= p.max_identical_failures:
            reasons.append(
                f"identical_failure_streak={self.identical_failure_streak} >= "
                f"max_identical_failures={p.max_identical_failures}"
            )
        if self.recovery_attempts >= p.max_recovery_attempts:
            reasons.append(
                f"recovery_attempts={self.recovery_attempts} >= "
                f"max_recovery_attempts={p.max_recovery_attempts}"
            )
        if self.no_progress_cycles >= p.max_no_progress_cycles:
            reasons.append(
                f"no_progress_cycles={self.no_progress_cycles} >= "
                f"max_no_progress_cycles={p.max_no_progress_cycles}"
            )
        if self.total_attempts >= p.max_total_attempts:
            reasons.append(
                f"total_attempts={self.total_attempts} >= max_total_attempts={p.max_total_attempts}"
            )
        # max_model_escalations may legitimately be 0 (escalation disabled).
        # Only flag a violation once at least one escalation has actually
        # happened; a fresh guard must never report "exceeded" for work it
        # hasn't done yet.
        if self.model_escalations > 0 and self.model_escalations >= p.max_model_escalations:
            reasons.append(
                f"model_escalations={self.model_escalations} >= "
                f"max_model_escalations={p.max_model_escalations}"
            )
        return (bool(reasons), tuple(reasons))
