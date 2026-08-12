"""Execution Watchdog: state machine, progress detection, stall detection,
bounded recovery, and structured BLOCKED reporting.

This is the OBLIGATORY core feature described in AgentGear's design:
NEVER STOP SILENTLY. An execution is always in one explicit
``ExecutionState``; it never just "goes quiet" and gets assumed done.

Round 2 / L6: this package exports both ``ExecutionWatchdog`` (the ONE
supported path for normal usage; see its own docstring and AG-05) and the
low-level primitives it composes (``ExecutionStateMachine``,
``StallDetector``, ``LoopGuard``, ``RecoveryEngine``, ...). Exposing both
is intentional for a v0.1.0 OSS library: advanced callers embedding just
one piece (e.g. only the stall heuristics, in a host that has its own
state machine) are a legitimate use case, and removing public API without
a strong reason before v0.1.0 is tagged would be gratuitous churn.
Ordinary callers should still only ever need ``ExecutionWatchdog``.
"""

from __future__ import annotations

from .blocked import build_blocked_report
from .coordinator import ExecutionWatchdog
from .heartbeat import HeartbeatWriter, build_heartbeat
from .progress import ProgressTracker
from .recovery import LoopGuard, RecoveryEngine
from .stall_detection import ActivityRecord, StallDetector, StallVerdict
from .state_machine import ExecutionStateMachine

__all__ = [
    "ActivityRecord",
    "ExecutionStateMachine",
    "ExecutionWatchdog",
    "HeartbeatWriter",
    "LoopGuard",
    "ProgressTracker",
    "RecoveryEngine",
    "StallDetector",
    "StallVerdict",
    "build_blocked_report",
    "build_heartbeat",
]
