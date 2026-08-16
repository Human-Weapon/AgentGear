"""Round 5 remediation: AG5-01 through AG5-11. See
docs/audits/remediation-round-5.md for the full finding writeups and
design decisions. (AG5-01's multiprocess/structural tests live in
tests/test_persistence_concurrency.py and tests/test_checkpoints.py,
alongside the other checkpoint-storage tests they extend.)
"""

from __future__ import annotations

import os

import pytest

import agentgear.watchdog.coordinator as coordinator_module
from agentgear.config import Policy, WatchdogPolicy
from agentgear.exceptions import ConfigurationError, InvalidObservationError
from agentgear.models import Checkpoint, ExecutionState, ProgressSignalKind, RecoveryResult
from agentgear.watchdog import ExecutionWatchdog
from agentgear.watchdog.heartbeat import build_heartbeat
from agentgear.watchdog.stall_detection import ActivityRecord


def _rd5_policy(**kw) -> Policy:
    defaults = dict(no_progress_seconds=5.0, no_progress_cycles=2, max_recovery_attempts=10)
    defaults.update(kw)
    return Policy(watchdog=WatchdogPolicy(**defaults))


def _watchdog_in_recovering(**kw) -> ExecutionWatchdog:
    w = ExecutionWatchdog("e1", _rd5_policy(), **kw)
    w.start(task="t", at_seconds=0.0)
    for i in range(5):
        w.record_activity(at_seconds=1.0 + i, fingerprint=f"f{i}", succeeded=True)
    w.evaluate(at_seconds=100000.0)
    assert w.state == ExecutionState.RECOVERING
    return w


def _snapshot(w: ExecutionWatchdog):
    return (
        w.state,
        list(w._recovery_attempts),
        list(w._recovery_history),
        len(w.transition_history),
        w._last_observed_at,
    )


# --- AG5-02: record_recovery_result must validate before mutating ----------


def test_success_with_non_string_evidence_is_rejected_with_zero_mutation() -> None:
    w = _watchdog_in_recovering()
    snap = _snapshot(w)
    with pytest.raises(InvalidObservationError):
        w.record_recovery_result(at_seconds=100001.0, result=RecoveryResult.SUCCESS, evidence=42)
    assert _snapshot(w) == snap


def test_pending_result_is_rejected_with_zero_mutation() -> None:
    w = _watchdog_in_recovering()
    snap = _snapshot(w)
    with pytest.raises(InvalidObservationError):
        w.record_recovery_result(at_seconds=100001.0, result=RecoveryResult.PENDING)
    assert _snapshot(w) == snap


def test_failure_with_blank_evidence_is_rejected_with_zero_mutation() -> None:
    w = _watchdog_in_recovering()
    snap = _snapshot(w)
    with pytest.raises(InvalidObservationError):
        w.record_recovery_result(at_seconds=100001.0, result=RecoveryResult.FAILURE, evidence="   ")
    assert _snapshot(w) == snap


def test_raw_string_result_is_rejected_despite_str_enum_subclassing() -> None:
    """RecoveryResult subclasses str, so a raw string could slip past a
    naive `value in (...)` or `value == Enum.X` check. isinstance() must
    reject it outright."""
    w = _watchdog_in_recovering()
    snap = _snapshot(w)
    with pytest.raises(InvalidObservationError):
        w.record_recovery_result(at_seconds=100001.0, result="success")
    assert _snapshot(w) == snap


def test_valid_success_still_works_after_validation_hardening() -> None:
    w = _watchdog_in_recovering()
    w.record_recovery_result(
        at_seconds=100001.0, result=RecoveryResult.SUCCESS, evidence="fixed it"
    )
    assert w.state == ExecutionState.RUNNING


def test_valid_failure_with_none_evidence_still_works() -> None:
    w = _watchdog_in_recovering()
    w.record_recovery_result(at_seconds=100001.0, result=RecoveryResult.FAILURE)
    assert w.state in (ExecutionState.STALLED, ExecutionState.BLOCKED)


# --- AG5-03: ActivityRecord strict field validation -------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(succeeded=1),
        dict(succeeded="false"),
        dict(succeeded="true"),
        dict(succeeded=0),
        dict(is_trivial=1),
        dict(is_trivial=0),
        dict(is_trivial="false"),
        dict(error="   "),
        dict(error=42),
    ],
)
def test_activity_record_rejects_non_strict_fields(kwargs: dict) -> None:
    base = dict(at_seconds=1.0, fingerprint="f", succeeded=True)
    base.update(kwargs)
    with pytest.raises(InvalidObservationError):
        ActivityRecord(**base)


