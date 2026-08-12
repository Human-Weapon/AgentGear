from __future__ import annotations

import json

import pytest

from agentgear.exceptions import CorruptStorageError, PathEscapeError
from agentgear.path_security import validate_contained
from agentgear.safe_json_store import SafeJsonStore


def test_read_missing_file_returns_default(tmp_path) -> None:
    store = SafeJsonStore(tmp_path / "data.json", default=dict)
    assert store.read() == {}


def test_write_atomic_then_read(tmp_path) -> None:
    store = SafeJsonStore(tmp_path / "data.json", default=dict)
    store.write_atomic({"a": 1})
    assert store.read() == {"a": 1}


def test_update_mutator_round_trips(tmp_path) -> None:
    store = SafeJsonStore(tmp_path / "data.json", default=list)
    store.update(lambda current: [*current, 1])
    store.update(lambda current: [*current, 2])
    assert store.read() == [1, 2]


def test_corrupt_file_is_quarantined(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = SafeJsonStore(path, default=dict)
    with pytest.raises(CorruptStorageError) as exc_info:
        store.read()
    assert exc_info.value.quarantined_path is not None
    assert not path.exists()


def test_trusted_root_blocks_path_escape(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    outside = tmp_path / "outside.json"
    with pytest.raises(PathEscapeError):
        SafeJsonStore(outside, trusted_root=root, default=dict)


def test_trusted_root_allows_contained_path(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    store = SafeJsonStore(root / "data.json", trusted_root=root, default=dict)
    store.write_atomic({"ok": True})
    assert store.read() == {"ok": True}


def test_concurrent_updates_do_not_corrupt(tmp_path) -> None:
    store_path = tmp_path / "counter.json"
    store = SafeJsonStore(store_path, default=lambda: {"count": 0})
    for _ in range(20):
        store.update(lambda cur: {"count": cur["count"] + 1})
    assert store.read() == {"count": 20}


def test_validate_contained_rejects_parent_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        validate_contained(tmp_path / "elsewhere.json", root)


def test_quarantine_invalid_raises_and_moves_file(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"bad": "schema"}), encoding="utf-8")
    store = SafeJsonStore(path, default=dict)
    with pytest.raises(CorruptStorageError):
        store.quarantine_invalid("schema mismatch")
    assert not path.exists()
