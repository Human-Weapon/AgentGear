"""Execution planning: multi-agent staffing + the top-level ExecutionPlan.

Staffing policy (principle #8/#9):

* Single agent (just a Builder) for low-risk, low-complexity work.
* A Planner joins when the change has real architectural impact.
* Researchers join only when ambiguity/novelty is high enough that
  evidence-gathering *before* building is worth its cost; 2 researchers
  only when ambiguity is high enough to expect genuinely divergent
  proposals.
* A Judge joins only when there is something to judge: >=2 researcher
  proposals, or risk high enough that an independent evaluation pass
  before the Builder acts is warranted.
* A Reviewer joins whenever the task is staffed multi-agent at all
  (Builder implements -> Reviewer verifies is the closing half of the
  pipeline) — never for a lone trivial Builder run.

Every hard budget (``Policy.budget``) is enforced by raising
``BudgetExceededError`` rather than silently clamping or returning a
plan that violates policy (principle #46).
"""

from __future__ import annotations

from .config import Policy
from .exceptions import BudgetExceededError
from .models import (
    AgentAssignment,
    AgentRole,
    ComplexityAssessment,
    ExecutionPlan,
    ExecutionStrategy,
    ModelProfile,
    ModelTier,
    ReasoningEffort,
    RiskAssessment,
    TaskProfile,
)
from .routing import critical_signal_reasons, estimate_cost, route

_RESEARCHER_AMBIGUITY_THRESHOLD = 0.5
_DUAL_RESEARCHER_AMBIGUITY_THRESHOLD = 0.75
_PLANNER_ARCHITECTURAL_THRESHOLD = 0.5
_JUDGE_RISK_THRESHOLD = 0.75

_ONE_STEP_DOWN = {
    ModelTier.FRONTIER: ModelTier.ADVANCED,
    ModelTier.ADVANCED: ModelTier.STANDARD,
    ModelTier.STANDARD: ModelTier.FAST,
    ModelTier.FAST: ModelTier.FAST,
}
_ONE_STEP_UP = {
    ModelTier.FAST: ModelTier.STANDARD,
    ModelTier.STANDARD: ModelTier.ADVANCED,
    ModelTier.ADVANCED: ModelTier.FRONTIER,
    ModelTier.FRONTIER: ModelTier.FRONTIER,
}


def build_execution_strategy(
    complexity: ComplexityAssessment,
    risk: RiskAssessment,
    primary_model: ModelProfile,
    policy: Policy,
) -> ExecutionStrategy:
    # Any of these independently justifies staffing beyond a lone Builder
    # (principle #8): overall complexity/risk score, OR one specific
    # dimension being high enough on its own (high ambiguity, real
    # architectural impact, a history of failure on this task) even if the
    # blended score hasn't crossed the combined threshold yet.
    ambiguity = complexity.factors.get("ambiguity", 0.0)
    architectural_impact = complexity.factors.get("architectural_impact", 0.0)
    prior_failure_signal = risk.factors.get("prior_failures", 0.0)
    critical_reasons = critical_signal_reasons(risk, policy)
    critical_forces_review = bool(critical_reasons) and policy.critical_risk.require_review
    needs_multi_agent = (
        complexity.score >= policy.multi_agent_complexity_threshold
        or risk.score >= policy.multi_agent_risk_threshold
        or ambiguity >= _RESEARCHER_AMBIGUITY_THRESHOLD
        or architectural_impact >= _PLANNER_ARCHITECTURAL_THRESHOLD
        or prior_failure_signal >= 0.5
        or critical_forces_review
    )

    rationale: list[str] = [
        f"multi_agent_needed={needs_multi_agent} "
        f"(complexity={complexity.score:.2f} vs threshold "
        f"{policy.multi_agent_complexity_threshold:.2f}; "
        f"risk={risk.score:.2f} vs threshold {policy.multi_agent_risk_threshold:.2f}; "
        f"ambiguity={ambiguity:.2f}, architectural_impact={architectural_impact:.2f}, "
        f"prior_failure_signal={prior_failure_signal:.2f})"
    ]
    if critical_reasons:
        rationale.append(
            f"critical individual risk signal(s) present: {'; '.join(critical_reasons)}"
            + (" (forces independent review)" if critical_forces_review else "")
        )

    if not needs_multi_agent:
        rationale.append("single Builder agent: low complexity and low risk")
        agents = (
            AgentAssignment(
                role=AgentRole.BUILDER, tier=primary_model.tier, reasoning=primary_model.reasoning
            ),
        )
        return ExecutionStrategy(
            agents=agents,
            judge_required=False,
            execution_order=(AgentRole.BUILDER,),
            rationale=tuple(rationale),
        )

    agents: list[AgentAssignment] = []
    order: list[AgentRole] = []

    if architectural_impact >= _PLANNER_ARCHITECTURAL_THRESHOLD:
        planner_tier = primary_model.tier
        agents.append(
            AgentAssignment(
                role=AgentRole.PLANNER, tier=planner_tier, reasoning=ReasoningEffort.LOW
            )
        )
        order.append(AgentRole.PLANNER)
        rationale.append(
            f"planner added: architectural_impact={architectural_impact:.2f} >= "
            f"{_PLANNER_ARCHITECTURAL_THRESHOLD}"
        )

    researcher_count = 0
    if ambiguity >= _DUAL_RESEARCHER_AMBIGUITY_THRESHOLD:
        researcher_count = 2
    elif ambiguity >= _RESEARCHER_AMBIGUITY_THRESHOLD:
        researcher_count = 1

    if researcher_count:
        researcher_tier = _ONE_STEP_DOWN[primary_model.tier]
        agents.append(
            AgentAssignment(
                role=AgentRole.RESEARCHER,
                tier=researcher_tier,
                reasoning=ReasoningEffort.MEDIUM,
                count=researcher_count,
            )
        )
        order.append(AgentRole.RESEARCHER)
        rationale.append(
            f"{researcher_count} researcher(s) added: ambiguity={ambiguity:.2f} >= "
            f"{_RESEARCHER_AMBIGUITY_THRESHOLD}"
        )

    judge_required = researcher_count >= 2 or risk.score >= _JUDGE_RISK_THRESHOLD
    if judge_required:
        judge_tier = (
            _ONE_STEP_UP[primary_model.tier]
            if risk.score >= _JUDGE_RISK_THRESHOLD
            else primary_model.tier
        )
        agents.append(
            AgentAssignment(role=AgentRole.JUDGE, tier=judge_tier, reasoning=ReasoningEffort.HIGH)
        )
        order.append(AgentRole.JUDGE)
        reason = (
            f"researcher_count={researcher_count} >= 2"
            if researcher_count >= 2
            else f"risk={risk.score:.2f} >= {_JUDGE_RISK_THRESHOLD}"
        )
        rationale.append(f"judge added: {reason}")

    agents.append(
        AgentAssignment(
            role=AgentRole.BUILDER, tier=primary_model.tier, reasoning=primary_model.reasoning
        )
    )
    order.append(AgentRole.BUILDER)

    reviewer_tier = (
        _ONE_STEP_UP[primary_model.tier]
        if risk.score >= policy.multi_agent_risk_threshold
        else primary_model.tier
    )
    agents.append(
        AgentAssignment(
            role=AgentRole.REVIEWER, tier=reviewer_tier, reasoning=ReasoningEffort.MEDIUM
        )
    )
    order.append(AgentRole.REVIEWER)
    rationale.append("reviewer added: multi-agent pipeline always closes with a review pass")

    return ExecutionStrategy(
        agents=tuple(agents),
        judge_required=judge_required,
        execution_order=tuple(order),
        rationale=tuple(rationale),
    )


