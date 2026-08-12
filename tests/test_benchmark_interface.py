from __future__ import annotations

import pytest

from agentgear.benchmark_interface import EvidenceSource, StrategyEvidence
from agentgear.models import ModelTier, ReasoningEffort


def test_evidence_source_is_abstract() -> None:
    with pytest.raises(TypeError):
        EvidenceSource()  # type: ignore[abstract]


def test_strategy_evidence_holds_provided_fields() -> None:
    evidence = StrategyEvidence(
        tier=ModelTier.FAST,
        reasoning=ReasoningEffort.MEDIUM,
        task_class="rename",
        sample_size=42,
        success_rate=0.9,
        average_cost=0.01,
        average_latency_seconds=3.2,
        regression_rate=0.01,
        stall_rate=0.02,
        recovery_rate=0.5,
    )
    assert evidence.tier == ModelTier.FAST
    assert evidence.sample_size == 42


def test_custom_evidence_source_implementation_works() -> None:
    class FakeSource(EvidenceSource):
        def evidence_for(self, task_class: str) -> tuple[StrategyEvidence, ...]:
            return ()

    source = FakeSource()
    assert source.evidence_for("anything") == ()
