"""Core domain models for AgentGear.

Everything here is a plain, typed, immutable-by-default dataclass or enum.
No I/O, no provider calls, no randomness. Determinism and auditability of
routing decisions depend on these types carrying no hidden state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from .exceptions import InvalidBlockedReportError, InvalidObservationError, TaskProfileError

# --------------------------------------------------------------------------
# Ordered enums
# --------------------------------------------------------------------------


class ModelTier(str, Enum):
    """Provider-agnostic capability tier. Mapped to real models via config."""

    FAST = "fast"
    STANDARD = "standard"
    ADVANCED = "advanced"
    FRONTIER = "frontier"

    @property
    def rank(self) -> int:
        return _TIER_ORDER.index(self)


_TIER_ORDER = [ModelTier.FAST, ModelTier.STANDARD, ModelTier.ADVANCED, ModelTier.FRONTIER]


class ReasoningEffort(str, Enum):
    """Reasoning/thinking depth. Independent dimension from ModelTier."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @property
    def rank(self) -> int:
        return _REASONING_ORDER.index(self)


_REASONING_ORDER = [
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
]


class AgentRole(str, Enum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    JUDGE = "judge"


class ComplexityLevel(str, Enum):
    TRIVIAL = "trivial"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"


class RiskLevel(str, Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionState(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    TESTING = "testing"
    REVIEWING = "reviewing"
    STALLED = "stalled"
    RECOVERING = "recovering"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class ProgressSignalKind(str, Enum):
    SUBTASK_COMPLETED = "subtask_completed"
    FILE_CHANGED = "file_changed"
    TEST_STATUS_IMPROVED = "test_status_improved"
    ERROR_RESOLVED = "error_resolved"
    NEW_EVIDENCE = "new_evidence"
    PHASE_ADVANCED = "phase_advanced"
    PENDING_WORK_REDUCED = "pending_work_reduced"
    TOOL_SUCCESS = "tool_success"
    DECISION_PRODUCED = "decision_produced"
    CHECKPOINT_REACHED = "checkpoint_reached"


class RecoveryResult(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


def _validate_unit_interval(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TaskProfileError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value):
        raise TaskProfileError(f"{name} must be finite, got {value}")
    if not (0.0 <= value <= 1.0):
        raise TaskProfileError(f"{name} must be within [0.0, 1.0], got {value}")
    return float(value)


def _validate_non_negative_int(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TaskProfileError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise TaskProfileError(f"{name} must be >= 0, got {value}")
    return value


def _validate_positive_int(name: str, value: int) -> int:
    """Strict positive-int boundary check (Round 2 / H1): ``bool`` is a
    subclass of ``int`` in Python, so ``isinstance(value, int)`` alone
    would silently accept ``True``/``False`` as ``1``/``0``. Every public
    "count"-shaped field must reject bool, float, str, and None
    explicitly, not just numbers below the floor.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskProfileError(f"{name} must be an int, got {type(value).__name__}")
    if value < 1:
        raise TaskProfileError(f"{name} must be >= 1, got {value}")
    return value


def _validate_observation_timestamp(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidObservationError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value):
        raise InvalidObservationError(f"{name} must be finite, got {value}")
    if value < 0:
        raise InvalidObservationError(f"{name} must be >= 0, got {value}")
    return float(value)


def _validate_non_blank_str(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidObservationError(f"{name} must be a non-empty, non-blank string")
    return value


def _require_blocked_report_str(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidBlockedReportError(f"{name} must be a non-empty, non-blank string")
    return value


def _validate_str_tuple(name: str, value: tuple[str, ...]) -> tuple[str, ...]:
    """A ``tuple[str, ...]`` boundary contract (Round 2 / H2/M9 pattern):
    reject a bare string/list/None outright (no silent coercion, since
    that would hide a caller bug), and reject any element that is not a
    non-blank string. An empty tuple is valid unless the caller enforces
    non-emptiness separately."""
    if not isinstance(value, tuple):
        raise InvalidBlockedReportError(
            f"{name} must be a tuple[str, ...], got {type(value).__name__}"
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidBlockedReportError(
                f"{name} must contain only non-blank strings, got {item!r}"
            )
    return value


# --------------------------------------------------------------------------
# Task analysis input/output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskProfile:
    """Raw, caller-supplied signals describing a task to be executed.

    All continuous signals are normalized to [0.0, 1.0] so the analysis
    engine stays deterministic and provider-agnostic. Callers (CLI,
    integrations) are responsible for producing these estimates; AgentGear
    does not infer them from free text beyond simple heuristics in the CLI.
    """

    description: str
    files_affected: int = 1
    modules_affected: int = 1
    architectural_impact: float = 0.0
    security_impact: float = 0.0
    data_impact: float = 0.0
    ambiguity: float = 0.0
    novelty: float = 0.0
    reversibility: float = 1.0  # 1.0 = fully reversible, 0.0 = irreversible
    existing_test_coverage: float = 0.5
    prior_failures: int = 0
    expected_output_tokens: int = 2000

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise TaskProfileError("description must be a non-empty string")
        object.__setattr__(
            self,
            "files_affected",
            _validate_non_negative_int("files_affected", self.files_affected),
        )
        object.__setattr__(
            self,
            "modules_affected",
            _validate_non_negative_int("modules_affected", self.modules_affected),
        )
        object.__setattr__(
            self,
            "architectural_impact",
            _validate_unit_interval("architectural_impact", self.architectural_impact),
        )
        object.__setattr__(
            self,
            "security_impact",
            _validate_unit_interval("security_impact", self.security_impact),
        )
        object.__setattr__(
            self, "data_impact", _validate_unit_interval("data_impact", self.data_impact)
        )
        object.__setattr__(self, "ambiguity", _validate_unit_interval("ambiguity", self.ambiguity))
        object.__setattr__(self, "novelty", _validate_unit_interval("novelty", self.novelty))
        object.__setattr__(
            self, "reversibility", _validate_unit_interval("reversibility", self.reversibility)
        )
        object.__setattr__(
            self,
            "existing_test_coverage",
            _validate_unit_interval("existing_test_coverage", self.existing_test_coverage),
        )
        object.__setattr__(
            self,
            "prior_failures",
            _validate_non_negative_int("prior_failures", self.prior_failures),
        )
        object.__setattr__(
            self,
            "expected_output_tokens",
            _validate_non_negative_int("expected_output_tokens", self.expected_output_tokens),
        )


@dataclass(frozen=True)
class ComplexityAssessment:
    """``score`` is a strictly validated [0, 1] boundary value (Round 2 /
    M7) since it directly drives routing decisions. Individual values
    inside ``factors`` are not independently range/finiteness-validated
    the way ``score`` is (that remains a deliberate, scoped M7 decision) —
    conventionally each is also in [0, 1] (analysis.py always produces
    them that way).

    Round 3 / AUDIT3-04 (correcting an inaccurate Round 2 note):
    ``factors`` is NOT merely diagnostic. ``planning.py`` reads
    ``factors["ambiguity"]`` and ``factors["architectural_impact"]``
    directly to decide multi-agent staffing, and ``routing.py`` reads the
    equivalent keys on ``RiskAssessment.factors`` to decide critical-risk
    tier/reasoning floors -- both are real, consumed routing/planning
    inputs. Because of that, ``factors`` is defensively copied and frozen
    the same way ``score`` is protected: neither mutating the dict a
    caller originally passed in, nor mutating ``assessment.factors``
    directly after construction, can change what a later routing/planning
    call sees for THIS SAME assessment object.
    """

    score: float
    level: ComplexityLevel
    factors: Mapping[str, float] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _validate_unit_interval("score", self.score))
        if not isinstance(self.level, ComplexityLevel):
            raise TaskProfileError(
                f"level must be a ComplexityLevel, got {type(self.level).__name__}"
            )
        if not isinstance(self.factors, Mapping):
            raise TaskProfileError(
                f"factors must be a dict[str, float], got {type(self.factors).__name__}"
            )
        object.__setattr__(self, "factors", MappingProxyType(dict(self.factors)))


@dataclass(frozen=True)
class RiskAssessment:
    """``score`` is a strictly validated [0, 1] boundary value (Round 2 /
    M7). See ``ComplexityAssessment`` for why ``factors`` is defensively
    frozen (Round 3 / AUDIT3-04) despite not being independently
    range-validated -- it IS a real routing input (``routing.py`` reads
    ``security_impact``/``data_impact``/``irreversibility`` from it for
    critical-risk overrides), not diagnostic-only."""

    score: float
    level: RiskLevel
    factors: Mapping[str, float] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _validate_unit_interval("score", self.score))
        if not isinstance(self.level, RiskLevel):
            raise TaskProfileError(f"level must be a RiskLevel, got {type(self.level).__name__}")
        if not isinstance(self.factors, Mapping):
            raise TaskProfileError(
                f"factors must be a dict[str, float], got {type(self.factors).__name__}"
            )
        object.__setattr__(self, "factors", MappingProxyType(dict(self.factors)))


# --------------------------------------------------------------------------
# Routing output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelProfile:
    """A resolved routing decision for a single dimension pair."""

    tier: ModelTier
    reasoning: ReasoningEffort
    resolved_model: str | None = None
    rationale: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AgentAssignment:
    role: AgentRole
    tier: ModelTier
    reasoning: ReasoningEffort
    count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "count", _validate_positive_int("AgentAssignment.count", self.count)
        )
        if not isinstance(self.role, AgentRole):
            raise TaskProfileError(
                f"AgentAssignment.role must be an AgentRole, got {type(self.role).__name__}"
            )
        if not isinstance(self.tier, ModelTier):
            raise TaskProfileError(
                f"AgentAssignment.tier must be a ModelTier, got {type(self.tier).__name__}"
            )
        if not isinstance(self.reasoning, ReasoningEffort):
            raise TaskProfileError(
                "AgentAssignment.reasoning must be a ReasoningEffort, got "
                f"{type(self.reasoning).__name__}"
            )


@dataclass(frozen=True)
class ExecutionStrategy:
    """The multi-agent staffing plan: who runs, in what order, and why."""

    agents: tuple[AgentAssignment, ...]
    judge_required: bool
    execution_order: tuple[AgentRole, ...]
    rationale: tuple[str, ...] = field(default_factory=tuple)

    @property
    def agent_count(self) -> int:
        return sum(a.count for a in self.agents)


@dataclass(frozen=True)
class ExecutionPlan:
    """The full, explainable output of AgentGear for one task."""

    task_profile: TaskProfile
    complexity: ComplexityAssessment
    risk: RiskAssessment
    primary_model: ModelProfile
    strategy: ExecutionStrategy
    context_budget_tokens: int
    max_estimated_cost: float
    max_agents: int
    escalation_policy_summary: str
    recovery_policy_summary: str
    review_required: bool
    rationale: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Watchdog domain models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressEvent:
    """Round 3 / AUDIT3-04 sweep note: unlike ``ComplexityAssessment.
    factors``/``RiskAssessment.factors`` (which ARE read by planning/
    routing to make real decisions, and are therefore defensively frozen),
    ``evidence`` here is a genuinely diagnostic annotation payload -- no
    code anywhere reads a key out of it to branch on. Mutating it
    post-construction has no effect on AgentGear's own behavior, so it is
    deliberately left as a plain, unfrozen dict rather than adding
    protection nothing downstream needs."""

    kind: ProgressSignalKind
    description: str
    at_seconds: float
    evidence: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "description", _validate_non_blank_str("description", self.description)
        )
        object.__setattr__(
            self, "at_seconds", _validate_observation_timestamp("at_seconds", self.at_seconds)
        )
        if not isinstance(self.kind, ProgressSignalKind):
            raise InvalidObservationError(
                f"kind must be a ProgressSignalKind, got {type(self.kind).__name__}"
            )


@dataclass(frozen=True)
class Checkpoint:
    execution_id: str
    phase: str
    completed: tuple[str, ...] = field(default_factory=tuple)
    pending: tuple[str, ...] = field(default_factory=tuple)
    last_good_state: str | None = None
    at_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "execution_id", _validate_non_blank_str("execution_id", self.execution_id)
        )
        object.__setattr__(self, "phase", _validate_non_blank_str("phase", self.phase))
        object.__setattr__(
            self, "at_seconds", _validate_observation_timestamp("at_seconds", self.at_seconds)
        )
        for name in ("completed", "pending"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(isinstance(value, str) for value in values):
                raise InvalidObservationError(f"{name} must be a tuple of strings")
        if self.last_good_state is not None:
            object.__setattr__(
                self,
                "last_good_state",
                _validate_non_blank_str("last_good_state", self.last_good_state),
            )


@dataclass(frozen=True)
class RecoveryAttempt:
    reason: str
    strategy: str
    attempt_number: int
    result: RecoveryResult = RecoveryResult.PENDING
    at_seconds: float = 0.0


class RecoveryEpisodeOutcome(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RecoveryEpisode:
    """One closed STALL -> attempt(s) -> resolution cycle (Round 2 / C1).

    Recovery-attempt counting and strategy selection are scoped to a
    single episode (a fresh STALL after a successful recovery starts a
    new episode with a clean attempt count); this record is the
    execution-wide, never-reset audit trail of every episode that has
    closed, independent of that per-episode operational state.

    Round 3 / AUDIT3-06: this type only ever represents a CLOSED episode
    -- the coordinator constructs it exactly once, at
    ``_close_recovery_episode``, always with a real ``outcome`` and
    ``closed_at`` already known (there is no "open episode" variant of
    this dataclass; the in-progress state lives in the coordinator's own
    private fields instead). Validation reflects that: ``attempts`` may
    legitimately be empty (e.g. a budget reservation denied BEFORE any
    attempt ran still closes the episode as BLOCKED with zero attempts,
    see ``test_recovery_episode_accepts_a_pre_attempt_budget_blocked_
    episode``) -- ``len(attempts) >= 1`` would be a false invariant.
    """

    episode_number: int
    stall_reason: str
    attempts: tuple[RecoveryAttempt, ...]
    outcome: RecoveryEpisodeOutcome
    opened_at: float
    closed_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "episode_number", _validate_positive_int("episode_number", self.episode_number)
        )
        object.__setattr__(
            self, "opened_at", _validate_observation_timestamp("opened_at", self.opened_at)
        )
        object.__setattr__(
            self, "closed_at", _validate_observation_timestamp("closed_at", self.closed_at)
        )
        if self.closed_at < self.opened_at:
            raise InvalidObservationError(
                f"closed_at={self.closed_at} must be >= opened_at={self.opened_at}"
            )
        if not isinstance(self.outcome, RecoveryEpisodeOutcome):
            raise InvalidObservationError(
                f"outcome must be a RecoveryEpisodeOutcome, got {type(self.outcome).__name__}"
            )
        if not isinstance(self.attempts, tuple) or not all(
            isinstance(a, RecoveryAttempt) for a in self.attempts
        ):
            raise InvalidObservationError("attempts must be a tuple[RecoveryAttempt, ...]")


@dataclass(frozen=True)
class BlockedReport:
    """AG-09/Round 2 M9: validated at this model boundary too, not only in
    ``build_blocked_report`` -- ``BlockedReport`` is a public dataclass and
    must not be constructible with a blank blocker/root_cause, a bool or
    negative ``attempts``, or a non-``tuple[str, ...]`` collection field,
    regardless of which constructor a caller uses.

    ``attempts`` and ``len(strategies_tried)`` are deliberately NOT cross-
    checked against each other: ``attempts`` counts recovery attempts made,
    while ``strategies_tried`` is the distinct set of strategy names used
    (a strategy can be retried), so they are not required to be equal.
    """

    blocker: str
    root_cause: str
    last_successful_checkpoint: Checkpoint | None
    attempts: int
    strategies_tried: tuple[str, ...]
    evidence: tuple[str, ...]
    files_affected: tuple[str, ...]
    recommended_human_action: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocker", _require_blocked_report_str("blocker", self.blocker))
        object.__setattr__(
            self, "root_cause", _require_blocked_report_str("root_cause", self.root_cause)
        )
        if self.last_successful_checkpoint is not None and not isinstance(
            self.last_successful_checkpoint, Checkpoint
        ):
            raise InvalidBlockedReportError(
                "last_successful_checkpoint must be a Checkpoint or None, got "
                f"{type(self.last_successful_checkpoint).__name__}"
            )
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise InvalidBlockedReportError(
                f"attempts must be an int, got {type(self.attempts).__name__}"
            )
        if self.attempts < 0:
            raise InvalidBlockedReportError(f"attempts must be >= 0, got {self.attempts}")
        object.__setattr__(
            self, "strategies_tried", _validate_str_tuple("strategies_tried", self.strategies_tried)
        )
        object.__setattr__(self, "evidence", _validate_str_tuple("evidence", self.evidence))
        object.__setattr__(
            self, "files_affected", _validate_str_tuple("files_affected", self.files_affected)
        )
        object.__setattr__(
            self,
            "recommended_human_action",
            _require_blocked_report_str("recommended_human_action", self.recommended_human_action),
        )


@dataclass(frozen=True)
class Heartbeat:
    """Lightweight liveness record. Cheap to emit; not a full audit log."""

    execution_id: str
    state: ExecutionState
    current_task: str
    current_subtask: str | None
    last_real_progress_at: float
    last_progress_evidence: str | None
    attempt_count: int
    current_strategy: str | None
    last_error: str | None
    pending_work: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "execution_id", _validate_non_blank_str("execution_id", self.execution_id)
        )
        if not isinstance(self.state, ExecutionState):
            raise InvalidObservationError(
                f"state must be an ExecutionState, got {type(self.state).__name__}"
            )
        object.__setattr__(
            self, "current_task", _validate_non_blank_str("current_task", self.current_task)
        )
        object.__setattr__(
            self,
            "last_real_progress_at",
            _validate_observation_timestamp("last_real_progress_at", self.last_real_progress_at),
        )
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int):
            raise InvalidObservationError("attempt_count must be an int")
        if self.attempt_count < 0:
            raise InvalidObservationError("attempt_count must be >= 0")
        for name in (
            "current_subtask",
            "last_progress_evidence",
            "current_strategy",
            "last_error",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _validate_non_blank_str(name, value))
        if not isinstance(self.pending_work, tuple) or not all(
            isinstance(value, str) for value in self.pending_work
        ):
            raise InvalidObservationError("pending_work must be a tuple of strings")
