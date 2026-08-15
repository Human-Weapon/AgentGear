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


# --- Round 2 / H2: constraints has a strict tuple[str, ...] contract ------


@pytest.mark.parametrize(
    "bad_constraints",
    [
        "no_pii",  # a bare string is iterable char-by-char -- must be rejected, not coerced
        ["no_pii"],
        42,
        None,
        ("valid", 3),
        ("",),
        ("   ",),
    ],
)
def test_context_request_rejects_invalid_constraints_shape(bad_constraints) -> None:
    with pytest.raises(ConfigurationError):
        ContextRequest(topic="auth", budget_tokens=100, constraints=bad_constraints)


def test_context_request_accepts_well_formed_constraints_tuple() -> None:
    request = ContextRequest(
        topic="auth", budget_tokens=100, constraints=("no_pii", "redact_emails")
    )
    assert request.constraints == ("no_pii", "redact_emails")


def test_context_request_default_constraints_is_empty_tuple() -> None:
    request = ContextRequest(topic="auth", budget_tokens=100)
    assert request.constraints == ()


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


# --- Round 4 / NEW-05: iterable/generator adapter safety --------------------


def _available_provider(monkeypatch, memory, **kwargs) -> PromptGraphContextProvider:
    class FakeInstance:
        pass

    instance = FakeInstance()
    instance.memory = memory
    monkeypatch.setattr(
        "agentgear.context_provider.PromptGraphContextProvider.is_available",
        staticmethod(lambda: True),
    )
    return PromptGraphContextProvider(promptgraph_instance=instance, **kwargs)


def test_search_returning_a_list_still_works(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return [{"content": "a"}, {"content": "b"}]

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "promptgraph"
    assert "a" in package.content and "b" in package.content


def test_search_returning_a_tuple_still_works(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return ({"content": "a"}, {"content": "b"})

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "promptgraph"


def test_search_returning_a_generator_does_not_crash_on_len(monkeypatch) -> None:
    """The exact NEW-05 regression: the old code called len(results or [])
    for the note string, which raises TypeError on a generator."""

    class Memory:
        def search(self, query, limit=10):
            yield {"content": "a"}
            yield {"content": "b"}

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "promptgraph"
    assert "a" in package.content and "b" in package.content


def test_effectively_infinite_generator_is_bounded_by_search_limit(monkeypatch) -> None:
    """An infinite generator of empty-content entries never trips the
    budget-based break, so consumption must be independently bounded by
    search_limit or this would hang forever."""

    class Memory:
        def search(self, query, limit=10):
            i = 0
            while True:
                i += 1
                yield {"content": ""}

    provider = _available_provider(monkeypatch, Memory(), search_limit=5)
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "promptgraph"
    assert package.content == ""
    assert "0 of 5 considered" in package.note


def test_generator_exception_before_first_yield_falls_back_safely(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            raise RuntimeError("cannot even start")
            yield {"content": "unreachable"}  # noqa: F401,E501 (unreachable, marks this a generator)

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "default"
    assert "failed" in package.note


def test_generator_exception_after_one_yield_falls_back_safely(monkeypatch) -> None:
    """The exact NEW-05 regression: an exception raised DURING iteration
    (not from the initial search() call) used to escape uncaught."""

    class Memory:
        def search(self, query, limit=10):
            yield {"content": "first"}
            raise RuntimeError("boom mid-iteration")

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "default"
    assert "failed" in package.note
    assert "boom mid-iteration" in package.note


@pytest.mark.parametrize(
    "malformed_item",
    [
        None,
        42,
        "a bare string",
        {"no_content_key": "x"},
    ],
)
def test_malformed_items_do_not_crash(monkeypatch, malformed_item) -> None:
    class Memory:
        def search(self, query, limit=10):
            return [malformed_item]

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    # Either coerced via str()/`.get(..., "")` and included, or (for an
    # item whose own conversion raises) safely falls back -- either way,
    # this must never raise out of request().
    assert package.source in ("promptgraph", "default")


def test_item_with_broken_str_falls_back_safely(monkeypatch) -> None:
    class Broken:
        def __str__(self):
            raise RuntimeError("broken __str__")

    class Memory:
        def search(self, query, limit=10):
            return [Broken()]

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "default"
    assert "failed" in package.note


def test_none_return_value_does_not_crash(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return None

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "default"


def test_wrong_return_type_does_not_crash(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return 42  # not iterable at all

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "default"
    assert "failed" in package.note


def test_unicode_content_is_handled(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return [{"content": "héllo wörld 你好 🎉"}]

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1000))
    assert package.source == "promptgraph"
    assert "你好" in package.content


def test_budget_of_one_token_does_not_crash(monkeypatch) -> None:
    class Memory:
        def search(self, query, limit=10):
            return [{"content": "some real content here"}]

    provider = _available_provider(monkeypatch, Memory())
    package = provider.request(ContextRequest(topic="x", budget_tokens=1))
    assert package.used_tokens <= 1


def test_search_limit_of_one_bounds_consumption(monkeypatch) -> None:
    seen = []

    class Memory:
        def search(self, query, limit=10):
            for i in range(100):
                seen.append(i)
                yield {"content": f"chunk-{i}"}

    provider = _available_provider(monkeypatch, Memory(), search_limit=1)
    provider.request(ContextRequest(topic="x", budget_tokens=10_000))
    assert len(seen) == 1


def test_final_rendered_content_never_exceeds_budget_across_failure_modes(monkeypatch) -> None:
    """AG-08's budget invariant must hold on every path: normal, fallback,
    and generator-failure."""

    def _raises(self, q, limit=10):
        raise RuntimeError("boom")

    memories = [
        type("M", (), {"search": lambda self, q, limit=10: [{"content": "x" * 5000}]})(),
        type("M", (), {"search": _raises})(),
    ]
    for memory in memories:
        provider = _available_provider(monkeypatch, memory)
        package = provider.request(ContextRequest(topic="x", budget_tokens=50))
        assert package.used_tokens <= 50
