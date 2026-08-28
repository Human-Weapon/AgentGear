from __future__ import annotations

from agentgear import ActionableTaskContext, TaskProfile, plan


def test_generic_task_does_not_invent_execution_specifics() -> None:
    result = plan(TaskProfile(description="Implement boss fight"))

    assert result.actionable_context == ActionableTaskContext()
    assert "src/boss.js" not in "\n".join(result.rationale)
    assert "affected files: UNKNOWN (not supplied)" in result.rationale
    assert "dependencies: UNKNOWN (not supplied)" in result.rationale
    assert "acceptance criteria: UNKNOWN (not supplied)" in result.rationale
    assert "verification: UNKNOWN (not supplied)" in result.rationale
    assert "rollback strategy: UNKNOWN (not supplied)" in result.rationale


def test_plan_preserves_rich_caller_context_without_inventing_it() -> None:
    context = ActionableTaskContext(
        affected_files=("src/boss.js", "src/damage.js"),
        dependencies=("explosive damage system",),
        acceptance_criteria=("a single grenade cannot reduce the boss from full health to zero",),
        verification=("automated grenade-damage test", "manual boss fight"),
        rollback_strategy="revert the isolated damage-rule change",
    )

    result = plan(
        TaskProfile(
            description="Boss cannot die from one grenade",
            files_affected=2,
            modules_affected=2,
            actionable_context=context,
        )
    )

    assert result.actionable_context == context
    assert "objective: Boss cannot die from one grenade" in result.rationale
    assert "affected files: src/boss.js, src/damage.js" in result.rationale
    assert "dependencies: explosive damage system" in result.rationale
    assert (
        "acceptance criteria: a single grenade cannot reduce the boss from full health to zero"
        in result.rationale
    )
    assert "verification: automated grenade-damage test; manual boss fight" in result.rationale
    assert "rollback strategy: revert the isolated damage-rule change" in result.rationale
