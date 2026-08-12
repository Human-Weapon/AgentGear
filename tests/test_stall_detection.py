from __future__ import annotations

import math

import pytest

from agentgear.config import WatchdogPolicy
from agentgear.exceptions import InvalidObservationError
from agentgear.watchdog.stall_detection import ActivityRecord, StallDetector


def _policy(**overrides) -> WatchdogPolicy:
    defaults = dict(
        no_progress_seconds=60.0,
        no_progress_cycles=3,
        max_identical_failures=2,
        trivial_command_timeout_seconds=5.0,
    )
    defaults.update(overrides)
    return WatchdogPolicy(**defaults)


def test_no_activity_is_not_stalled() -> None:
    detector = StallDetector(_policy())
    verdict = detector.evaluate(
        now=100.0, started_at=0.0, last_progress_at=90.0, recent_activities=[]
    )
    assert verdict.is_stalled is False


def test_disabled_watchdog_never_reports_stalled() -> None:
    detector = StallDetector(_policy(enabled=False))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="a", succeeded=False) for i in range(10)
    ]
    verdict = detector.evaluate(
        now=1000.0, started_at=0.0, last_progress_at=0.0, recent_activities=activities
    )
    assert verdict.is_stalled is False


def test_activity_without_progress_is_stalled_scenario_a() -> None:
    # Scenario A: agent reports activity but produces no genuine progress.
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=70.0 + i, fingerprint=f"tool-{i}", succeeded=True)
        for i in range(4)
    ]
    verdict = detector.evaluate(
        now=140.0, started_at=0.0, last_progress_at=10.0, recent_activities=activities
    )
    assert verdict.is_stalled is True


def test_time_alone_without_attempts_does_not_stall() -> None:
    # Elapsed time exceeds the threshold but there were no attempts logged
    # in that window: must NOT be flagged as stalled on time alone.
    detector = StallDetector(_policy())
    verdict = detector.evaluate(
        now=200.0, started_at=0.0, last_progress_at=0.0, recent_activities=[]
    )
    assert verdict.is_stalled is False


def test_many_attempts_but_short_elapsed_time_does_not_stall() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=1.0 + i, fingerprint=f"tool-{i}", succeeded=True)
        for i in range(10)
    ]
    verdict = detector.evaluate(
        now=5.0, started_at=0.0, last_progress_at=0.0, recent_activities=activities
    )
    assert verdict.is_stalled is False


def test_repeated_identical_failures_stall_scenario_b() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=1.0, fingerprint="run-tests", succeeded=False, error="boom"),
        ActivityRecord(at_seconds=2.0, fingerprint="run-tests", succeeded=False, error="boom"),
    ]
    verdict = detector.evaluate(
        now=3.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is True
    assert any("identical failures" in r for r in verdict.reasons)


def test_trivial_command_abnormally_slow_repeatedly_scenario_f() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(
            at_seconds=1.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        ),
        ActivityRecord(
            at_seconds=310.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=290.0,
        ),
    ]
    verdict = detector.evaluate(
        now=610.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is True
    assert any("trivial" in r for r in verdict.reasons)


def test_single_slow_trivial_command_is_not_enough() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(
            at_seconds=1.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        ),
    ]
    verdict = detector.evaluate(
        now=310.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert not any("trivial" in r for r in verdict.reasons)


def test_circular_attempts_without_new_evidence_scenario_g() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="same-analysis", succeeded=True)
        for i in range(4)
    ]
    verdict = detector.evaluate(
        now=10.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is True
    assert any("circular" in r for r in verdict.reasons)


# --- AG-01: first-progress stall detection --------------------------------


def test_ag01_never_made_progress_but_busy_forever_is_stalled() -> None:
    """100 unique successful activities, zero progress events, sufficient
    elapsed time since the execution *started* (not since progress, since
    there has been none) must still reach STALLED. Every fingerprint is
    unique, so no other signal (identical failure, circular, trivial-slow)
    can catch this — only the started_at boundary can.
    """
    detector = StallDetector(_policy(no_progress_seconds=60.0, no_progress_cycles=3))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint=f"unique-{i}", succeeded=True)
        for i in range(100)
    ]
    verdict = detector.evaluate(
        now=500.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is True
    assert any("no progress" in r for r in verdict.reasons)


def test_ag01_never_made_progress_but_not_enough_elapsed_time_is_not_stalled() -> None:
    detector = StallDetector(_policy(no_progress_seconds=600.0, no_progress_cycles=3))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint=f"unique-{i}", succeeded=True)
        for i in range(100)
    ]
    verdict = detector.evaluate(
        now=50.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is False


def test_ag01_never_made_progress_but_not_enough_attempts_is_not_stalled() -> None:
    detector = StallDetector(
        _policy(no_progress_seconds=10.0, no_progress_cycles=50, max_no_progress_cycles=50)
    )
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint=f"unique-{i}", succeeded=True)
        for i in range(5)
    ]
    verdict = detector.evaluate(
        now=500.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is False


# --- AG-02: progress boundary resets all stall signals ---------------------


def test_ag02_circular_fingerprint_before_progress_does_not_leak_after() -> None:
    """A A A -> REAL PROGRESS -> A must NOT immediately become circular."""
    detector = StallDetector(_policy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=1.0, fingerprint="A", succeeded=True),
        ActivityRecord(at_seconds=2.0, fingerprint="A", succeeded=True),
        ActivityRecord(at_seconds=3.0, fingerprint="A", succeeded=True),
        # progress happens at t=4.0
        ActivityRecord(at_seconds=5.0, fingerprint="A", succeeded=True),
    ]
    verdict = detector.evaluate(
        now=6.0, started_at=0.0, last_progress_at=4.0, recent_activities=activities
    )
    assert not any("circular" in r for r in verdict.reasons)


