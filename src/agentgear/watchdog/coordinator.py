"""``ExecutionWatchdog`` — the public runtime-supervision coordinator (AG-05).

Everything else in ``watchdog/`` (the state machine, progress tracker,
stall detector, recovery engine, loop guard, heartbeat writer, checkpoint
store) is a low-level, independently-testable utility. None of them alone
can honor "NEVER STOP SILENTLY" — that requires composing all of them
correctly, in the right order, every time. ``ExecutionWatchdog`` is that
composition, exposed as AgentGear's second public layer:

    PLANNING:            TaskProfile -> ExecutionPlan            (api.plan)
    RUNTIME SUPERVISION: ExecutionWatchdog -> lifecycle enforcement

A caller drives an ``ExecutionWatchdog`` with a small event-oriented API
(``start``, ``record_activity``, ``record_progress``, ``evaluate``,
``begin_recovery``, ``record_recovery_result``, ``checkpoint``,
``complete``, ``status``) and never has to call
``ExecutionStateMachine.transition`` directly — the coordinator decides
when a transition is legal and makes it, so a stall is always caught
(``record_activity`` re-evaluates automatically) and BLOCKED can only be
reached with a validated ``BlockedReport`` attached (AG-09).

AgentGear still does not execute real LLM/provider calls in v0.1.0 — the
coordinator supervises the *state* of an execution an external runtime
drives; it does not spawn or own any provider process itself.
"""

from __future__ import annotations

import math

from ..budget import ExecutionBudgetLedger, ReservationKind
from ..config import Policy
from ..escalation import EscalationDecision, EscalationSignals, decide_escalation
from ..exceptions import BudgetExceededError, InvalidObservationError, InvalidStateTransitionError
from ..models import (
    BlockedReport,
    Checkpoint,
    ExecutionPlan,
    ExecutionState,
    ModelTier,
    ProgressEvent,
    ProgressSignalKind,
    ReasoningEffort,
    RecoveryAttempt,
    RecoveryResult,
)
from ..routing import estimate_cost
from .blocked import build_blocked_report
from .heartbeat import HeartbeatWriter, build_heartbeat
from .progress import ProgressTracker
from .recovery import LoopGuard, RecoveryEngine
from .stall_detection import ActivityRecord, StallDetector
from .state_machine import ExecutionStateMachine

_ACTIVE_STATES = (ExecutionState.RUNNING, ExecutionState.TESTING, ExecutionState.REVIEWING)
_RECOVERY_RESUME_STATES = (
    ExecutionState.PLANNING,
    ExecutionState.RUNNING,
    ExecutionState.TESTING,
    ExecutionState.REVIEWING,
)


def _validate_finite_non_negative(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidObservationError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value):
        raise InvalidObservationError(f"{name} must be finite, got {value}")
    if value < 0:
        raise InvalidObservationError(f"{name} must be >= 0, got {value}")
    return float(value)


