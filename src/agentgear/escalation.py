"""Escalation engine: decide whether/how to raise model tier and reasoning
effort mid-execution.

Escalation is triggered by evidence, never by elapsed time alone
(principle #10). A single failure does not automatically escalate;
repeated failure does. Architectural/security risk signals can jump
directly to a high tier rather than climbing the ladder step by step,
mirroring the initial-routing critical-risk override in ``routing.py``.

Escalation is bounded by ``Policy.watchdog.max_model_escalations`` and by
the cost budget — this module never proposes an escalation that a caller
could apply to silently blow through either limit; it reports why not
instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Policy
from .models import ModelTier, ReasoningEffort
from .routing import estimate_cost

_REPEATED_FAILURE_THRESHOLD = 2
_UNCERTAINTY_THRESHOLD = 0.6

# Single source of truth for tier/reasoning ordering: Python guarantees Enum
# iteration follows declaration order, and ``ModelTier.rank`` /
# ``ReasoningEffort.rank`` (models.py) are defined against that same order.
# Escalation must never maintain its own, independently-drifting ladder.
_TIER_LADDER: tuple[ModelTier, ...] = tuple(ModelTier)
_REASONING_LADDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)


@dataclass(frozen=True)
class EscalationSignals:
    """Evidence considered when deciding whether to escalate.

    ``elapsed_seconds`` is accepted for logging/rationale only — it never
    participates in the escalation decision itself.
    """

    repeated_failures: int = 0
    uncertainty: float = 0.0
    architectural_risk: bool = False
    security_risk: bool = False
    insufficient_context: bool = False
    failed_tests: bool = False
    stalled: bool = False
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class EscalationDecision:
    should_escalate: bool
    next_tier: ModelTier | None
    next_reasoning: ReasoningEffort | None
    reason: str
    rationale: tuple[str, ...] = field(default_factory=tuple)


def _next_step(tier: ModelTier, reasoning: ReasoningEffort) -> tuple[ModelTier, ReasoningEffort]:
    tier_idx = min(_TIER_LADDER.index(tier) + 1, len(_TIER_LADDER) - 1)
    reasoning_idx = min(_REASONING_LADDER.index(reasoning) + 1, len(_REASONING_LADDER) - 1)
    return _TIER_LADDER[tier_idx], _REASONING_LADDER[reasoning_idx]


def _critical_jump(reasoning: ReasoningEffort) -> tuple[ModelTier, ReasoningEffort]:
    reasoning_idx = max(
        _REASONING_LADDER.index(reasoning), _REASONING_LADDER.index(ReasoningEffort.HIGH)
    )
    return ModelTier.FRONTIER, _REASONING_LADDER[reasoning_idx]


def _triggering_reason(signals: EscalationSignals) -> str | None:
    if signals.security_risk:
        return "security_risk"
    if signals.architectural_risk:
        return "architectural_risk"
    if signals.repeated_failures >= _REPEATED_FAILURE_THRESHOLD:
        return "repeated_failure"
    if signals.uncertainty >= _UNCERTAINTY_THRESHOLD:
        return "uncertainty"
    if signals.insufficient_context:
        return "insufficient_context"
    if signals.failed_tests:
        return "failed_tests"
    if signals.stalled:
        return "stalled_execution"
    return None


def decide_escalation(
    current_tier: ModelTier,
    current_reasoning: ReasoningEffort,
    escalations_used: int,
    signals: EscalationSignals,
    policy: Policy,
    context_budget_tokens: int = 2000,
) -> EscalationDecision:
    """Decide whether to escalate and to what, or explain why not."""
    reason = _triggering_reason(signals)
    if reason is None:
        return EscalationDecision(
            should_escalate=False,
            next_tier=None,
            next_reasoning=None,
            reason="no_trigger",
            rationale=(
                "no escalation signal met threshold; time elapsed alone never triggers escalation",
            ),
        )

    if escalations_used >= policy.watchdog.max_model_escalations:
        return EscalationDecision(
            should_escalate=False,
            next_tier=None,
            next_reasoning=None,
            reason="escalation_limit_reached",
            rationale=(
                f"trigger '{reason}' present but escalations_used={escalations_used} >= "
                f"max_model_escalations={policy.watchdog.max_model_escalations}",
            ),
        )

    if reason in ("security_risk", "architectural_risk"):
        next_tier, next_reasoning = _critical_jump(current_reasoning)
        jump_desc = "critical non-sequential jump to FRONTIER"
    else:
        next_tier, next_reasoning = _next_step(current_tier, current_reasoning)
        jump_desc = "one-step ladder escalation"

    if next_tier == current_tier and next_reasoning == current_reasoning:
        return EscalationDecision(
            should_escalate=False,
            next_tier=None,
            next_reasoning=None,
            reason="ceiling_reached",
            rationale=(
                f"trigger '{reason}' present but already at the maximum tier/reasoning "
                f"({current_tier.value}/{current_reasoning.value}); no further escalation "
                "is possible",
            ),
        )

    projected_cost = estimate_cost(next_tier, context_budget_tokens)
    if projected_cost > policy.budget.max_estimated_cost:
        return EscalationDecision(
            should_escalate=False,
            next_tier=None,
            next_reasoning=None,
            reason="cost_budget_exceeded",
            rationale=(
                f"trigger '{reason}' present but escalating to {next_tier.value} would cost "
                f"{projected_cost:.4f}, exceeding budget.max_estimated_cost="
                f"{policy.budget.max_estimated_cost:g}",
            ),
        )

    return EscalationDecision(
        should_escalate=True,
        next_tier=next_tier,
        next_reasoning=next_reasoning,
        reason=reason,
        rationale=(
            f"trigger='{reason}'; {jump_desc}: "
            f"{current_tier.value}/{current_reasoning.value} -> "
            f"{next_tier.value}/{next_reasoning.value}; "
            f"escalations_used={escalations_used + 1}/{policy.watchdog.max_model_escalations}",
        ),
    )
