from __future__ import annotations

from agentgear.config import BudgetPolicy, Policy, WatchdogPolicy
from agentgear.escalation import EscalationSignals, decide_escalation
from agentgear.models import ModelTier, ReasoningEffort


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
