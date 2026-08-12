"""AgentGear — adaptive compute orchestrator for AI software-engineering agents.

AgentGear decides HOW a task should be executed: which model tier, how
much reasoning effort, how many agents, which roles, when to escalate,
and when to stop because something is stuck. It does not decide WHAT
CONTEXT to load (PromptGraph), whether a skill is safe (SkillGuard), or
what strategy performed best historically (AgentBench).
"""

from __future__ import annotations

__version__ = "0.1.0"

from .api import analyze, plan
from .config import (
    BudgetPolicy,
    ModelTierMapping,
    Policy,
    ReasoningThresholds,
    RoutingThresholds,
    RoutingWeights,
    WatchdogPolicy,
)
from .exceptions import (
    AgentGearError,
    BudgetExceededError,
    ConfigurationError,
    CorruptStorageError,
    InvalidStateTransitionError,
    NotCompletedError,
    PathEscapeError,
    PersistenceError,
    PlanningError,
    RecoveryExhaustedError,
    RoutingError,
    StorageLockError,
    TaskProfileError,
    WatchdogError,
)
from .models import (
    AgentAssignment,
    AgentRole,
    BlockedReport,
    Checkpoint,
    ComplexityAssessment,
    ComplexityLevel,
    ExecutionPlan,
    ExecutionState,
    ExecutionStrategy,
    Heartbeat,
    ModelProfile,
    ModelTier,
    ProgressEvent,
    ProgressSignalKind,
    ReasoningEffort,
    RecoveryAttempt,
    RiskAssessment,
    RiskLevel,
    TaskProfile,
)

__all__ = [
    "__version__",
    "analyze",
    "plan",
    "Policy",
    "RoutingWeights",
    "RoutingThresholds",
    "ReasoningThresholds",
    "WatchdogPolicy",
    "BudgetPolicy",
    "ModelTierMapping",
    "TaskProfile",
    "ComplexityAssessment",
    "ComplexityLevel",
    "RiskAssessment",
    "RiskLevel",
    "ModelTier",
    "ReasoningEffort",
    "AgentRole",
    "ModelProfile",
    "AgentAssignment",
    "ExecutionStrategy",
    "ExecutionPlan",
    "ExecutionState",
    "ProgressEvent",
    "ProgressSignalKind",
    "Checkpoint",
    "RecoveryAttempt",
    "BlockedReport",
    "Heartbeat",
    "AgentGearError",
    "ConfigurationError",
    "TaskProfileError",
    "RoutingError",
    "BudgetExceededError",
    "PlanningError",
    "InvalidStateTransitionError",
    "WatchdogError",
    "RecoveryExhaustedError",
    "NotCompletedError",
    "PersistenceError",
    "StorageLockError",
    "CorruptStorageError",
    "PathEscapeError",
]
