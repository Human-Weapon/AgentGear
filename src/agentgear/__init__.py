"""AgentGear — adaptive compute orchestrator for AI software-engineering agents.

AgentGear has two public layers:

  PLANNING:            TaskProfile -> ExecutionPlan     (``analyze``, ``plan``)
  RUNTIME SUPERVISION: ``ExecutionWatchdog``             (state machine + stall
                        detection + bounded recovery + cumulative budget)

AgentGear decides HOW a task should be executed: which model tier, how
much reasoning effort, how many agents, which roles, when to escalate,
and when to stop because something is stuck. It does not decide WHAT
CONTEXT to load (PromptGraph), whether a skill is safe (SkillGuard), or
what strategy performed best historically (AgentBench). Its
``ExecutionWatchdog`` supervises the *state* of an execution that an
external runtime drives; AgentGear does not call real LLM/provider APIs
or own a provider process itself.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .api import analyze, plan
from .budget import ExecutionBudgetLedger, ReservationKind, ReservationState
from .config import (
    BudgetPolicy,
    CriticalRiskPolicy,
    ModelTierMapping,
    Policy,
    ReasoningThresholds,
    RoutingThresholds,
    RoutingWeights,
    WatchdogPolicy,
)
from .escalation import EscalationDecision, EscalationSignals, decide_escalation
from .exceptions import (
    AgentGearError,
    BudgetExceededError,
    ConfigurationError,
    CorruptStorageError,
    InvalidBlockedReportError,
    InvalidObservationError,
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
    ActionableTaskContext,
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
    RecoveryResult,
    RiskAssessment,
    RiskLevel,
    TaskProfile,
)
from .watchdog import ExecutionWatchdog

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
    "CriticalRiskPolicy",
    "ModelTierMapping",
    "TaskProfile",
    "ActionableTaskContext",
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
    "RecoveryResult",
    "BlockedReport",
    "Heartbeat",
    "ExecutionWatchdog",
    "ExecutionBudgetLedger",
    "ReservationKind",
    "ReservationState",
    "EscalationSignals",
    "EscalationDecision",
    "decide_escalation",
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
    "InvalidObservationError",
    "InvalidBlockedReportError",
    "PersistenceError",
    "StorageLockError",
    "CorruptStorageError",
    "PathEscapeError",
]
