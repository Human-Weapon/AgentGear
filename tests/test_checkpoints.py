from __future__ import annotations

import json

import pytest

from agentgear.checkpoints import _SEGMENT_CAPACITY, CheckpointStore
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


def _segment_path(tmp_path, execution_id: str, segment_number: int = 1):
    return tmp_path / f"{execution_id}.checkpoints" / f"segment-{segment_number:06d}.json"


def _write_raw(tmp_path, execution_id: str, payload, segment_number: int = 1) -> None:
    path = _segment_path(tmp_path, execution_id, segment_number)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    assert not _segment_path(tmp_path, "exec-1").exists()


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

    assert not _segment_path(tmp_path, "exec-1").exists()


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

    assert not _segment_path(tmp_path, "exec-1").exists()


# --- Round 4 / NEW-06: segmented storage, bounded per-append work ----------


def test_structural_per_append_cost_is_bounded_by_segment_capacity_not_history_length(
    tmp_path,
) -> None:
    """The exact NEW-06 regression: the old single-file design read+
    re-wrote the ENTIRE history on every append (O(history length) per
    append, O(N^2) total over N appends). This proves the fix
    STRUCTURALLY -- by inspecting how many segment files exist and how
    many entries the newest one holds -- rather than by fragile wall-clock
    timing, which varies by machine/antivirus/filesystem.
    """
    store = CheckpointStore(tmp_path)
    n = 5 * _SEGMENT_CAPACITY  # spans multiple full segments + a partial one
    for i in range(n):
        store.append(Checkpoint(execution_id="exec-1", phase=f"p{i}", at_seconds=float(i)))

    numbers = store._segment_numbers("exec-1")
    assert len(numbers) == 5, f"expected exactly 5 full segments for {n} entries, got {numbers}"

    # Every segment holds at most _SEGMENT_CAPACITY entries -- an append
    # NEVER has to serialize more than one bounded segment's worth of
    # data, regardless of how long the execution's total history is.
    for seg_num in numbers:
        data = store._segment_store("exec-1", seg_num).read()
        assert len(data) <= _SEGMENT_CAPACITY

    assert store.all("exec-1")[0].phase == "p0"
    assert store.all("exec-1")[-1].phase == f"p{n - 1}"
    assert len(store.all("exec-1")) == n
    assert store.latest("exec-1").phase == f"p{n - 1}"


def test_latest_only_reads_the_newest_segment(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    for i in range(_SEGMENT_CAPACITY + 3):
        store.append(Checkpoint(execution_id="exec-1", phase=f"p{i}", at_seconds=float(i)))
    numbers = store._segment_numbers("exec-1")
    assert numbers == [1, 2]
    # segment 1 is full and closed; only segment 2 (3 entries) is needed
    # to answer latest() -- corrupt segment 1 and confirm latest() is
    # UNAFFECTED (it never has to touch it).
    _write_raw(tmp_path, "exec-1", {"corrupt": True}, segment_number=1)
    assert store.latest("exec-1").phase == f"p{_SEGMENT_CAPACITY + 2}"


def test_append_rolls_over_to_a_new_segment_once_the_active_one_is_full(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    for i in range(_SEGMENT_CAPACITY):
        store.append(Checkpoint(execution_id="exec-1", phase=f"p{i}", at_seconds=float(i)))
    assert store._segment_numbers("exec-1") == [1]
    store.append(Checkpoint(execution_id="exec-1", phase="overflow", at_seconds=999.0))
    assert store._segment_numbers("exec-1") == [1, 2]
    assert len(store._segment_store("exec-1", 2).read()) == 1


@pytest.mark.parametrize("corrupt_position", ["first", "middle", "last"])
def test_corrupt_segment_is_reported_precisely_without_losing_other_segments(
    tmp_path, corrupt_position
) -> None:
    """A corrupt segment must be reported (and quarantined) WITHOUT
    silently losing or touching any other, healthy segment -- segmenting
    makes quarantine strictly more surgical than the single-file design.
    """
    store = CheckpointStore(tmp_path)
    for i in range(3 * _SEGMENT_CAPACITY):
        store.append(Checkpoint(execution_id="exec-1", phase=f"p{i}", at_seconds=float(i)))
    assert store._segment_numbers("exec-1") == [1, 2, 3]

    corrupt_segment = {"first": 1, "middle": 2, "last": 3}[corrupt_position]
    healthy_segments = [n for n in (1, 2, 3) if n != corrupt_segment]
    healthy_paths = [_segment_path(tmp_path, "exec-1", n) for n in healthy_segments]
    healthy_contents_before = [p.read_text(encoding="utf-8") for p in healthy_paths]

    _write_raw(tmp_path, "exec-1", {"corrupt": True}, segment_number=corrupt_segment)

    with pytest.raises(CorruptStorageError):
        store.all("exec-1")

    # the corrupt segment was quarantined (renamed away)...
    assert not _segment_path(tmp_path, "exec-1", corrupt_segment).exists()
    # ...but every healthy segment is completely untouched.
    for path, before in zip(healthy_paths, healthy_contents_before, strict=True):
        assert path.exists()
        assert path.read_text(encoding="utf-8") == before
