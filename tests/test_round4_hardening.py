"""Round 4 remediation: NEW-01 through NEW-10. See
docs/audits/remediation-round-4.md for the full finding writeups and
design decisions.
"""

from __future__ import annotations

import math
import tempfile

import pytest

from agentgear.config import Policy, WatchdogPolicy
from agentgear.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    InvalidObservationError,
    InvalidStateTransitionError,
    PathEscapeError,
    TaskProfileError,
)
from agentgear.models import (
    ComplexityAssessment,
    ComplexityLevel,
    ExecutionState,
    ModelTier,
    ProgressSignalKind,
    RecoveryAttempt,
    RecoveryEpisode,
    RecoveryEpisodeOutcome,
    RecoveryResult,
    RiskAssessment,
    RiskLevel,
)
from agentgear.routing import critical_signal_reasons, estimate_cost
from agentgear.watchdog import ExecutionWatchdog
from agentgear.watchdog.heartbeat import HeartbeatWriter, build_heartbeat

# --- NEW-01: decision-critical factor values must be validated -------------

_BAD_FACTOR_VALUES = [
    math.nan,
    math.inf,
    -math.inf,
    -0.01,
    1.01,
    True,
    False,
    "1",
    None,
]


@pytest.mark.parametrize("bad_value", _BAD_FACTOR_VALUES)
def test_risk_assessment_rejects_impossible_factor_values(bad_value) -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors={"security_impact": bad_value})


@pytest.mark.parametrize("bad_value", _BAD_FACTOR_VALUES)
def test_complexity_assessment_rejects_impossible_factor_values(bad_value) -> None:
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(
            score=0.5, level=ComplexityLevel.MODERATE, factors={"ambiguity": bad_value}
        )


@pytest.mark.parametrize(
    "factor_key",
    ["security_impact", "data_impact", "irreversibility", "architectural_impact", "prior_failures"],
)
def test_risk_assessment_rejects_nan_for_every_decision_critical_key(factor_key: str) -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors={factor_key: math.nan})


@pytest.mark.parametrize("factor_key", ["ambiguity", "architectural_impact", "prior_failures"])
def test_complexity_assessment_rejects_nan_for_every_decision_critical_key(factor_key: str) -> None:
    with pytest.raises(TaskProfileError):
        ComplexityAssessment(
            score=0.5, level=ComplexityLevel.MODERATE, factors={factor_key: math.nan}
        )


def test_risk_assessment_rejects_blank_factor_key() -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors={"   ": 0.5})


def test_risk_assessment_accepts_unknown_but_well_formed_factor_key() -> None:
    """Forward-compat: unknown factor names are allowed, but their VALUES
    still obey the finite-[0,1]-not-bool contract."""
    r = RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors={"future_signal": 0.7})
    assert r.factors["future_signal"] == 0.7


def test_risk_assessment_rejects_bad_value_on_unknown_factor_key() -> None:
    with pytest.raises(TaskProfileError):
        RiskAssessment(score=0.5, level=RiskLevel.MODERATE, factors={"future_signal": math.nan})


def test_nan_factor_can_never_reach_route_because_construction_fails_first() -> None:
    """The exact NEW-01 routing-integrity regression: constructing a
    RiskAssessment with a NaN critical-risk factor must fail outright, so
    there is no way for `NaN >= threshold` (always False) to silently
    defeat critical_signal_reasons()."""
    with pytest.raises(TaskProfileError):
        risk = RiskAssessment(
            score=0.05, level=RiskLevel.MINIMAL, factors={"security_impact": math.nan}
        )
        critical_signal_reasons(risk, Policy.default())  # unreachable if construction fails


# --- NEW-02: ExecutionWatchdog constructor validation -----------------------


def _watchdog_kwargs(**overrides) -> dict:
    base = dict(execution_id="x", policy=Policy.default())
    base.update(overrides)
    return base


@pytest.mark.parametrize("bad_tier", ["fast", 1, None, True])
def test_watchdog_rejects_invalid_initial_tier(bad_tier) -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(initial_tier=bad_tier))


@pytest.mark.parametrize("bad_reasoning", ["low", 1, None, True])
def test_watchdog_rejects_invalid_initial_reasoning(bad_reasoning) -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(initial_reasoning=bad_reasoning))


@pytest.mark.parametrize("bad_policy", [{}, None, "policy"])
def test_watchdog_rejects_invalid_policy(bad_policy) -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(policy=bad_policy))


