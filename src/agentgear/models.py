"""Core domain models for AgentGear.

Everything here is a plain, typed, immutable-by-default dataclass or enum.
No I/O, no provider calls, no randomness. Determinism and auditability of
routing decisions depend on these types carrying no hidden state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .exceptions import TaskProfileError

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
    score: float
    level: ComplexityLevel
    factors: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class RiskAssessment:
    score: float
    level: RiskLevel
    factors: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


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
        if self.count < 1:
            raise TaskProfileError(f"AgentAssignment.count must be >= 1, got {self.count}")


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
    kind: ProgressSignalKind
    description: str
    at_seconds: float
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Checkpoint:
    execution_id: str
    phase: str
    completed: tuple[str, ...] = field(default_factory=tuple)
    pending: tuple[str, ...] = field(default_factory=tuple)
    last_good_state: str | None = None
    at_seconds: float = 0.0


@dataclass(frozen=True)
class RecoveryAttempt:
    reason: str
    strategy: str
    attempt_number: int
    result: RecoveryResult = RecoveryResult.PENDING
    at_seconds: float = 0.0


@dataclass(frozen=True)
class BlockedReport:
    blocker: str
    root_cause: str
    last_successful_checkpoint: Checkpoint | None
    attempts: int
    strategies_tried: tuple[str, ...]
    evidence: tuple[str, ...]
    files_affected: tuple[str, ...]
    recommended_human_action: str


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
