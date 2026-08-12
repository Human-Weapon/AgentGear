"""Checkpoint persistence.

A checkpoint is a small, logical marker of "how far did this execution
get" (phase, completed/pending subtasks, last good state) — not a full
snapshot system. Callers append checkpoints as they reach them; recovery
and BLOCKED reporting read the most recent one back.
"""

from __future__ import annotations

from pathlib import Path

from .models import Checkpoint
from .safe_json_store import SafeJsonStore


def _to_dict(cp: Checkpoint) -> dict:
    return {
        "execution_id": cp.execution_id,
        "phase": cp.phase,
        "completed": list(cp.completed),
        "pending": list(cp.pending),
        "last_good_state": cp.last_good_state,
        "at_seconds": cp.at_seconds,
    }


def _from_dict(data: dict) -> Checkpoint:
    return Checkpoint(
        execution_id=data["execution_id"],
        phase=data["phase"],
        completed=tuple(data.get("completed", ())),
        pending=tuple(data.get("pending", ())),
        last_good_state=data.get("last_good_state"),
        at_seconds=data.get("at_seconds", 0.0),
    )


class CheckpointStore:
    """Append-only checkpoint history for one state directory, one file
    per execution_id, path-contained and concurrency-safe.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def _store(self, execution_id: str) -> SafeJsonStore:
        path = self.state_dir / f"{execution_id}.checkpoints.json"
        return SafeJsonStore(path, trusted_root=self.state_dir, default=list)

    def append(self, checkpoint: Checkpoint) -> None:
        store = self._store(checkpoint.execution_id)
        store.update(lambda current: [*(current or []), _to_dict(checkpoint)])

    def all(self, execution_id: str) -> list[Checkpoint]:
        data = self._store(execution_id).read() or []
        return [_from_dict(d) for d in data]

    def latest(self, execution_id: str) -> Checkpoint | None:
        history = self.all(execution_id)
        return history[-1] if history else None
