"""Verify AgentGear has no mandatory dependency on any sibling ecosystem
project.

"Standalone" means AgentGear's own code path never REQUIRES PromptGraph,
SkillGuard, AgentBench, or ProjectKaizen to function — it does NOT mean
those packages happen to be absent from whichever Python environment runs
this suite. An auditor's environment may have PromptGraph installed (e.g.
a shared hermes-oss monorepo venv); that must never make these tests
fail. So this suite never asserts "sibling X is not installed" against
the ambient environment. Instead it (a) statically proves no sibling is
imported at module load time, and (b) explicitly simulates the
"sibling unavailable" path via monkeypatching, so the degradation
behavior is verified deterministically regardless of what happens to be
on ``sys.path`` when the suite runs.
"""

from __future__ import annotations

import ast
import pathlib

import agentgear
from agentgear import _sibling_utils
from agentgear.context_provider import (
    ContextRequest,
    DefaultContextProvider,
    PromptGraphContextProvider,
    get_default_provider,
)

_SIBLING_PACKAGES = frozenset({"promptgraph", "skillguard", "agentbench", "projectkaizen"})


def test_agentgear_itself_is_importable_as_a_sibling() -> None:
    assert _sibling_utils.is_installed("agentgear") is True


def test_full_plan_pipeline_works_without_any_sibling() -> None:
    profile = agentgear.TaskProfile(description="ship a small fix", files_affected=2)
    plan = agentgear.plan(profile)
    assert plan.primary_model.tier is not None


def test_cli_plan_works_standalone() -> None:
    from agentgear.cli import main

    assert main(["plan", "--task", "standalone check", "--json"]) == 0


def test_agentgear_never_imports_siblings_at_module_level() -> None:
    """Static proof, independent of the runtime environment: no file in
    the ``agentgear`` package imports a sibling ecosystem package at
    module load time. Optional integration (``PromptGraphContextProvider``)
    only ever *checks availability* via ``importlib.util.find_spec`` at
    call time — it never imports the sibling module itself, since it only
    needs a caller-supplied duck-typed object.
    """
    package_dir = pathlib.Path(agentgear.__file__).parent
    for py_file in package_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module.split(".")[0]} if node.module else set()
            else:
                continue
            offending = names & _SIBLING_PACKAGES
            assert not offending, (
                f"{py_file} imports sibling package(s) {offending} at module level"
            )


def test_context_provider_degrades_when_promptgraph_forced_unavailable(monkeypatch) -> None:
    """Simulate "PromptGraph not installed" explicitly rather than relying
    on it actually being absent, so this test is deterministic in any
    environment."""
    monkeypatch.setattr(PromptGraphContextProvider, "is_available", staticmethod(lambda: False))
    monkeypatch.setattr(_sibling_utils, "is_installed", lambda name: False)

    provider = get_default_provider(promptgraph_instance=object())
    assert isinstance(provider, DefaultContextProvider)

    package = provider.request(ContextRequest(topic="anything", budget_tokens=500))
    assert package.source == "default"


def test_promptgraph_context_provider_reports_unavailable_when_forced(monkeypatch) -> None:
    monkeypatch.setattr(PromptGraphContextProvider, "is_available", staticmethod(lambda: False))
    provider = PromptGraphContextProvider(promptgraph_instance=object())
    package = provider.request(ContextRequest(topic="anything", budget_tokens=500))
    assert package.source == "default"
    assert "not installed" in package.note


def test_context_provider_degrades_when_promptgraph_forced_available_but_no_instance(
    monkeypatch,
) -> None:
    """Even if PromptGraph *is* importable (whatever the real environment
    happens to have), AgentGear must not construct or own an instance
    itself — no instance supplied means graceful degradation, not a
    crash, regardless of installation state."""
    monkeypatch.setattr(PromptGraphContextProvider, "is_available", staticmethod(lambda: True))
    provider = PromptGraphContextProvider(promptgraph_instance=None)
    package = provider.request(ContextRequest(topic="anything", budget_tokens=500))
    assert package.source == "default"
    assert "no PromptGraph instance" in package.note
