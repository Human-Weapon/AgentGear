"""Round 6 remediation: AG6-01 through AG6-03. See
docs/audits/remediation-round-6.md for the full finding writeups and
design decisions.
"""

from __future__ import annotations

import pytest

from agentgear.config import Policy, WatchdogPolicy
from agentgear.exceptions import (
    ConfigurationError,
    InvalidStateTransitionError,
)
from agentgear.models import ExecutionState, ProgressSignalKind, RecoveryResult
from agentgear.watchdog import ExecutionWatchdog
from agentgear.watchdog.state_machine import ExecutionStateMachine


def _rd6_policy(**kw) -> Policy:
    defaults = dict(no_progress_seconds=5.0, no_progress_cycles=2, max_recovery_attempts=10)
    defaults.update(kw)
    return Policy(watchdog=WatchdogPolicy(**defaults))


# --- state-reaching helpers, each via the public API only -------------------


def _at_planning(**kw) -> ExecutionWatchdog:
    return ExecutionWatchdog("e1", _rd6_policy(), **kw)


def _at_running(**kw) -> ExecutionWatchdog:
    w = _at_planning(**kw)
    w.start(task="t", at_seconds=0.0)
    return w


def _at_testing(**kw) -> ExecutionWatchdog:
    w = _at_running(**kw)
    w.advance(ExecutionState.TESTING, at_seconds=1.0)
    return w


def _at_reviewing(**kw) -> ExecutionWatchdog:
    w = _at_running(**kw)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    return w


def _at_completed(**kw) -> ExecutionWatchdog:
    w = _at_reviewing(**kw)
    w.complete(at_seconds=2.0, evidence=("done",))
    return w


def _at_recovering(**kw) -> ExecutionWatchdog:
    w = _at_running(**kw)
    for i in range(5):
        w.record_activity(at_seconds=1.0 + i, fingerprint=f"f{i}", succeeded=True)
    w.evaluate(at_seconds=100000.0)
    assert w.state == ExecutionState.RECOVERING
    return w


def _at_stalled(**kw) -> ExecutionWatchdog:
    w = _at_recovering(**kw)
    w.record_recovery_result(at_seconds=100001.0, result=RecoveryResult.FAILURE, evidence="nope")
    assert w.state == ExecutionState.STALLED
    return w


