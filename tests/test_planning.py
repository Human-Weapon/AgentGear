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


# --- AG-03: critical individual risk signals must not under-route ---------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"security_impact": 1.0},
        {"data_impact": 1.0},
        {"reversibility": 0.0},  # irreversibility = 1.0
    ],
)
def test_ag03_maximal_individual_risk_signal_forces_review_and_floor(
    kwargs: dict, policy: Policy
) -> None:
    profile = TaskProfile(description="otherwise trivial task", **kwargs)
    plan = _plan(profile, policy)

    assert plan.strategy.agent_count > 1, "must not remain a lone Builder"
    assert plan.review_required is True, "must not skip review"
    assert plan.primary_model.tier.rank >= policy.critical_risk.min_tier.rank
    assert plan.primary_model.reasoning.rank >= policy.critical_risk.min_reasoning.rank


def test_ag03_critical_review_can_be_disabled_via_policy() -> None:
    from agentgear.config import CriticalRiskPolicy

    lenient = Policy(critical_risk=CriticalRiskPolicy(require_review=False))
    profile = TaskProfile(description="otherwise trivial task", security_impact=1.0)
    plan = _plan(profile, lenient)
    # Tier/reasoning floor still applies (routing-level), but planning no
    # longer forces multi-agent staffing purely because of it.
    assert plan.primary_model.tier.rank >= lenient.critical_risk.min_tier.rank
    assert plan.strategy.agent_count == 1


def test_ag03_critical_thresholds_are_configurable() -> None:
    from agentgear.config import CriticalRiskPolicy

    strict = Policy(critical_risk=CriticalRiskPolicy(security_impact_at=0.4))
    profile = TaskProfile(description="mild security task", security_impact=0.5)
    plan = _plan(profile, strict)
    assert plan.primary_model.tier.rank >= strict.critical_risk.min_tier.rank
    assert plan.review_required is True


# --- Round 2 / L2: individual factor-dict keys must be correctly wired ----


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ambiguity": 0.6},
        {"architectural_impact": 0.6},
        {"prior_failures": 5},
    ],
)
def test_l2_individual_factor_key_forces_multi_agent_below_blended_thresholds(
    kwargs: dict, policy: Policy
) -> None:
    """``build_execution_strategy`` reads
    ``complexity.factors.get("ambiguity"/"architectural_impact", 0.0)`` and
    ``risk.factors.get("prior_failures", 0.0)`` -- a typo'd key would
    silently fall back to that 0.0 default instead of raising, so the
    individual-signal trigger would vanish without error. Each kwarg here
    isolates exactly ONE factor high enough to cross its own threshold
    while both BLENDED complexity and risk scores stay under their own
    multi-agent thresholds, so ``agent_count > 1`` can only be explained by
    the individually-keyed factor, not by the blended-score path.
    """
    profile = TaskProfile(description="isolated single-factor signal", **kwargs)
    c = assess_complexity(profile)
    r = assess_risk(profile)
    assert c.score < policy.multi_agent_complexity_threshold
    assert r.score < policy.multi_agent_risk_threshold

    plan = build_execution_plan(profile, c, r, policy)
    assert plan.strategy.agent_count > 1
