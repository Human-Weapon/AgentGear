from __future__ import annotations

from agentgear.models import ProgressEvent, ProgressSignalKind
from agentgear.watchdog.progress import ProgressTracker


def test_empty_tracker_has_no_last_progress() -> None:
    tracker = ProgressTracker()
    assert tracker.last_progress_at is None
    assert tracker.seconds_since_last_progress(100.0) is None


def test_record_updates_last_progress_at() -> None:
    tracker = ProgressTracker()
    tracker.record(
        ProgressEvent(kind=ProgressSignalKind.FILE_CHANGED, description="x", at_seconds=10.0)
    )
    assert tracker.last_progress_at == 10.0
    assert tracker.seconds_since_last_progress(15.0) == 5.0


def test_seconds_since_last_progress_never_negative() -> None:
    tracker = ProgressTracker()
    tracker.record(
        ProgressEvent(
            kind=ProgressSignalKind.TEST_STATUS_IMPROVED, description="x", at_seconds=50.0
        )
    )
    assert tracker.seconds_since_last_progress(10.0) == 0.0


def test_has_progress_since_only_counts_later_events() -> None:
    tracker = ProgressTracker()
    tracker.record(
        ProgressEvent(kind=ProgressSignalKind.DECISION_PRODUCED, description="x", at_seconds=5.0)
    )
    assert tracker.has_progress_since(1.0) is True
    assert tracker.has_progress_since(10.0) is False