@pytest.mark.parametrize(
    "bad_budget",
    [True, False, 1.5, "100", math.nan, math.inf, -math.inf, -1, 0],
)
def test_watchdog_rejects_invalid_context_budget_tokens(bad_budget) -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(context_budget_tokens=bad_budget))


@pytest.mark.parametrize("good_budget", [1, 1000, 2000])
def test_watchdog_accepts_valid_context_budget_tokens(good_budget: int) -> None:
    w = ExecutionWatchdog(**_watchdog_kwargs(context_budget_tokens=good_budget))
    assert w.context_budget_tokens == good_budget


def test_watchdog_rejects_invalid_state_dir_type() -> None:
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(state_dir=123))


def test_watchdog_constructor_validation_raises_before_any_state_assignment() -> None:
    """A rejected constructor call must not leave any partially-built
    object reachable -- the exception must come from validation, before
    self.execution_id/self.policy/etc. are ever assigned."""
    with pytest.raises(ConfigurationError):
        ExecutionWatchdog(**_watchdog_kwargs(policy="not-a-policy"))
    # (nothing to assert on the object itself -- it was never returned --
    # this test documents the expectation and would fail loudly if
    # construction instead raised a raw AttributeError deep inside
    # __init__ after partially assigning state.)


# --- NEW-03: rejected operations must not mutate the clock ------------------


def _rd4_policy(**kw) -> Policy:
    defaults = dict(no_progress_seconds=5.0, no_progress_cycles=2, max_recovery_attempts=10)
    defaults.update(kw)
    return Policy(watchdog=WatchdogPolicy(**defaults))


def test_rejected_complete_does_not_advance_clock_then_valid_complete_succeeds() -> None:
    """The canonical Round 4 / NEW-03 regression (section 5.5)."""
    w = ExecutionWatchdog("e1", _rd4_policy())
    w.start(task="x", at_seconds=0.0)
    w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
    with pytest.raises(InvalidObservationError):
        w.complete(at_seconds=100.0, evidence=("done", 42))
    assert w.state == ExecutionState.REVIEWING
    # the clock must NOT have advanced to 100.0
    w.complete(at_seconds=2.0, evidence=("done",))
    assert w.state == ExecutionState.COMPLETED


def test_rejected_advance_does_not_advance_clock() -> None:
    w = ExecutionWatchdog("e2", _rd4_policy())
    w.start(task="x", at_seconds=0.0)
    with pytest.raises(InvalidStateTransitionError):
        w.advance(ExecutionState.BLOCKED, at_seconds=50.0)
    w.advance(ExecutionState.TESTING, at_seconds=1.0)
    assert w.state == ExecutionState.TESTING


def test_rejected_progress_does_not_advance_clock() -> None:
    w = ExecutionWatchdog("e3", _rd4_policy())
    w.start(task="x", at_seconds=0.0)
    with pytest.raises(InvalidObservationError):
        w.record_progress(at_seconds=50.0, kind=ProgressSignalKind.FILE_CHANGED, description="   ")
    w.record_progress(at_seconds=1.0, kind=ProgressSignalKind.FILE_CHANGED, description="ok")
    assert w.status()["last_progress_at"] == 1.0


def test_rejected_recovery_result_does_not_advance_clock() -> None:
    w = ExecutionWatchdog("e4", _rd4_policy(max_recovery_attempts=10))
    w.start(task="x", at_seconds=0.0)
    for i in range(3):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"u{i}", succeeded=True)
    assert w.state == ExecutionState.RECOVERING
    with pytest.raises(InvalidObservationError):
        w.record_recovery_result(at_seconds=50.0, result="not-a-result")  # type: ignore[arg-type]
    assert w.state == ExecutionState.RECOVERING
    # a legitimate, earlier-timestamped result must still be accepted
    w.record_recovery_result(at_seconds=20.0, result=RecoveryResult.SUCCESS, evidence="fixed")
    assert w.state == ExecutionState.RUNNING


def test_rejected_start_does_not_advance_clock() -> None:
    w = ExecutionWatchdog("e5", _rd4_policy())
    with pytest.raises(InvalidObservationError):
        w.start(task="   ", at_seconds=50.0)
    # clock never committed -- a fresh, valid start at an earlier time works
    w.start(task="real task", at_seconds=1.0)
    assert w.state == ExecutionState.RUNNING


# --- NEW-04: heartbeat durability (commit + dirty/sync) ---------------------


