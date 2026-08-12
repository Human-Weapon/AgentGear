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

**Cumulative budget (AG-04).** When an ``ExecutionBudgetLedger`` is
supplied, the cost check is answered by the ledger — which already knows
what the initial plan and every prior escalation/recovery has committed —
instead of comparing this one operation's cost against the static policy
ceiling in isolation. That distinction matters: two escalations that are
each individually affordable can still be jointly unaffordable once
what's already been spent is accounted for. Without a ledger (e.g. a
caller doing one-shot routing simulation with no execution in progress),
this falls back to a single-operation check against
``Policy.budget.max_estimated_cost``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .budget import ExecutionBudgetLedger
from .config import Policy
from .exceptions import InvalidObservationError
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


def _require_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidObservationError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value):
        raise InvalidObservationError(f"{name} must be finite, got {value}")
    return float(value)


def _require_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidObservationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise InvalidObservationError(f"{name} must be >= 0, got {value}")
    return value


def _require_unit_interval(name: str, value: float) -> float:
    v = _require_finite(name, value)
    if not (0.0 <= v <= 1.0):
        raise InvalidObservationError(f"{name} must be within [0.0, 1.0], got {v}")
    return v


def _require_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise InvalidObservationError(f"{name} must be a bool, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class EscalationSignals:
    """Evidence considered when deciding whether to escalate.

    ``elapsed_seconds`` is accepted for logging/rationale only — it never
    participates in the escalation decision itself. Every field is
    validated on construction (AG-06): a stuck or malicious caller must
    not be able to force an escalation by feeding out-of-range numbers
    (negative counts, NaN/Infinity, uncertainty outside [0, 1]).
    """

    repeated_failures: int = 0
    uncertainty: float = 0.0
    architectural_risk: bool = False
    security_risk: bool = False
    insufficient_context: bool = False
    failed_tests: bool = False
    stalled: bool = False
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repeated_failures",
            _require_non_negative_int("repeated_failures", self.repeated_failures),
        )
        object.__setattr__(
            self, "uncertainty", _require_unit_interval("uncertainty", self.uncertainty)
        )
        object.__setattr__(
            self, "architectural_risk", _require_bool("architectural_risk", self.architectural_risk)
        )
        object.__setattr__(
            self, "security_risk", _require_bool("security_risk", self.security_risk)
        )
        object.__setattr__(
            self,
            "insufficient_context",
            _require_bool("insufficient_context", self.insufficient_context),
        )
        object.__setattr__(self, "failed_tests", _require_bool("failed_tests", self.failed_tests))
        object.__setattr__(self, "stalled", _require_bool("stalled", self.stalled))
        object.__setattr__(
            self, "elapsed_seconds", _require_finite("elapsed_seconds", self.elapsed_seconds)
        )
        if self.elapsed_seconds < 0:
            raise InvalidObservationError(
                f"elapsed_seconds must be >= 0, got {self.elapsed_seconds}"
            )


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
    ledger: ExecutionBudgetLedger | None = None,
) -> EscalationDecision:
    """Decide whether to escalate and to what, or explain why not."""
    escalations_used = _require_non_negative_int("escalations_used", escalations_used)
    if isinstance(context_budget_tokens, bool) or not isinstance(context_budget_tokens, int):
        raise InvalidObservationError(
            f"context_budget_tokens must be an int, got {type(context_budget_tokens).__name__}"
        )
    if context_budget_tokens <= 0:
        raise InvalidObservationError(
            f"context_budget_tokens must be > 0, got {context_budget_tokens}"
        )

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
    if ledger is not None:
        if not ledger.can_afford(tokens=context_budget_tokens, cost=projected_cost):
            return EscalationDecision(
                should_escalate=False,
                next_tier=None,
                next_reasoning=None,
                reason="cost_budget_exceeded",
                rationale=(
                    f"trigger '{reason}' present but escalating to {next_tier.value} "
                    f"(+{context_budget_tokens} tokens / +{projected_cost:.4f} cost) would "
                    f"exceed the execution's cumulative budget: already using "
                    f"{ledger.committed_tokens + ledger.reserved_tokens} tokens / "
                    f"{ledger.committed_cost + ledger.reserved_cost:.4f} cost against ceilings "
                    f"of {ledger.max_tokens} tokens / {ledger.max_cost:.4f} cost",
                ),
            )
    elif projected_cost > policy.budget.max_estimated_cost:
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
