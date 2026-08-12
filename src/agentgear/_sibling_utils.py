"""Optional sibling integration helpers.

Implements the ecosystem's "USEFUL ALONE, BETTER TOGETHER" pattern with
graceful degradation: each integration is discovered at runtime via
``importlib.util`` and is completely optional. AgentGear works fully
standalone with none of its siblings installed.
"""

from __future__ import annotations

import importlib.util
from typing import Any

_SIBLINGS = ("promptgraph", "skillguard", "agentbench", "projectkaizen", "agentgear")


def is_installed(name: str) -> bool:
    """Return True if the named package is importable (installed)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def load_sibling(name: str) -> Any | None:
    """Import and return a sibling package, or None if not installed.

    Never raises due to a missing or broken sibling — that is the whole
    point of optional integration.
    """
    if not is_installed(name):
        return None
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - degrade gracefully on any failure
        return None


def sibling_versions() -> dict[str, str | None]:
    """Report which ecosystem siblings are installed and their versions."""
    out: dict[str, str | None] = {}
    for name in _SIBLINGS:
        mod = load_sibling(name)
        out[name] = getattr(mod, "__version__", "unknown") if mod is not None else None
    return out
