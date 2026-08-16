"""Multi-process persistence hardening.

We already trust SafeJsonStore's file-lock primitive at the unit level
(tests/test_safe_json_store.py); this module proves it holds up under
*real* OS process concurrency, not just sequential in-process calls
within a single interpreter. Worker functions must be module-level (not
closures) so they are picklable for ``multiprocessing`` on every start
method, including Windows' default ``spawn``.
"""

from __future__ import annotations

import multiprocessing

import pytest


def _write_heartbeat_worker(state_dir: str, worker_id: int, iterations: int) -> None:
    from agentgear.models import ExecutionState
    from agentgear.watchdog.heartbeat import HeartbeatWriter, build_heartbeat

    writer = HeartbeatWriter(state_dir)
    for i in range(iterations):
        writer.write(
            build_heartbeat(
                execution_id="shared-exec",
                state=ExecutionState.RUNNING,
                current_task=f"worker-{worker_id}-iter-{i}",
                current_subtask=None,
                last_real_progress_at=float(i),
                last_progress_evidence=None,
                attempt_count=i,
                current_strategy=None,
                last_error=None,
            )
        )


def _append_checkpoint_worker(state_dir: str, worker_id: int, count: int) -> None:
    from agentgear.checkpoints import CheckpointStore
    from agentgear.models import Checkpoint

    store = CheckpointStore(state_dir)
    for i in range(count):
        store.append(
            Checkpoint(
                execution_id="shared-exec",
                phase=f"worker-{worker_id}-checkpoint-{i}",
                at_seconds=float(i),
            )
        )


def _append_checkpoint_after_barrier_worker(
    state_dir: str, worker_id: int, barrier: multiprocessing.Barrier
) -> None:
    """Round 5 / AG5-01: unlike ``_append_checkpoint_worker`` (which relies
    on natural OS scheduling overlap across many iterations), this worker
    does exactly ONE append, released by a shared barrier so every worker's
    single append is scheduled as close to simultaneously as possible --
    maximizing the odds of hitting the exact race the segment-capacity
    hard-cap fix must close: several processes independently observing
    "segment still has room" at once.
    """
    from agentgear.checkpoints import CheckpointStore
    from agentgear.models import Checkpoint

    store = CheckpointStore(state_dir)
    barrier.wait()
    store.append(
        Checkpoint(execution_id="race-exec", phase=f"race-{worker_id}", at_seconds=float(worker_id))
    )


def _run_workers(target, args_list, timeout: float = 60.0) -> None:
    procs = [multiprocessing.Process(target=target, args=args) for args in args_list]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=timeout)
    for p in procs:
        assert not p.is_alive(), "worker process did not finish within timeout"
        assert p.exitcode == 0, f"worker process exited with code {p.exitcode}"


@pytest.mark.parametrize("num_workers", [2, 5])
def test_heartbeat_writer_survives_concurrent_processes(tmp_path, num_workers: int) -> None:
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = str(tmp_path)
    _run_workers(_write_heartbeat_worker, [(state_dir, i, 20) for i in range(num_workers)])

    # The file must end in a fully valid, non-corrupt state -- some
    # worker's last write "wins" (heartbeats are overwrite-in-place), but
    # it must be a complete, schema-valid write, never a torn/partial one.
    result = HeartbeatWriter(state_dir).read("shared-exec")
    assert result is not None
    assert result.attempt_count >= 0


@pytest.mark.parametrize("num_workers", [2, 5, 10])
def test_checkpoint_store_survives_concurrent_processes(tmp_path, num_workers: int) -> None:
    """Round 4 / NEW-06: re-verified against the SEGMENTED storage design.
    ``iterations`` is sized so concurrent workers collectively span
    multiple segment rollovers (not just appends within one segment),
    which is the new race surface the segmented design introduces --
    ``append()``'s "peek a target segment, then update() under lock"
    pattern must never lose an entry even when multiple processes decide
    to roll over to the same next segment at once.
    """
    from agentgear.checkpoints import _SEGMENT_CAPACITY, CheckpointStore

    state_dir = str(tmp_path)
    iterations = max(10, (2 * _SEGMENT_CAPACITY) // max(num_workers, 1))
    _run_workers(
        _append_checkpoint_worker, [(state_dir, i, iterations) for i in range(num_workers)]
    )

    history = CheckpointStore(state_dir).all("shared-exec")
    # Every acknowledged checkpoint from every worker must survive --
    # concurrent appends must never silently lose an entry via a lost
    # update (read-modify-write race), even across segment rollovers.
    assert len(history) == num_workers * iterations
    phases = {c.phase for c in history}
    assert len(phases) == num_workers * iterations, "some checkpoint entries were overwritten/lost"


@pytest.mark.parametrize(
    "preload,num_workers",
    [(99, 5), (99, 10), (199, 2), (199, 5), (199, 10)],
)
def test_checkpoint_segment_capacity_is_a_hard_cap_under_real_concurrency(
    tmp_path, preload: int, num_workers: int
) -> None:
    """Round 5 / AG5-01: preload a segment to just under (or just past) the
    capacity boundary, then have several REAL processes race to append,
    released simultaneously via a barrier. NO segment may ever exceed
    ``_SEGMENT_CAPACITY`` acknowledged entries, and the total stored count
    must equal exactly preload + num_workers (every acknowledged append
    survives exactly once -- no loss, no duplication).
    """
    from agentgear.checkpoints import _SEGMENT_CAPACITY, CheckpointStore
    from agentgear.models import Checkpoint

    state_dir = str(tmp_path)
    store = CheckpointStore(state_dir)
    for i in range(preload):
        store.append(Checkpoint(execution_id="race-exec", phase=f"pre-{i}", at_seconds=float(i)))

    barrier = multiprocessing.Barrier(num_workers)
    _run_workers(
        _append_checkpoint_after_barrier_worker,
        [(state_dir, i, barrier) for i in range(num_workers)],
    )

    history = store.all("race-exec")
    assert len(history) == preload + num_workers
    phases = {c.phase for c in history}
    assert len(phases) == preload + num_workers, "some checkpoint entries were overwritten/lost"

    for segment_number in store._segment_numbers("race-exec"):
        segment_data = store._segment_store("race-exec", segment_number).read()
        assert len(segment_data) <= _SEGMENT_CAPACITY, (
            f"segment {segment_number} holds {len(segment_data)} entries, "
            f"exceeding the hard cap of {_SEGMENT_CAPACITY}"
        )


def test_repeated_heartbeat_runs_never_corrupt_the_file(tmp_path) -> None:
    """Multiple sequential multi-process 'runs' targeting the same
    execution_id — simulates a long-lived execution restarting workers —
    must never leave the heartbeat file in a corrupt state."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = str(tmp_path)
    for _run in range(3):
        _run_workers(_write_heartbeat_worker, [(state_dir, w, 5) for w in range(3)])
        result = HeartbeatWriter(state_dir).read("shared-exec")
        assert result is not None
