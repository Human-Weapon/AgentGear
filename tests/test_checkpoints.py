from __future__ import annotations

import json

import pytest

from agentgear.checkpoints import CheckpointStore
from agentgear.exceptions import CorruptStorageError
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
