from __future__ import annotations

import math

import pytest

from agentgear.budget import ExecutionBudgetLedger, ReservationKind
from agentgear.config import BudgetPolicy, Policy, WatchdogPolicy
from agentgear.escalation import EscalationSignals, decide_escalation
from agentgear.exceptions import InvalidObservationError
from agentgear.models import ModelTier, ReasoningEffort
from agentgear.routing import estimate_cost


def test_single_failure_does_not_escalate(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FAST, ReasoningEffort.LOW, 0, EscalationSignals(repeated_failures=1), policy
    )
    assert decision.should_escalate is False
    assert decision.reason == "no_trigger"


def test_repeated_failure_escalates(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FAST, ReasoningEffort.LOW, 0, EscalationSignals(repeated_failures=2), policy
    )
    assert decision.should_escalate is True
    assert decision.next_tier == ModelTier.STANDARD
    assert decision.next_reasoning == ReasoningEffort.MEDIUM


def test_time_elapsed_alone_never_escalates(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FAST,
        ReasoningEffort.LOW,
        0,
        EscalationSignals(elapsed_seconds=999999.0),
        policy,
    )
    assert decision.should_escalate is False


def test_security_risk_jumps_directly_to_frontier(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FAST, ReasoningEffort.LOW, 0, EscalationSignals(security_risk=True), policy
    )
    assert decision.should_escalate is True
    assert decision.next_tier == ModelTier.FRONTIER
    assert decision.next_reasoning.rank >= ReasoningEffort.HIGH.rank


def test_escalation_respects_max_limit() -> None:
    p = Policy(watchdog=WatchdogPolicy(max_model_escalations=1))
    decision = decide_escalation(
        ModelTier.FAST,
        ReasoningEffort.LOW,
        escalations_used=1,
        signals=EscalationSignals(repeated_failures=5),
        policy=p,
    )
    assert decision.should_escalate is False
    assert decision.reason == "escalation_limit_reached"


def test_escalation_stops_at_ceiling(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FRONTIER,
        ReasoningEffort.MAX,
        0,
        EscalationSignals(repeated_failures=5),
        policy,
    )
    assert decision.should_escalate is False
    assert decision.reason == "ceiling_reached"


def test_escalation_blocked_by_cost_budget() -> None:
    p = Policy(budget=BudgetPolicy(max_estimated_cost=0.00001))
    decision = decide_escalation(
        ModelTier.FAST,
        ReasoningEffort.LOW,
        0,
        EscalationSignals(repeated_failures=5),
        p,
        context_budget_tokens=100_000,
    )
    assert decision.should_escalate is False
    assert decision.reason == "cost_budget_exceeded"


def test_escalation_does_not_repeat_identical_strategy_forever() -> None:
    p = Policy(watchdog=WatchdogPolicy(max_model_escalations=3))
    tier, reasoning, used = ModelTier.FAST, ReasoningEffort.LOW, 0
    seen = []
    for _ in range(5):
        decision = decide_escalation(
            tier, reasoning, used, EscalationSignals(repeated_failures=5), p
        )
        if not decision.should_escalate:
            break
        seen.append((decision.next_tier, decision.next_reasoning))
        tier, reasoning = decision.next_tier, decision.next_reasoning
        used += 1
    assert len(seen) == len(set(seen))
    assert used <= p.watchdog.max_model_escalations


def test_uncertainty_threshold_escalates(policy: Policy) -> None:
    low = decide_escalation(
        ModelTier.FAST, ReasoningEffort.LOW, 0, EscalationSignals(uncertainty=0.2), policy
    )
    high = decide_escalation(
        ModelTier.FAST, ReasoningEffort.LOW, 0, EscalationSignals(uncertainty=0.9), policy
    )
    assert low.should_escalate is False
    assert high.should_escalate is True


# --- AG-04: escalation must consult the cumulative execution-wide ledger --


