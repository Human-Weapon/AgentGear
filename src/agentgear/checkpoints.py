"""Checkpoint persistence.

A checkpoint is a small, logical marker of "how far did this execution
get" (phase, completed/pending subtasks, last good state) — not a full
snapshot system. Callers append checkpoints as they reach them; recovery
and BLOCKED reporting read the most recent one back.

AG-07: valid JSON is not valid state. Each checkpoint segment file's root
must be a list of well-formed checkpoint objects; anything else (``{}``,
a bare object instead of a list, missing fields, wrong types) is
quarantined and reported as ``CorruptStorageError`` rather than raising a
raw ``KeyError``/``TypeError`` out of ``all()``/``latest()``.

Round 2 / H4: ``_construct_entry`` is the ONE authoritative dict ->
domain-``Checkpoint`` path — used both as the persistence-layer schema
validator and as the deserializer — so a schema-valid-but-domain-invalid
entry (e.g. a whitespace-only ``last_good_state``) cannot escape as a raw
``InvalidObservationError``; every failure mode normalizes to
``ValueError`` for ``SafeJsonStore``'s single quarantine path.

Round 4 / NEW-06: SEGMENTED storage, not one ever-growing file.

The original design kept one JSON file per execution containing the
FULL checkpoint history, and every ``append()`` read the whole file,
parsed it, appended one entry, re-serialized the whole (now one-longer)
list, and atomically rewrote it. Each append therefore did O(history
length) work, and N appends over one execution's lifetime did O(N^2)
total work -- for a long-running execution this made checkpointing
progressively, unboundedly slower the longer the execution had already
run, exactly the executions checkpoints exist to protect.

The fix: each execution's history lives in a directory of bounded
SEGMENT files (``{execution_id}.checkpoints/segment-NNNNNN.json``), each
holding at most ``_SEGMENT_CAPACITY`` checkpoints. ``append()`` only ever
reads/rewrites the CURRENT (newest, not-yet-full) segment -- bounded by
``_SEGMENT_CAPACITY``, independent of total history length -- rolling
over to a fresh segment once the active one is full. ``latest()`` only
ever needs to read the single newest segment. ``all()`` still reads every
segment (unavoidable -- returning the full history requires touching all
of it), but even that remains a simple, linear concatenation.

v0.1.0 has not shipped yet, so this replaces the single-file format
outright rather than carrying migration code for an on-disk format that
was never released (Round 4 / section 9.6).

Durability/concurrency/corruption-quarantine/path-containment guarantees
are unchanged and re-verified per segment: each segment is its own
``SafeJsonStore`` (atomic replace, fsync, real cross-process file lock,
containment-checked against ``trusted_root``). A corrupt segment is
quarantined and reported WITHOUT touching any other (healthy) segment --
segmenting makes quarantine strictly more surgical than the single-file
design, where the corrupt part and "the whole history" were the same
file.

Round 5 / AG5-01: ``_SEGMENT_CAPACITY`` is a HARD bound, not an advisory
target -- no execution schedule, including real concurrent processes, may
ever leave a segment holding more than ``_SEGMENT_CAPACITY`` acknowledged
checkpoints. The original design picked its target segment with an
UNLOCKED peek read, then relied on ``SafeJsonStore.update()``'s own
per-segment lock only for the individual append -- which prevents lost
writes, but not several processes independently observing "segment 1 has
99 entries, still room" and all deciding to append there, overshooting the
cap. ``append()`` now acquires ONE execution-scoped lock
(``{execution_id}.checkpoints/.execution.lock``) around the ENTIRE
select-target -> recheck-capacity -> choose/create-rollover -> append
sequence, so the whole decision is atomic with respect to every other
appender for the same execution. Lock order is always EXECUTION lock
first, then the per-segment ``SafeJsonStore`` lock second (acquired inside
``store.update()``) -- never the reverse anywhere in this module, so there
is no cross-lock deadlock potential.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .exceptions import InvalidObservationError
from .models import Checkpoint
from .path_security import (
    assert_path_family_contained,
    bind_persistence_root,
    validate_persistence_safe_id,
)
from .safe_json_store import FileLock, SafeJsonStore

_MISSING = object()

# Checkpoints per segment file. A fixed, small constant -- NOT derived
# from total history length -- is what bounds per-append work. Chosen
# generously enough that most short-lived executions never roll over past
# segment 1, while keeping any single segment's read/rewrite cost small
# even for a very long-running execution.
_SEGMENT_CAPACITY = 100

_SEGMENT_NAME_RE = re.compile(r"segment-(\d{6})\.json$")


def _to_dict(cp: Checkpoint) -> dict:
    return {
        "execution_id": cp.execution_id,
        "phase": cp.phase,
        "completed": list(cp.completed),
        "pending": list(cp.pending),
        "last_good_state": cp.last_good_state,
        "at_seconds": cp.at_seconds,
    }


def _construct_entry(entry: Any, index: int) -> Checkpoint:
    """The ONE authoritative dict -> domain-``Checkpoint`` path (H4)."""
    if not isinstance(entry, dict):
        raise ValueError(f"checkpoint[{index}] must be an object, got {type(entry).__name__}")

    for field_name in ("execution_id", "phase"):
        if field_name not in entry:
            raise ValueError(f"checkpoint[{index}] is missing required field '{field_name}'")

    for field_name in ("completed", "pending"):
        value = entry.get(field_name, [])
        if not isinstance(value, list):
            raise ValueError(f"checkpoint[{index}].{field_name} must be a list of strings")

    try:
        return Checkpoint(
            execution_id=entry["execution_id"],
            phase=entry["phase"],
            completed=tuple(entry.get("completed", ())),
            pending=tuple(entry.get("pending", ())),
            last_good_state=entry.get("last_good_state"),
            at_seconds=entry.get("at_seconds", 0.0),
        )
    except (InvalidObservationError, TypeError) as exc:
        raise ValueError(f"checkpoint[{index}]: {exc}") from exc


def _validate_segment_schema(data: Any) -> Any:
    """``SafeJsonStore`` validator: construct every entry in ONE segment
    purely to validate it (bounded by ``_SEGMENT_CAPACITY``), then return
    the ORIGINAL data so the on-disk representation stays plain JSON."""
    if not isinstance(data, list):
        raise ValueError(f"checkpoint segment must be a list, got {type(data).__name__}")
    for i, entry in enumerate(data):
        _construct_entry(entry, i)
    return data


class CheckpointStore:
    """Append-only, SEGMENTED checkpoint history for one state directory,
    one directory per execution_id, path-contained and concurrency-safe.
    See the module docstring (Round 4 / NEW-06) for why this is segmented
    rather than one ever-growing file.
    """

    def __init__(self, state_dir: str | Path) -> None:
        # Round 5 / AG5-06: bind to an absolute path NOW (see
        # `bind_persistence_root`) -- a relative `state_dir` must always
        # mean the same on-disk location for this store's lifetime, even
        # across a later `os.chdir()` elsewhere in the process.
        self.state_dir = bind_persistence_root(state_dir)

    def _segment_dir(self, execution_id: str) -> Path:
        # Round 4 / NEW-08: reject a permanently-unsafe execution_id (too
        # long, illegal filename characters) immediately -- it becomes a
        # literal directory-name component here, before it can reach a
        # filesystem call and eventually surface a confusing ~10s-later
        # StorageLockError instead of the real problem.
        validate_persistence_safe_id("execution_id", execution_id)
        return self.state_dir / f"{execution_id}.checkpoints"

    def _segment_path(self, execution_id: str, segment_number: int) -> Path:
        return self._segment_dir(execution_id) / f"segment-{segment_number:06d}.json"

    def _segment_numbers(self, execution_id: str) -> list[int]:
        seg_dir = self._segment_dir(execution_id)
        if not seg_dir.is_dir():
            return []
        numbers = []
        for entry in seg_dir.iterdir():
            match = _SEGMENT_NAME_RE.fullmatch(entry.name)
            if match:
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def _segment_store(self, execution_id: str, segment_number: int) -> SafeJsonStore:
        path = self._segment_path(execution_id, segment_number)
        # `_MISSING` (not the empty list `[]`) marks "segment file does
        # not exist yet", so a segment corrupted into literal JSON `null`
        # is never silently mistaken for a legitimate, merely-empty one.
        return SafeJsonStore(
            path,
            trusted_root=self.state_dir,
            default=lambda: _MISSING,
            validator=_validate_segment_schema,
        )

    def _execution_lock(self, execution_id: str) -> FileLock:
        """Round 5 / AG5-01: ONE lock per execution, held around the whole
        select-segment/recheck-capacity/rollover/append decision in
        ``append()`` -- not per-segment, and not global to every AgentGear
        execution. The lock file itself lives inside the execution's own
        segment directory (excluded from ``_segment_numbers()`` by name,
        since it never matches ``_SEGMENT_NAME_RE``), so it is naturally
        path-contained and cleaned up alongside that directory.
        """
        seg_dir = self._segment_dir(execution_id)
        lock_path = seg_dir / ".execution.lock"

        def _pre(p: Path) -> None:
            assert_path_family_contained(p, trusted_root=self.state_dir)

        return FileLock(lock_path, pre_create_check=_pre)

    def append(self, checkpoint: Checkpoint) -> None:
        execution_id = checkpoint.execution_id
        # Round 5 / AG5-01: the execution lock is acquired FIRST and held
        # across the entire decision -- select target segment, recheck its
        # current size, roll over if full, append -- so no other process
        # for this SAME execution can observe a stale "still room" snapshot
        # and independently decide to append to the same nearly-full
        # segment. `store.update()` below still acquires its own per-
        # segment lock internally (lock order: execution lock, then
        # segment lock -- never the reverse anywhere in this module).
        with self._execution_lock(execution_id):
            numbers = self._segment_numbers(execution_id)
            target = numbers[-1] if numbers else 1
            store = self._segment_store(execution_id, target)

            current = store.read()
            count = 0 if current is _MISSING else len(current)
            if count >= _SEGMENT_CAPACITY:
                target += 1
                store = self._segment_store(execution_id, target)

            store.update(
                lambda current: [*([] if current is _MISSING else current), _to_dict(checkpoint)]
            )

    def all(self, execution_id: str) -> list[Checkpoint]:
        result: list[Checkpoint] = []
        for segment_number in self._segment_numbers(execution_id):
            store = self._segment_store(execution_id, segment_number)
            data = store.read()
            if data is _MISSING:
                continue
            try:
                result.extend(_construct_entry(entry, i) for i, entry in enumerate(data))
            except (ValueError, KeyError, TypeError) as exc:
                store.quarantine_invalid(
                    f"invalid checkpoint schema for {execution_id!r} "
                    f"segment {segment_number}: {exc}"
                )
                raise
        return result

    def latest(self, execution_id: str) -> Checkpoint | None:
        # Only the NEWEST segment is needed -- bounded by
        # _SEGMENT_CAPACITY, independent of total history length.
        for segment_number in reversed(self._segment_numbers(execution_id)):
            store = self._segment_store(execution_id, segment_number)
            data = store.read()
            if data is _MISSING or not data:
                continue
            try:
                entries = [_construct_entry(entry, i) for i, entry in enumerate(data)]
            except (ValueError, KeyError, TypeError) as exc:
                store.quarantine_invalid(
                    f"invalid checkpoint schema for {execution_id!r} "
                    f"segment {segment_number}: {exc}"
                )
                raise
            if entries:
                return entries[-1]
        return None
