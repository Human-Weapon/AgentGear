"""Task analysis: turn a TaskProfile's raw signals into explainable,
deterministic ComplexityAssessment / RiskAssessment scores.

These numbers are NOT claimed to be scientifically precise measures of
"true" complexity or risk. They are a documented, configurable, auditable
heuristic: the same TaskProfile always produces the same score, and every
score carries its contributing factors so a human can see why.
"""

from __future__ import annotations

from .models import ComplexityAssessment, ComplexityLevel, RiskAssessment, RiskLevel, TaskProfile

# Saturating-count scale constants: how many files/modules before the
# "files affected" signal is considered maximally complex. Diminishing
# returns via n / (n + K) keeps a single huge outlier from dominating.
_FILES_SATURATION_K = 12.0
_MODULES_SATURATION_K = 5.0
_CRITICAL_INDIVIDUAL_RISK_THRESHOLD = 0.85


def _saturating(count: int, k: float) -> float:
    return count / (count + k)


def _complexity_level(score: float) -> ComplexityLevel:
    if score < 0.15:
        return ComplexityLevel.TRIVIAL
    if score < 0.35:
        return ComplexityLevel.LOW
    if score < 0.60:
        return ComplexityLevel.MODERATE
    if score < 0.85:
        return ComplexityLevel.HIGH
    return ComplexityLevel.VERY_HIGH


def _risk_level(score: float) -> RiskLevel:
    if score < 0.15:
        return RiskLevel.MINIMAL
    if score < 0.35:
        return RiskLevel.LOW
    if score < 0.60:
        return RiskLevel.MODERATE
    if score < 0.85:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


# Fixed, documented weights. Kept as module constants (not Policy) because
# they define what "complexity" and "risk" *mean* domain-wide, whereas
# Policy controls what to *do* once a score is known (tier thresholds,
# budgets, watchdog bounds).
_COMPLEXITY_WEIGHTS = {
    "files_affected": 0.20,
    "modules_affected": 0.20,
    "architectural_impact": 0.25,
    "ambiguity": 0.15,
    "novelty": 0.10,
    "low_test_coverage": 0.10,
}

_RISK_WEIGHTS = {
    "security_impact": 0.30,
    "data_impact": 0.25,
    "irreversibility": 0.20,
    "architectural_impact": 0.10,
    "prior_failures": 0.15,
}


def assess_complexity(profile: TaskProfile) -> ComplexityAssessment:
    factors = {
        "files_affected": _saturating(profile.files_affected, _FILES_SATURATION_K),
        "modules_affected": _saturating(profile.modules_affected, _MODULES_SATURATION_K),
        "architectural_impact": profile.architectural_impact,
        "ambiguity": profile.ambiguity,
        "novelty": profile.novelty,
        "low_test_coverage": 1.0 - profile.existing_test_coverage,
    }
    score = sum(factors[k] * _COMPLEXITY_WEIGHTS[k] for k in _COMPLEXITY_WEIGHTS)
    score = max(0.0, min(1.0, score))

    top = sorted(factors.items(), key=lambda kv: kv[1] * _COMPLEXITY_WEIGHTS[kv[0]], reverse=True)
    top_desc = ", ".join(f"{name}={value:.2f}" for name, value in top[:3])
    rationale = f"complexity={score:.2f}; dominant factors: {top_desc}"

    return ComplexityAssessment(
        score=score, level=_complexity_level(score), factors=factors, rationale=rationale
    )


def assess_risk(profile: TaskProfile) -> RiskAssessment:
    # prior_failures saturate quickly: 3+ prior failures on this task is
    # already maximal risk signal.
    prior_failure_signal = _saturating(profile.prior_failures, 3.0)
    factors = {
        "security_impact": profile.security_impact,
        "data_impact": profile.data_impact,
        "irreversibility": 1.0 - profile.reversibility,
        "architectural_impact": profile.architectural_impact,
        "prior_failures": prior_failure_signal,
    }
    score = sum(factors[k] * _RISK_WEIGHTS[k] for k in _RISK_WEIGHTS)
    score = max(0.0, min(1.0, score))

    top = sorted(factors.items(), key=lambda kv: kv[1] * _RISK_WEIGHTS[kv[0]], reverse=True)
    top_desc = ", ".join(f"{name}={value:.2f}" for name, value in top[:3])
    critical_signals = tuple(
        name
        for name in ("security_impact", "data_impact", "irreversibility")
        if factors[name] >= _CRITICAL_INDIVIDUAL_RISK_THRESHOLD
    )
    if critical_signals:
        level = RiskLevel.CRITICAL
        rationale = (
            f"risk={score:.2f}; critical individual signal(s): {', '.join(critical_signals)}; "
            f"dominant factors: {top_desc}"
        )
    else:
        level = _risk_level(score)
        rationale = f"risk={score:.2f}; dominant factors: {top_desc}"

    return RiskAssessment(score=score, level=level, factors=factors, rationale=rationale)
