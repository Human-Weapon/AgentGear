from __future__ import annotations

from agentgear.models import ExecutionState
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
