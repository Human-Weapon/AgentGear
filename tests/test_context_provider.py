from __future__ import annotations

import pytest

from agentgear.context_provider import (
    ContextRequest,
    DefaultContextProvider,
    PromptGraphContextProvider,
    _approx_token_count,
    get_default_provider,
)
from agentgear.exceptions import ConfigurationError


def test_context_request_rejects_empty_topic() -> None:
    with pytest.raises(ConfigurationError):
        ContextRequest(topic="  ", budget_tokens=100)


def test_context_request_rejects_non_positive_budget() -> None:
    with pytest.raises(ConfigurationError):
        ContextRequest(topic="auth", budget_tokens=0)


def test_default_provider_returns_empty_content_with_explanatory_note() -> None:
    provider = DefaultContextProvider()
    package = provider.request(ContextRequest(topic="auth", budget_tokens=1000))
    assert package.content == ""
    assert package.source == "default"
    assert "no context backend" in package.note


def test_default_provider_never_raises_when_no_backend_available() -> None:
    provider = DefaultContextProvider()
    package = provider.request(ContextRequest(topic="anything", budget_tokens=1))
    assert package.used_tokens == 0


def test_promptgraph_provider_degrades_when_sibling_not_installed() -> None:
    provider = PromptGraphContextProvider(promptgraph_instance=object())
    assert PromptGraphContextProvider.is_available() in (True, False)
    package = provider.request(ContextRequest(topic="auth", budget_tokens=1000))
    # Regardless of whether promptgraph happens to be installed in this
    # environment, an instance with no usable memory.search() must degrade
    # gracefully rather than raise.
    assert package.content == "" or package.source == "promptgraph"


def test_promptgraph_provider_uses_search_when_available(monkeypatch) -> None:
    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            return [{"content": f"note about {query}"}]

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    package = provider.request(ContextRequest(topic="auth", budget_tokens=1000))
    assert "note about auth" in package.content
    assert package.source == "promptgraph"


def test_promptgraph_provider_respects_budget(monkeypatch) -> None:
    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            return [{"content": "x" * 10_000}, {"content": "y" * 10_000}]

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    package = provider.request(ContextRequest(topic="auth", budget_tokens=100))
    assert package.used_tokens <= 100


def test_promptgraph_provider_survives_search_exception(monkeypatch) -> None:
    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            raise RuntimeError("backend exploded")

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    package = provider.request(ContextRequest(topic="auth", budget_tokens=100))
    assert "failed" in package.note


def test_get_default_provider_without_instance_is_default() -> None:
    provider = get_default_provider()
    assert isinstance(provider, DefaultContextProvider)


# --- AG-08: honest budget accounting and constraints provenance -----------


def test_default_provider_never_claims_applied_constraints() -> None:
    provider = DefaultContextProvider()
    package = provider.request(
        ContextRequest(topic="auth", budget_tokens=100, constraints=("no PII",))
    )
    assert package.constraints_requested == ("no PII",)
    assert package.constraints_applied == ()


def test_promptgraph_provider_never_claims_applied_constraints(monkeypatch) -> None:
    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            return [{"content": "a note"}]

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    package = provider.request(
        ContextRequest(topic="auth", budget_tokens=100, constraints=("no PII",))
    )
    assert package.constraints_requested == ("no PII",)
    assert package.constraints_applied == ()


def test_ag08_reported_used_tokens_matches_actual_content_including_separators(
    monkeypatch,
) -> None:
    """Regression: joining many small chunks with '\\n\\n' separators used
    to make the actual content larger than the sum of per-chunk token
    estimates, silently blowing the budget. used_tokens must always equal
    the real token count of package.content, and never exceed the budget.
    """

    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            return [{"content": "x" * 3} for _ in range(50)]

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    package = provider.request(ContextRequest(topic="auth", budget_tokens=40))

    actual = _approx_token_count(package.content)
    assert package.used_tokens == actual
    assert actual <= package.budget_tokens


def test_ag08_budget_invariant_holds_across_many_chunk_sizes(monkeypatch) -> None:
    class FakeMemory:
        def search(self, query: str, limit: int = 10):
            return [{"content": "z" * n} for n in range(1, 60)]

    class FakeInstance:
        memory = FakeMemory()

    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    provider = PromptGraphContextProvider(promptgraph_instance=FakeInstance())
    for budget in (10, 25, 50, 100, 500):
        package = provider.request(ContextRequest(topic="auth", budget_tokens=budget))
        actual = _approx_token_count(package.content)
        assert actual == package.used_tokens
        assert actual <= budget


def test_search_limit_must_be_positive() -> None:
    with pytest.raises(ConfigurationError):
        PromptGraphContextProvider(search_limit=0)
    with pytest.raises(ConfigurationError):
        PromptGraphContextProvider(search_limit=-1)


def test_search_limit_must_be_an_int() -> None:
    with pytest.raises(ConfigurationError):
        PromptGraphContextProvider(search_limit=1.5)  # type: ignore[arg-type]
