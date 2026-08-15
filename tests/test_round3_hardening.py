"""Round 3 remediation: AUDIT3-04 (deep immutability) and AUDIT3-06
(RecoveryEpisode defensive validation). See docs/audits/remediation-round-3.md
for the full finding writeups and design decisions.
"""

from __future__ import annotations

import pytest

from agentgear.analysis import assess_complexity, assess_risk
from agentgear.config import ModelTierMapping, Policy
from agentgear.exceptions import ConfigurationError, InvalidObservationError, TaskProfileError
from agentgear.models import (
    ComplexityAssessment,
    ComplexityLevel,
    ModelTier,
    RecoveryAttempt,
    RecoveryEpisode,
    RecoveryEpisodeOutcome,
    RecoveryResult,
    RiskAssessment,
    RiskLevel,
    TaskProfile,
)
from agentgear.planning import build_execution_plan
from agentgear.routing import route

_RECOVERY_EPISODE_ERRORS = (
    ConfigurationError,
    TaskProfileError,
    InvalidObservationError,
    TypeError,
    ValueError,
)

# --- AUDIT3-04: ModelTierMapping deep immutability -------------------------


def test_model_tier_mapping_is_unaffected_by_mutating_the_original_source_dict() -> None:
    source = {"fast": "Luna", "standard": "Luna", "advanced": "Terra", "frontier": "Sol"}
    mapping = ModelTierMapping(mapping=source)
    source["fast"] = "tampered"
    assert mapping.mapping["fast"] == "Luna"


def test_model_tier_mapping_rejects_direct_mutation() -> None:
    mapping = ModelTierMapping()
    with pytest.raises(TypeError):
        mapping.mapping["fast"] = "tampered"  # type: ignore[index]


def test_model_tier_mapping_resolve_still_works_after_freeze() -> None:
    mapping = ModelTierMapping()
    assert mapping.resolve(ModelTier.FAST)


def test_policy_routing_stays_deterministic_despite_mutation_attempts() -> None:
    """Canonical Round 3 / section 7 regression: the SAME Policy object,
    used for two identical routing calls, must produce the SAME result
    even if a caller attempts to mutate the nested mapping in between."""
    policy = Policy.default()
    profile = TaskProfile(description="x", files_affected=3)
    c, r = assess_complexity(profile), assess_risk(profile)

    before = route(c, r, policy)
    with pytest.raises(TypeError):
        policy.model_tier_mapping.mapping["fast"] = "tampered"  # type: ignore[index]
    after = route(c, r, policy)

    assert before.resolved_model == after.resolved_model
    assert before.tier == after.tier
    assert before.reasoning == after.reasoning


# --- AUDIT3-04: ComplexityAssessment / RiskAssessment factors immutability -


def test_complexity_assessment_unaffected_by_mutating_original_factors_dict() -> None:
    factors = {"ambiguity": 0.9}
    assessment = ComplexityAssessment(score=0.5, level=ComplexityLevel.MODERATE, factors=factors)
    factors["ambiguity"] = 0.0
    assert assessment.factors["ambiguity"] == 0.9


def test_complexity_assessment_factors_rejects_direct_mutation() -> None:
    assessment = ComplexityAssessment(
        score=0.5, level=ComplexityLevel.MODERATE, factors={"ambiguity": 0.9}
    )
    with pytest.raises(TypeError):
        assessment.factors["ambiguity"] = 0.0  # type: ignore[index]


def test_risk_assessment_unaffected_by_mutating_original_factors_dict() -> None:
    factors = {"security_impact": 0.9}
    assessment = RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors=factors)
    factors["security_impact"] = 0.0
    assert assessment.factors["security_impact"] == 0.9


def test_risk_assessment_factors_rejects_direct_mutation() -> None:
    assessment = RiskAssessment(
        score=0.5, level=RiskLevel.MODERATE, factors={"security_impact": 0.9}
    )
    with pytest.raises(TypeError):
        assessment.factors["security_impact"] = 0.0  # type: ignore[index]


def test_complexity_assessment_rejects_non_mapping_factors() -> None:
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(score=0.5, level=ComplexityLevel.MODERATE, factors="not-a-dict")  # type: ignore[arg-type]


def test_risk_assessment_rejects_non_mapping_factors() -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors=["not", "a", "dict"])  # type: ignore[arg-type]


