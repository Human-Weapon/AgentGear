"""Exception hierarchy for AgentGear.

Domain-specific exceptions so callers can handle expected failure modes
without catching broad ``Exception``. Persistence errors are a neutral
hierarchy so the safe JSON store is not coupled to any single domain type
(checkpoints, heartbeats, ...).
"""

from __future__ import annotations


class AgentGearError(Exception):
    """Base exception for all AgentGear errors."""


class ConfigurationError(AgentGearError):
    """Raised when policy/configuration values are invalid or contradictory."""


class TaskProfileError(AgentGearError):
    """Raised when a task profile cannot be analyzed (missing/invalid signals)."""


class RoutingError(AgentGearError):
    """Raised when no legal routing decision can be made under the given policy."""


class BudgetExceededError(AgentGearError):
    """Raised when a proposed execution plan would exceed a hard compute budget."""


class PlanningError(AgentGearError):
    """Raised when an execution plan cannot be produced."""


class InvalidStateTransitionError(AgentGearError):
    """Raised when an execution state machine transition is not permitted."""


class WatchdogError(AgentGearError):
    """Base class for watchdog-related errors."""


class RecoveryExhaustedError(WatchdogError):
    """Raised when recovery attempts/strategies are exhausted (path to BLOCKED)."""


class NotCompletedError(WatchdogError):
    """Raised when COMPLETED is asserted without sufficient evidence."""


# --- Neutral persistence hierarchy (shared with checkpoints/heartbeats) ---


class PersistenceError(AgentGearError):
    """Base for storage/IO persistence failures (domain-neutral)."""


class StorageLockError(PersistenceError):
    """Raised when a process lock cannot be acquired."""


class CorruptStorageError(PersistenceError):
    """Raised when persistent storage is corrupt or schema-invalid.

    The corrupt source file is quarantined (renamed) before this error
    is raised so the user can recover data.
    """

    def __init__(self, message: str, quarantined_path: str | None = None) -> None:
        super().__init__(message)
        self.quarantined_path = quarantined_path


class PathEscapeError(PersistenceError):
    """Raised when a resolved path escapes the allowed base directory."""