def test_activity_record_accepts_none_error_and_valid_bools() -> None:
    record = ActivityRecord(
        at_seconds=1.0, fingerprint="f", succeeded=False, is_trivial=True, error=None
    )
    assert record.error is None
    assert record.succeeded is False
    assert record.is_trivial is True


def test_record_activity_rejects_invalid_boolean_with_zero_mutation() -> None:
    w = ExecutionWatchdog("e1", _rd5_policy())
    w.start(task="t", at_seconds=0.0)
    activities_before = list(w._activities)
    with pytest.raises(InvalidObservationError):
        w.record_activity(at_seconds=1.0, fingerprint="f", succeeded="false")
    assert w._activities == activities_before
    # a legitimate call at a normal timestamp must still succeed afterward
    w.record_activity(at_seconds=1.0, fingerprint="f", succeeded=True)
    assert len(w._activities) == 1


def test_record_activity_rejects_invalid_error_with_zero_mutation() -> None:
    w = ExecutionWatchdog("e1", _rd5_policy())
    w.start(task="t", at_seconds=0.0)
    with pytest.raises(InvalidObservationError):
        w.record_activity(at_seconds=1.0, fingerprint="f", succeeded=False, error="   ")
    assert w._activities == []


# --- AG5-03/AG5-05: heartbeat dirty must cover construction, not just I/O --


def test_heartbeat_construction_failure_marks_dirty_and_propagates(tmp_path) -> None:
    """The domain mutation (advance to TESTING) must still have committed
    -- NEW-04's model never rolls back a completed domain operation for a
    persistence failure -- but heartbeat_dirty must become True even
    though the failure happened while BUILDING the projection, not while
    writing it."""
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    assert w.heartbeat_dirty is False

    real_build = coordinator_module.build_heartbeat

    def boom(**kwargs):
        raise RuntimeError("construction boom")

    coordinator_module.build_heartbeat = boom
    try:
        with pytest.raises(RuntimeError):
            w.advance(ExecutionState.TESTING, at_seconds=1.0)
    finally:
        coordinator_module.build_heartbeat = real_build

    assert w.state == ExecutionState.TESTING
    assert w.heartbeat_dirty is True
    assert w.sync_heartbeat() is True
    assert w.heartbeat_dirty is False