def test_planning_stays_deterministic_despite_factor_mutation_attempts() -> None:
    """Canonical Round 3 / section 8 regression: mutating factors that
    feed a REAL planning decision (ambiguity/architectural_impact/
    prior_failures, per planning.py) must not change staffing for a
    subsequent call using the SAME assessment objects."""
    profile = TaskProfile(description="ambiguous risky task", ambiguity=0.6)
    policy = Policy.default()
    c = assess_complexity(profile)
    r = assess_risk(profile)

    plan_before = build_execution_plan(profile, c, r, policy)
    with pytest.raises(TypeError):
        c.factors["ambiguity"] = 0.0  # type: ignore[index]
    plan_after = build_execution_plan(profile, c, r, policy)

    assert plan_before.strategy.agent_count == plan_after.strategy.agent_count
    assert plan_before.strategy.execution_order == plan_after.strategy.execution_order


# --- AUDIT3-06: RecoveryEpisode defensive validation ------------------------


def _attempt(n: int = 1) -> RecoveryAttempt:
    return RecoveryAttempt(
        reason="stall", strategy="re_read_error", attempt_number=n, result=RecoveryResult.SUCCESS
    )


def test_recovery_episode_accepts_well_formed_closed_episode() -> None:
    ep = RecoveryEpisode(
        episode_number=1,
        stall_reason="x",
        attempts=(_attempt(),),
        outcome=RecoveryEpisodeOutcome.SUCCESS,
        opened_at=0.0,
        closed_at=1.0,
    )
    assert ep.episode_number == 1


def test_recovery_episode_accepts_a_pre_attempt_budget_blocked_episode() -> None:
    """A real, legitimate lifecycle: budget denial before any attempt ever
    ran still closes the episode as BLOCKED with zero attempts (see
    coordinator.py's begin_recovery budget-reservation-denial path and
    test_coordinator.py::test_budget_exhausted_on_third_episode_blocks_
    correctly). len(attempts) >= 1 would be a FALSE invariant here."""
    ep = RecoveryEpisode(
        episode_number=1,
        stall_reason="x",
        attempts=(),
        outcome=RecoveryEpisodeOutcome.BLOCKED,
        opened_at=0.0,
        closed_at=0.0,
    )
    assert ep.attempts == ()


@pytest.mark.parametrize("bad_episode_number", [0, -1, True, False, 1.5, "1"])
def test_recovery_episode_rejects_bad_episode_number(bad_episode_number) -> None:
    with pytest.raises(_RECOVERY_EPISODE_ERRORS):
        RecoveryEpisode(
            episode_number=bad_episode_number,
            stall_reason="x",
            attempts=(),
            outcome=RecoveryEpisodeOutcome.SUCCESS,
            opened_at=0.0,
            closed_at=1.0,
        )


def test_recovery_episode_rejects_closed_before_opened() -> None:
    with pytest.raises(_RECOVERY_EPISODE_ERRORS):
        RecoveryEpisode(
            episode_number=1,
            stall_reason="x",
            attempts=(),
            outcome=RecoveryEpisodeOutcome.SUCCESS,
            opened_at=10.0,
            closed_at=5.0,
        )


def test_recovery_episode_rejects_non_finite_timestamps() -> None:
    for bad_ts in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(_RECOVERY_EPISODE_ERRORS):
            RecoveryEpisode(
                episode_number=1,
                stall_reason="x",
                attempts=(),
                outcome=RecoveryEpisodeOutcome.SUCCESS,
                opened_at=0.0,
                closed_at=bad_ts,
            )


def test_recovery_episode_rejects_wrong_outcome_type() -> None:
    with pytest.raises(_RECOVERY_EPISODE_ERRORS):
        RecoveryEpisode(
            episode_number=1,
            stall_reason="x",
            attempts=(),
            outcome="success",  # type: ignore[arg-type]
            opened_at=0.0,
            closed_at=1.0,
        )


def test_recovery_episode_rejects_wrong_attempts_type() -> None:
    with pytest.raises(_RECOVERY_EPISODE_ERRORS):
        RecoveryEpisode(
            episode_number=1,
            stall_reason="x",
            attempts=float("nan"),  # type: ignore[arg-type]
            outcome=RecoveryEpisodeOutcome.SUCCESS,
            opened_at=0.0,
            closed_at=1.0,
        )
