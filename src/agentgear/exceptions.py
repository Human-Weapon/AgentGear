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


class InvalidObservationError(WatchdogError):
    """Raised when a caller reports invalid activity/progress/escalation
    signals to the watchdog coordinator: non-finite or negative numbers,
    out-of-range fractions, backwards/decreasing timestamps, or blank
    mandatory text. A stuck agent must not be able to keep itself "alive"
    by feeding the watchdog garbage."""


class InvalidBlockedReportError(WatchdogError):
    """Raised when a BLOCKED report would be missing meaningful content
    (blank blocker/root_cause/recommendation, negative attempts, ...).
    BLOCKED must always carry a report a human can act on."""


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


class InvalidIdentifierError(PersistenceError):
    """Raised when an identifier (e.g. ``execution_id``) that becomes a
    literal filename/directory-name component is permanently unsafe for
    that purpose (too long, contains characters illegal in a filename) --
    Round 4 / NEW-08. Distinct from ``PathEscapeError`` (a traversal/
    containment attempt): this is "this identifier can never work on this
    filesystem," caught immediately rather than surfacing 10+ seconds
    later as a confusing ``StorageLockError`` once the underlying OS call
    finally fails."""