def test_ag04_second_escalation_denied_once_cumulative_spend_exceeds_ceiling() -> None:
    """initial=A, escalation1=B, escalation2=C. A+B < limit < A+B+C.
    The SECOND escalation must be denied even though its own cost, in
    isolation, would fit under the static ceiling.
    """
    p = Policy(
        budget=BudgetPolicy(max_estimated_cost=0.02, max_estimated_tokens=10_000_000),
        watchdog=WatchdogPolicy(max_model_escalations=5),
    )
    tokens = 1000
    ledger = ExecutionBudgetLedger(max_tokens=1_000_000, max_cost=p.budget.max_estimated_cost)

    initial_cost = estimate_cost(ModelTier.FAST, tokens)
    initial = ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=tokens, cost=initial_cost)
    ledger.commit(initial.reservation_id)

    decision_1 = decide_escalation(
        ModelTier.FAST,
        ReasoningEffort.LOW,
        escalations_used=0,
        signals=EscalationSignals(repeated_failures=5),
        policy=p,
        context_budget_tokens=tokens,
        ledger=ledger,
    )
    assert decision_1.should_escalate is True
    escalation_1_cost = estimate_cost(decision_1.next_tier, tokens)
    reservation_1 = ledger.reserve(
        kind=ReservationKind.ESCALATION, tokens=tokens, cost=escalation_1_cost
    )
    ledger.commit(reservation_1.reservation_id)

    decision_2 = decide_escalation(
        decision_1.next_tier,
        decision_1.next_reasoning,
        escalations_used=1,
        signals=EscalationSignals(repeated_failures=5),
        policy=p,
        context_budget_tokens=tokens,
        ledger=ledger,
    )

    assert decision_2.should_escalate is False
    assert decision_2.reason == "cost_budget_exceeded"
    # sanity: prove this is genuinely a *cumulative* denial, not something
    # a single-operation check against the static ceiling would also catch.
    escalation_2_cost_alone = estimate_cost(decision_1.next_tier, tokens)
    assert escalation_2_cost_alone <= p.budget.max_estimated_cost


def test_ag04_escalation_without_ledger_falls_back_to_static_check(policy: Policy) -> None:
    decision = decide_escalation(
        ModelTier.FAST,
        ReasoningEffort.LOW,
        0,
        EscalationSignals(repeated_failures=5),
        policy,
        ledger=None,
    )
    assert decision.should_escalate is True


# --- AG-06: escalation signals and parameters are validated ---------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repeated_failures": -1},
        {"uncertainty": -0.1},
        {"uncertainty": 1.1},
        {"uncertainty": math.nan},
        {"uncertainty": math.inf},
        {"elapsed_seconds": -1.0},
        {"elapsed_seconds": math.nan},
        {"architectural_risk": "yes"},
        {"security_risk": 1},
        {"stalled": "true"},
    ],
)
def test_escalation_signals_rejects_invalid_values(kwargs: dict) -> None:
    with pytest.raises(InvalidObservationError):
        EscalationSignals(**kwargs)


def test_escalation_signals_rejects_bool_for_repeated_failures() -> None:
    with pytest.raises(InvalidObservationError):
        EscalationSignals(repeated_failures=True)  # type: ignore[arg-type]


def test_decide_escalation_rejects_negative_escalations_used(policy: Policy) -> None:
    with pytest.raises(InvalidObservationError):
        decide_escalation(
            ModelTier.FAST, ReasoningEffort.LOW, -1, EscalationSignals(repeated_failures=5), policy
        )


@pytest.mark.parametrize("bad_tokens", [0, -100])
def test_decide_escalation_rejects_non_positive_context_budget(
    policy: Policy, bad_tokens: int
) -> None:
    with pytest.raises(InvalidObservationError):
        decide_escalation(
            ModelTier.FAST,
            ReasoningEffort.LOW,
            0,
            EscalationSignals(repeated_failures=5),
            policy,
            context_budget_tokens=bad_tokens,
        )


def test_nonsensical_uncertainty_can_no_longer_be_constructed(policy: Policy) -> None:
    """Regression for the exact AG-06 reproduction: uncertainty=999.0 used
    to silently trigger escalation. It must now be rejected at
    construction, before it ever reaches decide_escalation."""
    with pytest.raises(InvalidObservationError):
        EscalationSignals(uncertainty=999.0)
