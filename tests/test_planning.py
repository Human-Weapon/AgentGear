from __future__ import annotations

import pytest

from agentgear.analysis import assess_complexity, assess_risk
from agentgear.config import BudgetPolicy, Policy
from agentgear.exceptions import BudgetExceededError
from agentgear.models import AgentRole, TaskProfile
from agentgear.planning import build_execution_plan


def _plan(profile: TaskProfile, policy: Policy):
    c = assess_complexity(profile)
    r = assess_risk(profile)
    return build_execution_plan(profile, c, r, policy)


def test_trivial_task_gets_single_builder_agent(
    trivial_profile: TaskProfile, policy: Policy
) -> None:
    plan = _plan(trivial_profile, policy)
    assert plan.strategy.agent_count == 1
    assert plan.strategy.execution_order == (AgentRole.BUILDER,)
    assert plan.strategy.judge_required is False
    assert plan.review_required is False


def test_architectural_task_gets_planner(
    architectural_profile: TaskProfile, policy: Policy
) -> None:
    generous = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    plan = _plan(architectural_profile, generous)
    assert AgentRole.PLANNER in plan.strategy.execution_order


def test_ambiguous_task_gets_researchers(ambiguous_profile: TaskProfile) -> None:
    generous = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    plan = _plan(ambiguous_profile, generous)
    researcher = next(a for a in plan.strategy.agents if a.role == AgentRole.RESEARCHER)
    assert researcher.count >= 1


def test_dual_researchers_trigger_judge() -> None:
    very_ambiguous = TaskProfile(
        description="wildly uncertain task",
        files_affected=5,
        ambiguity=0.9,
        novelty=0.8,
    )
    generous = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    plan = _plan(very_ambiguous, generous)
    researcher = next(a for a in plan.strategy.agents if a.role == AgentRole.RESEARCHER)
    assert researcher.count == 2
    assert plan.strategy.judge_required is True


def test_multi_agent_pipeline_always_closes_with_reviewer(
    architectural_profile: TaskProfile,
) -> None:
    generous = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    plan = _plan(architectural_profile, generous)
    assert plan.strategy.execution_order[-1] == AgentRole.REVIEWER
    assert plan.review_required is True


def test_execution_order_follows_researcher_judge_builder_reviewer_pipeline() -> None:
    profile = TaskProfile(
        description="ambiguous risky change", files_affected=6, ambiguity=0.9, security_impact=0.8
    )
    generous = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    plan = _plan(profile, generous)
    order = plan.strategy.execution_order
    if AgentRole.RESEARCHER in order and AgentRole.JUDGE in order:
        assert order.index(AgentRole.RESEARCHER) < order.index(AgentRole.JUDGE)
    assert order.index(AgentRole.BUILDER) < order.index(AgentRole.REVIEWER)


def test_agent_count_exceeding_max_agents_raises_budget_error() -> None:
    profile = TaskProfile(
        description="everything at once",
        files_affected=40,
        modules_affected=15,
        architectural_impact=0.95,
        ambiguity=0.9,
        novelty=0.8,
        security_impact=0.9,
    )
    tight = Policy(
        budget=BudgetPolicy(
            max_agents=2, max_context_budget_tokens=200_000, max_estimated_cost=50.0
        )
    )
    with pytest.raises(BudgetExceededError):
        _plan(profile, tight)


def test_context_budget_exceeding_cap_raises_budget_error() -> None:
    profile = TaskProfile(
        description="huge output", expected_output_tokens=1_000_000, architectural_impact=0.9
    )
    tight = Policy(budget=BudgetPolicy(max_context_budget_tokens=1000))
    with pytest.raises(BudgetExceededError):
        _plan(profile, tight)


def test_cost_exceeding_budget_raises_budget_error() -> None:
    profile = TaskProfile(
        description="frontier-tier everything",
        files_affected=100,
        modules_affected=50,
        architectural_impact=1.0,
        security_impact=1.0,
        data_impact=1.0,
        ambiguity=1.0,
        novelty=1.0,
        reversibility=0.0,
        existing_test_coverage=0.0,
        expected_output_tokens=50_000,
    )
    tight_cost = Policy(
        budget=BudgetPolicy(
            max_agents=8, max_context_budget_tokens=1_000_000, max_estimated_cost=0.0001
        )
    )
    with pytest.raises(BudgetExceededError):
        _plan(profile, tight_cost)


def test_plan_never_returned_when_budget_violated(trivial_profile: TaskProfile) -> None:
    tight = Policy(budget=BudgetPolicy(max_agents=1, max_context_budget_tokens=1))
    with pytest.raises(BudgetExceededError):
        _plan(trivial_profile, tight)


def test_planning_is_deterministic(moderate_profile: TaskProfile, policy: Policy) -> None:
    a = _plan(moderate_profile, policy)
    b = _plan(moderate_profile, policy)
    assert a.primary_model == b.primary_model
    assert a.strategy == b.strategy
    assert a.context_budget_tokens == b.context_budget_tokens