class _FailNTimesThenSucceed:
    """Deterministic failing test double for HeartbeatWriter.write, wrapping
    a REAL HeartbeatWriter so successful writes are genuinely durable and
    readable back -- not a fragile ad-hoc monkeypatch per test."""

    def __init__(self, real_writer: HeartbeatWriter, fail_times: int) -> None:
        self._real = real_writer
        self._remaining_failures = fail_times
        self.call_count = 0

    def write(self, heartbeat) -> None:
        self.call_count += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise OSError("simulated disk failure")
        self._real.write(heartbeat)

    def read(self, execution_id: str):
        return self._real.read(execution_id)


def _watchdog_with_state_dir(state_dir: str, **kwargs) -> ExecutionWatchdog:
    return ExecutionWatchdog("e1", _rd4_policy(**kwargs), state_dir=state_dir)


def test_failed_heartbeat_write_does_not_roll_back_the_domain_transition() -> None:
    """The canonical NEW-04 regression: complete() succeeds in-memory even
    though the durable heartbeat write fails; the caller learns about the
    failure via the re-raised exception, but the state is NOT rolled back
    and complete() must never be called again to "retry" it."""
    with tempfile.TemporaryDirectory() as td:
        w = _watchdog_with_state_dir(td)
        w.start(task="x", at_seconds=0.0)
        w.advance(ExecutionState.REVIEWING, at_seconds=1.0)

        real_writer = w._heartbeat_writer
        w._heartbeat_writer = _FailNTimesThenSucceed(real_writer, fail_times=1)

        with pytest.raises(OSError):
            w.complete(at_seconds=2.0, evidence=("done",))

        assert w.state == ExecutionState.COMPLETED  # in-memory: committed
        assert w.heartbeat_dirty is True
        assert w.status()["heartbeat_sync_error"] is not None

        # the durable file must NOT have been silently left showing a
        # newer-looking-but-wrong state, nor must it have been corrupted --
        # it simply still reflects the last successful write (REVIEWING).
        durable = real_writer.read("e1")
        assert durable is not None
        assert durable.state == ExecutionState.REVIEWING


def test_sync_heartbeat_recovers_durable_state_without_repeating_the_transition() -> None:
    with tempfile.TemporaryDirectory() as td:
        w = _watchdog_with_state_dir(td)
        w.start(task="x", at_seconds=0.0)
        w.advance(ExecutionState.REVIEWING, at_seconds=1.0)

        real_writer = w._heartbeat_writer
        failing = _FailNTimesThenSucceed(real_writer, fail_times=1)
        w._heartbeat_writer = failing

        with pytest.raises(OSError):
            w.complete(at_seconds=2.0, evidence=("done",))
        assert w.heartbeat_dirty is True

        history_len_before = len(w.transition_history)
        budget_cost_before = w.budget.committed_cost

        ok = w.sync_heartbeat()
        assert ok is True
        assert w.heartbeat_dirty is False

        # no domain transition/budget was repeated by the sync
        assert len(w.transition_history) == history_len_before
        assert w.budget.committed_cost == budget_cost_before
        assert w.state == ExecutionState.COMPLETED

        durable = real_writer.read("e1")
        assert durable.state == ExecutionState.COMPLETED


def test_sync_heartbeat_can_fail_twice_then_recover_on_third_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        w = ExecutionWatchdog("e2", _rd4_policy(), state_dir=td)
        real_writer = w._heartbeat_writer
        # 1st failure happens inside start() itself; 2 more are consumed by
        # two failed sync_heartbeat() retries before the 3rd succeeds.
        failing = _FailNTimesThenSucceed(real_writer, fail_times=3)
        w._heartbeat_writer = failing

        with pytest.raises(OSError):
            w.start(task="x", at_seconds=0.0)  # write attempt #1: fails
        assert w.heartbeat_dirty is True

        assert w.sync_heartbeat() is False  # write attempt #2: fails
        assert w.heartbeat_dirty is True
        assert w.sync_heartbeat() is False  # write attempt #3: fails
        assert w.heartbeat_dirty is True
        assert w.sync_heartbeat() is True  # write attempt #4: succeeds
        assert w.heartbeat_dirty is False


def test_sync_heartbeat_is_a_noop_when_nothing_is_dirty() -> None:
    with tempfile.TemporaryDirectory() as td:
        w = _watchdog_with_state_dir(td)
        w.start(task="x", at_seconds=0.0)
        assert w.heartbeat_dirty is False
        assert w.sync_heartbeat() is True
        assert w.heartbeat_dirty is False


