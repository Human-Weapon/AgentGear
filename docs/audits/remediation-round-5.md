# AgentGear Remediation Round 5

Fifth independent adversarial audit against baseline `f8573ef4f0614ae84989609ed1c293d2da10c595`
(Remediation Round 4). Verdict: **C — FIX BEFORE PROMOTING** (0 P0, 0 P1, 6 P2, 5 P3, 0 P4).
See `docs/audits/index.md` for the cross-round traceability index.

## AG5-01 — checkpoint segment capacity race

**Finding.** `CheckpointStore.append()` picked its target segment via an UNLOCKED peek
read, then relied only on the per-segment `SafeJsonStore.update()` lock for the actual
append. Several processes could independently observe "segment 1 has 99 entries, still
room" and all decide to append there, overshooting `_SEGMENT_CAPACITY` -- the module's own
docstring called this "harmless" (documented as a race, never fixed).

**Reproduction.** Preload 99 checkpoints, spawn 5 real processes releasing simultaneously
via a barrier, each appending once: `segment-000001.json` ended up with **104** entries.

**Broken invariant.** "A segment never holds more than `_SEGMENT_CAPACITY` acknowledged
checkpoints" -- documented as a hard bound elsewhere, but never actually enforced under
concurrency.

**Root cause.** The capacity check-then-append sequence (peek segment size, decide
target, append) was not atomic with respect to other appenders for the same execution.

**Design decision.** Introduce ONE execution-scoped `FileLock`
(`{execution_id}.checkpoints/.execution.lock`), reusing the existing lock primitive rather
than inventing a second one, held around the WHOLE select-target/recheck/rollover/append
sequence. Lock order is always execution lock first, then the per-segment `SafeJsonStore`
lock second (acquired inside `store.update()`) -- no other code path acquires a segment
lock without first holding the execution lock, so there is no reverse-order acquisition
anywhere and therefore no deadlock potential. Scope is per-execution (one lock per
`execution_id`), not a single global lock across every AgentGear execution.

**Alternatives considered.** A database or a generic distributed-lock library was
rejected as disproportionate -- the existing `FileLock` primitive already provides
exactly what's needed (cross-process, cross-platform, path-contained).

**Fix.** `src/agentgear/checkpoints.py::CheckpointStore._execution_lock`, wired into
`append()`.

**Verification.**
- 99+5, 99+10, 199+2, 199+5, 199+10: `max_seg <= 100` and `total_stored == preload +
  acknowledged` in every case, using real `multiprocessing.Process` workers released via a
  `multiprocessing.Barrier`.
- Structural bound preserved: a single append only ever reads/rewrites its ONE target
  segment file (proven by asserting an untouched segment's mtime is unchanged after an
  append that rolls over to the next segment).
- Path security: a post-construction junction swap of the execution's segment directory
  is still rejected with `PathEscapeError` and zero artifacts outside `state_dir`.
- Sequential (non-concurrent) behavior unchanged: 250 sequential appends still land as
  `[100, 100, 50]`.

**Regression.** `tests/test_persistence_concurrency.py::
test_checkpoint_segment_capacity_is_a_hard_cap_under_real_concurrency` (5 parametrized
concurrency scenarios), `tests/test_checkpoints.py::
test_sequential_appends_never_exceed_segment_capacity`,
`test_no_single_append_rewrites_more_than_one_segment`,
`test_execution_lock_path_rejects_post_construction_junction_swap`.

---

## AG5-02 — `record_recovery_result()` mutated state before validating its own inputs

**Finding.** `result=RecoveryResult.SUCCESS, evidence=42` raised `InvalidObservationError`
-- but only from deep inside a follow-on `ProgressEvent` construction, by which point
`self._recovery_attempts[-1]` had already been overwritten, the recovery episode already
closed into `_recovery_history`, and the state machine already transitioned RECOVERING ->
RUNNING. `RecoveryResult.PENDING` (meant only to represent an in-progress attempt) was
also silently accepted as a "resolution", routed into the FAILURE branch.

