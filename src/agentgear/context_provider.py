"""ContextProvider: AgentGear can decide it *needs* context ("topic AUTH,
budget 8000 tokens") without ever building the context graph itself — that
responsibility belongs to PromptGraph. This module defines the abstract
interface plus a working standalone default and an optional, best-effort
PromptGraph-backed implementation.

AgentGear never imports PromptGraph at module load time. Availability is
checked at call time via ``_sibling_utils``; if PromptGraph is not
installed (or its API surface doesn't behave as expected), callers get a
``ContextPackage`` back with clear provenance instead of a crash.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from . import _sibling_utils
from .exceptions import ConfigurationError


@dataclass(frozen=True)
class ContextRequest:
    topic: str
    budget_tokens: int
    constraints: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ConfigurationError("ContextRequest.topic must be a non-empty string")
        if not isinstance(self.budget_tokens, int) or isinstance(self.budget_tokens, bool):
            raise ConfigurationError("ContextRequest.budget_tokens must be an int")
        if self.budget_tokens <= 0:
            raise ConfigurationError("ContextRequest.budget_tokens must be > 0")


@dataclass(frozen=True)
class ContextPackage:
    topic: str
    content: str
    budget_tokens: int
    used_tokens: int
    source: str
    constraints_applied: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


class ContextProvider(ABC):
    """Abstract source of task context. AgentGear only ever depends on
    this interface, never on a concrete backend.
    """

    @abstractmethod
    def request(self, request: ContextRequest) -> ContextPackage: ...


class DefaultContextProvider(ContextProvider):
    """Standalone provider: no external context backend is configured.

    Returns an explicit, empty-content package rather than fabricating
    context, so callers can tell "no context backend available" apart
    from "context backend returned nothing relevant".
    """

    def request(self, request: ContextRequest) -> ContextPackage:
        return ContextPackage(
            topic=request.topic,
            content="",
            budget_tokens=request.budget_tokens,
            used_tokens=0,
            source="default",
            constraints_applied=request.constraints,
            note=(
                "no context backend is configured; install and wire a ContextProvider "
                "(e.g. PromptGraphContextProvider) to receive real context packages"
            ),
        )


def _approx_token_count(text: str) -> int:
    # Deterministic, provider-agnostic estimate: ~4 chars/token.
    return max(1, len(text) // 4)


class PromptGraphContextProvider(ContextProvider):
    """Optional, best-effort integration with an existing PromptGraph
    instance's technical memory.

    Requires the caller to supply an already-constructed
    ``promptgraph.PromptGraph`` (or any duck-typed object exposing
    ``.memory.search(query, limit)``); AgentGear does not construct or
    own a PromptGraph instance itself. If PromptGraph is not installed,
    or the supplied object doesn't support ``search``, this degrades to
    ``DefaultContextProvider`` behavior with a note explaining why.
    """

    def __init__(
        self, promptgraph_instance: object | None = None, *, search_limit: int = 10
    ) -> None:
        self._instance = promptgraph_instance
        self._search_limit = search_limit
        self._fallback = DefaultContextProvider()

    @staticmethod
    def is_available() -> bool:
        return _sibling_utils.is_installed("promptgraph")

    def request(self, request: ContextRequest) -> ContextPackage:
        if not self.is_available() or self._instance is None:
            package = self._fallback.request(request)
            note = (
                "promptgraph is not installed"
                if not self.is_available()
                else "no PromptGraph instance was supplied to PromptGraphContextProvider"
            )
            return ContextPackage(**{**package.__dict__, "note": note})

        memory = getattr(self._instance, "memory", None)
        search = getattr(memory, "search", None)
        if not callable(search):
            package = self._fallback.request(request)
            return ContextPackage(
                **{
                    **package.__dict__,
                    "note": "supplied PromptGraph instance has no usable memory.search()",
                }
            )

        try:
            results = search(request.topic, limit=self._search_limit)
        except Exception as exc:  # noqa: BLE001 - optional integration must never crash callers
            package = self._fallback.request(request)
            return ContextPackage(
                **{**package.__dict__, "note": f"promptgraph.memory.search() failed: {exc}"}
            )

        chunks: list[str] = []
        used_tokens = 0
        for entry in results or []:
            content = str(entry.get("content", "")) if isinstance(entry, dict) else str(entry)
            if not content:
                continue
            tokens = _approx_token_count(content)
            if used_tokens + tokens > request.budget_tokens:
                break
            chunks.append(content)
            used_tokens += tokens

        combined = "\n\n".join(chunks)
        return ContextPackage(
            topic=request.topic,
            content=combined,
            budget_tokens=request.budget_tokens,
            used_tokens=used_tokens,
            source="promptgraph",
            constraints_applied=request.constraints,
            note=f"{len(chunks)} of {len(results or [])} matching memory entries fit the budget",
        )


def get_default_provider(promptgraph_instance: object | None = None) -> ContextProvider:
    """Convenience factory: use PromptGraph if available and supplied,
    otherwise fall back to the standalone default. AgentGear itself never
    calls this automatically — callers opt in explicitly.
    """
    if promptgraph_instance is not None and PromptGraphContextProvider.is_available():
        return PromptGraphContextProvider(promptgraph_instance)
    return DefaultContextProvider()