def test_ag02_trivial_slow_before_progress_does_not_leak_after() -> None:
    """slow slow -> progress -> one slow must not trigger the trivial-slow
    signal (which requires >= 2 slow commands in the current window)."""
    detector = StallDetector(_policy(trivial_command_timeout_seconds=5.0))
    activities = [
        ActivityRecord(
            at_seconds=1.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        ),
        ActivityRecord(
            at_seconds=2.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        ),
        # progress happens at t=3.0
        ActivityRecord(
            at_seconds=4.0,
            fingerprint="print",
            succeeded=True,
            is_trivial=True,
            duration_seconds=300.0,
        ),
    ]
    verdict = detector.evaluate(
        now=5.0, started_at=0.0, last_progress_at=3.0, recent_activities=activities
    )
    assert not any("trivial" in r for r in verdict.reasons)


def test_ag02_repeated_failure_before_progress_does_not_leak_after() -> None:
    """failure failure -> progress -> one failure must not trigger the
    identical-failure signal (which requires >= 2 trailing failures)."""
    detector = StallDetector(_policy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=1.0, fingerprint="run-tests", succeeded=False),
        ActivityRecord(at_seconds=2.0, fingerprint="run-tests", succeeded=False),
        # progress happens at t=3.0
        ActivityRecord(at_seconds=4.0, fingerprint="run-tests", succeeded=False),
    ]
    verdict = detector.evaluate(
        now=5.0, started_at=0.0, last_progress_at=3.0, recent_activities=activities
    )
    assert not any("identical failures" in r for r in verdict.reasons)


def test_ag02_no_progress_attempt_count_only_counts_after_boundary() -> None:
    detector = StallDetector(_policy(no_progress_seconds=1.0, no_progress_cycles=3))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint=f"x-{i}", succeeded=True) for i in range(20)
    ]
    # progress happens right before `now`, at t=19.5; only ONE activity
    # (t=19.0 is before the boundary, nothing is after it) exists in the
    # post-progress window, so the attempt-count trigger must not fire.
    verdict = detector.evaluate(
        now=20.0, started_at=0.0, last_progress_at=19.5, recent_activities=activities
    )
    assert not any("no progress" in r for r in verdict.reasons)


# --- Round 2 / M8/L8: timestamp boundary contract must match progress.py --


def test_m8_activity_exactly_at_progress_boundary_is_excluded() -> None:
    """``ProgressTracker.events_since`` (progress.py) treats an event AT
    the boundary timestamp T as NOT new (``at_seconds > T``). Stall
    detection must apply the identical contract to raw activity, or a
    stale failure at exactly the progress boundary gets resurrected as
    "since progress" the instant progress resets the clock, silently
    weakening AG-02. Only ONE genuinely new failure exists strictly after
    T=3.0, so with max_identical_failures=2 this must NOT stall."""
    detector = StallDetector(_policy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=3.0, fingerprint="run-tests", succeeded=False),  # AT boundary
        ActivityRecord(at_seconds=4.0, fingerprint="run-tests", succeeded=False),  # after boundary
    ]
    verdict = detector.evaluate(
        now=5.0, started_at=0.0, last_progress_at=3.0, recent_activities=activities
    )
    assert not any("identical failures" in r for r in verdict.reasons)


def test_m8_activity_strictly_after_progress_boundary_still_counts() -> None:
    """The exact-boundary exclusion must not overreach: two genuinely new
    failures strictly after the boundary must still stall."""
    detector = StallDetector(_policy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=3.0, fingerprint="run-tests", succeeded=False),  # AT boundary
        ActivityRecord(at_seconds=4.0, fingerprint="run-tests", succeeded=False),
        ActivityRecord(at_seconds=5.0, fingerprint="run-tests", succeeded=False),
    ]
    verdict = detector.evaluate(
        now=6.0, started_at=0.0, last_progress_at=3.0, recent_activities=activities
    )
    assert any("identical failures" in r for r in verdict.reasons)


def test_m8_boundary_contract_holds_for_sub_second_floats() -> None:
    detector = StallDetector(_policy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=3.123456, fingerprint="run-tests", succeeded=False),
        ActivityRecord(at_seconds=3.123457, fingerprint="run-tests", succeeded=False),
    ]
    verdict = detector.evaluate(
        now=5.0, started_at=0.0, last_progress_at=3.123456, recent_activities=activities
    )
    assert not any("identical failures" in r for r in verdict.reasons)


# --- AG-06: ActivityRecord must not accept bogus/invalid signals -----------


@pytest.mark.parametrize("at_seconds", [-1.0, math.nan, math.inf])
def test_activity_record_rejects_invalid_timestamp(at_seconds: float) -> None:
    with pytest.raises(InvalidObservationError):
        ActivityRecord(at_seconds=at_seconds, fingerprint="x", succeeded=True)


@pytest.mark.parametrize("fingerprint", ["", "   "])
def test_activity_record_rejects_blank_fingerprint(fingerprint: str) -> None:
    with pytest.raises(InvalidObservationError):
        ActivityRecord(at_seconds=1.0, fingerprint=fingerprint, succeeded=True)


def test_activity_record_rejects_negative_duration() -> None:
    with pytest.raises(InvalidObservationError):
        ActivityRecord(at_seconds=1.0, fingerprint="x", succeeded=True, duration_seconds=-1.0)
