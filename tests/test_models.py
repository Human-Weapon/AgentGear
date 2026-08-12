from __future__ import annotations

import math

import pytest

from agentgear.exceptions import TaskProfileError
from agentgear.models import AgentAssignment, AgentRole, ModelTier, ReasoningEffort, TaskProfile


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


def test_task_profile_is_frozen() -> None:
    tp = TaskProfile(description="x")
    with pytest.raises((AttributeError, TypeError)):
        tp.files_affected = 5  # type: ignore[misc]


def test_task_profile_rejects_nan_and_inf_everywhere() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(TaskProfileError):
            TaskProfile(description="x", ambiguity=bad)