def test_record_activity_updates_disk_heartbeat_on_normal_non_stalling_activity(
    tmp_path,
) -> None:
    """AG5-05: normal (non-stalling) activity must update the durable
    heartbeat, not only a state-machine transition."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    w.record_activity(at_seconds=1.0, fingerprint="f", succeeded=True)

    assert w.heartbeat_dirty is False
    disk = HeartbeatWriter(str(tmp_path)).read("e1")
    assert disk.attempt_count == w.status()["attempt_count"] == 1


# --- AG5-04: str-enum trap on advance()/transition() ------------------------


@pytest.mark.parametrize("bad_target", ["testing", "blocked", 1, True, None])
def test_advance_rejects_non_enum_target_with_zero_mutation(bad_target) -> None:
    """ExecutionState subclasses str, so a raw same-valued string compares
    AND hashes equal to the real enum member -- only isinstance() catches
    it. A poisoned target must never reach self._sm.state (which would
    then be a raw str with no `.value` attribute)."""
    w = ExecutionWatchdog("e1", _rd5_policy())
    w.start(task="t", at_seconds=0.0)
    snap = _snapshot(w)
    with pytest.raises(InvalidObservationError):
        w.advance(bad_target, at_seconds=1.0)
    assert _snapshot(w) == snap
    assert w.state == ExecutionState.RUNNING
    w.status()  # must not raise -- state.value must still be reachable


def test_budget_ledger_reserve_rejects_raw_string_kind() -> None:
    """Cross-cutting enum sweep: ReservationKind subclasses str just like
    ExecutionState does -- a raw same-valued string previously slipped
    through reserve() and poisoned the resulting BudgetReservation.kind
    with a plain str instead of a real enum member."""
    from agentgear import ExecutionBudgetLedger

    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=10.0)
    with pytest.raises(ConfigurationError):
        ledger.reserve(kind="initial_plan", tokens=10, cost=0.1)
    assert ledger.status()["reservation_count"] == 0


def test_low_level_state_machine_transition_rejects_raw_string() -> None:
    from agentgear.watchdog.state_machine import ExecutionStateMachine

    sm = ExecutionStateMachine(execution_id="x")
    with pytest.raises(InvalidObservationError):
        sm.transition("running", at_seconds=1.0)
    assert sm.state == ExecutionState.PLANNING
    assert isinstance(sm.state, ExecutionState)


def test_state_machine_constructor_rejects_raw_string_state() -> None:
    """The str-enum trap applies to the CONSTRUCTOR too, not just
    transition() -- a separate entry point into the same public class."""
    from agentgear.watchdog.state_machine import ExecutionStateMachine

    with pytest.raises(InvalidObservationError):
        ExecutionStateMachine(execution_id="x", state="running")
    sm = ExecutionStateMachine(execution_id="x")
    assert sm.state == ExecutionState.PLANNING


# --- AG5-05: full heartbeat projection matrix -------------------------------


def _assert_heartbeat_current(w: ExecutionWatchdog, state_dir) -> None:
    """AG5-05: the durable heartbeat must reflect current in-memory state
    after every step in this walk -- never dirty, never stale."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    assert w.heartbeat_dirty is False, "heartbeat_dirty must be False after a clean sync"
    disk = HeartbeatWriter(str(state_dir)).read(w.execution_id)
    status = w.status()
    assert disk.state == w.state
    assert disk.current_task == status["current_task"]
    assert disk.attempt_count == status["attempt_count"]
    assert disk.last_error == w._last_error
    expected_pending = w._checkpoints[-1].pending if w._checkpoints else ()
    assert disk.pending_work == expected_pending


def test_heartbeat_projection_matrix_success_path(tmp_path) -> None:
    """Walks start -> activity -> progress -> TESTING -> REVIEWING ->
    stall -> recovery begin -> recovery SUCCESS -> checkpoint ->
    COMPLETED, asserting the durable heartbeat is current after EVERY
    step (section 18)."""
    w = ExecutionWatchdog(
        "e1",
        _rd5_policy(no_progress_seconds=1.0, no_progress_cycles=1, max_recovery_attempts=5),
        state_dir=str(tmp_path),
    )

    w.start(task="t", at_seconds=0.0)
    _assert_heartbeat_current(w, tmp_path)

    w.record_activity(at_seconds=1.0, fingerprint="f1", succeeded=True)
    _assert_heartbeat_current(w, tmp_path)

    w.record_progress(
        at_seconds=2.0, kind=ProgressSignalKind.SUBTASK_COMPLETED, description="did a thing"
    )
    _assert_heartbeat_current(w, tmp_path)

    w.advance(ExecutionState.TESTING, at_seconds=3.0)
    _assert_heartbeat_current(w, tmp_path)

    w.advance(ExecutionState.REVIEWING, at_seconds=4.0)
    _assert_heartbeat_current(w, tmp_path)

    w.advance(ExecutionState.RUNNING, at_seconds=5.0)
    _assert_heartbeat_current(w, tmp_path)

    # force a stall -> RECOVERING (evaluate() runs inside record_activity)
    w.record_activity(at_seconds=100.0, fingerprint="f2", succeeded=False)
    assert w.state == ExecutionState.RECOVERING
    _assert_heartbeat_current(w, tmp_path)

    w.record_recovery_result(at_seconds=101.0, result=RecoveryResult.SUCCESS, evidence="recovered")
    assert w.state == ExecutionState.RUNNING
    _assert_heartbeat_current(w, tmp_path)

    w.checkpoint(at_seconds=102.0, phase="p1", pending=("next-step",))
    _assert_heartbeat_current(w, tmp_path)

    w.advance(ExecutionState.REVIEWING, at_seconds=103.0)
    w.complete(at_seconds=104.0, evidence=("done",))
    _assert_heartbeat_current(w, tmp_path)


