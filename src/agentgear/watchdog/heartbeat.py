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

Round 2 / H4: schema-valid JSON can still be domain-invalid (e.g. a
whitespace-only ``current_subtask`` passes a naive "is it a string"
structural check but fails ``Heartbeat``'s own non-blank invariant). A
structural pre-check that doesn't also attempt real domain construction
can let that gap through as a raw ``InvalidObservationError`` instead of
a quarantined ``CorruptStorageError``. ``_construct`` is the ONE
authoritative path from a raw dict to a domain ``Heartbeat`` — used both
as the persistence-layer schema validator and as the deserializer — so
there is no second, independently-drifting set of rules, and every
failure mode (structural or domain-level) normalizes to ``ValueError``
for ``SafeJsonStore``'s single quarantine path to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..exceptions import InvalidObservationError
from ..models import ExecutionState, Heartbeat
from ..path_security import bind_persistence_root, validate_persistence_safe_id
from ..safe_json_store import SafeJsonStore

_MISSING = object()


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


def _construct(data: Any) -> Heartbeat:
    """The ONE authoritative dict -> domain-``Heartbeat`` path (H4)."""
    if not isinstance(data, dict):
        raise ValueError(f"heartbeat root must be an object, got {type(data).__name__}")
    if not data:
        raise ValueError("heartbeat object is empty")

    required = ("execution_id", "current_task", "state", "last_real_progress_at", "attempt_count")
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"missing required field(s): {missing}")

    try:
        state = ExecutionState(data["state"])
    except ValueError as exc:
        raise ValueError(f"'state' is not a valid ExecutionState: {data.get('state')!r}") from exc

    pending_work = data.get("pending_work", [])
    if not isinstance(pending_work, list):
        raise ValueError("'pending_work' must be a list of strings")

    try:
        return Heartbeat(
            execution_id=data["execution_id"],
            state=state,
            current_task=data["current_task"],
            current_subtask=data.get("current_subtask"),
            last_real_progress_at=data["last_real_progress_at"],
            last_progress_evidence=data.get("last_progress_evidence"),
            attempt_count=data["attempt_count"],
            current_strategy=data.get("current_strategy"),
            last_error=data.get("last_error"),
            pending_work=tuple(pending_work),
        )
    except (InvalidObservationError, TypeError) as exc:
        # Domain-level construction failure (e.g. a whitespace-only
        # string, or a wrong-typed field the structural checks above
        # didn't already catch) is exactly as "corrupt" as a missing
        # field — normalize it the same way.
        raise ValueError(str(exc)) from exc


def _validate_schema(data: Any) -> Any:
    """``SafeJsonStore`` validator: construct the real domain object
    purely to validate it, then return the ORIGINAL dict so the on-disk
    representation stays plain JSON."""
    _construct(data)
    return data


def _from_dict(data: Any) -> Heartbeat:
    return _construct(data)


class HeartbeatWriter:
    """Persists the single latest heartbeat for an execution, overwriting
    in place. Backed by ``SafeJsonStore`` for atomic, path-contained,
    concurrency-safe writes.
    """

    def __init__(self, state_dir: str | Path) -> None:
        # Round 5 / AG5-06: bind to an absolute path NOW, using the
        # process's cwd at construction time -- a relative `state_dir`
        # must always mean the same on-disk location for this writer's
        # lifetime, even if the process later changes its working
        # directory. See `bind_persistence_root` for why this is distinct
        # from (and not a replacement for) the per-operation symlink/
        # junction containment check that still runs on every read/write.
        self.state_dir = bind_persistence_root(state_dir)

    def _store(self, execution_id: str) -> SafeJsonStore:
        # Round 4 / NEW-08: reject a permanently-unsafe execution_id (too
        # long, illegal filename characters) immediately, before it can
        # reach a filesystem call and eventually surface a confusing
        # ~10s-later StorageLockError instead of the real problem.
        validate_persistence_safe_id("execution_id", execution_id)
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
