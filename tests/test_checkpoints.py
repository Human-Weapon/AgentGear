from __future__ import annotations

from agentgear.checkpoints import CheckpointStore
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