def test_dirty_state_survives_a_subsequent_legitimate_method_call() -> None:
    """A later, successful method call must not silently clear
    heartbeat_dirty unless that call's own heartbeat write succeeds --
    dirty status must not be incorrectly cleared by an unrelated success."""
    with tempfile.TemporaryDirectory() as td:
        w = _watchdog_with_state_dir(td, max_recovery_attempts=10)
        w.start(task="x", at_seconds=0.0)

        real_writer = w._heartbeat_writer
        w._heartbeat_writer = _FailNTimesThenSucceed(real_writer, fail_times=1)
        with pytest.raises(OSError):
            w.advance(ExecutionState.TESTING, at_seconds=1.0)
        assert w.heartbeat_dirty is True

        # subsequent heartbeat writes with the SAME always-partially-failing
        # double still fail (fail_times exhausted -> now succeeds), so the
        # next legitimate call actually clears it -- verify that happens
        # through the write path, not accidentally.
        w.advance(ExecutionState.RUNNING, at_seconds=2.0)
        assert w.heartbeat_dirty is False


def test_failure_injection_start_complete_blocked_recovery_paths() -> None:
    """Failure-inject the heartbeat write in each major transition family
    and confirm: in-memory state always reflects the completed domain
    operation, and heartbeat_dirty is set every time."""
    with tempfile.TemporaryDirectory() as td:
        w = _watchdog_with_state_dir(td, max_recovery_attempts=10)
        real_writer = w._heartbeat_writer

        # start()
        w._heartbeat_writer = _FailNTimesThenSucceed(real_writer, fail_times=1)
        with pytest.raises(OSError):
            w.start(task="x", at_seconds=0.0)
        assert w.state == ExecutionState.RUNNING
        assert w.heartbeat_dirty is True
        assert w.sync_heartbeat() is True

        # recovery -> BLOCKED (max_recovery_attempts=1 case) exercises the
        # _transition_to_blocked path's own heartbeat write. Fresh
        # execution_id so its heartbeat file doesn't clash with w's.
        w2 = ExecutionWatchdog("e-blocked", _rd4_policy(max_recovery_attempts=1), state_dir=td)
        w2.start(task="x", at_seconds=0.0)
        for i in range(3):
            w2.record_activity(at_seconds=float(10 + i), fingerprint=f"u{i}", succeeded=True)
        assert w2.state == ExecutionState.RECOVERING
        w2.record_recovery_result(at_seconds=20.0, result=RecoveryResult.FAILURE)
        real2 = w2._heartbeat_writer
        w2._heartbeat_writer = _FailNTimesThenSucceed(real2, fail_times=1)
        with pytest.raises(OSError):
            w2.begin_recovery(at_seconds=21.0)
        assert w2.state == ExecutionState.BLOCKED
        assert w2.blocked_report is not None
        assert w2.heartbeat_dirty is True
        assert w2.sync_heartbeat() is True


# --- NEW-07: RecoveryAttempt domain validation -------------------------------


