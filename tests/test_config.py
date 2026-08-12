from __future__ import annotations

import pytest

from agentgear.config import (
    BudgetPolicy,
    ModelTierMapping,
    Policy,
    ReasoningThresholds,
    RoutingThresholds,
    RoutingWeights,
    WatchdogPolicy,
)
from agentgear.exceptions import ConfigurationError


def test_default_policy_is_valid() -> None:
    p = Policy.default()
    assert p.budget.max_agents == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_agents": -1},
        {"max_agents": 0},
        {"max_context_budget_tokens": -1},
        {"max_estimated_cost": -1.0},
        {"max_estimated_cost": 0.0},
        {"max_estimated_tokens": 0},
    ],
)
def test_budget_policy_rejects_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ConfigurationError):
        BudgetPolicy(**kwargs)


def test_routing_weights_all_zero_is_contradictory() -> None:
    with pytest.raises(ConfigurationError):
        RoutingWeights(cost_weight=0.0, quality_weight=0.0, latency_weight=0.0)


def test_routing_weights_negative_rejected() -> None:
    with pytest.raises(ConfigurationError):
        RoutingWeights(cost_weight=-0.1)


def test_routing_thresholds_out_of_order_rejected() -> None:
    with pytest.raises(ConfigurationError):
        RoutingThresholds(standard_at=0.6, advanced_at=0.5, frontier_at=0.9)


def test_routing_thresholds_above_one_rejected() -> None:
    with pytest.raises(ConfigurationError):
        RoutingThresholds(frontier_at=1.5)


def test_reasoning_thresholds_out_of_order_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ReasoningThresholds(low_at=0.5, medium_at=0.4)


def test_watchdog_policy_zero_retries_rejected() -> None:
    with pytest.raises(ConfigurationError):
        WatchdogPolicy(max_recovery_attempts=0)


def test_watchdog_policy_negative_rejected() -> None:
    with pytest.raises(ConfigurationError):
        WatchdogPolicy(max_total_attempts=-3)


def test_watchdog_policy_zero_no_progress_seconds_rejected() -> None:
    with pytest.raises(ConfigurationError):
        WatchdogPolicy(no_progress_seconds=0)


def test_watchdog_policy_negative_model_escalations_rejected() -> None:
    with pytest.raises(ConfigurationError):
        WatchdogPolicy(max_model_escalations=-1)


def test_watchdog_policy_allows_zero_model_escalations() -> None:
    w = WatchdogPolicy(max_model_escalations=0)
    assert w.max_model_escalations == 0


def test_watchdog_policy_contradictory_cycle_bounds_rejected() -> None:
    with pytest.raises(ConfigurationError):
        WatchdogPolicy(no_progress_cycles=10, max_no_progress_cycles=2)


def test_model_tier_mapping_requires_all_tiers() -> None:
    with pytest.raises(ConfigurationError):
        ModelTierMapping(mapping={"fast": "Luna"})


def test_model_tier_mapping_rejects_unknown_tier() -> None:
    with pytest.raises(ConfigurationError):
        ModelTierMapping(
            mapping={"fast": "A", "standard": "A", "advanced": "B", "frontier": "C", "bogus": "D"}
        )


def test_model_tier_mapping_rejects_empty_model_name() -> None:
    with pytest.raises(ConfigurationError):
        ModelTierMapping(mapping={"fast": "", "standard": "A", "advanced": "B", "frontier": "C"})


def test_policy_from_dict_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigurationError):
        Policy.from_dict({"not_a_real_field": 1})


def test_policy_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(ConfigurationError):
        Policy.from_dict([1, 2, 3])  # type: ignore[arg-type]


def test_policy_from_dict_round_trip() -> None:
    data = {
        "budget": {"max_agents": 6},
        "watchdog": {"max_recovery_attempts": 5},
        "default_reasoning_floor": "medium",
    }
    p = Policy.from_dict(data)
    assert p.budget.max_agents == 6
    assert p.watchdog.max_recovery_attempts == 5
    assert p.default_reasoning_floor.value == "medium"


def test_policy_rejects_unknown_reasoning_floor_string() -> None:
    with pytest.raises(ConfigurationError):
        Policy(default_reasoning_floor="not-a-real-effort")  # type: ignore[arg-type]


def test_policy_from_dict_unknown_nested_key_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Policy.from_dict({"budget": {"bogus_field": 1}})


def test_policy_multi_agent_thresholds_out_of_range_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Policy(multi_agent_risk_threshold=1.5)
    with pytest.raises(ConfigurationError):
        Policy(multi_agent_complexity_threshold=-0.2)


def test_policy_yaml_missing_pyyaml_raises_configuration_error(tmp_path, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    config_file = tmp_path / "policy.yaml"
    config_file.write_text("budget:\n  max_agents: 2\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        Policy.from_yaml(str(config_file))
