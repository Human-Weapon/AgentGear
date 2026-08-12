from __future__ import annotations

from agentgear.config import Policy, RoutingWeights
from agentgear.models import (
    ComplexityAssessment,
    ComplexityLevel,
    ModelTier,
    ReasoningEffort,
    RiskAssessment,
    RiskLevel,
)
from agentgear.routing import route, select_model_tier, select_reasoning_effort


def _assessment(score: float, factors: dict | None = None) -> ComplexityAssessment:
    return ComplexityAssessment(score=score, level=ComplexityLevel.MODERATE, factors=factors or {})


def _risk(score: float) -> RiskAssessment:
    return RiskAssessment(score=score, level=RiskLevel.MODERATE)


def test_trivial_task_routes_to_fast_low(policy) -> None:
    tier, _ = select_model_tier(_assessment(0.0), _risk(0.0), policy)
    reasoning, _ = select_reasoning_effort(_assessment(0.0), _risk(0.0), policy)
    assert tier == ModelTier.FAST
    assert reasoning in (ReasoningEffort.NONE, ReasoningEffort.LOW)


def test_high_complexity_high_risk_routes_to_frontier(policy) -> None:
    tier, _ = select_model_tier(_assessment(0.95), _risk(0.95), policy)
    assert tier == ModelTier.FRONTIER


def test_critical_risk_forces_minimum_advanced_even_if_complexity_low(policy) -> None:
    tier, rationale = select_model_tier(_assessment(0.05), _risk(0.9), policy)
    assert tier.rank >= ModelTier.ADVANCED.rank
    assert any("critical risk override" in r for r in rationale)


def test_tier_and_reasoning_are_independent_dimensions(policy) -> None:
    # Two very different (complexity, risk) pairs can land on the same tier
    # while requiring different reasoning effort, proving the dimensions
    # are not silently coupled (e.g. "tier X reasoning Y" != "tier Z reasoning Y").
    tier_a, _ = select_model_tier(_assessment(0.3), _risk(0.3), policy)
    reasoning_a, _ = select_reasoning_effort(_assessment(0.3), _risk(0.3), policy)
    tier_b, _ = select_model_tier(_assessment(0.05), _risk(0.55), policy)
    reasoning_b, _ = select_reasoning_effort(_assessment(0.05), _risk(0.55), policy)
    # risk-heavy low-complexity task should demand more reasoning per token
    # of complexity than a balanced one, even at a similar/lower tier.
    assert (tier_a, reasoning_a) != (tier_b, reasoning_b)


def test_cost_dominant_weights_favor_cheaper_tier() -> None:
    cost_heavy = Policy(
        routing_weights=RoutingWeights(cost_weight=0.9, quality_weight=0.1, latency_weight=0.0)
    )
    quality_heavy = Policy(
        routing_weights=RoutingWeights(cost_weight=0.1, quality_weight=0.9, latency_weight=0.0)
    )
    score = _assessment(0.5)
    risk = _risk(0.5)
    cheap_tier, _ = select_model_tier(score, risk, cost_heavy)
    rich_tier, _ = select_model_tier(score, risk, quality_heavy)
    assert cheap_tier.rank <= rich_tier.rank


def test_router_never_defaults_to_frontier_for_trivial_task(policy) -> None:
    tp_complexity = _assessment(0.02)
    tp_risk = _risk(0.02)
    tier, _ = select_model_tier(tp_complexity, tp_risk, policy)
    assert tier == ModelTier.FAST


def test_route_resolves_model_name_from_policy_mapping(policy) -> None:
    profile_complexity = _assessment(0.0)
    profile_risk = _risk(0.0)
    model = route(profile_complexity, profile_risk, policy)
    assert model.resolved_model == policy.model_tier_mapping.resolve(model.tier)


def test_routing_is_deterministic(policy) -> None:
    a = route(_assessment(0.42), _risk(0.31), policy)
    b = route(_assessment(0.42), _risk(0.31), policy)
    assert a == b


def test_reasoning_thresholds_reach_max_and_none() -> None:
    p = Policy(default_reasoning_floor=ReasoningEffort.NONE)
    none_effort, _ = select_reasoning_effort(_assessment(0.0), _risk(0.0), p)
    max_effort, _ = select_reasoning_effort(_assessment(1.0), _risk(1.0), p)
    assert none_effort == ReasoningEffort.NONE
    assert max_effort == ReasoningEffort.MAX


def test_default_reasoning_floor_is_respected() -> None:
    p = Policy(default_reasoning_floor=ReasoningEffort.MEDIUM)
    effort, _ = select_reasoning_effort(_assessment(0.0), _risk(0.0), p)
    assert effort.rank >= ReasoningEffort.MEDIUM.rank