**Valid result domain.** `record_recovery_result()` resolves an attempt that has already
finished -- valid values are exactly `SUCCESS` and `FAILURE`. `PENDING` remains a valid
`RecoveryAttempt.result` value (set internally by `begin_recovery()` to mark an attempt as
in-progress) but is rejected specifically as an argument to this method.

**Evidence contract.** `evidence: str | None` -- must be `None` or a non-empty, non-blank
string, validated explicitly rather than discovered indirectly.

**Fix.** `src/agentgear/watchdog/coordinator.py::ExecutionWatchdog.record_recovery_result`
-- every input (`at_seconds`, `result` type AND domain, `resume_state` type and legality,
`evidence` type) is validated BEFORE `self._recovery_attempts[-1]` is ever reassigned.

**Zero-mutation verification.** Captured state/attempts/history/transition-count/clock
before each of: SUCCESS+non-string evidence, PENDING, FAILURE+blank evidence, and a raw
`"success"` string (testing `RecoveryResult` subclassing `str` doesn't let it slip past
`isinstance`) -- all four leave every captured observable byte-for-byte unchanged.

**Regression.** `tests/test_round5_hardening.py` (AG5-02 section, 6 tests).

---

## AG5-03 — `ActivityRecord` accepted non-bool `succeeded`/`is_trivial` and malformed `error`

**Finding.** `ActivityRecord(succeeded="false", ...)` was accepted (a non-empty string is
truthy) and could silently corrupt the stall detector's trailing-failure/circular-attempt
signals, which read `.succeeded` directly. `is_trivial=1`/`0` and `error="   "`/`error=42`
were likewise unvalidated.

**Fix.** `src/agentgear/watchdog/stall_detection.py::ActivityRecord.__post_init__` --
`succeeded`/`is_trivial` must be a strict `bool` (no coercion documented, so both rejected
outright rather than guessed at); `error` must be `None` or a non-blank string.

**Construct-before-insert.** Already true structurally: `record_activity()` builds the
`ActivityRecord` (now validating) as a local variable before appending it to
`self._activities` or touching the loop guard -- no coordinator change was needed for
this half.

**Heartbeat build-failure gap (folded in here, see also AG5-05).** `_write_heartbeat()`
built the `Heartbeat` object and only wrapped the WRITER'S `.write()` call in try/except --
a domain validation failure inside `build_heartbeat()` itself (not an I/O error) would
propagate uncaught, past the point where `heartbeat_dirty` gets set, silently leaving a
stale-but-clean projection.

**Fix.** `_write_heartbeat()` now sets `self._heartbeat_dirty = True` BEFORE attempting to
build OR write the projection (the in-memory state it's about to mirror has already
changed, so the durable copy is provisionally stale from the moment this method starts);
only a build AND write that BOTH fully succeed clear it.

**Regression.** `tests/test_round5_hardening.py` (AG5-03 section): parametrized invalid
`ActivityRecord` fields, zero-mutation `record_activity()` regression for both bad-boolean
and bad-error inputs, and a heartbeat-construction-failure-marks-dirty test (monkeypatches
`coordinator_module.build_heartbeat` to raise).

---

## AG5-04 — `ExecutionState` subclasses `str`, so `advance("testing")` poisoned the state machine

**Finding.** `w.advance("testing", at_seconds=1.0)` succeeded and left `w.state` as the
raw Python string `"testing"` (not `ExecutionState.TESTING`) -- because `ExecutionState`
is `str, Enum`, `"testing"` compares AND hashes equal to the real member, so both
`advance()`'s membership check and `ExecutionStateMachine.can_transition()`'s
`target in _ALLOWED[self.state]` accepted it. Every subsequent `.state.value` access
(including inside `status()`) then raised a raw `AttributeError`.

**Fix.** `isinstance(target, ExecutionState)` checks added at BOTH:
- `ExecutionWatchdog.advance()` (`src/agentgear/watchdog/coordinator.py`) -- the
  coordinator's own public boundary.
