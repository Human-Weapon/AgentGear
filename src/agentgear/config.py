"""Policy / configuration for AgentGear.

All magic numbers live here, not in routing/planning/watchdog logic. Every
field is validated on construction so an invalid policy fails loudly at
startup rather than producing silently-wrong plans.

The default tier -> model mapping is a POLICY, not a truth baked into the
router: swap it in your own config without touching any routing code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any

from .exceptions import ConfigurationError
from .models import ModelTier, ReasoningEffort

_VALID_TIERS = {t.value for t in ModelTier}
_VALID_REASONING = {r.value for r in ReasoningEffort}


def _require_finite_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value):
        raise ConfigurationError(f"{name} must be finite, got {value}")
    return float(value)


def _require_non_negative(name: str, value: Any) -> float:
    v = _require_finite_number(name, value)
    if v < 0:
        raise ConfigurationError(f"{name} must be >= 0, got {v}")
    return v


def _require_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 1:
        raise ConfigurationError(f"{name} must be >= 1, got {value}")
    return value


def _require_non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ConfigurationError(f"{name} must be >= 0, got {value}")
    return value


@dataclass(frozen=True)
class RoutingWeights:
    """Relative importance of cost/quality/latency in tier selection.

    Weights need not sum to 1 (they are normalized internally) but at
    least one must be strictly positive, or every candidate ties and the
    router degenerates into an unexplainable coin flip.
    """

    cost_weight: float = 0.4
    quality_weight: float = 0.4
    latency_weight: float = 0.2

    def __post_init__(self) -> None:
        cw = _require_non_negative("routing.cost_weight", self.cost_weight)
        qw = _require_non_negative("routing.quality_weight", self.quality_weight)
        lw = _require_non_negative("routing.latency_weight", self.latency_weight)
        object.__setattr__(self, "cost_weight", cw)
        object.__setattr__(self, "quality_weight", qw)
        object.__setattr__(self, "latency_weight", lw)
        if cw + qw + lw <= 0:
            raise ConfigurationError(
                "routing weights are contradictory: cost_weight, quality_weight, and "
                "latency_weight cannot all be zero"
            )

    def normalized(self) -> tuple[float, float, float]:
        total = self.cost_weight + self.quality_weight + self.latency_weight
        return (self.cost_weight / total, self.quality_weight / total, self.latency_weight / total)


@dataclass(frozen=True)
class RoutingThresholds:
    """Score breakpoints (on a 0..1 combined complexity/risk score) that
    select a model tier. Each threshold is the minimum score required to
    reach that tier; FAST has an implicit threshold of 0.0.
    """

    standard_at: float = 0.25
    advanced_at: float = 0.55
    frontier_at: float = 0.80

    def __post_init__(self) -> None:
        s = _require_non_negative("routing.standard_at", self.standard_at)
        a = _require_non_negative("routing.advanced_at", self.advanced_at)
        f = _require_non_negative("routing.frontier_at", self.frontier_at)
        for name, v in (("standard_at", s), ("advanced_at", a), ("frontier_at", f)):
            if v > 1.0:
                raise ConfigurationError(f"routing.{name} must be <= 1.0, got {v}")
        if not (s <= a <= f):
            raise ConfigurationError(
                "routing thresholds are contradictory: require "
                f"standard_at <= advanced_at <= frontier_at, got {s}, {a}, {f}"
            )
        object.__setattr__(self, "standard_at", s)
        object.__setattr__(self, "advanced_at", a)
        object.__setattr__(self, "frontier_at", f)


@dataclass(frozen=True)
class ReasoningThresholds:
    """Score breakpoints for reasoning effort. Deliberately independent
    from ``RoutingThresholds``: reasoning is driven by a different blend
    of complexity/risk (see ``routing.reasoning_score``), so
    ``tier=X, reasoning=high`` never implicitly means the same thing as
    ``tier=Y, reasoning=low`` for a different tier.
    """

    low_at: float = 0.15
    medium_at: float = 0.35
    high_at: float = 0.60
    xhigh_at: float = 0.80
    max_at: float = 0.93

    def __post_init__(self) -> None:
        values = {
            "low_at": _require_non_negative("reasoning.low_at", self.low_at),
            "medium_at": _require_non_negative("reasoning.medium_at", self.medium_at),
            "high_at": _require_non_negative("reasoning.high_at", self.high_at),
            "xhigh_at": _require_non_negative("reasoning.xhigh_at", self.xhigh_at),
            "max_at": _require_non_negative("reasoning.max_at", self.max_at),
        }
        for name, v in values.items():
            if v > 1.0:
                raise ConfigurationError(f"reasoning.{name} must be <= 1.0, got {v}")
            object.__setattr__(self, name, v)
        ordered = [
            values["low_at"],
            values["medium_at"],
            values["high_at"],
            values["xhigh_at"],
            values["max_at"],
        ]
        if ordered != sorted(ordered):
            raise ConfigurationError(
                "reasoning thresholds are contradictory: require "
                f"low_at <= medium_at <= high_at <= xhigh_at <= max_at, got {ordered}"
            )


@dataclass(frozen=True)
class WatchdogPolicy:
    """Bounds for stall detection, recovery, and escalation loops.

    Every bound here exists so a stuck agent fails LOUD (STALLED ->
    RECOVERING -> BLOCKED) instead of running forever or vanishing.
    """

    enabled: bool = True
    no_progress_seconds: float = 120.0
    no_progress_cycles: int = 3
    max_identical_failures: int = 2
    max_recovery_attempts: int = 3
    max_no_progress_cycles: int = 5
    max_total_attempts: int = 10
    max_model_escalations: int = 2
    trivial_command_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("watchdog.enabled must be a bool")
        object.__setattr__(
            self,
            "no_progress_seconds",
            _require_non_negative("watchdog.no_progress_seconds", self.no_progress_seconds),
        )
        if self.no_progress_seconds == 0:
            raise ConfigurationError("watchdog.no_progress_seconds must be > 0")
        object.__setattr__(
            self,
            "no_progress_cycles",
            _require_positive_int("watchdog.no_progress_cycles", self.no_progress_cycles),
        )
        object.__setattr__(
            self,
            "max_identical_failures",
            _require_positive_int("watchdog.max_identical_failures", self.max_identical_failures),
        )
        object.__setattr__(
            self,
            "max_recovery_attempts",
            _require_positive_int("watchdog.max_recovery_attempts", self.max_recovery_attempts),
        )
        object.__setattr__(
            self,
            "max_no_progress_cycles",
            _require_positive_int("watchdog.max_no_progress_cycles", self.max_no_progress_cycles),
        )
        object.__setattr__(
            self,
            "max_total_attempts",
            _require_positive_int("watchdog.max_total_attempts", self.max_total_attempts),
        )
        object.__setattr__(
            self,
            "max_model_escalations",
            _require_non_negative_int("watchdog.max_model_escalations", self.max_model_escalations),
        )
        object.__setattr__(
            self,
            "trivial_command_timeout_seconds",
            _require_non_negative(
                "watchdog.trivial_command_timeout_seconds", self.trivial_command_timeout_seconds
            ),
        )
        if self.trivial_command_timeout_seconds == 0:
            raise ConfigurationError("watchdog.trivial_command_timeout_seconds must be > 0")
        if self.max_no_progress_cycles < self.no_progress_cycles:
            raise ConfigurationError(
                "watchdog policy is contradictory: max_no_progress_cycles "
                f"({self.max_no_progress_cycles}) must be >= no_progress_cycles "
                f"({self.no_progress_cycles})"
            )


@dataclass(frozen=True)
class BudgetPolicy:
    """Hard compute/cost ceilings. A plan that would exceed these is never
    silently returned — callers get BudgetExceededError instead."""

    max_agents: int = 4
    max_context_budget_tokens: int = 32_000
    max_estimated_cost: float = 5.0
    max_estimated_tokens: int = 200_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_agents", _require_positive_int("budget.max_agents", self.max_agents)
        )
        object.__setattr__(
            self,
            "max_context_budget_tokens",
            _require_positive_int(
                "budget.max_context_budget_tokens", self.max_context_budget_tokens
            ),
        )
        object.__setattr__(
            self,
            "max_estimated_cost",
            _require_non_negative("budget.max_estimated_cost", self.max_estimated_cost),
        )
        if self.max_estimated_cost == 0:
            raise ConfigurationError("budget.max_estimated_cost must be > 0")
        object.__setattr__(
            self,
            "max_estimated_tokens",
            _require_positive_int("budget.max_estimated_tokens", self.max_estimated_tokens),
        )


_DEFAULT_TIER_MODEL_MAP: dict[str, str] = {
    ModelTier.FAST.value: "Luna",
    ModelTier.STANDARD.value: "Luna",
    ModelTier.ADVANCED.value: "Terra",
    ModelTier.FRONTIER.value: "Sol",
}


@dataclass(frozen=True)
class ModelTierMapping:
    """Maps conceptual tiers to real provider/model names. Purely data —
    the router never hardcodes a provider or model name."""

    mapping: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_TIER_MODEL_MAP))

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, dict):
            raise ConfigurationError("model_tier_mapping must be a dict")
        missing = _VALID_TIERS - set(self.mapping)
        if missing:
            raise ConfigurationError(
                f"model_tier_mapping is missing entries for tiers: {sorted(missing)}"
            )
        for tier_name, model_name in self.mapping.items():
            if tier_name not in _VALID_TIERS:
                raise ConfigurationError(
                    f"model_tier_mapping has unknown tier '{tier_name}'; "
                    f"valid tiers are {sorted(_VALID_TIERS)}"
                )
            if not isinstance(model_name, str) or not model_name.strip():
                raise ConfigurationError(
                    f"model_tier_mapping['{tier_name}'] must be a non-empty string"
                )

    def resolve(self, tier: ModelTier) -> str:
        return self.mapping[tier.value]


def _coerce_tier(name: str, value: ModelTier | str) -> ModelTier:
    if isinstance(value, ModelTier):
        return value
    if isinstance(value, str):
        if value not in _VALID_TIERS:
            raise ConfigurationError(
                f"{name} '{value}' is not a known model tier; valid tiers are "
                f"{sorted(_VALID_TIERS)}"
            )
        return ModelTier(value)
    raise ConfigurationError(f"{name} must be a ModelTier or known string value")


def _coerce_reasoning(name: str, value: ReasoningEffort | str) -> ReasoningEffort:
    if isinstance(value, ReasoningEffort):
        return value
    if isinstance(value, str):
        if value not in _VALID_REASONING:
            raise ConfigurationError(
                f"{name} '{value}' is not a known reasoning effort; valid values are "
                f"{sorted(_VALID_REASONING)}"
            )
        return ReasoningEffort(value)
    raise ConfigurationError(f"{name} must be a ReasoningEffort or known string value")


@dataclass(frozen=True)
class CriticalRiskPolicy:
    """Per-signal critical-risk floors, independent of the blended risk
    score (AG-03).

    A single maxed-out individual risk signal (e.g. ``security_impact=1.0``
    on an otherwise trivial task) must not be diluted into a merely "low"
    blended risk score and routed cheaply. When any of these thresholds is
    met, routing is floored at ``min_tier``/``min_reasoning`` and planning
    forces an independent review, regardless of what the blended
    complexity/risk score alone would have selected.
    """

    security_impact_at: float = 0.85
    data_impact_at: float = 0.85
    irreversibility_at: float = 0.85
    min_tier: ModelTier = ModelTier.ADVANCED
    min_reasoning: ReasoningEffort = ReasoningEffort.HIGH
    require_review: bool = True

    def __post_init__(self) -> None:
        for name in ("security_impact_at", "data_impact_at", "irreversibility_at"):
            v = _require_non_negative(f"critical_risk.{name}", getattr(self, name))
            if v > 1.0:
                raise ConfigurationError(f"critical_risk.{name} must be <= 1.0, got {v}")
            object.__setattr__(self, name, v)
        object.__setattr__(self, "min_tier", _coerce_tier("critical_risk.min_tier", self.min_tier))
        object.__setattr__(
            self,
            "min_reasoning",
            _coerce_reasoning("critical_risk.min_reasoning", self.min_reasoning),
        )
        if not isinstance(self.require_review, bool):
            raise ConfigurationError("critical_risk.require_review must be a bool")


@dataclass(frozen=True)
class Policy:
    """Top-level AgentGear configuration."""

    routing_weights: RoutingWeights = field(default_factory=RoutingWeights)
    routing_thresholds: RoutingThresholds = field(default_factory=RoutingThresholds)
    reasoning_thresholds: ReasoningThresholds = field(default_factory=ReasoningThresholds)
    watchdog: WatchdogPolicy = field(default_factory=WatchdogPolicy)
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    model_tier_mapping: ModelTierMapping = field(default_factory=ModelTierMapping)
    critical_risk: CriticalRiskPolicy = field(default_factory=CriticalRiskPolicy)
    default_reasoning_floor: ReasoningEffort = ReasoningEffort.LOW
    multi_agent_risk_threshold: float = 0.5
    multi_agent_complexity_threshold: float = 0.55

    def __post_init__(self) -> None:
        if not isinstance(self.routing_weights, RoutingWeights):
            raise ConfigurationError("routing_weights must be a RoutingWeights instance")
        if not isinstance(self.routing_thresholds, RoutingThresholds):
            raise ConfigurationError("routing_thresholds must be a RoutingThresholds instance")
        if not isinstance(self.reasoning_thresholds, ReasoningThresholds):
            raise ConfigurationError("reasoning_thresholds must be a ReasoningThresholds instance")
        if not isinstance(self.watchdog, WatchdogPolicy):
            raise ConfigurationError("watchdog must be a WatchdogPolicy instance")
        if not isinstance(self.budget, BudgetPolicy):
            raise ConfigurationError("budget must be a BudgetPolicy instance")
        if not isinstance(self.model_tier_mapping, ModelTierMapping):
            raise ConfigurationError("model_tier_mapping must be a ModelTierMapping instance")
        if not isinstance(self.critical_risk, CriticalRiskPolicy):
            raise ConfigurationError("critical_risk must be a CriticalRiskPolicy instance")
        if isinstance(self.default_reasoning_floor, str):
            if self.default_reasoning_floor not in _VALID_REASONING:
                raise ConfigurationError(
                    f"default_reasoning_floor '{self.default_reasoning_floor}' is not a known "
                    f"reasoning effort; valid values are {sorted(_VALID_REASONING)}"
                )
            object.__setattr__(
                self, "default_reasoning_floor", ReasoningEffort(self.default_reasoning_floor)
            )
        elif not isinstance(self.default_reasoning_floor, ReasoningEffort):
            raise ConfigurationError(
                "default_reasoning_floor must be a ReasoningEffort or known string value"
            )
        object.__setattr__(
            self,
            "multi_agent_risk_threshold",
            _require_non_negative("multi_agent_risk_threshold", self.multi_agent_risk_threshold),
        )
        object.__setattr__(
            self,
            "multi_agent_complexity_threshold",
            _require_non_negative(
                "multi_agent_complexity_threshold", self.multi_agent_complexity_threshold
            ),
        )
        if self.multi_agent_risk_threshold > 1.0:
            raise ConfigurationError("multi_agent_risk_threshold must be <= 1.0")
        if self.multi_agent_complexity_threshold > 1.0:
            raise ConfigurationError("multi_agent_complexity_threshold must be <= 1.0")

    @classmethod
    def default(cls) -> Policy:
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        """Build a Policy from a nested dict (e.g. parsed YAML/JSON).

        Unknown top-level keys raise ConfigurationError rather than being
        silently ignored, so typos in config files are caught early.
        """
        if not isinstance(data, dict):
            raise ConfigurationError(f"policy config must be a mapping, got {type(data).__name__}")

        known_keys = {f.name for f in fields(cls)}
        unknown = set(data) - known_keys
        if unknown:
            raise ConfigurationError(f"unknown policy keys: {sorted(unknown)}")

        kwargs: dict[str, Any] = {}
        if "routing_weights" in data:
            kwargs["routing_weights"] = _build(RoutingWeights, data["routing_weights"])
        if "routing_thresholds" in data:
            kwargs["routing_thresholds"] = _build(RoutingThresholds, data["routing_thresholds"])
        if "reasoning_thresholds" in data:
            kwargs["reasoning_thresholds"] = _build(
                ReasoningThresholds, data["reasoning_thresholds"]
            )
        if "watchdog" in data:
            kwargs["watchdog"] = _build(WatchdogPolicy, data["watchdog"])
        if "budget" in data:
            kwargs["budget"] = _build(BudgetPolicy, data["budget"])
        if "model_tier_mapping" in data:
            raw = data["model_tier_mapping"]
            if isinstance(raw, dict) and "mapping" in raw:
                kwargs["model_tier_mapping"] = ModelTierMapping(mapping=raw["mapping"])
            elif isinstance(raw, dict):
                kwargs["model_tier_mapping"] = ModelTierMapping(mapping=raw)
            else:
                raise ConfigurationError("model_tier_mapping must be a mapping of tier -> model")
        if "critical_risk" in data:
            kwargs["critical_risk"] = _build(CriticalRiskPolicy, data["critical_risk"])
        for scalar in (
            "default_reasoning_floor",
            "multi_agent_risk_threshold",
            "multi_agent_complexity_threshold",
        ):
            if scalar in data:
                kwargs[scalar] = data[scalar]
        return cls(**kwargs)

    @classmethod
    def from_yaml(cls, path: str) -> Policy:
        """Build a Policy from a YAML file. Requires the optional
        ``PyYAML`` dependency (``pip install agentgear[yaml]``)."""
        try:
            import yaml
        except ImportError as exc:
            raise ConfigurationError(
                "reading YAML policy files requires PyYAML: pip install agentgear[yaml]"
            ) from exc
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)


def _build(dataclass_type: type, data: Any) -> Any:
    if isinstance(data, dataclass_type):
        return data
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"{dataclass_type.__name__} config must be a mapping, got {type(data).__name__}"
        )
    known = {f.name for f in fields(dataclass_type)}
    unknown = set(data) - known
    if unknown:
        raise ConfigurationError(f"unknown {dataclass_type.__name__} keys: {sorted(unknown)}")
    return dataclass_type(**data)
