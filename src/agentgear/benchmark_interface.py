"""AgentBench-ready interface.

AgentGear v0.1.0 does NOT depend on AgentBench and does not implement it.
This module only defines the shape of evidence AgentGear could one day
consume to tune its routing policy (e.g. "FAST/medium has a 40% stall
rate on tasks like this; stop recommending it"). Nothing in the routing,
planning, or watchdog modules imports or requires this interface — it
exists purely so a future AgentBench integration has a stable contract to
implement against.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import ModelTier, ReasoningEffort


@dataclass(frozen=True)
class StrategyEvidence:
    """Observed outcomes for one (tier, reasoning) strategy on a class of
    tasks, as reported by an external benchmarking tool."""

    tier: ModelTier
    reasoning: ReasoningEffort
    task_class: str
    sample_size: int
    success_rate: float
    average_cost: float
    average_latency_seconds: float
    regression_rate: float
    stall_rate: float
    recovery_rate: float


class EvidenceSource(ABC):
    """Abstract source of ``StrategyEvidence``. AgentBench (or any other
    benchmarking tool) can implement this to feed evidence into AgentGear
    policy tuning in a future release. Not wired into routing in v0.1.0.
    """

    @abstractmethod
    def evidence_for(self, task_class: str) -> tuple[StrategyEvidence, ...]: ...
