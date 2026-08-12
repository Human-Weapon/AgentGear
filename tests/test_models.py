from __future__ import annotations

import math

import pytest

from agentgear.exceptions import TaskProfileError
from agentgear.models import (
    AgentAssignment,
    AgentRole,
    ComplexityAssessment,
    ComplexityLevel,
    ModelTier,
    ReasoningEffort,
    RiskAssessment,
    RiskLevel,
    TaskProfile,
)


def test_task_profile_defaults_are_valid() -> None:
    tp = TaskProfile(description="do a thing")
    assert tp.files_affected == 1
    assert tp.reversibility == 1.0


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("architectural_impact", -0.1),
        ("architectural_impact", 1.1),
        ("security_impact", 2.0),
        ("ambiguity", -1.0),
        ("novelty", float("nan")),
        ("reversibility", float("inf")),
        ("existing_test_coverage", -0.5),
    ],
)
def test_task_profile_rejects_out_of_range_floats(field_name: str, value: float) -> None:
    kwargs = {"description": "x", field_name: value}
    with pytest.raises(TaskProfileError):
        TaskProfile(**kwargs)


def test_task_profile_rejects_empty_description() -> None:
    with pytest.raises(TaskProfileError):
        TaskProfile(description="   ")


@pytest.mark.parametrize("field_name", ["files_affected", "modules_affected", "prior_failures"])
def test_task_profile_rejects_negative_ints(field_name: str) -> None:
    with pytest.raises(TaskProfileError):
        TaskProfile(description="x", **{field_name: -1})


def test_task_profile_rejects_bool_for_numeric_field() -> None:
    with pytest.raises(TaskProfileError):
        TaskProfile(description="x", files_affected=True)


def test_model_tier_rank_is_ordered() -> None:
    assert ModelTier.FAST.rank < ModelTier.STANDARD.rank < ModelTier.ADVANCED.rank
    assert ModelTier.ADVANCED.rank < ModelTier.FRONTIER.rank


def test_reasoning_effort_rank_is_ordered() -> None:
    ranks = [r.rank for r in ReasoningEffort]
    assert ranks == sorted(ranks)


def test_agent_assignment_rejects_zero_count() -> None:
    with pytest.raises(TaskProfileError):
        AgentAssignment(
            role=AgentRole.BUILDER, tier=ModelTier.FAST, reasoning=ReasoningEffort.LOW, count=0
        )


@pytest.mark.parametrize("bad_count", [True, False, 1.5, 2.0, "2", None, -1])
def test_agent_assignment_rejects_non_strict_positive_int_count(bad_count) -> None:
    """Round 2 / H1: bool is a subclass of int in Python, so count=True
    must not silently pass as count=1, and count must not accept
    floats/strings/None even when numerically plausible."""
    with pytest.raises(TaskProfileError):
        AgentAssignment(
            role=AgentRole.BUILDER,
            tier=ModelTier.FAST,
            reasoning=ReasoningEffort.LOW,
            count=bad_count,
        )


@pytest.mark.parametrize("good_count", [1, 2, 10])
def test_agent_assignment_accepts_strict_positive_int_count(good_count: int) -> None:
    assignment = AgentAssignment(
        role=AgentRole.BUILDER,
        tier=ModelTier.FAST,
        reasoning=ReasoningEffort.LOW,
        count=good_count,
    )
    assert assignment.count == good_count


def test_agent_assignment_rejects_wrong_enum_types() -> None:
    with pytest.raises(TaskProfileError):
        AgentAssignment(role="builder", tier=ModelTier.FAST, reasoning=ReasoningEffort.LOW)  # type: ignore[arg-type]
    with pytest.raises(TaskProfileError):
        AgentAssignment(role=AgentRole.BUILDER, tier="fast", reasoning=ReasoningEffort.LOW)  # type: ignore[arg-type]
    with pytest.raises(TaskProfileError):
        AgentAssignment(role=AgentRole.BUILDER, tier=ModelTier.FAST, reasoning="low")  # type: ignore[arg-type]


def test_task_profile_is_frozen() -> None:
    tp = TaskProfile(description="x")
    with pytest.raises((AttributeError, TypeError)):
        tp.files_affected = 5  # type: ignore[misc]


def test_task_profile_rejects_nan_and_inf_everywhere() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(TaskProfileError):
            TaskProfile(description="x", ambiguity=bad)


# --- Round 2 / M7: ComplexityAssessment / RiskAssessment score validation --


@pytest.mark.parametrize(
    "bad_score",
    [-0.1, 1.1, 2.5, math.nan, math.inf, -math.inf, True, False, "0.5"],
)
def test_complexity_assessment_rejects_impossible_scores(bad_score) -> None:
    """Round 2 / M7: score feeds routing directly, so it must be a strict
    finite [0, 1] float — not out-of-range, not NaN/Infinity, and not a
    bool (bool is a subclass of int in Python) or numeric-looking string."""
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(score=bad_score, level=ComplexityLevel.MODERATE)


@pytest.mark.parametrize(
    "bad_score",
    [-0.1, 1.1, 2.5, math.nan, math.inf, -math.inf, True, False, "0.5"],
)
def test_risk_assessment_rejects_impossible_scores(bad_score) -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=bad_score, level=RiskLevel.MODERATE)


@pytest.mark.parametrize("good_score", [0.0, 0.5, 1.0])
def test_complexity_assessment_accepts_valid_scores(good_score: float) -> None:
    assessment = ComplexityAssessment(score=good_score, level=ComplexityLevel.MODERATE)
    assert assessment.score == good_score


@pytest.mark.parametrize("good_score", [0.0, 0.5, 1.0])
def test_risk_assessment_accepts_valid_scores(good_score: float) -> None:
    assessment = RiskAssessment(score=good_score, level=RiskLevel.MODERATE)
    assert assessment.score == good_score


def test_complexity_assessment_rejects_wrong_level_type() -> None:
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(score=0.5, level="moderate")  # type: ignore[arg-type]
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(score=0.5, level=RiskLevel.MODERATE)  # type: ignore[arg-type]


def test_risk_assessment_rejects_wrong_level_type() -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level="moderate")  # type: ignore[arg-type]
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=ComplexityLevel.MODERATE)  # type: ignore[arg-type]
