from __future__ import annotations

import pytest

from agentgear.exceptions import InvalidBlockedReportError
from agentgear.models import BlockedReport, Checkpoint
from agentgear.watchdog.blocked import build_blocked_report


def _report_kwargs(**overrides) -> dict:
    base = dict(
        blocker="x",
        root_cause="y",
        last_successful_checkpoint=None,
        attempts=1,
        strategies_tried=("re_read_error",),
        evidence=("some evidence",),
        files_affected=("src/app.py",),
        recommended_human_action="review it",
    )
    base.update(overrides)
    return base


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


# --- Round 2 / M9: BlockedReport itself validates, not only the builder ---


def test_blocked_report_direct_construction_accepts_well_formed_data() -> None:
    report = BlockedReport(**_report_kwargs())
    assert report.blocker == "x"
    assert report.attempts == 1


@pytest.mark.parametrize("blocker", ["", "   "])
def test_blocked_report_direct_construction_rejects_blank_blocker(blocker: str) -> None:
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(blocker=blocker))


@pytest.mark.parametrize("root_cause", ["", "   "])
def test_blocked_report_direct_construction_rejects_blank_root_cause(root_cause: str) -> None:
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(root_cause=root_cause))


@pytest.mark.parametrize("bad_attempts", [True, False, 1.5, "1", None, -1])
def test_blocked_report_direct_construction_rejects_bad_attempts(bad_attempts) -> None:
    """bool is a subclass of int in Python -- attempts=True must not
    silently pass as attempts=1."""
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(attempts=bad_attempts))


@pytest.mark.parametrize("field_name", ["strategies_tried", "evidence", "files_affected"])
@pytest.mark.parametrize("bad_value", ["not-a-tuple", ["a", "list"], None, ("ok", ""), ("ok", 42)])
def test_blocked_report_direct_construction_rejects_bad_collection_fields(
    field_name: str, bad_value
) -> None:
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(**{field_name: bad_value}))


@pytest.mark.parametrize("field_name", ["strategies_tried", "evidence", "files_affected"])
def test_blocked_report_direct_construction_accepts_empty_collection_fields(
    field_name: str,
) -> None:
    """An empty tuple must remain valid: e.g. no strategies were tried when
    the very first stall immediately exhausted a max_recovery_attempts=0-
    style policy, or no files were touched before getting stuck."""
    report = BlockedReport(**_report_kwargs(**{field_name: ()}))
    assert getattr(report, field_name) == ()


def test_blocked_report_direct_construction_rejects_blank_recommended_action() -> None:
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(recommended_human_action="   "))


def test_blocked_report_direct_construction_rejects_wrong_checkpoint_type() -> None:
    with pytest.raises(InvalidBlockedReportError):
        BlockedReport(**_report_kwargs(last_successful_checkpoint="not-a-checkpoint"))


def test_blocked_report_direct_construction_accepts_real_checkpoint() -> None:
    cp = Checkpoint(execution_id="e1", phase="build", at_seconds=0.0)
    report = BlockedReport(**_report_kwargs(last_successful_checkpoint=cp))
    assert report.last_successful_checkpoint is cp


def test_blocked_report_attempts_need_not_equal_strategies_tried_length() -> None:
    """A strategy can be retried, so attempts and len(strategies_tried) are
    NOT required to match -- enforcing that would be a false invariant."""
    report = BlockedReport(**_report_kwargs(attempts=5, strategies_tried=("re_read_error",)))
    assert report.attempts == 5
    assert len(report.strategies_tried) == 1
