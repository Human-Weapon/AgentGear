"""Lightweight heartbeat/checkpoint record and its persistence.

A heartbeat is intentionally tiny: scalar fields and short strings only,
overwritten in place (not appended forever) so watching an execution never
costs meaningful tokens or disk. It exists purely so an external observer
(CLI ``status`` command, a monitoring dashboard) can answer "what is this
execution doing right now" without re-deriving it from a transcript.
"""

from __future__ import annotations

from pathlib import Path

from ..models import ExecutionState, Heartbeat
from ..safe_json_store import SafeJsonStore


def build_heartbeat(
    *,
    execution_id: str,
    state: ExecutionState,
    current_task: str,
    current_subtask: str | None,
    last_real_progress_at: float,
    last_progress_evidence: str | None,
    attempt_count: int,
    current_strategy: str | None,
    last_error: str | None,
    pending_work: tuple[str, ...] = (),
) -> Heartbeat:
    return Heartbeat(
        execution_id=execution_id,
        state=state,
        current_task=current_task,
        current_subtask=current_subtask,
        last_real_progress_at=last_real_progress_at,
        last_progress_evidence=last_progress_evidence,
        attempt_count=attempt_count,
        current_strategy=current_strategy,
        last_error=last_error,
        pending_work=pending_work,
    )


def _to_dict(hb: Heartbeat) -> dict:
    return {
        "execution_id": hb.execution_id,
        "state": hb.state.value,
        "current_task": hb.current_task,
        "current_subtask": hb.current_subtask,
        "last_real_progress_at": hb.last_real_progress_at,
        "last_progress_evidence": hb.last_progress_evidence,
        "attempt_count": hb.attempt_count,
        "current_strategy": hb.current_strategy,
        "last_error": hb.last_error,
        "pending_work": list(hb.pending_work),
    }


def _from_dict(data: dict) -> Heartbeat:
    return Heartbeat(
        execution_id=data["execution_id"],
        state=ExecutionState(data["state"]),
        current_task=data["current_task"],
        current_subtask=data.get("current_subtask"),
        last_real_progress_at=data["last_real_progress_at"],
        last_progress_evidence=data.get("last_progress_evidence"),
        attempt_count=data["attempt_count"],
        current_strategy=data.get("current_strategy"),
        last_error=data.get("last_error"),
        pending_work=tuple(data.get("pending_work", ())),
    )


class HeartbeatWriter:
    """Persists the single latest heartbeat for an execution, overwriting
    in place. Backed by ``SafeJsonStore`` for atomic, path-contained,
    concurrency-safe writes.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def _store(self, execution_id: str) -> SafeJsonStore:
        path = self.state_dir / f"{execution_id}.heartbeat.json"
        return SafeJsonStore(path, trusted_root=self.state_dir, default=lambda: None)

    def write(self, heartbeat: Heartbeat) -> None:
        store = self._store(heartbeat.execution_id)
        store.update(lambda _current: _to_dict(heartbeat))

    def read(self, execution_id: str) -> Heartbeat | None:
        data = self._store(execution_id).read()
        if data is None:
            return None
        return _from_dict(data)
