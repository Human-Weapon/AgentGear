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
from ..exceptions import (
    BudgetExceededError,
    ConfigurationError,
    InvalidObservationError,
    InvalidPersistenceRootError,
    InvalidStateTransitionError,
    RecoveryExhaustedError,
)
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
    RecoveryEpisode,
    RecoveryEpisodeOutcome,
    RecoveryResult,
)
from ..path_security import PersistenceRoot, validate_persistence_safe_id
from ..routing import estimate_cost
from .blocked import build_blocked_report
from .heartbeat import HeartbeatWriter, build_heartbeat
from .progress import ProgressTracker
from .recovery import LoopGuard, RecoveryEngine
from .stall_detection import ActivityRecord, StallDetector
from .state_machine import ExecutionStateMachine, TransitionRecord

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
    """Supervises one execution end-to-end. One instance per execution_id.

    Round 3 / AUDIT3-05 -- concurrency contract: ``ExecutionWatchdog`` is a
    **single-writer coordinator** in v0.1.0. It holds no internal lock, and
    none of its state-mutating methods (``start``, ``record_activity``,
    ``record_progress``, ``evaluate``, ``begin_recovery``,
    ``record_recovery_result``, ``checkpoint``, ``complete``, ...) are
    safe to call concurrently from multiple threads/processes against the
    SAME instance. If your integration has multiple agents or workers
    reporting events for the same execution, serialize their event
    submission through a single caller-owned queue/lock before it reaches
    one ``ExecutionWatchdog`` instance -- "multi-agent orchestration" here
    describes what AgentGear plans and routes for, not a claim that the
    coordinator object itself tolerates unsynchronized concurrent writers.
    The persistence layer it uses (``HeartbeatWriter``, ``CheckpointStore``)
    IS safe for concurrent/multiprocess writers independently of this,
    since each write is a separate atomic file operation; see their own
    docstrings.

    Round 4 / NEW-04 -- heartbeat durability contract ("commit + dirty/
    sync"): the in-memory state machine is ALWAYS the sole authority for
    "what state is this execution in" -- it is never rolled back because a
    heartbeat write failed. The on-disk heartbeat file (when ``state_dir``
    is given) is a best-effort DURABLE MIRROR of that in-memory state, not
    a second source of truth. If a heartbeat write fails (disk full,
    permission error, ...), the method you called (``complete()``,
    ``start()``, ...) still re-raises that error so you learn about it
    immediately -- but the domain transition it performed has ALREADY
    happened and is not undone; do not call that method again to "retry"
    it (most transitions, like reaching COMPLETED, cannot legally be
    repeated anyway). Instead: check ``status()["heartbeat_dirty"]`` (or
    the ``heartbeat_dirty`` property), and call ``sync_heartbeat()`` to
    idempotently retry writing the CURRENT state -- it never repeats a
    state transition, budget charge, history entry, or attempt count, and
    is safe to call any number of times, including when nothing is dirty.
    An external reader of the heartbeat file alone (e.g. the CLI ``status``
    command, running as a separate process) cannot distinguish "this
    execution is idle at this state" from "this execution has since moved
    on but the last write failed" -- only the in-process caller holding
    this ``ExecutionWatchdog`` instance can see ``heartbeat_dirty`` and
    resolve it.
    """

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
        # Round 4 / NEW-02: every constructor argument is validated BEFORE
        # any state is assigned. Previously only execution_id and a loose
        # `context_budget_tokens <= 0` check ran here -- `bool` (a subclass
        # of `int`) and `float('nan')` both silently pass `<= 0` (`True<=0`
        # is `False`; `nan <= 0` is `False`), and passing a raw string for
        # `initial_tier`/`initial_reasoning` or a non-``Policy`` object for
        # `policy` was accepted at this point and only surfaced as a raw,
        # non-domain `AttributeError` moments later inside `__init__`
        # itself. One error family (`ConfigurationError`) for every
        # constructor-time problem, consistent with how `Policy` and its
        # nested config classes already validate themselves.
        _require_non_blank("execution_id", execution_id)
        if not isinstance(policy, Policy):
            raise ConfigurationError(
                f"policy must be a Policy instance, got {type(policy).__name__}"
            )
        if not isinstance(initial_tier, ModelTier):
            raise ConfigurationError(
                f"initial_tier must be a ModelTier, got {type(initial_tier).__name__}"
            )
        if not isinstance(initial_reasoning, ReasoningEffort):
            raise ConfigurationError(
                "initial_reasoning must be a ReasoningEffort, got "
                f"{type(initial_reasoning).__name__}"
            )
        if isinstance(context_budget_tokens, bool) or not isinstance(context_budget_tokens, int):
            raise ConfigurationError(
                f"context_budget_tokens must be an int, got {type(context_budget_tokens).__name__}"
            )
        if context_budget_tokens <= 0:
            raise ConfigurationError(
                f"context_budget_tokens must be > 0, got {context_budget_tokens}"
            )
        if state_dir is not None and not isinstance(state_dir, str):
            raise ConfigurationError(
                f"state_dir must be a str or None, got {type(state_dir).__name__}"
            )
        # Round 5 / AG5-10: only `None` means "persistence disabled" --
        # `""`/whitespace-only previously passed the type check above and
        # then silently disabled persistence too (`if state_dir else None`
        # treats any falsy string, including `""`, the same as `None`),
        # with no error at all. Worse, a NON-empty whitespace string like
        # `"   "` passed straight through as a real (if bizarre) directory
        # name. A caller passing a blank string almost certainly meant to
        # configure a real path and made a mistake -- silently downgrading
        # that to "no persistence" hides the mistake instead of surfacing
        # it, so it is now rejected explicitly.
        if state_dir is not None and not state_dir.strip():
            raise ConfigurationError(
                "state_dir must be a non-blank string or None, got "
                f"{state_dir!r} -- pass None to disable persistence"
            )
        # Self-adversarial pass (section 33): a state_dir-backed watchdog
        # previously only discovered a filesystem-unsafe execution_id (see
        # NEW-08's InvalidIdentifierError) on its first start(), by which
        # point the state machine had already transitioned to RUNNING and
        # budget had been committed -- an irreversible domain mutation
        # (NEW-04's "commit is authoritative, never rolled back" model)
        # left permanently stuck with a heartbeat that can never sync,
        # since this failure is NOT transient like a full disk. Reject it
        # atomically here instead, before any state exists to get stuck.
        if state_dir is not None:
            validate_persistence_safe_id("execution_id", execution_id)
        # Round 6 / AG6-03: an existing REGULAR FILE at state_dir is
        # structurally impossible to use as a persistence directory --
        # reject it here, before start() can ever mutate PLANNING ->
        # RUNNING or consume budget/history, rather than letting the
        # first heartbeat write fail with a raw FileExistsError deep
        # inside start(). Uses the SAME shared root-identity guard the
        # low-level HeartbeatWriter/CheckpointStore construct below (so
        # there is exactly one validation rule, not a duplicated one),
        # translating its persistence-domain exception into the stable,
        # public `ConfigurationError` family this constructor already
        # uses for every other caller-configuration mistake.
        if state_dir is not None:
            try:
                PersistenceRoot(state_dir)
            except InvalidPersistenceRootError as exc:
                raise ConfigurationError(str(exc)) from exc

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
        # EPISODE-scoped (Round 2 / C1): reset by _start_new_recovery_episode()
        # whenever a fresh STALL begins after the previous episode closed.
        # Never read these for anything that must survive across episodes —
        # use self._recovery_history (below) or the LoopGuard's global
        # counters (total_attempts, model_escalations) for that.
        self._recovery_attempts: list[RecoveryAttempt] = []
        self._tried_strategies: tuple[str, ...] = ()
        self._episode_number: int = 0
        self._episode_opened_at: float | None = None
        self._episode_stall_reason: str = ""
        # GLOBAL, execution-wide audit trail of every CLOSED episode.
        # Never cleared, never shrunk.
        self._recovery_history: list[RecoveryEpisode] = []
        self._blocked_report: BlockedReport | None = None
        self._started_at: float | None = None
        self._last_observed_at: float | None = None
        self._current_task: str = ""
        self._last_progress_evidence: str | None = None
        self._last_error: str | None = None
        self._planned_initial_tokens: int | None = None
        self._planned_initial_cost: float | None = None

        self._heartbeat_writer = HeartbeatWriter(state_dir) if state_dir is not None else None
        # Round 4 / NEW-04: durability model is "commit + dirty/sync" (see
        # ExecutionWatchdog's own docstring and
        # docs/audits/remediation-round-4.md). The in-memory state machine
        # is ALWAYS authoritative and is never rolled back for a
        # persistence failure. If a heartbeat write fails, we mark the
        # heartbeat dirty and re-raise the original error so the immediate
        # caller learns about it -- but the already-committed domain
        # transition stands, and the caller recovers durable status via
        # sync_heartbeat(), never by repeating the domain operation.
        self._heartbeat_dirty = False
        self._heartbeat_sync_error: str | None = None
        self._last_heartbeat_at_seconds: float | None = None
        self._checkpoint_store = None
        if state_dir is not None:
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
        """Round 4 / NEW-03: PURE check -- validates ``at_seconds`` is
        finite/non-negative and not before the last COMMITTED observation,
        but never mutates the clock itself. A caller that goes on to fail
        its OWN validation after this check (e.g. ``complete()`` rejecting
        malformed evidence) must not have already advanced
        ``_last_observed_at``, or a subsequent legitimate retry at an
        earlier, more reasonable timestamp would be incorrectly rejected
        as "before the last observed time" even though the operation that
        supposedly observed that later time never actually succeeded. See
        ``_commit_time``, which every public method calls explicitly, only
        once its own operation has fully succeeded.
        """
        at_seconds = _validate_finite_non_negative("at_seconds", at_seconds)
        if self._last_observed_at is not None and at_seconds < self._last_observed_at:
            raise InvalidObservationError(
                f"at_seconds={at_seconds} is before the last observed time "
                f"{self._last_observed_at}; the watchdog requires non-decreasing timestamps"
            )
        return at_seconds

    def _commit_time(self, at_seconds: float) -> None:
        """Advances the clock watermark. Must only be called once the
        calling method's operation has fully succeeded -- never on a path
        that is about to raise."""
        self._last_observed_at = at_seconds

    def _require_active_state(self, operation: str) -> None:
        """Round 6 / AG6-01: lifecycle ADMISSION for every ordinary
        (non-lifecycle-transition) public event, checked as the very
        FIRST thing in each such method -- before ``_validate_time()`` or
        any other input validation -- so an illegal call in the wrong
        lifecycle state is rejected on its own terms and never masked by
        (or dependent on) an unrelated input problem like a bad
        timestamp.

        "Active" means RUNNING/TESTING/REVIEWING -- the states where the
        underlying task is actually being worked on outside the recovery
        subsystem. Ordinary work events are deliberately NOT legal:

        * before ``start()`` (PLANNING) -- there is no task in progress
          yet to record activity/progress/checkpoints/escalation against;
        * once STALLED/RECOVERING -- the recovery subsystem has its own
          dedicated APIs (``begin_recovery()``, ``record_recovery_result()``)
          for tracking what happens during a stall/recovery episode;
          ordinary activity tracking exists to feed stall DETECTION, which
          is meaningless once a stall has already been detected;
        * once BLOCKED -- only the dedicated ``BLOCKED -> RECOVERING``
          path (``begin_recovery()``) may resume the execution;
        * once COMPLETED -- terminal; no further domain mutation of any
          kind is legal (see the class docstring's terminal invariant).

        ``advance()`` -- despite being a state TRANSITION rather than an
        ordinary event -- uses this SAME check for its CURRENT state, not
        because its target validation is insufficient, but because
        without it ``advance()`` was a live escape hatch: called from
        PLANNING it silently bypassed ``start()``'s own initialization
        (leaving ``_started_at=None`` while the state reads RUNNING, which
        permanently disables stall detection -- ``evaluate()`` no-ops
        whenever ``_started_at is None``); called from RECOVERING it
        bypassed ``record_recovery_result()`` entirely, silently
        abandoning a still-PENDING ``RecoveryAttempt`` and leaving the
        recovery episode forever unresolved. ``advance()``'s own
        documented purpose (RUNNING <-> TESTING <-> REVIEWING) never
        needs any state outside this set, so requiring it costs nothing
        legitimate while closing both escapes.
        """
        if self._sm.state not in _ACTIVE_STATES:
            raise InvalidStateTransitionError(
                f"{operation}() requires an active execution state "
                f"(running/testing/reviewing), current state is {self._sm.state.value}"
            )

    def _write_heartbeat(self, at_seconds: float) -> None:
        """Best-effort durable mirror of the in-memory state, called AFTER
        the domain operation (state transition, clock commit, ...) has
        already fully succeeded (Round 4 / NEW-04). A failure here marks
        the heartbeat dirty and re-raises the original exception unchanged
        -- the caller learns persistence failed, but the domain operation
        that already completed is never rolled back or repeated. See
        ``sync_heartbeat()`` for the idempotent recovery path.

        Round 5 / AG5-03/AG5-05: dirty is set BEFORE attempting to build
        OR write the projection, not only around the writer's I/O call.
        The in-memory state this method is about to mirror has already
        changed (that's WHY a caller reached this point), so the durable
        projection is provisionally stale from the moment this method
        starts -- if ``build_heartbeat()`` itself fails (a domain
        validation error inside ``Heartbeat.__post_init__``, not just an
        I/O error from the writer), the projection must still be reported
        dirty. Only a build AND write that BOTH fully succeed clear it.
        There must be no reachable state where the durable heartbeat is
        stale yet ``heartbeat_dirty`` reads ``False``.
        """
        if self._heartbeat_writer is None:
            return
        self._last_heartbeat_at_seconds = at_seconds
        self._heartbeat_dirty = True
        try:
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
        except Exception as exc:
            self._heartbeat_sync_error = str(exc)
            raise
        self._heartbeat_dirty = False
        self._heartbeat_sync_error = None

    @property
    def heartbeat_dirty(self) -> bool:
        """True if the last attempted heartbeat write failed and the
        durable heartbeat file may be stale relative to in-memory state.
        The in-memory state itself is never affected by this -- only the
        external, best-effort mirror of it. Call ``sync_heartbeat()`` to
        retry."""
        return self._heartbeat_dirty

    def sync_heartbeat(self) -> bool:
        """Idempotently retries writing the CURRENT in-memory state as a
        heartbeat, without repeating any domain transition, budget charge,
        history entry, or attempt count -- purely a durability catch-up.
        Returns True if the heartbeat is (now) synchronized, False if the
        retry itself failed again (check ``heartbeat_dirty``/status() for
        the latest error). Safe to call any number of times, including
        when nothing is dirty (a no-op returning True).
        """
        if not self._heartbeat_dirty:
            return True
        if self._heartbeat_writer is None or self._last_heartbeat_at_seconds is None:
            self._heartbeat_dirty = False
            return True
        try:
            self._write_heartbeat(self._last_heartbeat_at_seconds)
        except Exception:
            return False
        return True

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
        the coordinator never exposes any other way to reach BLOCKED. This
        also closes the current recovery episode (Round 2 / C1) with a
        BLOCKED outcome, using this episode's own attempts/strategies —
        never the full execution-wide history.
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
        self._close_recovery_episode(outcome=RecoveryEpisodeOutcome.BLOCKED, at_seconds=at_seconds)
        self._sm.transition(ExecutionState.BLOCKED, at_seconds=at_seconds, note=blocker)
        self._last_error = blocker
        # Round 4 / NEW-04: commit the clock/state BEFORE the best-effort
        # heartbeat mirror, so a heartbeat write failure (which re-raises,
        # see _write_heartbeat) never blocks the already-decided BLOCKED
        # transition from being fully committed in memory.
        self._commit_time(at_seconds)
        self._write_heartbeat(at_seconds)
        return report

    def _open_new_recovery_episode(self, *, at_seconds: float, stall_reason: str) -> None:
        """Start a fresh recovery episode (Round 2 / C1): the episode's
        own attempt count and tried-strategy list reset to empty, and the
        loop guard's episode-scoped ``recovery_attempts`` counter resets
        with it. Deliberately never touches anything global (total
        attempts, model escalations, the budget ledger, or the
        execution-wide ``_recovery_history`` audit trail) — see
        ``recovery.LoopGuard`` for the full scope rationale.
        """
        self._episode_number += 1
        self._episode_opened_at = at_seconds
        self._episode_stall_reason = stall_reason
        self._recovery_attempts = []
        self._tried_strategies = ()
        self._loop_guard.start_new_recovery_episode()

    def _close_recovery_episode(
        self, *, outcome: RecoveryEpisodeOutcome, at_seconds: float
    ) -> None:
        if self._episode_opened_at is None:
            return
        self._recovery_history.append(
            RecoveryEpisode(
                episode_number=self._episode_number,
                stall_reason=self._episode_stall_reason,
                attempts=tuple(self._recovery_attempts),
                outcome=outcome,
                opened_at=self._episode_opened_at,
                closed_at=at_seconds,
            )
        )

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
        self._commit_time(at_seconds)
        self._write_heartbeat(at_seconds)

    def advance(self, target: ExecutionState, *, at_seconds: float, note: str = "") -> None:
        """Escape hatch for ordinary forward/backward transitions that
        aren't stall/recovery/blocked/complete-specific (e.g. RUNNING ->
        TESTING -> REVIEWING -> RUNNING). Still funnels through the same
        validated state machine as every other coordinator method.

        Round 5 / AG5-04: ``ExecutionState`` subclasses ``str``, so a raw
        string like ``"testing"`` compares AND hashes equal to
        ``ExecutionState.TESTING`` -- both the membership check just below
        and ``ExecutionStateMachine.can_transition()`` would silently
        accept it, and ``self._sm.state`` would then be assigned that raw
        string (not a real ``ExecutionState`` member), poisoning every
        later ``self._sm.state.value`` access (including inside
        ``status()``) with a raw ``AttributeError``. ``isinstance()`` is
        the only check that actually distinguishes a real enum member from
        a same-valued plain string, so it must run BEFORE the membership
        check below, which relies on equality/hashing that a raw string
        satisfies just as well as the real enum member.
        """
        self._require_active_state("advance")
        if not isinstance(target, ExecutionState):
            raise InvalidObservationError(
                f"target must be an ExecutionState, got {type(target).__name__}"
            )
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
        self._commit_time(at_seconds)
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
        self._require_active_state("record_activity")
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
        self._commit_time(at_seconds)

        # Round 5 / AG5-05: `attempt_count`/`last_error` (both Heartbeat
        # fields) just changed above -- `evaluate()` only reaches
        # `_write_heartbeat()` on its STALL path, so ordinary, non-
        # stalling activity used to leave the durable heartbeat silently
        # stale (dirty=False, but no longer representing the current
        # attempt_count). Writing unconditionally here, AFTER evaluate()
        # has run, keeps the projection current on every path -- on the
        # stall path evaluate() will have already written it once for the
        # (different) post-stall state; this second write is a harmless,
        # idempotent re-sync of whatever the CURRENT state is by then, not
        # a stale duplicate.
        self.evaluate(at_seconds=at_seconds)
        self._write_heartbeat(at_seconds)

    def record_progress(
        self,
        *,
        at_seconds: float,
        kind: ProgressSignalKind,
        description: str,
        evidence: dict | None = None,
    ) -> None:
        self._require_active_state("record_progress")
        _require_non_blank("description", description)
        at_seconds = self._validate_time(at_seconds)
        event = ProgressEvent(
            kind=kind, description=description, at_seconds=at_seconds, evidence=evidence or {}
        )
        self._progress.record(event)
        self._loop_guard.record_progress()
        self._last_progress_evidence = description
        self._commit_time(at_seconds)
        self._write_heartbeat(at_seconds)

    def evaluate(self, *, at_seconds: float) -> None:
        """Re-check for a stall and, if one is found, automatically drive
        RUNNING/TESTING/REVIEWING -> STALLED -> RECOVERING (or BLOCKED, if
        recovery is already exhausted). Callers never need to call
        ``ExecutionStateMachine.transition`` themselves.
        """
        at_seconds = self._validate_time(at_seconds)
        if self._sm.state not in _ACTIVE_STATES or self._started_at is None:
            self._commit_time(at_seconds)
            return

        verdict = self._stall_detector.evaluate(
            now=at_seconds,
            started_at=self._started_at,
            last_progress_at=self._progress.last_progress_at,
            recent_activities=self._activities,
        )
        if not verdict.is_stalled:
            self._commit_time(at_seconds)
            return

        reasons = "; ".join(verdict.reasons)
        self._last_error = reasons
        self._loop_guard.record_no_progress_cycle()
        self._sm.transition(ExecutionState.STALLED, at_seconds=at_seconds, note=reasons)
        # This is always a FRESH stall (evaluate() only runs from an ACTIVE
        # state), so it always opens a new recovery episode — never a
        # continuation. A retry within the SAME episode instead goes
        # through record_recovery_result()'s FAILURE branch, which routes
        # RECOVERING -> STALLED directly and does NOT open a new episode.
        self._open_new_recovery_episode(at_seconds=at_seconds, stall_reason=reasons)
        self._commit_time(at_seconds)
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

        # Check-then-act (Round 2 / C2): admission is decided BEFORE any
        # counter is incremented, so max_recovery_attempts=N genuinely
        # permits N attempts in this episode, never N-1. Global bounds
        # (total attempts, model escalations) are checked independently —
        # either one blocks.
        exceeded, guard_reasons = self._loop_guard.exceeded()
        if exceeded or not self._loop_guard.can_start_recovery_attempt():
            if not exceeded:
                guard_reasons = (
                    f"recovery_attempts={self._loop_guard.recovery_attempts} >= "
                    f"max_recovery_attempts={self.policy.watchdog.max_recovery_attempts} "
                    "for this episode",
                )
            self._sm.transition(ExecutionState.RECOVERING, at_seconds=at_seconds, note=reason)
            self._transition_to_blocked(
                at_seconds=at_seconds,
                blocker="loop protection bound reached during recovery",
                root_cause="; ".join(guard_reasons),
            )
            return None

        attempt_number = len(self._recovery_attempts) + 1

        try:
            strategy = self._recovery_engine.next_strategy(
                tried_strategies=self._tried_strategies, attempt_number=attempt_number
            )
        except RecoveryExhaustedError as exc:
            # Round 3 / AUDIT3-03: this clause is narrowed to the ONE
            # documented business exception RecoveryEngine.next_strategy()
            # can raise. It used to catch `Exception` broadly (with a
            # comment implying only RecoveryExhaustedError was expected),
            # which meant a genuine programming bug in a caller-supplied
            # or buggy RecoveryEngine (AttributeError, TypeError, KeyError,
            # ...) would be silently reclassified as a normal-looking
            # BLOCKED business report instead of propagating as the crash
            # it actually is -- hiding real defects from whoever operates
            # the execution. Any other exception type now propagates
            # unchanged, before any state/budget/history mutation below.
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

        # Only now, having actually committed to making this attempt, does
        # it count against the episode's admission gate (C2: increment
        # AFTER the decision to proceed, never before).
        self._loop_guard.record_recovery_attempt_started()
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
        self._commit_time(at_seconds)
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

        Round 5 / AG5-02: this method RESOLVES an attempt that has already
        finished -- ``result`` must therefore be exactly ``SUCCESS`` or
        ``FAILURE``, never ``PENDING`` (that value exists on
        ``RecoveryAttempt`` only to represent an attempt still in
        progress, set internally by ``begin_recovery()``; it is never a
        valid RESOLUTION to report here). Every input, including
        ``evidence``'s structural contract (``None`` or a non-blank
        string, matching its own type hint), is validated BEFORE any
        mutation -- previously, an invalid ``evidence`` value was only
        discovered deep inside a follow-on ``ProgressEvent``/
        ``TransitionRecord`` construction, by which point the attempt had
        already been overwritten, the episode already closed, and the
        state machine already transitioned.
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
        if result not in (RecoveryResult.SUCCESS, RecoveryResult.FAILURE):
            raise InvalidObservationError(
                "record_recovery_result() resolves a completed attempt; result must be "
                f"RecoveryResult.SUCCESS or RecoveryResult.FAILURE, got {result.value!r} -- "
                "PENDING represents an attempt still in progress and cannot be reported as "
                "a resolution"
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
        if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
            raise InvalidObservationError(
                f"evidence must be None or a non-empty, non-blank string, got {evidence!r}"
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
            # Close this recovery episode as a SUCCESS (Round 2 / C1)
            # BEFORE the transition: the very next stall (if any) opens a
            # brand new episode with a clean attempt count, but this
            # episode's own history is preserved in _recovery_history
            # exactly as it stood at the moment of success.
            self._close_recovery_episode(
                outcome=RecoveryEpisodeOutcome.SUCCESS, at_seconds=at_seconds
            )
            self._sm.transition(
                resume_state,
                at_seconds=at_seconds,
                note=progress_description,
            )
            # Round 3 / section 13: "recovery SUCCESS" means the recovery
            # ACTION itself succeeded (execution capability was restored --
            # e.g. a tool was restarted, an approach was changed) -- it is
            # NOT a claim that the original task materially advanced. That
            # distinction still matters here: a successful recovery
            # establishes a new operational baseline and IS treated as
            # genuine new evidence, resetting every stall-detection signal
            # to this point rather than immediately re-counting the
            # activity that caused the original stall. If the underlying
            # task still isn't moving after that fresh baseline, normal
            # stall detection will catch it again on its own -- resetting
            # the boundary here does not grant any additional immunity
            # beyond the standard no-progress/no-attempts window.
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
            self._commit_time(at_seconds)
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
            self._commit_time(at_seconds)
            self._write_heartbeat(at_seconds)

    def record_escalation(
        self, *, at_seconds: float, signals: EscalationSignals
    ) -> EscalationDecision:
        """Consult the shared budget ledger (AG-04) via ``decide_escalation``
        and, if approved, commit the escalation's cost/tokens and advance
        ``self.tier``/``self.reasoning``.
        """
        self._require_active_state("record_escalation")
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
        self._commit_time(at_seconds)
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
        self._require_active_state("checkpoint")
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
        # Round 4 / section 15 (validate-before-mutate sweep): persist
        # FIRST, then mirror into the in-memory cache -- if the durable
        # append fails (disk full, quarantine, ...), self._checkpoints
        # must not already claim a checkpoint exists that was never
        # actually written, since _transition_to_blocked() reads
        # self._checkpoints[-1] as the BlockedReport's "last successful
        # checkpoint" and _write_heartbeat() reads it for pending_work.
        if self._checkpoint_store is not None:
            self._checkpoint_store.append(cp)
        self._checkpoints.append(cp)
        self._commit_time(at_seconds)
        # Round 5 / AG5-05: a checkpoint changes `pending_work` (read from
        # `self._checkpoints[-1].pending`), which IS a Heartbeat field --
        # this call was previously missing entirely, so the durable
        # heartbeat's `pending_work` silently went stale on every
        # checkpoint while `heartbeat_dirty` stayed False.
        self._write_heartbeat(at_seconds)
        return cp

    def complete(self, *, at_seconds: float, evidence: tuple[str, ...]) -> None:
        at_seconds = self._validate_time(at_seconds)
        self._sm.transition(ExecutionState.COMPLETED, at_seconds=at_seconds, evidence=evidence)
        self._commit_time(at_seconds)
        self._write_heartbeat(at_seconds)

    @property
    def state(self) -> ExecutionState:
        return self._sm.state

    @property
    def blocked_report(self) -> BlockedReport | None:
        return self._blocked_report

    @property
    def recovery_history(self) -> tuple[RecoveryEpisode, ...]:
        """Every CLOSED recovery episode this execution has ever had,
        oldest first. Never cleared, never shrunk — the audit trail
        survives regardless of how many episodes later succeeded
        (Round 2 / C1)."""
        return tuple(self._recovery_history)

    @property
    def transition_history(self) -> tuple[TransitionRecord, ...]:
        """The full, ordered state-transition history, including the
        evidence supplied at each transition (Round 2 / H3) — the
        authoritative record of what evidence justified reaching
        COMPLETED (or any other transition), retrievable through the
        public coordinator without reaching into private internals.
        """
        return tuple(self._sm.history)

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
            # Current/most-recent episode's own attempt count (resets to 0
            # each time a new episode opens) -- NOT a lifetime total.
            "recovery_attempts": len(self._recovery_attempts),
            "recovery_episode_number": self._episode_number,
            "recovery_episodes_completed": len(self._recovery_history),
            "recovery_history": self.recovery_history,
            "total_attempts": self._loop_guard.total_attempts,
            "budget": self.budget.status(),
            "blocked_report": self._blocked_report,
            "latest_checkpoint": self._checkpoints[-1] if self._checkpoints else None,
            # Round 4 / NEW-04: durability status. `heartbeat_dirty=True`
            # means the durable heartbeat file may be stale relative to
            # this in-memory state (the last write attempt failed) -- the
            # in-memory state above is unaffected either way. Call
            # sync_heartbeat() to retry.
            "heartbeat_dirty": self._heartbeat_dirty,
            "heartbeat_sync_error": self._heartbeat_sync_error,
        }
