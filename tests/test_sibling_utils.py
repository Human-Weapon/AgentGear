from __future__ import annotations

from agentgear import _sibling_utils


def test_is_installed_false_for_bogus_package() -> None:
    assert _sibling_utils.is_installed("this_package_does_not_exist_xyz") is False


def test_is_installed_true_for_stdlib() -> None:
    assert _sibling_utils.is_installed("json") is True


def test_load_sibling_returns_none_for_missing_package() -> None:
    assert _sibling_utils.load_sibling("this_package_does_not_exist_xyz") is None


def test_load_sibling_returns_module_for_stdlib() -> None:
    mod = _sibling_utils.load_sibling("json")
    assert mod is not None
    assert mod.__name__ == "json"


def test_sibling_versions_reports_all_known_siblings() -> None:
    versions = _sibling_utils.sibling_versions()
    assert "promptgraph" in versions
    assert "agentgear" in versions
    assert versions["agentgear"] is not None  # this package is always installed here


def test_sibling_versions_none_for_uninstalled_sibling() -> None:
    versions = _sibling_utils.sibling_versions()
    for version in versions.values():
        assert version is None or isinstance(version, str)
