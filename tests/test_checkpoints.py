from __future__ import annotations

import json

import pytest

from agentgear.checkpoints import CheckpointStore
from agentgear.exceptions import CorruptStorageError, InvalidObservationError
from agentgear.models import Checkpoint


def test_latest_is_none_when_no_checkpoints(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    assert store.latest("exec-1") is None
    assert store.all("exec-1") == []


def test_append_and_read_back(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    cp = Checkpoint(
        execution_id="exec-1",
        phase="implementation",
        completed=("parser", "tests"),
        pending=("integration", "docs"),
        last_good_state="abc123",
        at_seconds=42.0,
    )
    store.append(cp)
    latest = store.latest("exec-1")
    assert latest is not None
    assert latest.phase == "implementation"
    assert latest.completed == ("parser", "tests")
    assert latest.pending == ("integration", "docs")


def test_checkpoints_accumulate_in_order(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    for i in range(3):
        store.append(Checkpoint(execution_id="exec-1", phase=f"phase-{i}", at_seconds=float(i)))
    history = store.all("exec-1")
    assert [c.phase for c in history] == ["phase-0", "phase-1", "phase-2"]
    assert store.latest("exec-1").phase == "phase-2"


def test_different_executions_are_isolated(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    store.append(Checkpoint(execution_id="exec-a", phase="a", at_seconds=0.0))
    store.append(Checkpoint(execution_id="exec-b", phase="b", at_seconds=0.0))
    assert store.latest("exec-a").phase == "a"
    assert store.latest("exec-b").phase == "b"


# --- AG-07: schema integrity ------------------------------------------------


def _write_raw(tmp_path, execution_id: str, payload) -> None:
    path = tmp_path / f"{execution_id}.checkpoints.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {},  # wrong root: must be a list
        "just a string",
        42,
        None,  # literal JSON null must not be mistaken for "no file"
        [{}],  # entry missing everything
        [{"execution_id": "exec-1"}],  # entry missing 'phase'
        [{"execution_id": "exec-1", "phase": ""}],  # blank phase
        [{"execution_id": "exec-1", "phase": "x", "completed": "not-a-list"}],
        [{"execution_id": "exec-1", "phase": "x", "completed": [123]}],
        [{"execution_id": "exec-1", "phase": "x", "at_seconds": "not-a-number"}],
        ["not-an-object"],
    ],
)
def test_malformed_checkpoint_file_is_quarantined_not_crashed(tmp_path, payload) -> None:
    _write_raw(tmp_path, "exec-1", payload)
    store = CheckpointStore(tmp_path)
    with pytest.raises(CorruptStorageError):
        store.all("exec-1")
    assert not (tmp_path / "exec-1.checkpoints.json").exists()


def test_empty_list_checkpoint_file_is_valid_not_corrupt(tmp_path) -> None:
    _write_raw(tmp_path, "exec-1", [])
    store = CheckpointStore(tmp_path)
    assert store.all("exec-1") == []


def test_quarantined_checkpoint_file_does_not_block_a_fresh_append(tmp_path) -> None:
    _write_raw(tmp_path, "exec-1", {})
    store = CheckpointStore(tmp_path)
    with pytest.raises(CorruptStorageError):
        store.all("exec-1")
    store.append(Checkpoint(execution_id="exec-1", phase="fresh", at_seconds=0.0))
    assert store.latest("exec-1").phase == "fresh"


def test_append_never_silently_overwrites_schema_invalid_history(tmp_path) -> None:
    """A successful append must not discard corrupt acknowledged state."""
    _write_raw(tmp_path, "exec-1", {})
    store = CheckpointStore(tmp_path)

    with pytest.raises(CorruptStorageError):
        store.append(Checkpoint(execution_id="exec-1", phase="fresh", at_seconds=0.0))

    assert not (tmp_path / "exec-1.checkpoints.json").exists()


@pytest.mark.parametrize("kwargs", [{"execution_id": ""}, {"phase": "  "}, {"at_seconds": -1.0}])
def test_checkpoint_rejects_invalid_domain_data(kwargs: dict) -> None:
    base = {"execution_id": "exec-1", "phase": "build", "at_seconds": 0.0}
    base.update(kwargs)
    with pytest.raises(InvalidObservationError):
        Checkpoint(**base)


# --- Round 2 / H4: schema-valid but domain-invalid content must quarantine


def test_whitespace_only_last_good_state_is_quarantined_not_raised_raw(tmp_path) -> None:
    """Regression for the exact H4 finding: a whitespace-only
    last_good_state passes a naive "is it a string or null" structural
    check, but Checkpoint's own domain validation rejects blank strings.
    That must still become a quarantined CorruptStorageError, never a raw
    InvalidObservationError escaping the persistence boundary."""
    payload = [{"execution_id": "exec-1", "phase": "build", "last_good_state": "   "}]
    _write_raw(tmp_path, "exec-1", payload)
    store = CheckpointStore(tmp_path)

    with pytest.raises(CorruptStorageError):
        store.all("exec-1")

    assert not (tmp_path / "exec-1.checkpoints.json").exists()