- `ExecutionStateMachine.transition()` (`src/agentgear/watchdog/state_machine.py`) -- the
  low-level primitive is independently public/exported, so a coordinator-level guard alone
  would leave a caller using the state machine directly unprotected.

**Cross-cutting enum sweep (section 6.3/15).** Two MORE instances of the exact same
pattern were found and fixed during the sweep, neither pre-specified by the audit:
- `ExecutionStateMachine.__init__` accepted `state="running"` directly at construction --
  a separate entry point from `transition()`, unprotected by that method's own guard.
  Fixed with a `__post_init__` isinstance check.
- `ExecutionBudgetLedger.reserve(kind=...)` accepted a raw string for `kind:
  ReservationKind`, storing it on the resulting `BudgetReservation.kind` and poisoning it
  the same way -- `kind.value` would raise `AttributeError` the next time anything
  (including `reserve()`'s own error-message formatting on a later call) touched it. Fixed
  with an isinstance check in `src/agentgear/budget.py::ExecutionBudgetLedger.reserve`.

Every other public enum-typed parameter across the package (`RecoveryResult` in
`record_recovery_result`, `ModelTier`/`ReasoningEffort`/`Policy` in the coordinator
constructor, every enum field on every `models.py` dataclass, `config.py`'s
`_coerce_tier`/`_coerce_reasoning` helpers) was reviewed and already guards with
`isinstance()` or performs safe, explicit coercion (never a bare membership/equality
check) -- see `docs/audits/remediation-round-5.md`'s own history for the two gaps that
were NOT already covered.

**Regression.** `tests/test_round5_hardening.py`: `advance()` rejects
`"testing"`/`"blocked"`/`1`/`True`/`None` with zero mutation; low-level
`ExecutionStateMachine.transition()` and `__init__` both reject a raw string;
`ExecutionBudgetLedger.reserve()` rejects a raw string `kind`.

---

## AG5-05 — heartbeat went stale on ordinary activity/checkpoint events

**Finding.** After `record_activity()` on a NON-stalling event, in-memory
`attempt_count=1` but the durable heartbeat still showed `attempt_count=0`, with
`heartbeat_dirty=False` (a stale-but-clean projection -- exactly Invariant C's forbidden
state). Root cause: `record_activity()` never called `_write_heartbeat()` itself; it only
ever got written as a side effect of `evaluate()`'s STALL path, which a normal activity
never takes. `checkpoint()` had an analogous gap: it changes `pending_work` (a real
`Heartbeat` field, via `self._checkpoints[-1].pending`) but never called
`_write_heartbeat()` at all.

**Heartbeat projection matrix.** Built explicitly (`_assert_heartbeat_current` helper in
the test suite): for every `Heartbeat` field (`state`, `current_task`, `attempt_count`,
`last_error`, `pending_work`, ...), which public methods change it, and does that method
sync the durable copy afterward.

**Fix.**
- `record_activity()`: now calls `self._write_heartbeat(at_seconds)` unconditionally
  after `evaluate()` returns (not only relying on evaluate()'s own conditional write) --
  on the stall path this is a second, harmless, idempotent re-sync of whatever the CURRENT
  state is by then; on the common non-stall path it is the ONLY write that happens.
- `checkpoint()`: now calls `self._write_heartbeat(at_seconds)` after the checkpoint is
  durably appended and mirrored in-memory.
- `record_escalation()` was reviewed and confirmed to correctly NOT call
  `_write_heartbeat()` -- it only changes `tier`/`reasoning`/`escalations_used`/`budget`,
  none of which are `Heartbeat` fields. Documented and tested as intentional, not an
  oversight.

**Full lifecycle walk (permanent test).** start -> activity -> progress -> TESTING ->
REVIEWING -> RUNNING -> stall -> RECOVERING -> recovery SUCCESS -> checkpoint -> REVIEWING
-> COMPLETED, and a second walk for stall -> RECOVERING -> recovery FAILURE (exhausted) ->
BLOCKED -- the durable heartbeat is asserted current after EVERY step in both walks.

**Dirty-then-multiple-events-then-sync.** Verified `sync_heartbeat()` after several
mutations while dirty persists the LATEST authoritative state, never a stale first-dirty
snapshot -- because `_write_heartbeat()` always rebuilds the projection from current
`self` state at call time, there is no cached-snapshot risk by construction.

**Regression.** `tests/test_round5_hardening.py`: `test_heartbeat_projection_matrix_
success_path`, `test_heartbeat_projection_matrix_blocked_path`,
`test_record_escalation_intentionally_does_not_touch_heartbeat_fields`,
`test_sync_after_dirty_writes_the_latest_state_not_a_stale_snapshot`,
`test_sync_heartbeat_is_idempotent_and_does_not_duplicate_history`,
`test_sync_heartbeat_can_fail_twice_then_succeed_without_duplicating_history`.

---

## AG5-06 — relative `state_dir` silently rebinds after `os.chdir()`

**Finding.** `HeartbeatWriter("state")`/`CheckpointStore("state")` stored
`Path(state_dir)` as-is; every later file operation resolved that RELATIVE path against
the process's CURRENT working directory at operation time, not construction time. A later
`os.chdir()` elsewhere in the process silently redirected all subsequent reads/writes to a
different location on disk than the one originally configured.

**Design decision.** Bind to an ABSOLUTE path at construction time
(`bind_persistence_root`, `os.path.abspath` -- lexical only, no symlink resolution),
deliberately SEPARATE from `resolve_via_nearest_existing_ancestor`'s per-operation
SECURITY canonicalization (which still runs on every read/write to catch a directory entry
swapped for a symlink/junction AFTER construction). Resolving symlinks once at
construction and trusting that forever would defeat the per-operation tamper check the
"security" side depends on -- the two mechanisms serve different purposes and both stay in
place.

**Fix.** `src/agentgear/path_security.py::bind_persistence_root`, applied in
`HeartbeatWriter.__init__` and `CheckpointStore.__init__`.

**Verification.**
- Construct under directory A with a relative `state_dir`, `chdir` to B, write: the
  artifact lands under `A/state`, nothing appears under `B/state` -- for `HeartbeatWriter`,
  `CheckpointStore`, and end-to-end through `ExecutionWatchdog(state_dir="state")`.
- Security regression: construct under A, `chdir` to B, THEN swap a checkpoint execution's
  segment subdirectory (a real, existing directory -- not the root itself) for a junction
  pointing outside `A`, append: still rejected with `PathEscapeError` and zero artifacts in
  the junction target. Confirms the lexical-binding fix did not weaken the existing
  per-operation containment check.

**Regression.** `tests/test_round5_hardening.py`:
`test_heartbeat_writer_relative_root_stays_bound_after_chdir`,
`test_checkpoint_store_relative_root_stays_bound_after_chdir`,
`test_execution_watchdog_relative_state_dir_stays_bound_after_chdir`,
`test_relative_root_binding_does_not_weaken_junction_containment`.

---

## AG5-07 — PromptGraph adapter error text leaked into a public note

**Finding.** `PromptGraphContextProvider.request()`'s fallback path echoed
`str(exc)` -- the raw exception message from an external, untrusted adapter -- directly
into `ContextPackage.note`, a public field that may itself flow into logs or a model's own
context. An adapter's own error path could embed anything (a credential from a failed auth
call, a local file path, part of a request payload).

**Fix.** `src/agentgear/context_provider.py::PromptGraphContextProvider.request` -- the
except-block note now includes only `type(exc).__name__` (the exception CLASS), never
`str(exc)`.

**Secret test.** An adapter raising
`RuntimeError("API_KEY=sk-audit-secret C:\\private\\config.py\npassword=hunter2")`
(embedding a key, a path, and a password, across multiple lines) produces a
`ContextPackage.note` containing none of those fragments -- only `"PromptGraph
integration failed (RuntimeError); default context fallback used."`

**Pre-existing Round-4 test updated.** `test_generator_exception_after_one_yield_falls_
back_safely` previously asserted the raw message WAS present (`"boom mid-iteration" in
package.note`) -- that assertion described the OLD, insecure behavior and was updated to
assert the message is absent and only the exception class name appears.

**Regression.** `tests/test_context_provider.py::
test_promptgraph_provider_search_exception_message_is_never_echoed`.

---

## AG5-08 — stale `hermes-oss/promptgraph` link in current-facing README

**Finding.** `README.md` linked `https://github.com/hermes-oss/promptgraph` -- the dead
org, same class of finding as Round 4's NEW-09 (which only checked
`hermes-oss/agentgear`, not the sibling-project reference).

