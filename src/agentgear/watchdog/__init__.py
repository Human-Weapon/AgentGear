"""Execution Watchdog: state machine, progress detection, stall detection,
bounded recovery, and structured BLOCKED reporting.

This is the OBLIGATORY core feature described in AgentGear's design:
NEVER STOP SILENTLY. An execution is always in one explicit
``ExecutionState``; it never just "goes quiet" and gets assumed done.
"""

from __future__ import annotations

from .blocked import build_blocked_report
from .heartbeat import HeartbeatWriter, build_heartbeat
from .progress import ProgressTracker
from .recovery import LoopGuard, RecoveryEngine
from .stall_detection import ActivityRecord, StallDetector, StallVerdict
from .state_machine import ExecutionStateMachine

__all__ = [
    "ActivityRecord",
    "ExecutionStateMachine",
    "HeartbeatWriter",
    "LoopGuard",
    "ProgressTracker",
    "RecoveryEngine",
    "StallDetector",
    "StallVerdict",
    "build_blocked_report",
    "build_heartbeat",
]
