"""Minimal example: analyze a task and generate an execution plan.

Run with no arguments, no network access, no API keys:

    python examples/basic_plan.py
"""

from __future__ import annotations

import agentgear


def main() -> None:
    task = agentgear.TaskProfile(
        description="Refactor authentication across 8 files",
        files_affected=8,
        modules_affected=3,
        architectural_impact=0.4,
        security_impact=0.6,
        ambiguity=0.3,
        existing_test_coverage=0.5,
    )

    complexity, risk = agentgear.analyze(task)
    print(f"complexity: {complexity.score:.2f} ({complexity.level.value})")
    print(f"risk:       {risk.score:.2f} ({risk.level.value})")

    plan = agentgear.plan(task)
    print(f"\nprimary model: {plan.primary_model.tier.value}/{plan.primary_model.reasoning.value}")
    print("agents:")
    for agent in plan.strategy.agents:
        print(f"  - {agent.role.value}: {agent.tier.value}/{agent.reasoning.value} x{agent.count}")
    print(f"\njudge_required: {plan.strategy.judge_required}")
    print(f"review_required: {plan.review_required}")
    print(f"context_budget_tokens: {plan.context_budget_tokens}")
    print(f"max_estimated_cost: {plan.max_estimated_cost:.4f}")

    print("\nrationale:")
    for line in plan.rationale:
        print(f"  - {line}")


if __name__ == "__main__":
    main()
