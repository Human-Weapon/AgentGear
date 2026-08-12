from __future__ import annotations

import os

import pytest

from agentgear.exceptions import PathEscapeError
from agentgear.path_security import assert_path_family_contained, safe_join, validate_contained


def test_validate_contained_accepts_nested_path(tmp_path) -> None:
    nested = tmp_path / "a" / "b" / "c.json"
    result = validate_contained(nested, tmp_path)
    assert str(result).startswith(str(tmp_path.resolve()))


def test_validate_contained_rejects_sibling_directory(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sibling = tmp_path / "sibling" / "file.json"
    with pytest.raises(PathEscapeError):
        validate_contained(sibling, root)


def test_validate_contained_rejects_dotdot_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    escaping = root / ".." / "escape.json"
    with pytest.raises(PathEscapeError):
        validate_contained(escaping, root)


def test_safe_join_returns_contained_path(tmp_path) -> None:
    result = safe_join(tmp_path, "a", "b.json")
    assert result == (tmp_path / "a" / "b.json").resolve()


def test_safe_join_rejects_escape_via_parts(tmp_path) -> None:
    with pytest.raises(PathEscapeError):
        safe_join(tmp_path, "..", "escape.json")


def test_assert_path_family_contained_checks_existing_ancestors(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert_path_family_contained(root / "new_file.json", trusted_root=root)


def test_assert_path_family_contained_rejects_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        assert_path_family_contained(tmp_path / "outside.json", trusted_root=root)


@pytest.mark.skipif(os.name != "nt", reason="junction/symlink escape test is Windows-specific")
def test_symlinked_directory_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape_link"
    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks requires elevated privileges on this system")
    with pytest.raises(PathEscapeError):
        validate_contained(link / "file.json", root)
