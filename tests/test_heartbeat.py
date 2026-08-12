from __future__ import annotations

import json

import pytest

from agentgear.exceptions import CorruptStorageError, InvalidObservationError
from agentgear.models import ExecutionState, Heartbeat
from agentgear.watchdog.heartbeat import HeartbeatWriter, build_heartbeat


def _hb(execution_id: str = "exec-1"):
    return build_heartbeat(
        execution_id=execution_id,
        state=ExecutionState.RUNNING,
        current_task="implement feature",
        current_subtask="write tests",
        last_real_progress_at=12.5,
        last_progress_evidence="test_foo passed",
        attempt_count=2,
        current_strategy=None,
        last_error=None,
        pending_work=("docs",),
    )


def test_read_missing_heartbeat_returns_none(tmp_path) -> None:
    writer = HeartbeatWriter(tmp_path)
    assert writer.read("nope") is None


def test_write_then_read_round_trips(tmp_path) -> None:
    writer = HeartbeatWriter(tmp_path)
    hb = _hb()
    writer.write(hb)
    read_back = writer.read("exec-1")
    assert read_back == hb


def test_write_overwrites_in_place(tmp_path) -> None:
    writer = HeartbeatWriter(tmp_path)
    writer.write(_hb())
    updated = build_heartbeat(
        execution_id="exec-1",
        state=ExecutionState.STALLED,
        current_task="implement feature",
        current_subtask="write tests",
        last_real_progress_at=12.5,
        last_progress_evidence="test_foo passed",
        attempt_count=3,
        current_strategy="re_read_error",
        last_error="boom",
        pending_work=(),
    )
    writer.write(updated)
    read_back = writer.read("exec-1")
    assert read_back.state == ExecutionState.STALLED
    assert read_back.attempt_count == 3

    files = list(tmp_path.glob("exec-1.heartbeat.json*"))
    json_files = [f for f in files if f.suffix == ".json"]
    assert len(json_files) == 1


# --- AG-07: schema integrity ------------------------------------------------


def _write_raw(tmp_path, execution_id: str, payload) -> None:
    path = tmp_path / f"{execution_id}.heartbeat.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        "just a string",
        42,
        None,
        {"execution_id": "exec-1"},  # missing everything else
        {
            "execution_id": "exec-1",
            "state": "not-a-real-state",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": 0.0,
            "last_progress_evidence": None,
            "attempt_count": 0,
            "current_strategy": None,
            "last_error": None,
            "pending_work": [],
        },
        {
            "execution_id": "exec-1",
            "state": "running",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": "not-a-number",
            "attempt_count": 0,
            "pending_work": [],
        },
        {
            "execution_id": "exec-1",
            "state": "running",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": 0.0,
            "attempt_count": "not-an-int",
            "pending_work": [],
        },
        {
            "execution_id": "",
            "state": "running",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": 0.0,
            "attempt_count": 0,
            "pending_work": [],
        },
        {
            "execution_id": "exec-1",
            "state": "running",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": 0.0,
            "attempt_count": -1,
            "pending_work": [],
        },
        {
            "execution_id": "exec-1",
            "state": "running",
            "current_task": "x",
            "current_subtask": None,
            "last_real_progress_at": 0.0,
            "attempt_count": 0,
            "pending_work": [123],
        },
    ],
)
def test_malformed_heartbeat_is_quarantined_not_crashed(tmp_path, payload) -> None:
    _write_raw(tmp_path, "exec-1", payload)
    writer = HeartbeatWriter(tmp_path)
    with pytest.raises(CorruptStorageError):
        writer.read("exec-1")
    # the malformed source file must be gone (renamed to .corrupt), never
    # left in place to be misread again next time.
    assert not (tmp_path / "exec-1.heartbeat.json").exists()


def test_quarantined_heartbeat_does_not_block_a_fresh_write(tmp_path) -> None:
    _write_raw(tmp_path, "exec-1", {})
    writer = HeartbeatWriter(tmp_path)
    with pytest.raises(CorruptStorageError):
        writer.read("exec-1")
    writer.write(_hb("exec-1"))
    assert writer.read("exec-1") == _hb("exec-1")


def test_write_never_silently_overwrites_schema_invalid_heartbeat(tmp_path) -> None:
    """A caller must learn that the prior state was corrupt before replacing it."""
    _write_raw(tmp_path, "exec-1", {})
    writer = HeartbeatWriter(tmp_path)

    with pytest.raises(CorruptStorageError):
        writer.write(_hb("exec-1"))

    assert not (tmp_path / "exec-1.heartbeat.json").exists()


def test_heartbeat_rejects_invalid_domain_data_before_it_reaches_storage() -> None:
    with pytest.raises(InvalidObservationError):
        Heartbeat(
            execution_id="exec-1",
            state=ExecutionState.RUNNING,
            current_task="x",
            current_subtask=None,
            last_real_progress_at=float("nan"),
            last_progress_evidence=None,
            attempt_count=0,
            current_strategy=None,
            last_error=None,
        )
