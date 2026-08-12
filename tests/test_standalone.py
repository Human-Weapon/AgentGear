"""Verify AgentGear works with none of its ecosystem siblings installed.

This dev environment intentionally installs only ``agentgear`` (plus
pytest/ruff/build tooling) — PromptGraph, SkillGuard, AgentBench, and
ProjectKaizen are never installed here, so these tests exercise real
standalone behavior rather than a mocked approximation of it.
"""

from __future__ import annotations

import agentgear
from agentgear import _sibling_utils
from agentgear.context_provider import ContextRequest, DefaultContextProvider, get_default_provider


def test_no_siblings_are_installed_in_this_environment() -> None:
    for name in ("promptgraph", "skillguard", "agentbench", "projectkaizen"):
        assert _sibling_utils.is_installed(name) is False


def test_agentgear_itself_is_importable_as_a_sibling() -> None:
    assert _sibling_utils.is_installed("agentgear") is True


def test_full_plan_pipeline_works_without_any_sibling() -> None:
    profile = agentgear.TaskProfile(description="ship a small fix", files_affected=2)
    plan = agentgear.plan(profile)
    assert plan.primary_model.tier is not None


def test_context_provider_degrades_gracefully_without_promptgraph() -> None:
    provider = get_default_provider()
    assert isinstance(provider, DefaultContextProvider)
    package = provider.request(ContextRequest(topic="anything", budget_tokens=500))
    assert package.source == "default"


def test_cli_plan_works_standalone() -> None:
    from agentgear.cli import main

    assert main(["plan", "--task", "standalone check", "--json"]) == 0