**Fix.** `README.md` updated to `https://github.com/Human-Weapon/PromptGraph`.

**Static check (not build-dependent).** `tests/test_release_metadata.py` checks the
repo's own current-facing `README.md` directly (no build step needed) for the correct
PromptGraph URL and the absence of both dead org references. CI's "Inspect release
metadata" step extended with the same checks against the built artifacts' bundled README.
Historical audit documents (`docs/audits/remediation-round-*.md`) are deliberately exempt
-- they legitimately quote the old, already-fixed URL as the bug being described.

**Regression.** `tests/test_release_metadata.py::
test_readme_points_at_the_real_promptgraph_repo`,
`test_readme_does_not_reference_the_dead_agentgear_org`.

---

## AG5-09 — audit index shipped with placeholder commit SHAs

**Finding.** `docs/audits/index.md`'s ENTIRE Round 4 section (all `NEW-01`..`NEW-10`,
`R4-SA-01`, `R4-SA-02` rows, plus the top-of-file commit-reference table) still said
`(this round)` -- Round 4 was finalized and pushed as `f8573ef`, but the index was never
updated afterward to reference that now-immutable SHA. A sixth audit reading the pushed
index would find every Round-4 "Fix commit" column pointing at nothing.

**Why previous tooling missed it.** Round 4 had no CI/test check for this at all; nothing
would have caught a stale placeholder shipping in the pushed HEAD.