def test_heartbeat_projection_matrix_blocked_path(tmp_path) -> None:
    """Walks a stall -> recovery begin -> recovery FAILURE -> exhausted ->
    BLOCKED, asserting the durable heartbeat is current at each step."""
    w = ExecutionWatchdog(
        "e1",
        _rd5_policy(no_progress_seconds=1.0, no_progress_cycles=1, max_recovery_attempts=1),
        state_dir=str(tmp_path),
    )
    w.start(task="t", at_seconds=0.0)
    w.record_activity(at_seconds=100.0, fingerprint="f1", succeeded=False)
    assert w.state == ExecutionState.RECOVERING
    _assert_heartbeat_current(w, tmp_path)

    w.record_recovery_result(at_seconds=101.0, result=RecoveryResult.FAILURE, evidence="nope")
    assert w.state == ExecutionState.STALLED
    _assert_heartbeat_current(w, tmp_path)

    # max_recovery_attempts=1 was already used by the first attempt --
    # retrying now hits the per-episode admission gate and drives
    # straight to BLOCKED via _transition_to_blocked().
    w.begin_recovery(at_seconds=102.0)
    assert w.state == ExecutionState.BLOCKED
    _assert_heartbeat_current(w, tmp_path)
    assert w.blocked_report is not None


def test_record_escalation_intentionally_does_not_touch_heartbeat_fields(tmp_path) -> None:
    """record_escalation() changes tier/reasoning/escalations_used/budget
    -- none of which are Heartbeat fields -- so it correctly does NOT
    call _write_heartbeat(); this documents that as intentional, not an
    oversight."""
    from agentgear.escalation import EscalationSignals
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    before = HeartbeatWriter(str(tmp_path)).read("e1")

    decision = w.record_escalation(
        at_seconds=1.0,
        signals=EscalationSignals(security_risk=True),
    )
    assert decision.should_escalate is True
    after = HeartbeatWriter(str(tmp_path)).read("e1")
    assert before == after, "record_escalation must not touch the durable heartbeat"
    assert w.heartbeat_dirty is False


