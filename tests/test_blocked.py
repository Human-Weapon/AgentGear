from __future__ import annotations

import pytest

from agentgear.watchdog.blocked import build_blocked_report


def test_blocked_report_has_all_required_fields() -> None:
    report = build_blocked_report(
        blocker="tests keep failing with the same error",
        root_cause="a missing environment variable",
        last_successful_checkpoint=None,
        attempts=5,
        strategies_tried=("re_read_error", "change_approach"),
        evidence=("error: MISSING_ENV_VAR",),
        files_affected=("src/app.py",),
    )
    assert report.blocker
    assert report.root_cause
    assert report.attempts == 5
    assert "re_read_error" in report.strategies_tried
    assert report.recommended_human_action


def test_blocked_report_rejects_negative_attempts() -> None:
    with pytest.raises(ValueError):
        build_blocked_report(
            blocker="x",
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=-1,
            strategies_tried=(),
            evidence=(),
        )


def test_recommendation_mentions_exhaustion_when_human_intervention_tried() -> None:
    report = build_blocked_report(
        blocker="x",
        root_cause="y",
        last_successful_checkpoint=None,
        attempts=10,
        strategies_tried=("re_read_error", "request_human_intervention"),
        evidence=("e",),
    )
    assert "exhausted" in report.recommended_human_action.lower()


def test_recommendation_for_no_strategies_tried_suggests_review() -> None:
    report = build_blocked_report(
        blocker="x",
        root_cause="y",
        last_successful_checkpoint=None,
        attempts=0,
        strategies_tried=(),
        evidence=(),
    )
    assert "human should review" in report.recommended_human_action.lower()
