"""Model tier routing and reasoning-effort routing.

Two independent decisions, each explainable and deterministic:

* **Tier routing** answers "which capability tier" (FAST/STANDARD/ADVANCED/
  FRONTIER), a provider-agnostic concept mapped to real models only via
  ``Policy.model_tier_mapping``. No model name is ever hardcoded here.
* **Reasoning routing** answers "how much thinking", using a *different*
  score blend and a *different* threshold set than tier routing, so tier
  and reasoning never collapse into one hidden dimension (e.g. "Luna
  high" is never assumed equal to "Terra low").

Cost-awareness is implemented as threshold selection: pick the cheapest
tier whose threshold the combined score clears, then shift thresholds by
the configured cost/quality weighting. This directly encodes "use the
minimum amount of intelligence and compute capable of meeting the
required quality level" (principle #6) without ever defaulting to the
most powerful tier.
"""

from __future__ import annotations

from .config import Policy
from .models import ComplexityAssessment, ModelProfile, ModelTier, ReasoningEffort, RiskAssessment

# Relative compute-cost units per 1000 tokens, ordered FAST < FRONTIER.
# These are NOT real provider prices; they exist so budget checks and
# escalation cost pressure have a deterministic, provider-agnostic proxy.
RELATIVE_COST_PER_1K_TOKENS: dict[ModelTier, float] = {
    ModelTier.FAST: 0.001,
    ModelTier.STANDARD: 0.004,
    ModelTier.ADVANCED: 0.018,
    ModelTier.FRONTIER: 0.070,
}

# A risk score at or above this level forces at least ADVANCED tier and a
# review requirement, regardless of how "simple" the task otherwise looks
# (principle: security/architectural risk cannot be routed around cheaply).
_CRITICAL_RISK_OVERRIDE = 0.85
_CRITICAL_RISK_MIN_TIER = ModelTier.ADVANCED

# Weight-bias shift bounds applied to routing thresholds. Kept small and
# symmetric so cost/quality weighting nudges the boundary rather than
# reordering the tier ladder.
_MAX_THRESHOLD_SHIFT = 0.15


def combined_tier_score(complexity: ComplexityAssessment, risk: RiskAssessment) -> float:
    """Score used for tier selection: equal weight complexity vs. risk."""
    return max(0.0, min(1.0, 0.5 * complexity.score + 0.5 * risk.score))


def reasoning_score(complexity: ComplexityAssessment, risk: RiskAssessment) -> float:
    """Score used for reasoning-effort selection.

    Weighted toward risk (0.65) over complexity (0.35): a low-complexity
    but high-risk change (e.g. a one-line auth check) still deserves
    careful reasoning even on a cheap model tier.
    """
    return max(0.0, min(1.0, 0.35 * complexity.score + 0.65 * risk.score))


def critical_signal_reasons(risk: RiskAssessment, policy: Policy) -> tuple[str, ...]:
    """Individual-signal critical-risk triggers (AG-03), independent of the
    blended risk score. A single maxed-out signal (e.g.
    ``security_impact=1.0`` on an otherwise trivial task) must not be
    diluted into a merely "low" blended score and routed cheaply.

    Round 2 / L1: ``select_model_tier`` applies TWO deliberately separate
    overrides -- this per-signal one, and a blended-score override
    (``risk.score >= _CRITICAL_RISK_OVERRIDE``) a few lines below its call
    site. They are not redundant: the blended override catches "risk is
    high overall" even when no single factor is individually extreme,
    while this one catches "one factor is extreme" even when the blend
    dilutes it below any sane blended threshold. Merging them into one
    check would reintroduce the exact dilution bug AG-03 exists to
    prevent, so this is defense-in-depth, not accidental duplication.
    """
    cr = policy.critical_risk
    security = risk.factors.get("security_impact", 0.0)
    data = risk.factors.get("data_impact", 0.0)
    irreversibility = risk.factors.get("irreversibility", 0.0)

    reasons: list[str] = []
    if security >= cr.security_impact_at:
        reasons.append(f"security_impact={security:.2f} >= {cr.security_impact_at:.2f}")
    if data >= cr.data_impact_at:
        reasons.append(f"data_impact={data:.2f} >= {cr.data_impact_at:.2f}")
    if irreversibility >= cr.irreversibility_at:
        reasons.append(f"irreversibility={irreversibility:.2f} >= {cr.irreversibility_at:.2f}")
    return tuple(reasons)


