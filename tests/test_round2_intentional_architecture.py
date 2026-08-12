"""Round 2 remediation, section 58: M1-M6, L1, L3, L5-L7.

The Round 2 audit spec explicitly flagged these as *possibly* intentional
architecture rather than bugs, and asked for investigation + documentation
+ a regression test rather than a reflexive "fix" -- forcing a false
invariant here (e.g. "every role's reasoning must be >= Builder's") would
itself be a bug. Each test below locks in the INVESTIGATED, CONFIRMED
behavior so a future change can't silently alter it without a test
failing; see the docstrings in the corresponding source modules for the
full rationale.
"""

from __future__ import annotations

from agentgear.analysis import assess_complexity, assess_risk
from agentgear.config import CriticalRiskPolicy, Policy, RoutingThresholds
from agentgear.models import (
    AgentRole,
    ComplexityAssessment,
    ComplexityLevel,
    ExecutionState,
    ModelTier,
    ReasoningEffort,
    RiskAssessment,
    RiskLevel,
    TaskProfile,
)
from agentgear.planning import build_execution_plan
from agentgear.routing import critical_signal_reasons, select_model_tier
from agentgear.watchdog import ExecutionWatchdog
from agentgear.watchdog.stall_detection import ActivityRecord, StallDetector
from agentgear.watchdog.state_machine import ExecutionStateMachine

# --- M1: a critical-risk threshold of 0.0 is valid config, not a bug ------


def test_m1_critical_risk_threshold_zero_means_always_apply() -> None:
    lenient_floor = CriticalRiskPolicy(security_impact_at=0.0)
    assert lenient_floor.security_impact_at == 0.0  # accepted, not rejected

    risk = RiskAssessment(score=0.01, level=RiskLevel.MINIMAL, factors={"security_impact": 0.001})
    reasons = critical_signal_reasons(risk, Policy(critical_risk=lenient_floor))
    assert reasons, "threshold=0.0 must force the floor on ANY nonzero signal"


# --- M2: equal routing thresholds are valid config, not a bug -------------


def test_m2_equal_thresholds_are_accepted_and_collapse_a_tier() -> None:
    t = RoutingThresholds(standard_at=0.5, advanced_at=0.5, frontier_at=0.9)
    assert t.standard_at == t.advanced_at == 0.5  # accepted, not rejected

    policy = Policy(routing_thresholds=t)
    complexity = ComplexityAssessment(score=0.6, level=ComplexityLevel.MODERATE)
    risk = RiskAssessment(score=0.6, level=RiskLevel.MODERATE)
    tier, _ = select_model_tier(complexity, risk, policy)
    # A score clearing standard_at also clears advanced_at (they're equal),
    # so STANDARD is structurally unreachable -- by design, not a bug.
    assert tier == ModelTier.ADVANCED


# --- M3: a multi-agent threshold of 0.0 is valid config, not a bug --------


def test_m3_multi_agent_threshold_zero_forces_staffing_for_every_task() -> None:
    policy = Policy(multi_agent_complexity_threshold=0.0)
    profile = TaskProfile(description="rename a local variable", files_affected=1)
    c = assess_complexity(profile)
    r = assess_risk(profile)
    assert c.score >= 0.0  # always true -- that's exactly the point

    plan = build_execution_plan(profile, c, r, policy)
    assert plan.strategy.agent_count > 1


# --- M4/M5/M6: role reasoning is fixed per role, not coupled to Builder ---


def test_m4_m5_m6_role_reasoning_is_fixed_and_not_floored_with_the_builder() -> None:
    """A critical-risk task floors the Builder's own reasoning (via
    CriticalRiskPolicy.min_reasoning), but the Reviewer keeps its fixed
    role-appropriate MEDIUM regardless -- there is no policy contradiction
    here, just two independent, deliberate design decisions."""
    profile = TaskProfile(description="critical security change", security_impact=1.0)
    c = assess_complexity(profile)
    r = assess_risk(profile)
    policy = Policy.default()
    plan = build_execution_plan(profile, c, r, policy)

    builder = next(a for a in plan.strategy.agents if a.role == AgentRole.BUILDER)
    reviewer = next(a for a in plan.strategy.agents if a.role == AgentRole.REVIEWER)

    assert builder.reasoning.rank >= policy.critical_risk.min_reasoning.rank
    assert reviewer.reasoning == ReasoningEffort.MEDIUM


# --- L1: two independent critical-risk overrides is defense-in-depth -----


