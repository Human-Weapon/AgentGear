from __future__ import annotations

import pytest

from agentgear.config import Policy
from agentgear.models import TaskProfile


@pytest.fixture
def policy() -> Policy:
    return Policy.default()


@pytest.fixture
def trivial_profile() -> TaskProfile:
    return TaskProfile(description="Rename a local variable", files_affected=1, modules_affected=1)


@pytest.fixture
def moderate_profile() -> TaskProfile:
    return TaskProfile(
        description="Add a new REST endpoint",
        files_affected=4,
        modules_affected=2,
        architectural_impact=0.2,
        ambiguity=0.2,
        existing_test_coverage=0.6,
    )


@pytest.fixture
def architectural_profile() -> TaskProfile:
    return TaskProfile(
        description="Restructure the repository's module boundaries",
        files_affected=40,
        modules_affected=15,
        architectural_impact=0.95,
        ambiguity=0.3,
        novelty=0.4,
        existing_test_coverage=0.5,
    )


@pytest.fixture
def high_risk_profile() -> TaskProfile:
    return TaskProfile(
        description="Change how session tokens are stored",
        files_affected=3,
        modules_affected=1,
        security_impact=0.95,
        data_impact=0.8,
        reversibility=0.1,
        existing_test_coverage=0.5,
    )


@pytest.fixture
def ambiguous_profile() -> TaskProfile:
    return TaskProfile(
        description="Investigate an intermittent production issue",
        files_affected=6,
        modules_affected=3,
        ambiguity=0.85,
        novelty=0.6,
        existing_test_coverage=0.4,
    )