def _at_blocked(**kw) -> ExecutionWatchdog:
    """Requires a tight max_recovery_attempts to reach BLOCKED reliably."""
    kw.setdefault("state_dir", None)
    w = ExecutionWatchdog(
        "e1",
        _rd6_policy(no_progress_seconds=1.0, no_progress_cycles=1, max_recovery_attempts=1),
        state_dir=kw["state_dir"],
    )
    w.start(task="t", at_seconds=0.0)
    w.record_activity(at_seconds=1.0, fingerprint="f0", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    w.record_recovery_result(at_seconds=2.0, result=RecoveryResult.FAILURE, evidence="nope")
    assert w.state == ExecutionState.STALLED
    w.begin_recovery(at_seconds=3.0)
    assert w.state == ExecutionState.BLOCKED
    return w


_STATE_REACHERS = {
    ExecutionState.PLANNING: _at_planning,
    ExecutionState.RUNNING: _at_running,
    ExecutionState.TESTING: _at_testing,
    ExecutionState.REVIEWING: _at_reviewing,
    ExecutionState.STALLED: _at_stalled,
    ExecutionState.RECOVERING: _at_recovering,
    ExecutionState.BLOCKED: _at_blocked,
    ExecutionState.COMPLETED: _at_completed,
}

_ACTIVE = {ExecutionState.RUNNING, ExecutionState.TESTING, ExecutionState.REVIEWING}
_INACTIVE = set(_STATE_REACHERS) - _ACTIVE


def _snapshot(w: ExecutionWatchdog):
    return (
        w.state,
        len(w._activities),
        len(w._checkpoints),
        w._progress.last_progress_at,
        w._last_progress_evidence,
        len(w.transition_history),
        w._last_observed_at,
        w.tier,
        w.reasoning,
        w.escalations_used,
        w.budget.status(),
        len(w._recovery_attempts),
        len(w._recovery_history),
    )


def _run_ordinary_event(w: ExecutionWatchdog, operation: str, at_seconds: float) -> None:
    if operation == "record_activity":
        w.record_activity(at_seconds=at_seconds, fingerprint="g", succeeded=True)
    elif operation == "record_progress":
        w.record_progress(
            at_seconds=at_seconds,
            kind=ProgressSignalKind.SUBTASK_COMPLETED,
            description="did a thing",
        )
    elif operation == "checkpoint":
        w.checkpoint(at_seconds=at_seconds, phase="p")
    elif operation == "record_escalation":
        from agentgear.escalation import EscalationSignals

        w.record_escalation(at_seconds=at_seconds, signals=EscalationSignals(security_risk=True))
    elif operation == "advance":
        w.advance(ExecutionState.RUNNING, at_seconds=at_seconds)
    else:
        raise ValueError(operation)


_ORDINARY_OPERATIONS = ("record_activity", "record_progress", "checkpoint", "record_escalation")


# --- AG6-01: the authoritative state x event admission matrix --------------


@pytest.mark.parametrize("operation", _ORDINARY_OPERATIONS)
@pytest.mark.parametrize("state", sorted(_ACTIVE, key=lambda s: s.value))
def test_ordinary_events_are_allowed_in_every_active_state(operation: str, state) -> None:
    w = _STATE_REACHERS[state]()
    _run_ordinary_event(w, operation, at_seconds=1000.0)  # must not raise


@pytest.mark.parametrize("operation", _ORDINARY_OPERATIONS)
@pytest.mark.parametrize("state", sorted(_INACTIVE, key=lambda s: s.value))
def test_ordinary_events_are_rejected_outside_active_states_with_zero_mutation(
    operation: str, state
) -> None:
    w = _STATE_REACHERS[state]()
    snap = _snapshot(w)
    with pytest.raises(InvalidStateTransitionError):
        _run_ordinary_event(w, operation, at_seconds=1000.0)
    assert _snapshot(w) == snap


_LEGAL_ACTIVE_ADVANCE_TARGET = {
    # REVIEWING's own _ALLOWED targets are {RUNNING, COMPLETED, STALLED} --
    # RUNNING is its only legal ACTIVE-state target via advance().
    ExecutionState.RUNNING: ExecutionState.TESTING,
    ExecutionState.TESTING: ExecutionState.RUNNING,
    ExecutionState.REVIEWING: ExecutionState.RUNNING,
}


@pytest.mark.parametrize("state", sorted(_ACTIVE, key=lambda s: s.value))
def test_advance_between_active_states_is_allowed(state) -> None:
    w = _STATE_REACHERS[state]()
    target = _LEGAL_ACTIVE_ADVANCE_TARGET[state]
    w.advance(target, at_seconds=1000.0)  # must not raise
    assert w.state == target


@pytest.mark.parametrize("state", sorted(_INACTIVE, key=lambda s: s.value))
def test_advance_is_rejected_outside_active_states_with_zero_mutation(state) -> None:
    w = _STATE_REACHERS[state]()
    snap = _snapshot(w)
    with pytest.raises(InvalidStateTransitionError):
        w.advance(ExecutionState.RUNNING, at_seconds=1000.0)
    assert _snapshot(w) == snap


def test_advance_no_longer_bypasses_start_from_planning() -> None:
    """The original AG6-01-adjacent discovery: advance() used to silently
    skip start()'s own initialization (leaving _started_at=None while the
    state read RUNNING), which permanently disabled stall detection since
    evaluate() no-ops whenever _started_at is None."""
    w = _at_planning()
    with pytest.raises(InvalidStateTransitionError):
        w.advance(ExecutionState.RUNNING, at_seconds=1.0)
    assert w.state == ExecutionState.PLANNING
    assert w._started_at is None


def test_advance_no_longer_escapes_recovering() -> None:
    """advance() used to be able to leave RECOVERING directly, abandoning
    a still-PENDING RecoveryAttempt and leaving the recovery episode
    forever unresolved -- record_recovery_result() is the only legal
    resolution path."""
    w = _at_recovering()
    with pytest.raises(InvalidStateTransitionError):
        w.advance(ExecutionState.RUNNING, at_seconds=1000.0)
    assert w.state == ExecutionState.RECOVERING
    assert w._recovery_attempts
    assert w._recovery_attempts[-1].result == RecoveryResult.PENDING


def test_blocked_to_recovering_remains_legal_at_the_low_level_state_machine() -> None:
    """AG6-01 scoping note: the PUBLIC ExecutionWatchdog coordinator has
    never exposed a resumption method for BLOCKED (begin_recovery()
    requires STALLED; advance() has always blocked RECOVERING/BLOCKED as
    targets, both before and after this round) -- BLOCKED -> RECOVERING is
    a low-level ExecutionStateMachine legality fact (Round 2 / L5,
    INTENTIONAL), preserved here unchanged and untouched by this round's
    coordinator-level admission guard."""
    sm = ExecutionStateMachine(execution_id="x", state=ExecutionState.BLOCKED)
    sm.transition(ExecutionState.RECOVERING, at_seconds=1.0)
    assert sm.state == ExecutionState.RECOVERING


def test_blocked_ordinary_events_all_rejected_but_begin_recovery_still_legal() -> None:
    """The one EXPLICIT legal way out of BLOCKED through the public
    coordinator is begin_recovery() -- reached only via a fresh STALLED
    (never directly from BLOCKED); this test proves ordinary events are
    rejected from BLOCKED while begin_recovery()'s own precondition
    (state == STALLED) is unaffected by the new admission guard."""
    w = _at_blocked()
    for op in _ORDINARY_OPERATIONS:
        with pytest.raises(InvalidStateTransitionError):
            _run_ordinary_event(w, op, at_seconds=10.0)
    assert w.state == ExecutionState.BLOCKED


def test_terminal_invariant_completed_rejects_all_domain_mutation() -> None:
    w = _at_completed()
    snap = _snapshot(w)
    for op in (*_ORDINARY_OPERATIONS, "advance"):
        with pytest.raises(InvalidStateTransitionError):
            _run_ordinary_event(w, op, at_seconds=1000.0)
    assert _snapshot(w) == snap


def test_sync_heartbeat_remains_legal_after_completed(tmp_path) -> None:
    """sync_heartbeat() is explicitly NOT gated -- it synchronizes
    already-committed state rather than adding new domain work, and must
    remain callable even once COMPLETED."""
    w = ExecutionWatchdog("e1", _rd6_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    w.complete(at_seconds=2.0, evidence=("done",))
    assert w.sync_heartbeat() is True  # must not raise


# --- section 17: lifecycle admission has priority over other input errors --


def test_lifecycle_rejection_takes_priority_over_invalid_timestamp() -> None:
    """COMPLETED + record_activity(at_seconds=-10, ...): the lifecycle
    error must surface, not a timestamp error -- the illegal call should
    be rejected on its own terms regardless of what else is wrong with
    the call."""
    w = _at_completed()
    with pytest.raises(InvalidStateTransitionError):
        w.record_activity(at_seconds=-10.0, fingerprint="f", succeeded=True)


# --- pre-start / post-complete zero-artifact cross-tests --------------------


def test_checkpoint_before_start_creates_no_durable_artifact(tmp_path) -> None:
    w = ExecutionWatchdog("e1", _rd6_policy(), state_dir=str(tmp_path))
    with pytest.raises(InvalidStateTransitionError):
        w.checkpoint(at_seconds=1.0, phase="p0")
    assert list(tmp_path.iterdir()) == []


def test_post_completion_events_do_not_change_durable_heartbeat(tmp_path) -> None:
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    w = ExecutionWatchdog("e1", _rd6_policy(), state_dir=str(tmp_path))
    w.start(task="t", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    w.complete(at_seconds=2.0, evidence=("done",))
    before = HeartbeatWriter(str(tmp_path)).read("e1")

    for op in _ORDINARY_OPERATIONS:
        with pytest.raises(InvalidStateTransitionError):
            _run_ordinary_event(w, op, at_seconds=3.0)

    after = HeartbeatWriter(str(tmp_path)).read("e1")
    assert before == after


# --- active-state clock semantics unaffected by the new admission guard ----


def test_active_state_clock_ordering_still_enforced() -> None:
    from agentgear.exceptions import InvalidObservationError

    w = _at_running()
    w.record_activity(at_seconds=5.0, fingerprint="f", succeeded=True)
    with pytest.raises(InvalidObservationError):
        w.record_activity(at_seconds=4.0, fingerprint="g", succeeded=True)


# --- AG6-03: existing-file state_dir --------------------------------------


def test_existing_regular_file_state_dir_rejected_at_construction(tmp_path) -> None:
    state_path = tmp_path / "state"
    state_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog("e1", _rd6_policy(), state_dir=str(state_path))


# --- AG6-02/AG6-03 path-security test matrix (section 8, A-J) --------------


def _make_junction(link: str, target: str) -> None:
    import subprocess

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.skip(f"could not create junction on this system: {result.stderr or result.stdout}")


def _hb():
    from agentgear.watchdog.heartbeat import build_heartbeat

    return build_heartbeat(
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


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_heartbeat_root_junction_swap_after_construction_is_rejected(tmp_path) -> None:
    """Matrix E: an EXISTING root, swapped for a junction pointing
    outside AFTER construction, must be rejected before any artifact
    (lock, JSON) is created."""
    import shutil

    from agentgear.exceptions import PathEscapeError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()

    writer = HeartbeatWriter(str(state_dir))
    shutil.rmtree(state_dir)
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        writer.write(_hb())
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_heartbeat_absent_root_replaced_by_junction_before_first_write_is_rejected(
    tmp_path,
) -> None:
    """Matrix F: the root did NOT exist at construction -- an attacker
    creating it as a junction before the first write must still be
    rejected. This is the case that distinguishes a sound root-identity
    design from one that only protects roots that already existed."""
    from agentgear.exceptions import PathEscapeError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()

    writer = HeartbeatWriter(str(state_dir))  # state_dir does not exist yet
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        writer.write(_hb())
    assert list(outside.iterdir()) == []


def test_heartbeat_absent_root_normal_creation_still_works(tmp_path) -> None:
    """Matrix B/section 4.12: the non-attack case for the SAME
    nonexistent-root code path -- a legitimate directory created at the
    lexical target must be accepted, not falsely rejected."""
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "a" / "b" / "state"
    writer = HeartbeatWriter(str(state_dir))
    writer.write(_hb())  # must not raise
    assert state_dir.is_dir()


def test_heartbeat_root_becomes_regular_file_after_construction_is_rejected(tmp_path) -> None:
    """Matrix G / section 5.5: the root was absent at construction; before
    the first operation, something creates a REGULAR FILE there instead
    of a directory. Must be caught on this later operation, not only at
    construction time."""
    from agentgear.exceptions import InvalidPersistenceRootError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "state"
    writer = HeartbeatWriter(str(state_dir))  # absent at construction
    state_dir.write_text("surprise, a file", encoding="utf-8")

    with pytest.raises(InvalidPersistenceRootError):
        writer.write(_hb())


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_checkpoint_root_junction_swap_after_construction_is_rejected(tmp_path) -> None:
    """Matrix E for CheckpointStore: the execution lock must not be
    created outside either (section 10/11)."""
    import shutil

    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import PathEscapeError
    from agentgear.models import Checkpoint

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()

    store = CheckpointStore(str(state_dir))
    shutil.rmtree(state_dir)
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        store.append(Checkpoint(execution_id="e1", phase="p0", at_seconds=0.0))
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_checkpoint_absent_root_replaced_by_junction_before_first_write_is_rejected(
    tmp_path,
) -> None:
    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import PathEscapeError
    from agentgear.models import Checkpoint

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()

    store = CheckpointStore(str(state_dir))
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        store.append(Checkpoint(execution_id="e1", phase="p0", at_seconds=0.0))
    assert list(outside.iterdir()) == []


def test_checkpoint_root_becomes_regular_file_after_construction_is_rejected(tmp_path) -> None:
    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import InvalidPersistenceRootError
    from agentgear.models import Checkpoint

    state_dir = tmp_path / "state"
    store = CheckpointStore(str(state_dir))
    state_dir.write_text("surprise, a file", encoding="utf-8")

    with pytest.raises(InvalidPersistenceRootError):
        store.append(Checkpoint(execution_id="e1", phase="p0", at_seconds=0.0))


def test_checkpoint_absent_root_normal_creation_still_works(tmp_path) -> None:
    from agentgear.checkpoints import CheckpointStore
    from agentgear.models import Checkpoint

    state_dir = tmp_path / "a" / "b" / "state"
    store = CheckpointStore(str(state_dir))
    store.append(Checkpoint(execution_id="e1", phase="p0", at_seconds=0.0))
    assert store.all("e1")[0].phase == "p0"


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_watchdog_end_to_end_root_junction_swap_is_rejected_without_outside_artifacts(
    tmp_path,
) -> None:
    """Section 4.11: through the public ExecutionWatchdog, not just the
    low-level stores directly."""
    import shutil

    from agentgear.exceptions import PathEscapeError

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()

    w = ExecutionWatchdog("e1", _rd6_policy(), state_dir=str(state_dir))
    shutil.rmtree(state_dir)
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        w.start(task="t", at_seconds=0.0)
    assert list(outside.iterdir()) == []


def test_traversal_path_component_is_rejected(tmp_path) -> None:
    """Matrix I: a lexically-traversing execution_id-derived path must
    still be rejected. execution_id itself is already rejected by
    validate_persistence_safe_id (illegal filename characters include
    '/' and '\\\\'), so this proves that boundary independently of the
    root-identity guard added this round."""
    from agentgear.exceptions import InvalidIdentifierError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    writer = HeartbeatWriter(str(tmp_path))
    with pytest.raises(InvalidIdentifierError):
        writer.read("../../escape")


def test_heartbeat_existing_root_replaced_by_regular_file_is_rejected(tmp_path) -> None:
    """Section 28.I: an EXISTING directory root, later deleted and
    replaced by a regular file, must be rejected -- distinct from the
    absent-root-becomes-a-file case already covered above."""
    import shutil

    from agentgear.exceptions import InvalidPersistenceRootError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    writer = HeartbeatWriter(str(state_dir))
    shutil.rmtree(state_dir)
    state_dir.write_text("now a file", encoding="utf-8")

    with pytest.raises(InvalidPersistenceRootError):
        writer.write(_hb())


def test_direct_heartbeat_writer_existing_file_root_rejected_at_construction(tmp_path) -> None:
    """Section 28.P: HeartbeatWriter constructed directly (not through
    ExecutionWatchdog) against an existing regular file must reject it
    immediately, using the same shared root guard."""
    from agentgear.exceptions import InvalidPersistenceRootError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    file_path = tmp_path / "state"
    file_path.write_text("a file", encoding="utf-8")
    with pytest.raises(InvalidPersistenceRootError):
        HeartbeatWriter(str(file_path))


def test_direct_checkpoint_store_existing_file_root_rejected_at_construction(tmp_path) -> None:
    """Section 28.Q: same as above, for CheckpointStore."""
    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import InvalidPersistenceRootError

    file_path = tmp_path / "state"
    file_path.write_text("a file", encoding="utf-8")
    with pytest.raises(InvalidPersistenceRootError):
        CheckpointStore(str(file_path))


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_corrupt_heartbeat_plus_malicious_root_swap_quarantine_does_not_escape(tmp_path) -> None:
    """Section 28.L: a heartbeat file corrupt enough to trigger
    quarantine, combined with the root ALSO having been swapped for a
    junction, must still reject before quarantine's own rename -- the
    root-identity check runs before ANY artifact touch, quarantine
    included."""
    import shutil

    from agentgear.exceptions import PathEscapeError
    from agentgear.watchdog.heartbeat import HeartbeatWriter

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()

    writer = HeartbeatWriter(str(state_dir))
    (state_dir / "e1.heartbeat.json").write_text("{not valid json", encoding="utf-8")

    shutil.rmtree(state_dir)
    _make_junction(str(state_dir), str(outside))

    with pytest.raises(PathEscapeError):
        writer.read("e1")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="junctions are Windows-only")
def test_nested_junction_two_levels_deep_is_rejected(tmp_path) -> None:
    """Matrix J: a junction planted two directory levels below the
    configured root (not the root itself) must still be caught by the
    existing CHILD containment check -- proving root-identity pinning
    (this round) and child containment (pre-existing) compose correctly
    rather than one accidentally superseding the other."""
    from agentgear.checkpoints import CheckpointStore
    from agentgear.exceptions import PathEscapeError
    from agentgear.models import Checkpoint

    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()

    store = CheckpointStore(str(state_dir))
    store.append(Checkpoint(execution_id="e1", phase="p0", at_seconds=0.0))
    seg_dir = state_dir / "e1.checkpoints"
    import shutil

    shutil.rmtree(seg_dir)
    _make_junction(str(seg_dir), str(outside))

    with pytest.raises(PathEscapeError):
        store.append(Checkpoint(execution_id="e1", phase="p1", at_seconds=1.0))
    assert list(outside.iterdir()) == []
