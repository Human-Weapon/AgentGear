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


def test_write_atomic_retries_transient_permission_error_on_replace(tmp_path, monkeypatch) -> None:
    """Regression: real multi-process runs on Windows intermittently hit
    PermissionError (WinError 5) on the final rename/replace, even while
    holding our own lock -- almost certainly antivirus/indexer briefly
    opening the just-written file. The first N replace attempts must be
    retried transparently instead of surfacing a spurious failure."""
    import pathlib

    from agentgear import safe_json_store as sjs_module

    store = SafeJsonStore(tmp_path / "data.json", default=dict)

    calls = {"count": 0}
    real_replace = pathlib.Path.replace

    def flaky_replace(self, target):
        calls["count"] += 1
        if calls["count"] <= 3:
            raise PermissionError("simulated transient WinError 5")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", flaky_replace)
    monkeypatch.setattr(sjs_module, "_REPLACE_RETRY_DELAY_S", 0.0)

    store.write_atomic({"a": 1})
    assert calls["count"] == 4
    monkeypatch.undo()
    assert store.read() == {"a": 1}


def test_write_atomic_gives_up_after_exhausting_replace_retries(tmp_path, monkeypatch) -> None:
    import pathlib

    from agentgear import safe_json_store as sjs_module

    store = SafeJsonStore(tmp_path / "data.json", default=dict)

    def always_denied(self, target):
        raise PermissionError("simulated persistent WinError 5")

    monkeypatch.setattr(pathlib.Path, "replace", always_denied)
    monkeypatch.setattr(sjs_module, "_REPLACE_RETRY_DELAY_S", 0.0)

    with pytest.raises(PermissionError):
        store.write_atomic({"a": 1})


def test_read_retries_transient_permission_error_instead_of_quarantining(
    tmp_path, monkeypatch
) -> None:
    """Round 4 / NEW-06 concurrency fix: discovered via real multiprocess
    testing of the segmented checkpoint design, whose append() does an
    UNLOCKED peek read that can race with another process's locked
    write_atomic (temp-write + atomic replace) on the SAME file. On
    Windows this transiently raises PermissionError while the replace is
    in flight -- NOT evidence of corruption. The old code treated ANY
    OSError from read_text() as corruption and quarantined a perfectly
    healthy file. The first N transient read failures must be retried
    transparently instead of destroying the file."""
    import pathlib

    from agentgear import safe_json_store as sjs_module

    path = tmp_path / "data.json"
    store = SafeJsonStore(path, default=dict)
    store.write_atomic({"a": 1})

    calls = {"count": 0}
    real_read_text = pathlib.Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] <= 3:
            raise PermissionError("simulated transient WinError 32")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", flaky_read_text)
    monkeypatch.setattr(sjs_module, "_READ_RETRY_DELAY_S", 0.0)

    result = store.read()
    assert result == {"a": 1}
    assert calls["count"] == 4
    monkeypatch.undo()
    # the file was never quarantined/renamed away by the transient errors
    assert path.exists()
    assert store.read() == {"a": 1}


def test_read_gives_up_after_exhausting_retries_and_quarantines(tmp_path, monkeypatch) -> None:
    """A PERSISTENT (not transient) read failure must still be treated as
    corruption and quarantined -- the retry must not mask a genuine,
    permanently unreadable file."""
    import pathlib

    from agentgear import safe_json_store as sjs_module
    from agentgear.exceptions import CorruptStorageError

    path = tmp_path / "data.json"
    store = SafeJsonStore(path, default=dict)
    store.write_atomic({"a": 1})

    def always_denied(self, *args, **kwargs):
        raise PermissionError("simulated persistent denial")

    monkeypatch.setattr(pathlib.Path, "read_text", always_denied)
    monkeypatch.setattr(sjs_module, "_READ_RETRY_DELAY_S", 0.0)

    with pytest.raises(CorruptStorageError):
        store.read()
