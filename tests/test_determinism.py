from __future__ import annotations

import agentgear
from agentgear.config import Policy
from agentgear.models import TaskProfile


def _profile() -> TaskProfile:
    return TaskProfile(
        description="Refactor payment processing across modules",
        files_affected=9,
        modules_affected=4,
        architectural_impact=0.4,
        security_impact=0.5,
        ambiguity=0.3,
        novelty=0.2,
        reversibility=0.6,
        existing_test_coverage=0.5,
        prior_failures=1,
    )


def test_same_task_same_policy_yields_identical_plan() -> None:
    policy = Policy.default()
    plan_a = agentgear.plan(_profile(), policy)
    plan_b = agentgear.plan(_profile(), policy)
    assert plan_a == plan_b


def test_same_task_same_policy_from_dict_yields_identical_plan() -> None:
    policy_a = Policy.from_dict({"budget": {"max_agents": 5}})
    policy_b = Policy.from_dict({"budget": {"max_agents": 5}})
    plan_a = agentgear.plan(_profile(), policy_a)
    plan_b = agentgear.plan(_profile(), policy_b)
    assert plan_a == plan_b


def test_different_policy_can_yield_different_plan() -> None:
    cheap = Policy(
        routing_weights=agentgear.RoutingWeights(
            cost_weight=1.0, quality_weight=0.0, latency_weight=0.0
        )
    )
    rich = Policy(
        routing_weights=agentgear.RoutingWeights(
            cost_weight=0.0, quality_weight=1.0, latency_weight=0.0
        )
    )
    plan_cheap = agentgear.plan(_profile(), cheap)
    plan_rich = agentgear.plan(_profile(), rich)
    assert plan_cheap.primary_model.tier.rank <= plan_rich.primary_model.tier.rank


def test_plan_repeated_100_times_is_stable() -> None:
    policy = Policy.default()
    profile = _profile()
    plans = [agentgear.plan(profile, policy) for _ in range(100)]
    assert all(p == plans[0] for p in plans)