def _watchdog_summary(policy: Policy) -> str:
    w = policy.watchdog
    if not w.enabled:
        return "watchdog disabled by policy"
    return (
        f"stall after {w.no_progress_cycles} no-progress cycles or "
        f"{w.no_progress_seconds:.0f}s without evidence; "
        f"recover up to {w.max_recovery_attempts} times "
        f"(max {w.max_total_attempts} total attempts, "
        f"{w.max_model_escalations} model escalations); "
        f"then BLOCKED with a structured report"
    )


def _escalation_summary(policy: Policy) -> str:
    return (
        f"escalation permitted on repeated failure, uncertainty, or risk signals, "
        f"bounded to {policy.watchdog.max_model_escalations} tier escalation(s) and "
        f"cost budget {policy.budget.max_estimated_cost:g}; never escalates on elapsed "
        f"time alone"
    )


def build_execution_plan(
    task_profile: TaskProfile,
    complexity: ComplexityAssessment,
    risk: RiskAssessment,
    policy: Policy,
) -> ExecutionPlan:
    """Combine routing + staffing into a full ExecutionPlan.

    Raises ``BudgetExceededError`` if the resulting plan would violate any
    hard budget in ``policy.budget`` — it never returns a violating plan.
    """
    primary_model = route(complexity, risk, policy)
    strategy = build_execution_strategy(complexity, risk, primary_model, policy)

    if strategy.agent_count > policy.budget.max_agents:
        raise BudgetExceededError(
            f"execution strategy requires {strategy.agent_count} agents, exceeding "
            f"budget.max_agents={policy.budget.max_agents}"
        )

    desired_context_budget = int(
        task_profile.expected_output_tokens * (2.0 + 4.0 * complexity.score)
    )
    if desired_context_budget > policy.budget.max_context_budget_tokens:
        raise BudgetExceededError(
            f"desired context budget {desired_context_budget} tokens exceeds "
            f"budget.max_context_budget_tokens={policy.budget.max_context_budget_tokens}"
        )
    context_budget_tokens = max(desired_context_budget, 1)

    total_cost = sum(
        estimate_cost(a.tier, context_budget_tokens) * a.count for a in strategy.agents
    )
    if total_cost > policy.budget.max_estimated_cost:
        raise BudgetExceededError(
            f"estimated cost {total_cost:.4f} exceeds "
            f"budget.max_estimated_cost={policy.budget.max_estimated_cost:g}"
        )

    estimated_tokens = context_budget_tokens * strategy.agent_count
    if estimated_tokens > policy.budget.max_estimated_tokens:
        raise BudgetExceededError(
            f"estimated tokens {estimated_tokens} exceed "
            f"budget.max_estimated_tokens={policy.budget.max_estimated_tokens}"
        )

    review_required = AgentRole.REVIEWER in strategy.execution_order

    rationale = (
        (complexity.rationale, risk.rationale)
        + primary_model.rationale
        + strategy.rationale
        + (
            f"context_budget_tokens={context_budget_tokens}",
            f"estimated_cost={total_cost:.4f} (budget={policy.budget.max_estimated_cost:g})",
        )
    )

    return ExecutionPlan(
        task_profile=task_profile,
        complexity=complexity,
        risk=risk,
        primary_model=primary_model,
        strategy=strategy,
        context_budget_tokens=context_budget_tokens,
        max_estimated_cost=total_cost,
        max_agents=policy.budget.max_agents,
        escalation_policy_summary=_escalation_summary(policy),
        recovery_policy_summary=_watchdog_summary(policy),
        review_required=review_required,
        rationale=rationale,
    )