def _require_non_blank(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidObservationError(f"{name} must be a non-empty, non-blank string")
    return value


class ExecutionWatchdog:
    """Supervises one execution end-to-end. One instance per execution_id."""

    def __init__(
        self,
        execution_id: str,
        policy: Policy,
        *,
        initial_tier: ModelTier = ModelTier.FAST,
        initial_reasoning: ReasoningEffort = ReasoningEffort.LOW,
        context_budget_tokens: int = 2000,
        state_dir: str | None = None,
    ) -> None:
        _require_non_blank("execution_id", execution_id)
        if context_budget_tokens <= 0:
            raise InvalidObservationError("context_budget_tokens must be > 0")

        self.execution_id = execution_id
        self.policy = policy
        self.tier = initial_tier
        self.reasoning = initial_reasoning
        self.context_budget_tokens = context_budget_tokens
        self.escalations_used = 0

        self._sm = ExecutionStateMachine(execution_id=execution_id)
        self._progress = ProgressTracker()
        self._stall_detector = StallDetector(policy.watchdog)
        self._recovery_engine = RecoveryEngine(policy.watchdog)
        self._loop_guard = LoopGuard(policy=policy.watchdog)
        self.budget = ExecutionBudgetLedger(
            max_tokens=policy.budget.max_estimated_tokens, max_cost=policy.budget.max_estimated_cost
        )

        self._activities: list[ActivityRecord] = []
        self._checkpoints: list[Checkpoint] = []
        self._recovery_attempts: list[RecoveryAttempt] = []
        self._tried_strategies: tuple[str, ...] = ()
        self._blocked_report: BlockedReport | None = None
        self._started_at: float | None = None
        self._last_observed_at: float | None = None
        self._current_task: str = ""
        self._last_progress_evidence: str | None = None
        self._last_error: str | None = None
        self._planned_initial_tokens: int | None = None
        self._planned_initial_cost: float | None = None

        self._heartbeat_writer = HeartbeatWriter(state_dir) if state_dir else None
        self._checkpoint_store = None
        if state_dir:
            from ..checkpoints import CheckpointStore

            self._checkpoint_store = CheckpointStore(state_dir)

    @classmethod
    def from_plan(
        cls,
        execution_id: str,
        execution_plan: ExecutionPlan,
        policy: Policy,
        *,
        state_dir: str | None = None,
    ) -> ExecutionWatchdog:
        """Create runtime supervision for a concrete plan.

        The plan's full initial multi-agent estimate is committed when
        ``start()`` succeeds, so subsequent escalations and recoveries
        cannot treat the already-planned execution as free budget.
        """
        watchdog = cls(
            execution_id,
            policy,
            initial_tier=execution_plan.primary_model.tier,
            initial_reasoning=execution_plan.primary_model.reasoning,
            context_budget_tokens=execution_plan.context_budget_tokens,
            state_dir=state_dir,
        )
        watchdog._planned_initial_tokens = (
            execution_plan.context_budget_tokens * execution_plan.strategy.agent_count
        )
        watchdog._planned_initial_cost = execution_plan.max_estimated_cost
        return watchdog

    # -- internal helpers ----------------------------------------------

    def _validate_time(self, at_seconds: float) -> float:
        at_seconds = _validate_finite_non_negative("at_seconds", at_seconds)
        if self._last_observed_at is not None and at_seconds < self._last_observed_at:
            raise InvalidObservationError(
                f"at_seconds={at_seconds} is before the last observed time "
                f"{self._last_observed_at}; the watchdog requires non-decreasing timestamps"
            )
        self._last_observed_at = at_seconds
        return at_seconds

    def _write_heartbeat(self, at_seconds: float) -> None:
        if self._heartbeat_writer is None:
            return
        heartbeat = build_heartbeat(
            execution_id=self.execution_id,
            state=self._sm.state,
            current_task=self._current_task,
            current_subtask=None,
            last_real_progress_at=self._progress.last_progress_at
            if self._progress.last_progress_at is not None
            else (self._started_at if self._started_at is not None else at_seconds),
            last_progress_evidence=self._last_progress_evidence,
            attempt_count=len(self._activities),
            current_strategy=self._tried_strategies[-1] if self._tried_strategies else None,
            last_error=self._last_error,
            pending_work=self._checkpoints[-1].pending if self._checkpoints else (),
        )
        self._heartbeat_writer.write(heartbeat)

    def _transition_to_blocked(
        self,
        *,
        at_seconds: float,
        blocker: str,
        root_cause: str,
        evidence: tuple[str, ...] = (),
        files_affected: tuple[str, ...] = (),
    ) -> BlockedReport:
        """The ONLY path to BLOCKED. Always builds and validates a
        BlockedReport (AG-09) before the state machine transition, and
        the coordinator never exposes any other way to reach BLOCKED.
        """
        report = build_blocked_report(
            blocker=blocker,
            root_cause=root_cause,
            last_successful_checkpoint=self._checkpoints[-1] if self._checkpoints else None,
            attempts=len(self._recovery_attempts),
            strategies_tried=self._tried_strategies,
            evidence=evidence
            or tuple(f"attempt {a.attempt_number}: {a.strategy}" for a in self._recovery_attempts),
            files_affected=files_affected,
        )
        self._blocked_report = report
        self._sm.transition(ExecutionState.BLOCKED, at_seconds=at_seconds, note=blocker)
        self._last_error = blocker
        self._write_heartbeat(at_seconds)
        return report

    # -- public lifecycle API -------------------------------------------

    def start(
        self,
        *,
        task: str,
        at_seconds: float,
        initial_tokens: int = 0,
        initial_cost: float = 0.0,
    ) -> None:
        _require_non_blank("task", task)
        if self._sm.state != ExecutionState.PLANNING or self._started_at is not None:
            raise InvalidStateTransitionError(
                "start() requires a new PLANNING execution, current state is "
                f"{self._sm.state.value}"
            )
        if self._planned_initial_tokens is not None and (initial_tokens or initial_cost):
            raise InvalidObservationError(
                "start() cannot accept initial_tokens/initial_cost when the watchdog was "
                "created with from_plan(); the plan's full estimate is charged automatically"
            )
        at_seconds = self._validate_time(at_seconds)
        if self._planned_initial_tokens is not None:
            initial_tokens = self._planned_initial_tokens
            initial_cost = self._planned_initial_cost or 0.0
        if initial_tokens or initial_cost:
            reservation = self.budget.reserve(
                kind=ReservationKind.INITIAL_PLAN,
                tokens=initial_tokens,
                cost=initial_cost,
                label="initial_plan",
            )
            self.budget.commit(reservation.reservation_id)
        self._started_at = at_seconds
        self._current_task = task
        self._sm.transition(ExecutionState.RUNNING, at_seconds=at_seconds, note=f"started: {task}")
        self._write_heartbeat(at_seconds)

    def advance(self, target: ExecutionState, *, at_seconds: float, note: str = "") -> None:
        """Escape hatch for ordinary forward/backward transitions that
        aren't stall/recovery/blocked/complete-specific (e.g. RUNNING ->
        TESTING -> REVIEWING -> RUNNING). Still funnels through the same
        validated state machine as every other coordinator method.
        """
        if target in {
            ExecutionState.STALLED,
            ExecutionState.RECOVERING,
            ExecutionState.BLOCKED,
            ExecutionState.COMPLETED,
        }:
            raise InvalidStateTransitionError(
                f"advance() cannot target {target.value}; use the dedicated lifecycle method "
                "so watchdog/reporting invariants are enforced"
            )
        at_seconds = self._validate_time(at_seconds)
        self._sm.transition(target, at_seconds=at_seconds, note=note)
        self._write_heartbeat(at_seconds)

    def record_activity(
        self,
        *,
        at_seconds: float,
        fingerprint: str,
        succeeded: bool,
        is_trivial: bool = False,
        duration_seconds: float = 0.0,
        error: str | None = None,
    ) -> None:
        _require_non_blank("fingerprint", fingerprint)
        at_seconds = self._validate_time(at_seconds)
        duration_seconds = _validate_finite_non_negative("duration_seconds", duration_seconds)

        is_repeat = (
            bool(self._activities)
            and not succeeded
            and not self._activities[-1].succeeded
            and self._activities[-1].fingerprint == fingerprint
        )
        record = ActivityRecord(
            at_seconds=at_seconds,
            fingerprint=fingerprint,
            succeeded=succeeded,
            is_trivial=is_trivial,
            duration_seconds=duration_seconds,
            error=error,
        )
        self._activities.append(record)
        self._loop_guard.record_attempt()
        self._loop_guard.record_identical_failure(is_repeat=is_repeat)
        if error:
            self._last_error = error

        self.evaluate(at_seconds=at_seconds)

    def record_progress(
        self,
        *,
        at_seconds: float,
        kind: ProgressSignalKind,
        description: str,
        evidence: dict | None = None,
    ) -> None:
        _require_non_blank("description", description)
        at_seconds = self._validate_time(at_seconds)
        event = ProgressEvent(
            kind=kind, description=description, at_seconds=at_seconds, evidence=evidence or {}
        )
        self._progress.record(event)
        self._loop_guard.record_progress()
        self._last_progress_evidence = description
        self._write_heartbeat(at_seconds)

    def evaluate(self, *, at_seconds: float) -> None:
        """Re-check for a stall and, if one is found, automatically drive
        RUNNING/TESTING/REVIEWING -> STALLED -> RECOVERING (or BLOCKED, if
        recovery is already exhausted). Callers never need to call
        ``ExecutionStateMachine.transition`` themselves.
        """
        at_seconds = self._validate_time(at_seconds)
        if self._sm.state not in _ACTIVE_STATES or self._started_at is None:
            return

        verdict = self._stall_detector.evaluate(
            now=at_seconds,
            started_at=self._started_at,
            last_progress_at=self._progress.last_progress_at,
            recent_activities=self._activities,
        )
        if not verdict.is_stalled:
            return

        reasons = "; ".join(verdict.reasons)
        self._last_error = reasons
        self._loop_guard.record_no_progress_cycle()
        self._sm.transition(ExecutionState.STALLED, at_seconds=at_seconds, note=reasons)
        self._write_heartbeat(at_seconds)
        self.begin_recovery(at_seconds=at_seconds, reason="stall_detected")

    def begin_recovery(
        self, *, at_seconds: float, reason: str = "stall_detected"
    ) -> RecoveryAttempt | None:
        """STALLED -> RECOVERING, selecting the next (never-repeated)
        recovery strategy. Returns ``None`` (having already driven the
        execution to BLOCKED with a validated report) if no strategy is
        available or a loop-protection bound has been reached.
        """
        at_seconds = self._validate_time(at_seconds)
        if self._sm.state != ExecutionState.STALLED:
            raise InvalidStateTransitionError(
                f"begin_recovery requires STALLED, current state is {self._sm.state.value}"
            )

        self._loop_guard.record_recovery_attempt()
        exceeded, guard_reasons = self._loop_guard.exceeded()
        attempt_number = len(self._recovery_attempts) + 1

        if exceeded:
            self._sm.transition(ExecutionState.RECOVERING, at_seconds=at_seconds, note=reason)
            self._transition_to_blocked(
                at_seconds=at_seconds,
                blocker="loop protection bound reached during recovery",
                root_cause="; ".join(guard_reasons),
            )
            return None

        try:
            strategy = self._recovery_engine.next_strategy(
                tried_strategies=self._tried_strategies, attempt_number=attempt_number
            )
        except Exception as exc:  # RecoveryExhaustedError
            self._sm.transition(ExecutionState.RECOVERING, at_seconds=at_seconds, note=reason)
            self._transition_to_blocked(
                at_seconds=at_seconds,
                blocker="no recovery strategy available",
                root_cause=str(exc),
            )
            return None

        try:
            reservation = self.budget.reserve(
                kind=ReservationKind.RECOVERY,
                tokens=self.context_budget_tokens,
                cost=estimate_cost(self.tier, self.context_budget_tokens),
                label=strategy,
            )
        except BudgetExceededError as exc:
            self._sm.transition(ExecutionState.RECOVERING, at_seconds=at_seconds, note=reason)
            self._transition_to_blocked(
                at_seconds=at_seconds,
                blocker="recovery budget exhausted",
                root_cause=str(exc),
            )
            return None
        self.budget.commit(reservation.reservation_id)

        self._tried_strategies = (*self._tried_strategies, strategy)
        attempt = RecoveryAttempt(
            reason=reason,
            strategy=strategy,
            attempt_number=attempt_number,
            result=RecoveryResult.PENDING,
            at_seconds=at_seconds,
        )
        self._recovery_attempts.append(attempt)
        self._sm.transition(
            ExecutionState.RECOVERING, at_seconds=at_seconds, note=f"recovering via {strategy}"
        )
        self._write_heartbeat(at_seconds)
        return attempt

    def record_recovery_result(
        self,
        *,
        at_seconds: float,
        result: RecoveryResult,
        resume_state: ExecutionState = ExecutionState.RUNNING,
        evidence: str | None = None,
    ) -> None:
        """Resolve the current recovery attempt. On SUCCESS, transitions
        RECOVERING -> ``resume_state``. On FAILURE, either returns to
        STALLED for another attempt or, if bounds/strategies are
        exhausted, drives straight to a validated BLOCKED.
        """
        at_seconds = self._validate_time(at_seconds)
        if self._sm.state != ExecutionState.RECOVERING:
            raise InvalidStateTransitionError(
                f"record_recovery_result requires RECOVERING, current state is "
                f"{self._sm.state.value}"
            )
        if not self._recovery_attempts:
            raise InvalidObservationError("no recovery attempt is in progress")
        if not isinstance(result, RecoveryResult):
            raise InvalidObservationError(
                f"result must be a RecoveryResult, got {type(result).__name__}"
            )
        if not isinstance(resume_state, ExecutionState):
            raise InvalidObservationError(
                f"resume_state must be an ExecutionState, got {type(resume_state).__name__}"
            )
        if result == RecoveryResult.SUCCESS and resume_state not in _RECOVERY_RESUME_STATES:
            raise InvalidStateTransitionError(
                "successful recovery can resume only to planning, running, testing, or reviewing; "
                f"got {resume_state.value}"
            )

        last = self._recovery_attempts[-1]
        self._recovery_attempts[-1] = RecoveryAttempt(
            reason=last.reason,
            strategy=last.strategy,
            attempt_number=last.attempt_number,
            result=result,
            at_seconds=at_seconds,
        )

        if result == RecoveryResult.SUCCESS:
            progress_description = evidence or f"recovered via {last.strategy}"
            self._sm.transition(
                resume_state,
                at_seconds=at_seconds,
                note=progress_description,
            )
            # A successful recovery is genuine new evidence. Record it as
            # such so every detector signal uses this point as its fresh
            # post-progress boundary rather than immediately re-counting
            # the activity that caused the original stall.
            self._progress.record(
                ProgressEvent(
                    kind=ProgressSignalKind.ERROR_RESOLVED,
                    description=progress_description,
                    at_seconds=at_seconds,
                    evidence={"recovery_strategy": last.strategy},
                )
            )
            self._last_progress_evidence = progress_description
            self._loop_guard.record_progress()
            self._write_heartbeat(at_seconds)
            return

        exceeded, guard_reasons = self._loop_guard.exceeded()
        if exceeded or last.strategy == "request_human_intervention":
            self._transition_to_blocked(
                at_seconds=at_seconds,
                blocker="recovery attempts exhausted",
                root_cause=evidence
                or "; ".join(guard_reasons)
                or f"recovery via {last.strategy} failed",
            )
        else:
            self._sm.transition(
                ExecutionState.STALLED,
                at_seconds=at_seconds,
                note=evidence or f"recovery via {last.strategy} failed",
            )
            self._write_heartbeat(at_seconds)

    def record_escalation(
        self, *, at_seconds: float, signals: EscalationSignals
    ) -> EscalationDecision:
        """Consult the shared budget ledger (AG-04) via ``decide_escalation``
        and, if approved, commit the escalation's cost/tokens and advance
        ``self.tier``/``self.reasoning``.
        """
        at_seconds = self._validate_time(at_seconds)
        decision = decide_escalation(
            self.tier,
            self.reasoning,
            self.escalations_used,
            signals,
            self.policy,
            context_budget_tokens=self.context_budget_tokens,
            ledger=self.budget,
        )
        if decision.should_escalate:
            projected_cost = estimate_cost(decision.next_tier, self.context_budget_tokens)
            reservation = self.budget.reserve(
                kind=ReservationKind.ESCALATION,
                tokens=self.context_budget_tokens,
                cost=projected_cost,
                label=decision.reason,
            )
            self.budget.commit(reservation.reservation_id)
            self.tier = decision.next_tier
            self.reasoning = decision.next_reasoning
            self.escalations_used += 1
            self._loop_guard.record_escalation()
        return decision

    def checkpoint(
        self,
        *,
        at_seconds: float,
        phase: str,
        completed: tuple[str, ...] = (),
        pending: tuple[str, ...] = (),
        last_good_state: str | None = None,
    ) -> Checkpoint:
        _require_non_blank("phase", phase)
        at_seconds = self._validate_time(at_seconds)
        cp = Checkpoint(
            execution_id=self.execution_id,
            phase=phase,
            completed=completed,
            pending=pending,
            last_good_state=last_good_state,
            at_seconds=at_seconds,
        )
        self._checkpoints.append(cp)
        if self._checkpoint_store is not None:
            self._checkpoint_store.append(cp)
        return cp

    def complete(self, *, at_seconds: float, evidence: tuple[str, ...]) -> None:
        at_seconds = self._validate_time(at_seconds)
        self._sm.transition(ExecutionState.COMPLETED, at_seconds=at_seconds, evidence=evidence)
        self._write_heartbeat(at_seconds)

    @property
    def state(self) -> ExecutionState:
        return self._sm.state

    @property
    def blocked_report(self) -> BlockedReport | None:
        return self._blocked_report

    def status(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "state": self._sm.state.value,
            "tier": self.tier.value,
            "reasoning": self.reasoning.value,
            "escalations_used": self.escalations_used,
            "current_task": self._current_task,
            "attempt_count": len(self._activities),
            "started_at": self._started_at,
            "last_progress_at": self._progress.last_progress_at,
            "recovery_attempts": len(self._recovery_attempts),
            "budget": self.budget.status(),
            "blocked_report": self._blocked_report,
            "latest_checkpoint": self._checkpoints[-1] if self._checkpoints else None,
        }
