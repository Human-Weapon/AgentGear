"""Checkpoint persistence.

A checkpoint is a small, logical marker of "how far did this execution
get" (phase, completed/pending subtasks, last good state) — not a full
snapshot system. Callers append checkpoints as they reach them; recovery
and BLOCKED reporting read the most recent one back.

AG-07: valid JSON is not valid state. The checkpoint history file's root
must be a list of well-formed checkpoint objects; anything else (``{}``,
a bare object instead of a list, missing fields, wrong types) is
quarantined and reported as ``CorruptStorageError`` rather than raising a
raw ``KeyError``/``TypeError`` out of ``all()``/``latest()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Checkpoint
from .safe_json_store import SafeJsonStore

_MISSING = object()


def _to_dict(cp: Checkpoint) -> dict:
    return {
        "execution_id": cp.execution_id,
        "phase": cp.phase,
        "completed": list(cp.completed),
        "pending": list(cp.pending),
        "last_good_state": cp.last_good_state,
        "at_seconds": cp.at_seconds,
    }


def _validate_entry_schema(entry: Any, index: int) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"checkpoint[{index}] must be an object, got {type(entry).__name__}")

    for field_name in ("execution_id", "phase"):
        if field_name not in entry:
            raise ValueError(f"checkpoint[{index}] is missing required field '{field_name}'")
        if not isinstance(entry[field_name], str) or not entry[field_name].strip():
            raise ValueError(f"checkpoint[{index}].{field_name} must be a non-empty string")

    for field_name in ("completed", "pending"):
        value = entry.get(field_name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"checkpoint[{index}].{field_name} must be a list of strings")

    last_good_state = entry.get("last_good_state")
    if last_good_state is not None and not isinstance(last_good_state, str):
        raise ValueError(f"checkpoint[{index}].last_good_state must be a string or null")

    at_seconds = entry.get("at_seconds", 0.0)
    if isinstance(at_seconds, bool) or not isinstance(at_seconds, (int, float)):
        raise ValueError(f"checkpoint[{index}].at_seconds must be a number")

    return entry


def _from_dict(data: Any, index: int) -> Checkpoint:
    data = _validate_entry_schema(data, index)
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
        # `_MISSING` (not the empty list `[]`) marks "file does not exist",
        # so a checkpoint file corrupted into literal JSON `null` is never
        # silently mistaken for a legitimate, merely-empty history.
        return SafeJsonStore(path, trusted_root=self.state_dir, default=lambda: _MISSING)

    def append(self, checkpoint: Checkpoint) -> None:
        store = self._store(checkpoint.execution_id)
        store.update(
            lambda current: [
                *(current if isinstance(current, list) else []),
                _to_dict(checkpoint),
            ]
        )

    def all(self, execution_id: str) -> list[Checkpoint]:
        store = self._store(execution_id)
        data = store.read()
        if data is _MISSING:
            return []
        if not isinstance(data, list):
            store.quarantine_invalid(
                f"checkpoint history for {execution_id!r} must be a list, got {type(data).__name__}"
            )
        try:
            return [_from_dict(entry, i) for i, entry in enumerate(data)]
        except (ValueError, KeyError, TypeError) as exc:
            store.quarantine_invalid(f"invalid checkpoint schema for {execution_id!r}: {exc}")
            raise

    def latest(self, execution_id: str) -> Checkpoint | None:
        history = self.all(execution_id)
        return history[-1] if history else None
