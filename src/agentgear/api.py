"""Small, stable convenience API sitting on top of the individual modules.

Most callers just want "given a task, give me a plan" without wiring
analysis -> routing -> planning by hand. ``analyze`` and ``plan`` are that
shortcut; the underlying modules remain independently usable.
"""

from __future__ import annotations

from .analysis import assess_complexity, assess_risk
from .config import Policy
from .models import ComplexityAssessment, ExecutionPlan, RiskAssessment, TaskProfile
from .planning import build_execution_plan


def analyze(profile: TaskProfile) -> tuple[ComplexityAssessment, RiskAssessment]:
    return assess_complexity(profile), assess_risk(profile)


def plan(profile: TaskProfile, policy: Policy | None = None) -> ExecutionPlan:
    policy = policy or Policy.default()
    complexity, risk = analyze(profile)
    return build_execution_plan(profile, complexity, risk, policy)
