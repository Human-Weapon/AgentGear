from __future__ import annotations

from agentgear.config import WatchdogPolicy
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
    verdict = detector.evaluate(now=100.0, last_progress_at=90.0, recent_activities=[])
    assert verdict.is_stalled is False


def test_disabled_watchdog_never_reports_stalled() -> None:
    detector = StallDetector(_policy(enabled=False))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="a", succeeded=False) for i in range(10)
    ]
    verdict = detector.evaluate(now=1000.0, last_progress_at=0.0, recent_activities=activities)
    assert verdict.is_stalled is False


def test_activity_without_progress_is_stalled_scenario_a() -> None:
    # Scenario A: agent reports activity but produces no genuine progress.
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=70.0 + i, fingerprint=f"tool-{i}", succeeded=True)
        for i in range(4)
    ]
    verdict = detector.evaluate(now=140.0, last_progress_at=10.0, recent_activities=activities)
    assert verdict.is_stalled is True


def test_time_alone_without_attempts_does_not_stall() -> None:
    # Elapsed time exceeds the threshold but there were no attempts logged
    # in that window: must NOT be flagged as stalled on time alone.
    detector = StallDetector(_policy())
    verdict = detector.evaluate(now=200.0, last_progress_at=0.0, recent_activities=[])
    assert verdict.is_stalled is False


def test_many_attempts_but_short_elapsed_time_does_not_stall() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=1.0 + i, fingerprint=f"tool-{i}", succeeded=True)
        for i in range(10)
    ]
    verdict = detector.evaluate(now=5.0, last_progress_at=0.0, recent_activities=activities)
    assert verdict.is_stalled is False


def test_repeated_identical_failures_stall_scenario_b() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=1.0, fingerprint="run-tests", succeeded=False, error="boom"),
        ActivityRecord(at_seconds=2.0, fingerprint="run-tests", succeeded=False, error="boom"),
    ]
    verdict = detector.evaluate(now=3.0, last_progress_at=None, recent_activities=activities)
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
    verdict = detector.evaluate(now=610.0, last_progress_at=None, recent_activities=activities)
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
    verdict = detector.evaluate(now=310.0, last_progress_at=None, recent_activities=activities)
    assert not any("trivial" in r for r in verdict.reasons)


def test_circular_attempts_without_new_evidence_scenario_g() -> None:
    detector = StallDetector(_policy())
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="same-analysis", succeeded=True)
        for i in range(4)
    ]
    verdict = detector.evaluate(now=10.0, last_progress_at=None, recent_activities=activities)
    assert verdict.is_stalled is True
    assert any("circular" in r for r in verdict.reasons)