def test_sync_after_dirty_writes_the_latest_state_not_a_stale_snapshot(tmp_path) -> None:
    """Section 21: once dirty, further mutations are still allowed (the
    coordinator never blocks on a dirty heartbeat) -- sync_heartbeat()
    must persist the CURRENT authoritative state at sync time, not
    whatever state first went dirty."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)

    real_write = w._heartbeat_writer.write
    w._heartbeat_writer.write = lambda hb: (_ for _ in ()).throw(OSError("disk full"))
    with pytest.raises(OSError):
        w.advance(ExecutionState.TESTING, at_seconds=1.0)
    assert w.heartbeat_dirty is True

    # further mutations happen WHILE dirty, without syncing first
    w._heartbeat_writer.write = real_write
    w.record_activity(at_seconds=2.0, fingerprint="f1", succeeded=True)
    w.advance(ExecutionState.REVIEWING, at_seconds=3.0)

    # record_activity/advance both call _write_heartbeat internally and
    # succeeded (writer was restored), so dirty should already be clear --
    # but even if it weren't, an explicit sync must reflect REVIEWING/
    # attempt_count=1, never the stale TESTING/attempt_count=0 snapshot.
    assert w.sync_heartbeat() is True
    assert w.heartbeat_dirty is False
    disk = HeartbeatWriter(str(tmp_path)).read("e1")
    assert disk.state == ExecutionState.REVIEWING
    assert disk.attempt_count == 1


def test_sync_heartbeat_is_idempotent_and_does_not_duplicate_history(tmp_path) -> None:
    """Repeated sync_heartbeat() calls (including when nothing is dirty)
    must never repeat a domain transition, attempt count, or history
    entry -- purely a durability catch-up on the CURRENT projection."""
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    w.record_activity(at_seconds=1.0, fingerprint="f1", succeeded=True)

    transitions_before = len(w.transition_history)
    attempts_before = w.status()["attempt_count"]
    for _ in range(5):
        assert w.sync_heartbeat() is True
    assert len(w.transition_history) == transitions_before
    assert w.status()["attempt_count"] == attempts_before


def test_sync_heartbeat_can_fail_twice_then_succeed_without_duplicating_history(
    tmp_path,
) -> None:
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)

    real_write = w._heartbeat_writer.write
    fail_calls = {"count": 0}

    def flaky(hb):
        fail_calls["count"] += 1
        if fail_calls["count"] <= 2:
            raise OSError("transient")
        return real_write(hb)

    w._heartbeat_writer.write = flaky
    with pytest.raises(OSError):
        w.advance(ExecutionState.TESTING, at_seconds=1.0)
    assert w.heartbeat_dirty is True

    assert w.sync_heartbeat() is False  # 2nd failure
    assert w.heartbeat_dirty is True
    assert w.sync_heartbeat() is True  # 3rd call succeeds
    assert w.heartbeat_dirty is False

    transitions_before = len(w.transition_history)
    assert w.sync_heartbeat() is True  # no-op, nothing dirty
    assert len(w.transition_history) == transitions_before


# --- AG5-06: relative state_dir must stay bound after os.chdir() -----------


def test_heartbeat_writer_relative_root_stays_bound_after_chdir(tmp_path, monkeypatch) -> None:
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.chdir(a)
    writer = HeartbeatWriter("state")
    hb = build_heartbeat(
        execution_id="e1",
        state=ExecutionState.RUNNING,
        current_task="t",
        current_subtask=None,
        last_real_progress_at=0.0,
        last_progress_evidence=None,
        attempt_count=0,
        current_strategy=None,
        last_error=None,
    )
    monkeypatch.chdir(b)
    writer.write(hb)

    assert (a / "state" / "e1.heartbeat.json").exists()
    assert not (b / "state").exists()


def test_checkpoint_store_relative_root_stays_bound_after_chdir(tmp_path, monkeypatch) -> None:
    from agentgear.checkpoints import CheckpointStore

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.chdir(a)
    store = CheckpointStore("state")
    monkeypatch.chdir(b)
    store.append(Checkpoint(execution_id="exec-1", phase="p0", at_seconds=0.0))

    assert (a / "state" / "exec-1.checkpoints").exists()
    assert not (b / "state").exists()


def test_execution_watchdog_relative_state_dir_stays_bound_after_chdir(
    tmp_path, monkeypatch
) -> None:
    """End-to-end through the public coordinator, not just the low-level
    writers directly."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.chdir(a)
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir="state")
    monkeypatch.chdir(b)
    w.start(task="t", at_seconds=0.0)

    assert HeartbeatWriter(str(a / "state")).read("e1") is not None
    assert not (b / "state").exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_relative_root_binding_does_not_weaken_junction_containment(tmp_path, monkeypatch) -> None:
    """AG5-06's fix must not accidentally bypass the EXISTING per-
    operation symlink/junction containment check -- lexical root binding
    (os.path.abspath) and security canonicalization
    (resolve_via_nearest_existing_ancestor) are two different mechanisms
    that must both keep working together."""
    import shutil
    import subprocess

    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import PathEscapeError

    def _make_junction(link: str, target: str) -> None:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.skip(f"could not create junction: {result.stderr or result.stdout}")

    a = tmp_path / "a"
    outside = tmp_path / "outside"
    a.mkdir()
    outside.mkdir()

    monkeypatch.chdir(a)
    store = CheckpointStore("state")
    store.append(Checkpoint(execution_id="exec-1", phase="p0", at_seconds=0.0))

    seg_dir = a / "state" / "exec-1.checkpoints"
    shutil.rmtree(seg_dir)
    _make_junction(str(seg_dir), str(outside))

    with pytest.raises(PathEscapeError):
        store.append(Checkpoint(execution_id="exec-1", phase="p1", at_seconds=1.0))
    assert list(outside.iterdir()) == []


# --- AG5-10: blank state_dir must be rejected, not silently ignored --------


@pytest.mark.parametrize("bad_state_dir", ["", "   ", "\t\n"])
def test_blank_state_dir_is_rejected(bad_state_dir: str) -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog("e1", _rd5_policy(), state_dir=bad_state_dir)


def test_none_state_dir_still_disables_persistence() -> None:
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=None)
    assert w._heartbeat_writer is None
    assert w._checkpoint_store is None


def test_valid_state_dir_still_enables_persistence(tmp_path) -> None:
    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    assert w._heartbeat_writer is not None
    assert w._checkpoint_store is not None


def test_record_progress_updates_disk_heartbeat(tmp_path) -> None:
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _rd5_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    w.record_progress(
        at_seconds=1.0,
        kind=ProgressSignalKind.SUBTASK_COMPLETED,
        description="made progress",
    )

    assert w.heartbeat_dirty is False
    disk = HeartbeatWriter(str(tmp_path)).read("e1")
    assert disk.last_progress_evidence == "made progress"