def test_l1_blended_and_individual_critical_overrides_each_fire_independently() -> None:
    policy = Policy.default()

    # High individual signal, low blended score: only the per-signal path
    # can catch this (mirrors AG-03's own dilution scenario).
    diluted = RiskAssessment(score=0.2, level=RiskLevel.LOW, factors={"security_impact": 0.9})
    assert critical_signal_reasons(diluted, policy)
    tier, rationale = select_model_tier(
        ComplexityAssessment(score=0.1, level=ComplexityLevel.LOW), diluted, policy
    )
    assert any("individual risk signal" in r for r in rationale)

    # High blended score, no single extreme factor: only the blended path
    # can catch this.
    blended_only = RiskAssessment(
        score=0.9,
        level=RiskLevel.CRITICAL,
        factors={"security_impact": 0.5, "data_impact": 0.5, "irreversibility": 0.5},
    )
    assert not critical_signal_reasons(blended_only, policy)
    tier2, rationale2 = select_model_tier(
        ComplexityAssessment(score=0.1, level=ComplexityLevel.LOW), blended_only, policy
    )
    assert any("critical risk override" in r for r in rationale2)


# --- L3: architectural_impact legitimately feeds both complexity and risk


def test_l3_architectural_impact_intentionally_feeds_both_complexity_and_risk() -> None:
    profile = TaskProfile(description="large refactor", architectural_impact=0.9)
    c = assess_complexity(profile)
    r = assess_risk(profile)
    assert c.factors["architectural_impact"] == 0.9
    assert r.factors["architectural_impact"] == 0.9
    # And each assessment's *own* score is measurably affected by it.
    baseline = TaskProfile(description="large refactor")
    c0 = assess_complexity(baseline)
    r0 = assess_risk(baseline)
    assert c.score > c0.score
    assert r.score > r0.score


# --- L5: BLOCKED is not terminal; BLOCKED -> RECOVERING stays legal ------


def test_l5_blocked_is_not_terminal_and_can_still_recover() -> None:
    sm = ExecutionStateMachine(execution_id="e1")
    sm.transition(ExecutionState.RUNNING, at_seconds=0.0)
    sm.transition(ExecutionState.STALLED, at_seconds=1.0)
    sm.transition(ExecutionState.RECOVERING, at_seconds=2.0)
    sm.transition(ExecutionState.BLOCKED, at_seconds=3.0)

    assert sm.is_terminal is False
    assert sm.can_transition(ExecutionState.RECOVERING) is True
    sm.transition(ExecutionState.RECOVERING, at_seconds=4.0)
    assert sm.state == ExecutionState.RECOVERING

    sm2 = ExecutionStateMachine(execution_id="e2")
    sm2.transition(ExecutionState.RUNNING, at_seconds=0.0)
    sm2.transition(ExecutionState.REVIEWING, at_seconds=1.0)
    sm2.transition(ExecutionState.COMPLETED, at_seconds=2.0, evidence=("done",))
    assert sm2.is_terminal is True
    assert sm2.can_transition(ExecutionState.RECOVERING) is False


# --- L6: exporting low-level watchdog primitives alongside the coordinator


def test_l6_watchdog_package_exports_both_coordinator_and_low_level_primitives() -> None:
    import agentgear.watchdog as watchdog_pkg

    assert "ExecutionWatchdog" in watchdog_pkg.__all__
    for low_level_name in ("ExecutionStateMachine", "StallDetector", "LoopGuard", "RecoveryEngine"):
        assert low_level_name in watchdog_pkg.__all__
        assert hasattr(watchdog_pkg, low_level_name)
    # And the coordinator remains the one path that can reach BLOCKED with
    # a validated report / COMPLETED with evidence in ordinary usage.
    assert hasattr(ExecutionWatchdog, "start")


# --- L7: successful repeated identical activity still counts as circular -


def test_l7_successful_repeated_identical_activity_is_still_circular() -> None:
    from agentgear.config import WatchdogPolicy

    detector = StallDetector(WatchdogPolicy(max_identical_failures=2))
    activities = [
        ActivityRecord(at_seconds=float(i), fingerprint="same-read-only-check", succeeded=True)
        for i in range(4)
    ]
    verdict = detector.evaluate(
        now=10.0, started_at=0.0, last_progress_at=None, recent_activities=activities
    )
    assert verdict.is_stalled is True
    assert any("circular" in r for r in verdict.reasons)
