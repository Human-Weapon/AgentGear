"""Lightweight heartbeat/checkpoint record and its persistence.

A heartbeat is intentionally tiny: scalar fields and short strings only,
overwritten in place (not appended forever) so watching an execution never
costs meaningful tokens or disk. It exists purely so an external observer
(CLI ``status`` command, a monitoring dashboard) can answer "what is this
execution doing right now" without re-deriving it from a transcript.

AG-07: valid JSON is not valid state. A heartbeat file that is technically
parseable JSON but doesn't match the expected schema (``{}``, ``[]``,
missing fields, wrong types, an unrecognized ``ExecutionState`` value) is
quarantined and reported as ``CorruptStorageError`` — never as a raw
``KeyError``/``ValueError`` leaking out of a read.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..models import ExecutionState, Heartbeat
from ..safe_json_store import SafeJsonStore

_MISSING = object()

_REQUIRED_STRING_FIELDS = ("execution_id", "current_task")
_REQUIRED_NULLABLE_STRING_FIELDS = (
    "current_subtask",
    "last_progress_evidence",
    "current_strategy",
    "last_error",
)


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


def _validate_schema(data: Any) -> dict:
    """Raise ``ValueError`` with a precise reason on any schema violation.
    Callers turn this into a quarantined ``CorruptStorageError``.
    """
    if not isinstance(data, dict):
        raise ValueError(f"heartbeat root must be an object, got {type(data).__name__}")
    if not data:
        raise ValueError("heartbeat object is empty")

    for field_name in _REQUIRED_STRING_FIELDS:
        if field_name not in data:
            raise ValueError(f"missing required field '{field_name}'")
        if not isinstance(data[field_name], str) or not data[field_name].strip():
            raise ValueError(f"'{field_name}' must be a non-empty string")

    if "state" not in data:
        raise ValueError("missing required field 'state'")
    if not isinstance(data["state"], str) or data["state"] not in {s.value for s in ExecutionState}:
        raise ValueError(f"'state' is not a valid ExecutionState: {data.get('state')!r}")

    if "last_real_progress_at" not in data:
        raise ValueError("missing required field 'last_real_progress_at'")
    if isinstance(data["last_real_progress_at"], bool) or not isinstance(
        data["last_real_progress_at"], (int, float)
    ):
        raise ValueError("'last_real_progress_at' must be a number")
    if not math.isfinite(data["last_real_progress_at"]) or data["last_real_progress_at"] < 0:
        raise ValueError("'last_real_progress_at' must be a finite non-negative number")

    if "attempt_count" not in data:
        raise ValueError("missing required field 'attempt_count'")
    if isinstance(data["attempt_count"], bool) or not isinstance(data["attempt_count"], int):
        raise ValueError("'attempt_count' must be an int")
    if data["attempt_count"] < 0:
        raise ValueError("'attempt_count' must be >= 0")

    for field_name in _REQUIRED_NULLABLE_STRING_FIELDS:
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"'{field_name}' must be a string or null")

    pending_work = data.get("pending_work", [])
    if not isinstance(pending_work, list) or not all(isinstance(p, str) for p in pending_work):
        raise ValueError("'pending_work' must be a list of strings")

    return data


def _from_dict(data: Any) -> Heartbeat:
    data = _validate_schema(data)
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
        # A dedicated sentinel (not `None`) marks "file does not exist" —
        # `None` is itself valid JSON (`null`), so reusing it as the
        # missing-file default would make a corrupt file containing a
        # literal `null` indistinguishable from "no heartbeat written yet".
        return SafeJsonStore(
            path,
            trusted_root=self.state_dir,
            default=lambda: _MISSING,
            validator=_validate_schema,
        )

    def write(self, heartbeat: Heartbeat) -> None:
        store = self._store(heartbeat.execution_id)
        store.update(lambda _current: _to_dict(heartbeat))

    def read(self, execution_id: str) -> Heartbeat | None:
        store = self._store(execution_id)
        data = store.read()
        if data is _MISSING:
            return None
        try:
            return _from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            # quarantine_invalid() always raises CorruptStorageError.
            store.quarantine_invalid(f"invalid heartbeat schema for {execution_id!r}: {exc}")
            raise