**Two-commit workflow (required, not optional).** A document inside commit X cannot
contain X's own SHA (editing the document changes X). So:
- **Commit A** (this round's code + tests + `remediation-round-5.md`): produces
  `ROUND5_FIX_SHA`.
- **Commit B**: finalizes `docs/audits/index.md`, replacing this round's own `(this
  round)` placeholders with the now-known `ROUND5_FIX_SHA`, and (see below) retroactively
  fixing Round 4's stale placeholders using its ALREADY-known, already-verified SHA.
- CI runs on commit B, the actual pushed HEAD.

**Historical SHAs -- verified via `git log`, not assumed.**
```
5330554d863af6b55f441c2fa81e22ce1075809c  AgentGear v0.1.0 release candidate
dbdcaa99f345ecba5f6e44ec21947020cba00596  Remediation Round 1: fix AG-01 through AG-09, P3, and standalone test
51bfac84c638348edd49c58ceadf29c513fdaaa4  Harden remediation runtime invariants
1cff32b6ed6bf46163423d86d870ee1c8ddee314  Remediation Round 2: recovery episode model, boundary/validation hardening
bc531510c93b5a390132b95374efbcd228fb94c8  Remediation Round 3: strict evidence validation, exception narrowing, deep immutability
f8573ef4f0614ae84989609ed1c293d2da10c595  Remediation Round 4: public boundary + durability hardening
```
Note: the audit brief's suggested "Round 1 candidate" SHA (`51bfac8`) does not match `git
log` -- the actual Round 1 commit (covering AG-01 through AG-09/P3, per its own commit
message) is `dbdcaa9`; `51bfac8` ("Harden remediation runtime invariants") is a separate,
later commit. The index's existing (correct, unchanged) attribution already reflects this:
Round 1 rows cite `dbdcaa9`, and the Round 2 baseline note already lists both `51bfac8` and
`1cff32b` together as preceding Round 2's actual fix commit (`1cff32b`, cited on every
Round 2 row). Only Round 4's rows needed correcting -- from `(this round)`/`uncommitted`
to `f8573ef` -- since Rounds 1-3 were already correctly finalized.

**CI traceability check (section 12.4).** New: `tests/test_release_metadata.py::
test_audit_index_has_no_unfinalized_placeholder_shas` and a matching CI assertion, both
checking `docs/audits/index.md` never contains `"(this round)"`, `"uncommitted"`, `"TBD
SHA"`, or `"FIXME SHA"` -- catching this exact class of gap on every future push, not only
retroactively this once.