def _threshold_shift(policy: Policy) -> float:
    """Cost AND latency both push toward cheaper/faster tiers; quality
    pushes toward richer ones. Higher tiers cost more compute per call,
    and in this provider-agnostic model that compute cost is treated as
    the proxy for latency too (a FRONTIER call is assumed slower than a
    FAST one) — so ``latency_weight`` shares cost's sign in the bias
    rather than being a third, disconnected axis. This keeps the shift
    formula a single documented, deterministic number instead of two
    weights that could point in unrelated directions with no combined
    meaning.
    """
    cw, qw, lw = policy.routing_weights.normalized()
    bias = (cw + lw) - qw  # positive => raise thresholds => cheaper/faster tiers favored
    return bias * _MAX_THRESHOLD_SHIFT


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def select_model_tier(
    complexity: ComplexityAssessment,
    risk: RiskAssessment,
    policy: Policy,
) -> tuple[ModelTier, tuple[str, ...]]:
    """Select the cheapest sufficient model tier. Returns (tier, rationale)."""
    score = combined_tier_score(complexity, risk)
    shift = _threshold_shift(policy)
    t = policy.routing_thresholds
    standard_at = _clip(t.standard_at + shift)
    advanced_at = _clip(t.advanced_at + shift)
    frontier_at = _clip(t.frontier_at + shift)

    if score >= frontier_at:
        tier = ModelTier.FRONTIER
    elif score >= advanced_at:
        tier = ModelTier.ADVANCED
    elif score >= standard_at:
        tier = ModelTier.STANDARD
    else:
        tier = ModelTier.FAST

    rationale = [
        f"combined_tier_score={score:.2f} "
        f"(complexity={complexity.score:.2f}, risk={risk.score:.2f})",
        f"thresholds after cost/quality weighting: standard>={standard_at:.2f}, "
        f"advanced>={advanced_at:.2f}, frontier>={frontier_at:.2f} (shift={shift:+.2f})",
        f"selected tier={tier.value} as the cheapest tier whose threshold is cleared",
    ]

    if risk.score >= _CRITICAL_RISK_OVERRIDE and tier.rank < _CRITICAL_RISK_MIN_TIER.rank:
        rationale.append(
            f"critical risk override: risk={risk.score:.2f} >= {_CRITICAL_RISK_OVERRIDE} "
            f"forces minimum tier {_CRITICAL_RISK_MIN_TIER.value}"
        )
        tier = _CRITICAL_RISK_MIN_TIER

    signal_reasons = critical_signal_reasons(risk, policy)
    min_tier = policy.critical_risk.min_tier
    if signal_reasons and tier.rank < min_tier.rank:
        rationale.append(
            f"critical individual risk signal override ({'; '.join(signal_reasons)}) "
            f"forces minimum tier {min_tier.value}"
        )
        tier = min_tier

    return tier, tuple(rationale)


def select_reasoning_effort(
    complexity: ComplexityAssessment,
    risk: RiskAssessment,
    policy: Policy,
) -> tuple[ReasoningEffort, tuple[str, ...]]:
    """Select reasoning effort using its own score and threshold set."""
    score = reasoning_score(complexity, risk)
    rt = policy.reasoning_thresholds

    if score >= rt.max_at:
        effort = ReasoningEffort.MAX
    elif score >= rt.xhigh_at:
        effort = ReasoningEffort.XHIGH
    elif score >= rt.high_at:
        effort = ReasoningEffort.HIGH
    elif score >= rt.medium_at:
        effort = ReasoningEffort.MEDIUM
    elif score >= rt.low_at:
        effort = ReasoningEffort.LOW
    else:
        effort = ReasoningEffort.NONE

    if effort.rank < policy.default_reasoning_floor.rank:
        effort = policy.default_reasoning_floor

    rationale = [
        f"reasoning_score={score:.2f} "
        f"(complexity_weight=0.35, risk_weight=0.65) -> effort={effort.value}"
    ]

    signal_reasons = critical_signal_reasons(risk, policy)
    min_reasoning = policy.critical_risk.min_reasoning
    if signal_reasons and effort.rank < min_reasoning.rank:
        rationale.append(
            f"critical individual risk signal override ({'; '.join(signal_reasons)}) "
            f"forces minimum reasoning {min_reasoning.value}"
        )
        effort = min_reasoning

    return effort, tuple(rationale)


def route(
    complexity: ComplexityAssessment,
    risk: RiskAssessment,
    policy: Policy,
) -> ModelProfile:
    """Produce the full, explainable model + reasoning routing decision."""
    tier, tier_rationale = select_model_tier(complexity, risk, policy)
    reasoning, reasoning_rationale = select_reasoning_effort(complexity, risk, policy)
    resolved = policy.model_tier_mapping.resolve(tier)
    return ModelProfile(
        tier=tier,
        reasoning=reasoning,
        resolved_model=resolved,
        rationale=tuple(tier_rationale) + tuple(reasoning_rationale),
    )


def estimate_cost(tier: ModelTier, estimated_tokens: int) -> float:
    """Deterministic relative-cost estimate for a tier/token-count pair."""
    return RELATIVE_COST_PER_1K_TOKENS[tier] * (estimated_tokens / 1000.0)
