from __future__ import annotations

import pytest

from agentgear.exceptions import InvalidBlockedReportError
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
    with pytest.raises(InvalidBlockedReportError):
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


# --- AG-09: BLOCKED must require a meaningful, validated report -----------


@pytest.mark.parametrize("blocker", ["", "   "])
def test_blank_blocker_is_rejected(blocker: str) -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker=blocker,
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=0,
            strategies_tried=(),
            evidence=(),
        )


@pytest.mark.parametrize("root_cause", ["", "   "])
def test_blank_root_cause_is_rejected(root_cause: str) -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker="x",
            root_cause=root_cause,
            last_successful_checkpoint=None,
            attempts=0,
            strategies_tried=(),
            evidence=(),
        )


def test_blank_explicit_recommended_action_is_rejected() -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker="x",
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=0,
            strategies_tried=(),
            evidence=(),
            recommended_human_action="   ",
        )


def test_blank_entries_in_strategies_tried_are_rejected() -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker="x",
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=1,
            strategies_tried=("re_read_error", "   "),
            evidence=(),
        )


def test_blank_entries_in_evidence_are_rejected() -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker="x",
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=1,
            strategies_tried=(),
            evidence=("", "real evidence"),
        )


def test_non_int_attempts_is_rejected() -> None:
    with pytest.raises(InvalidBlockedReportError):
        build_blocked_report(
            blocker="x",
            root_cause="y",
            last_successful_checkpoint=None,
            attempts=1.5,  # type: ignore[arg-type]
            strategies_tried=(),
            evidence=(),
        )