**Fix.** `docs/audits/index.md` -- Round 4's placeholders replaced with `f8573ef`
(committed as part of commit A, since that SHA was already known and immutable); Round
5's own rows finalized with `ROUND5_FIX_SHA` in commit B (see Git section of the final
report for the actual SHA).

---

## AG5-10 — `state_dir=""` silently disabled persistence

**Finding.** `ExecutionWatchdog(..., state_dir="")` passed the existing `isinstance(...,
str)` type check, then `self._heartbeat_writer = HeartbeatWriter(state_dir) if state_dir
else None` (and the equivalent `if state_dir:` guard for `CheckpointStore`) treated the
empty string as falsy, silently disabling persistence with no error. A NON-empty
whitespace string (`"   "`) was worse: it passed straight through as a real (if bizarre)
directory name.

**Fix.** `ExecutionWatchdog.__init__` now explicitly rejects `state_dir is not None and
not state_dir.strip()` with `ConfigurationError`, and the two truthiness checks
(`if state_dir else None`, `if state_dir:`) were changed to explicit `is not None` checks
-- consistent with the documented contract that only `None` disables persistence.

**Regression.** `tests/test_round5_hardening.py`:
`test_blank_state_dir_is_rejected` (parametrized `""`, `"   "`, `"\t\n"`),
`test_none_state_dir_still_disables_persistence`,
`test_valid_state_dir_still_enables_persistence`.

---

## AG5-11 — standalone CI check asserted the WRONG definition of "standalone"

**Finding.** The CI "Standalone black-box test" step asserted
`_sibling_utils.is_installed(name) is False` for every optional sibling, including
`promptgraph`. `tests/test_standalone.py`'s own module docstring already correctly
documents that "standalone" means AgentGear never REQUIRES a sibling to function -- it
does NOT mean a sibling must be ABSENT from the environment (an auditor's environment may
have PromptGraph installed, e.g. a shared hermes-oss monorepo venv). The CI step
contradicted the test suite's own stated contract and would give a false negative in any
environment where a sibling genuinely is importable.

**Fix.** `.github/workflows/ci.yml` -- split into two steps:
- **True isolation**: proves core `agentgear` (import, `plan()`, the state machine) works
  in a fresh wheel-only install with no siblings present (simply what the CI runner's venv
  already is) -- WITHOUT asserting anything about sibling absence.
- **Optional sibling present**: creates a minimal, real, importable `promptgraph` package
  on `PYTHONPATH` (not a monkeypatched stub of AgentGear's own detection function) and
  proves core behavior -- and `PromptGraphContextProvider.is_available()` correctly
  reporting it as present -- is completely unaffected by its mere presence.

**Local test (not CI-only).** `tests/test_standalone.py::
test_core_plan_pipeline_unaffected_by_an_actually_importable_sibling` -- creates a real
package directory added to `sys.path` (discoverable by `importlib.util.find_spec` exactly
like a real pip install), proving the same contract locally and fast, without a CI run.

---

## Cross-cutting notes

No test was weakened, removed, or reclassified as "intentional" to make it pass (one
PRE-EXISTING Round-4 test's assertion was updated, documented above under AG5-07, because
it explicitly asserted the OLD insecure behavior this round deliberately changed). Every
fix above has an independent reproducer that failed against baseline
`f8573ef4f0614ae84989609ed1c293d2da10c595` before the corresponding change landed. No
database, distributed transaction manager, background heartbeat daemon, generic event bus,
new process supervisor, or custom async framework was introduced -- AG5-01's fix is one
reused `FileLock` primitive scoped per-execution; AG5-06's fix is one `os.path.abspath`
call.