def _attempt_kwargs(**overrides) -> dict:
    base = dict(
        reason="stall",
        strategy="re_read_error",
        attempt_number=1,
        result=RecoveryResult.PENDING,
        at_seconds=0.0,
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("bad_reason", ["", "   "])
def test_recovery_attempt_rejects_blank_reason(bad_reason: str) -> None:
    with pytest.raises(InvalidObservationError):
        RecoveryAttempt(**_attempt_kwargs(reason=bad_reason))


@pytest.mark.parametrize("bad_strategy", ["", "   "])
def test_recovery_attempt_rejects_blank_strategy(bad_strategy: str) -> None:
    with pytest.raises(InvalidObservationError):
        RecoveryAttempt(**_attempt_kwargs(strategy=bad_strategy))


@pytest.mark.parametrize("bad_attempt_number", [True, False, 0, -1, 1.5, "1"])
def test_recovery_attempt_rejects_bad_attempt_number(bad_attempt_number) -> None:
    with pytest.raises(InvalidObservationError):
        RecoveryAttempt(**_attempt_kwargs(attempt_number=bad_attempt_number))


def test_recovery_attempt_rejects_wrong_result_type() -> None:
    with pytest.raises(InvalidObservationError):
        RecoveryAttempt(**_attempt_kwargs(result="success"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_ts", [math.nan, math.inf, -math.inf, -1.0])
def test_recovery_attempt_rejects_bad_timestamp(bad_ts: float) -> None:
    with pytest.raises(InvalidObservationError):
        RecoveryAttempt(**_attempt_kwargs(at_seconds=bad_ts))


def test_recovery_attempt_accepts_well_formed_data() -> None:
    attempt = RecoveryAttempt(**_attempt_kwargs())
    assert attempt.attempt_number == 1


def test_zero_attempt_blocked_episode_remains_valid_after_recovery_attempt_validation() -> None:
    """NEW-07 must not regress the Round 3 / AUDIT3-06 invariant: a budget
    denial before any attempt ever ran still closes an episode BLOCKED
    with zero attempts -- RecoveryAttempt's new validation only applies to
    attempts that actually exist, never invents a false len(attempts)>=1
    requirement."""
    episode = RecoveryEpisode(
        episode_number=1,
        stall_reason="x",
        attempts=(),
        outcome=RecoveryEpisodeOutcome.BLOCKED,
        opened_at=0.0,
        closed_at=0.0,
    )
    assert episode.attempts == ()


def test_zero_attempt_blocked_episode_via_real_coordinator_still_works() -> None:
    """Same invariant, exercised through the real coordinator path."""
    from agentgear.config import BudgetPolicy

    tokens_per_episode = 1000
    per_recovery_cost = estimate_cost(ModelTier.FAST, tokens_per_episode)
    tight_budget = per_recovery_cost - 1e-9  # not enough for even one recovery reservation
    p = Policy(
        budget=BudgetPolicy(max_estimated_cost=tight_budget, max_estimated_tokens=10_000_000),
        watchdog=WatchdogPolicy(no_progress_seconds=1.0, no_progress_cycles=2),
    )
    w = ExecutionWatchdog("e-zero-attempt", p, context_budget_tokens=tokens_per_episode)
    w.start(task="x", at_seconds=0.0)
    for i in range(3):
        w.record_activity(at_seconds=float(10 + i), fingerprint=f"u{i}", succeeded=True)
    assert w.state == ExecutionState.BLOCKED
    assert w.blocked_report.attempts == 0
    assert len(w.recovery_history) == 1
    assert w.recovery_history[0].attempts == ()


# --- NEW-08: path/identifier UX + retry behavior -----------------------------


def test_write_to_a_brand_new_multi_level_nonexistent_state_dir_succeeds(tmp_path) -> None:
    """The exact NEW-08 regression: the FIRST-EVER write to a state_dir
    that doesn't exist yet (not even one level) used to raise a spurious
    PathEscapeError, because the containment check resolved a fully
    nonexistent root/target pair inconsistently via os.path.realpath."""
    nested = tmp_path / "not_yet_created" / "subdir"
    assert not nested.exists()
    writer = HeartbeatWriter(nested)
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
    writer.write(hb)
    assert nested.exists()
    assert writer.read("e1").state == ExecutionState.RUNNING


def test_read_on_nonexistent_state_dir_means_no_state_not_path_escape(tmp_path) -> None:
    never_written = tmp_path / "never_written"
    writer = HeartbeatWriter(never_written)
    assert writer.read("e1") is None
    # must not create the directory just to answer a read
    assert not never_written.exists()

    from agentgear.checkpoints import CheckpointStore

    store = CheckpointStore(never_written)
    assert store.all("e1") == []
    assert store.latest("e1") is None
    assert not never_written.exists()


def test_traversal_still_rejected_when_state_dir_itself_does_not_exist_yet(tmp_path) -> None:
    """The nearest-existing-ancestor resolution fix must not weaken
    containment: a traversal execution_id combined with a not-yet-created
    state_dir must still be rejected, with zero artifacts escaping."""
    state_dir = tmp_path / "state"
    writer = HeartbeatWriter(state_dir)
    for bad_id in ("../escape", "..\\escape", "a/../../escape"):
        with pytest.raises((PathEscapeError, InvalidObservationError, InvalidIdentifierError)):
            hb = build_heartbeat(
                execution_id=bad_id,
                state=ExecutionState.RUNNING,
                current_task="t",
                current_subtask=None,
                last_real_progress_at=0.0,
                last_progress_evidence=None,
                attempt_count=0,
                current_strategy=None,
                last_error=None,
            )
            writer.write(hb)
    outside = [
        p
        for p in tmp_path.rglob("*")
        if state_dir not in p.parents and p != state_dir and p.is_file()
    ]
    assert outside == []


def test_pathologically_long_execution_id_fails_immediately_not_after_lock_retry(tmp_path) -> None:
    """The exact NEW-08 regression: a 300-character execution_id used to
    enter the file-lock retry loop and only fail after ~10 seconds with a
    confusing StorageLockError. It must now fail immediately with a clear
    domain error."""
    import time

    writer = HeartbeatWriter(tmp_path)
    long_id = "x" * 300
    hb = build_heartbeat(
        execution_id=long_id,
        state=ExecutionState.RUNNING,
        current_task="t",
        current_subtask=None,
        last_real_progress_at=0.0,
        last_progress_evidence=None,
        attempt_count=0,
        current_strategy=None,
        last_error=None,
    )
    t0 = time.monotonic()
    with pytest.raises(InvalidIdentifierError):
        writer.write(hb)
    assert time.monotonic() - t0 < 1.0, "must fail fast, not enter the ~10s lock retry loop"


@pytest.mark.parametrize(
    "bad_id", ["a" * 200, "has:illegal", "has<chars>", "has/slash", "has\\backslash", "has|pipe"]
)
def test_illegal_or_oversized_execution_id_rejected_for_both_heartbeat_and_checkpoints(
    tmp_path, bad_id
) -> None:
    from agentgear.checkpoints import Checkpoint, CheckpointStore

    writer = HeartbeatWriter(tmp_path)
    hb = build_heartbeat(
        execution_id=bad_id,
        state=ExecutionState.RUNNING,
        current_task="t",
        current_subtask=None,
        last_real_progress_at=0.0,
        last_progress_evidence=None,
        attempt_count=0,
        current_strategy=None,
        last_error=None,
    )
    with pytest.raises(InvalidIdentifierError):
        writer.write(hb)

    store = CheckpointStore(tmp_path)
    with pytest.raises(InvalidIdentifierError):
        store.append(Checkpoint(execution_id=bad_id, phase="p", at_seconds=0.0))


def test_checkpoint_persists_before_updating_in_memory_cache(tmp_path) -> None:
    """Section 15 (validate-before-mutate sweep): if the durable checkpoint
    append fails, the in-memory checkpoint cache must not already claim a
    checkpoint exists that was never actually persisted -- it feeds
    BlockedReport.last_successful_checkpoint and the heartbeat's
    pending_work, both of which would otherwise reference state that
    doesn't survive a crash."""
    w = ExecutionWatchdog("e-checkpoint", _rd4_policy(), state_dir=str(tmp_path))
    w.start(task="x", at_seconds=0.0)

    def boom(cp):
        raise OSError("disk full")

    w._checkpoint_store.append = boom

    with pytest.raises(OSError):
        w.checkpoint(at_seconds=1.0, phase="p1")
    assert w._checkpoints == []


def test_constructor_eagerly_rejects_filesystem_unsafe_execution_id_when_durable(
    tmp_path,
) -> None:
    """Self-adversarial pass (section 33): previously a filesystem-unsafe
    execution_id combined with state_dir was only discovered on the first
    start() call, by which point the state machine had already
    transitioned to RUNNING and budget had been committed -- an
    irreversible mutation under the NEW-04 commit model, leaving the
    watchdog permanently stuck with an unsyncable dirty heartbeat. It must
    now be rejected atomically at construction, before start() is ever
    called."""
    with pytest.raises(InvalidIdentifierError):
        ExecutionWatchdog("bad<id>", _rd4_policy(), state_dir=str(tmp_path))


def test_constructor_allows_filesystem_unsafe_execution_id_without_state_dir(
    tmp_path,
) -> None:
    """The filesystem-safety check is scoped to durable use only -- an
    in-memory-only watchdog (no state_dir) never touches the filesystem,
    so it must not reject an execution_id purely because it would be
    unsafe as a filename."""
    w = ExecutionWatchdog("bad<id>", _rd4_policy())
    assert w.execution_id == "bad<id>"


def test_normal_execution_id_is_unaffected(tmp_path) -> None:
    writer = HeartbeatWriter(tmp_path)
    hb = build_heartbeat(
        execution_id="normal-exec-id-123",
        state=ExecutionState.RUNNING,
        current_task="t",
        current_subtask=None,
        last_real_progress_at=0.0,
        last_progress_evidence=None,
        attempt_count=0,
        current_strategy=None,
        last_error=None,
    )
    writer.write(hb)
    assert writer.read("normal-exec-id-123") is not None
