"""Bounded recovery: what to try after STALLED, and when to give up.

``RecoveryEngine`` never proposes the same strategy twice in a row for the
same execution (principle #17: "no repetir exactamente la misma accion").
It walks a fixed, documented strategy ladder and raises
``RecoveryExhaustedError`` once every strategy has been tried or the
configured attempt bound is reached — the caller is expected to catch
that and transition the state machine to BLOCKED.

``LoopGuard`` implements the broader loop-protection bounds (principle
#18): identical failures, recovery attempts, no-progress cycles, total
attempts, and model escalations are all tracked, and none of these limits
auto-increases itself to hide a stuck execution.

Two different scopes of state (Round 2 / C1):

* GLOBAL, execution-wide, NEVER reset by a successful recovery:
  ``total_attempts``, ``model_escalations``. (Cumulative budget lives in
  ``ExecutionBudgetLedger``, not here, and is equally never reset.)
* RECOVERY-EPISODE-scoped, reset every time a NEW stall begins a fresh
  episode: ``recovery_attempts``. A successful recovery closes the
  episode; the next stall starts a new one with a clean attempt count —
  see ``start_new_recovery_episode()``. This is what stops "STALL ->
  recover -> STALL -> recover -> ..." from being treated as one
  ever-growing recovery attempt count that eventually blocks a healthy,
  repeatedly-self-healing execution for no real reason, while still
  letting the *global* bounds (total attempts, cumulative budget) catch a
  caller that abuses successful recovery to bypass real limits (Round 2 /
  principle #7: episode counters may reset; global ones must not).

Off-by-one contract (Round 2 / C2): every bound here means "N are
allowed, N+1 is rejected" — ``max_recovery_attempts=1`` permits exactly
one recovery attempt per episode, not zero. ``exceeded()`` reports a
bound as violated only once its counter has gone *strictly past* the
configured maximum; recovery-attempt admission is a separate, dedicated
check (``can_start_recovery_attempt()``) rather than folded into
``exceeded()``, so nothing else can reinterpret the same limit with
different semantics.
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
    reports which GLOBAL bound(s), if any, have been exceeded.
    Recovery-attempt admission (episode-scoped) is handled by the
    dedicated ``can_start_recovery_attempt`` family below, not by
    ``exceeded()`` — see the module docstring for why the two are kept
    separate.
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

    def record_no_progress_cycle(self) -> None:
        self.no_progress_cycles += 1

    def record_progress(self) -> None:
        self.no_progress_cycles = 0

    def record_escalation(self) -> None:
        self.model_escalations += 1

    # -- recovery-attempt admission: episode-scoped, "N allowed" (C2) ----

    def can_start_recovery_attempt(self) -> bool:
        """True if one more recovery attempt may begin in the CURRENT
        episode. ``max_recovery_attempts=N`` permits exactly N attempts
        per episode; the caller must check this BEFORE doing any work for
        the attempt, then call ``record_recovery_attempt_started()`` only
        once it actually commits to making the attempt — never the
        increment-then-check ordering that caused the original off-by-one
        (incrementing first made N=1 mean zero real attempts).
        """
        return self.recovery_attempts < self.policy.max_recovery_attempts

    def recovery_attempts_remaining(self) -> int:
        return max(0, self.policy.max_recovery_attempts - self.recovery_attempts)

    def is_recovery_exhausted(self) -> bool:
        return not self.can_start_recovery_attempt()

    def record_recovery_attempt_started(self) -> None:
        self.recovery_attempts += 1

    def start_new_recovery_episode(self) -> None:
        """Close out the previous recovery episode (if any) and reset
        EPISODE-scoped state for a fresh one. Deliberately does NOT touch
        ``total_attempts``, ``model_escalations``, or anything in the
        execution's budget ledger — those are global and must keep
        accumulating no matter how many times recovery succeeds
        (Round 2 / principle #7: no global-limit bypass via repeated
        successful episodes).
        """
        self.recovery_attempts = 0

    # -- global bounds -----------------------------------------------------

    def exceeded(self) -> tuple[bool, tuple[str, ...]]:
        """GLOBAL loop-protection bounds only (never episode-scoped
        recovery attempts — see ``can_start_recovery_attempt``). Each
        bound is violated only once its counter has gone strictly PAST
        the configured maximum: ``max_total_attempts=N`` permits exactly
        N attempts, the same "N allowed" contract as recovery attempts.
        """
        p = self.policy
        reasons: list[str] = []
        if self.identical_failure_streak > p.max_identical_failures:
            reasons.append(
                f"identical_failure_streak={self.identical_failure_streak} > "
                f"max_identical_failures={p.max_identical_failures}"
            )
        if self.no_progress_cycles > p.max_no_progress_cycles:
            reasons.append(
                f"no_progress_cycles={self.no_progress_cycles} > "
                f"max_no_progress_cycles={p.max_no_progress_cycles}"
            )
        if self.total_attempts > p.max_total_attempts:
            reasons.append(
                f"total_attempts={self.total_attempts} > max_total_attempts={p.max_total_attempts}"
            )
        # max_model_escalations may legitimately be 0 (escalation disabled).
        # Only flag a violation once at least one escalation has actually
        # happened; a fresh guard must never report "exceeded" for work it
        # hasn't done yet.
        if self.model_escalations > 0 and self.model_escalations > p.max_model_escalations:
            reasons.append(
                f"model_escalations={self.model_escalations} > "
                f"max_model_escalations={p.max_model_escalations}"
            )
        return (bool(reasons), tuple(reasons))
