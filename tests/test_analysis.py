from __future__ import annotations

from agentgear.analysis import assess_complexity, assess_risk
from agentgear.models import ComplexityLevel, RiskLevel, TaskProfile


def test_trivial_task_is_low_complexity_low_risk(trivial_profile: TaskProfile) -> None:
    c = assess_complexity(trivial_profile)
    r = assess_risk(trivial_profile)
    assert c.level in (ComplexityLevel.TRIVIAL, ComplexityLevel.LOW)
    assert r.level in (RiskLevel.MINIMAL, RiskLevel.LOW)


def test_architectural_task_has_high_complexity(architectural_profile: TaskProfile) -> None:
    c = assess_complexity(architectural_profile)
    assert c.score > 0.5
    assert c.factors["architectural_impact"] == architectural_profile.architectural_impact


def test_high_risk_profile_scores_high_risk(high_risk_profile: TaskProfile) -> None:
    r = assess_risk(high_risk_profile)
    assert r.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def test_scores_are_bounded_0_1() -> None:
    extreme = TaskProfile(
        description="x",
        files_affected=10_000,
        modules_affected=10_000,
        architectural_impact=1.0,
        security_impact=1.0,
        data_impact=1.0,
        ambiguity=1.0,
        novelty=1.0,
        reversibility=0.0,
        existing_test_coverage=0.0,
        prior_failures=1000,
    )
    c = assess_complexity(extreme)
    r = assess_risk(extreme)
    assert 0.0 <= c.score <= 1.0
    assert 0.0 <= r.score <= 1.0


def test_analysis_is_deterministic(moderate_profile: TaskProfile) -> None:
    c1 = assess_complexity(moderate_profile)
    c2 = assess_complexity(moderate_profile)
    r1 = assess_risk(moderate_profile)
    r2 = assess_risk(moderate_profile)
    assert c1 == c2
    assert r1 == r2


def test_more_files_never_decreases_complexity() -> None:
    small = TaskProfile(description="x", files_affected=1)
    large = TaskProfile(description="x", files_affected=100)
    assert assess_complexity(large).score >= assess_complexity(small).score


def test_rationale_mentions_score() -> None:
    tp = TaskProfile(description="x")
    c = assess_complexity(tp)
    assert f"{c.score:.2f}" in c.rationale
