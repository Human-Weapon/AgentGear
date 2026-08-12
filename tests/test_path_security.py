from __future__ import annotations

import os
import subprocess

import pytest

from agentgear.exceptions import PathEscapeError
from agentgear.path_security import assert_path_family_contained, safe_join, validate_contained
from agentgear.safe_json_store import SafeJsonStore


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


def test_symlinked_directory_escape_is_rejected(tmp_path) -> None:
    """Real symlink escape. Unprivileged symlink creation just works on
    POSIX; on Windows it requires Developer Mode or elevation, so we skip
    gracefully there rather than assume the platform can't be tested at
    all (junctions, tested separately below, need no such privilege).
    """
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


def _make_junction(link: str, target: str) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create junction on this system: {result.stderr or result.stdout}")


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows/NTFS reparse-point concept")
def test_real_windows_junction_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape_junction"
    _make_junction(str(link), str(outside))

    with pytest.raises(PathEscapeError):
        validate_contained(link / "file.json", root)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows/NTFS reparse-point concept")
def test_nested_windows_junction_escape_is_rejected(tmp_path) -> None:
    """A junction two levels deep inside the trusted root must be caught
    just as reliably as one directly under it."""
    root = tmp_path / "root"
    nested_parent = root / "a" / "b"
    outside = tmp_path / "outside"
    nested_parent.mkdir(parents=True)
    outside.mkdir()
    link = nested_parent / "escape_junction"
    _make_junction(str(link), str(outside))

    with pytest.raises(PathEscapeError):
        validate_contained(link / "deep" / "file.json", root)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows/NTFS reparse-point concept")
def test_post_construction_junction_swap_is_rejected_with_zero_artifacts(tmp_path) -> None:
    """TOCTOU hardening: a SafeJsonStore is constructed while its
    subdirectory is an ordinary directory (containment check passes at
    construction time). The subdirectory is THEN swapped for a junction
    pointing outside trusted_root, simulating an attacker racing the
    write. The subsequent write must still be rejected, and must leave
    ZERO artifacts (json/lock/temp) outside trusted_root.
    """
    root = tmp_path / "root"
    subdir = root / "subdir"
    outside = tmp_path / "outside"
    subdir.mkdir(parents=True)
    outside.mkdir()

    store = SafeJsonStore(subdir / "data.json", trusted_root=root, default=dict)

    # Swap subdir -> junction pointing outside, AFTER construction.
    subdir.rmdir()
    _make_junction(str(subdir), str(outside))

    with pytest.raises(PathEscapeError):
        store.write_atomic({"malicious": True})

    # Nothing must have been written into `outside` through the swapped junction.
    assert list(outside.iterdir()) == []
