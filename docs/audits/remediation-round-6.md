# AgentGear Remediation Round 6

Sixth independent adversarial audit against baseline `68c829d833ecfd270fa68193b8feae4959406dd7`
(the AG5-09 traceability micro-fix). Verdict: **D — NOT RELEASE READY** (0 P0, 2 P1, 1 P2, 0 P3,
0 P4). See `docs/audits/index.md` for the cross-round traceability index.

## AG6-01 — public watchdog events accepted outside lifecycle

**Severity.** P1.

**Reproduction.**
```python
w = ExecutionWatchdog("e1", Policy.default())
w.checkpoint(at_seconds=1.0, phase="p0")  # accepted -- state is still PLANNING

w2 = ExecutionWatchdog("e2", Policy.default())
w2.start(task="t", at_seconds=0.0)
w2.advance(ExecutionState.REVIEWING, at_seconds=1.0)
w2.complete(at_seconds=2.0, evidence=("done",))
w2.record_activity(at_seconds=3.0, fingerprint="f", succeeded=True)  # accepted after COMPLETED
```

**Broken invariant.** A public watchdog event illegal in the current lifecycle state must fail
BEFORE any mutation (timestamp commit, history append, activity/progress mutation, recovery
mutation, budget reservation, checkpoint persistence, heartbeat projection, dirty-flag change).
Four ordinary event methods -- `record_activity()`, `record_progress()`,
`record_escalation()`, `checkpoint()` -- had NO lifecycle admission check at all.

**Root cause.** `start()`, `begin_recovery()`, `record_recovery_result()` each independently
check their own required current state; `advance()`/`complete()` rely on the state machine's
own `_ALLOWED` transition table. The four "ordinary work event" methods were never given an
equivalent check when they were originally written -- there was no single admission mechanism
that ALL public mutating methods funneled through, so this class of gap was invisible to five
prior audits: each individual finding (checkpoint-before-start, activity-after-complete) looks
like an isolated missing check rather than a systemic absence of one.

### The formal OPERATION x STATE matrix

Derived by inspecting the DOCUMENTED purpose of every public mutating method, not by guessing:

| Operation | PLANNING | RUNNING/TESTING/REVIEWING | STALLED | RECOVERING | BLOCKED | COMPLETED |
|---|---|---|---|---|---|---|
| `start()` | ALLOWED (once) | REJECTED | REJECTED | REJECTED | REJECTED | REJECTED |
| `advance()` | REJECTED (see below) | ALLOWED (between active states) | REJECTED | REJECTED (see below) | REJECTED | REJECTED |
| `record_activity()` | REJECTED | ALLOWED | REJECTED | REJECTED | REJECTED | REJECTED |
| `record_progress()` | REJECTED | ALLOWED | REJECTED | REJECTED | REJECTED | REJECTED |
| `record_escalation()` | REJECTED | ALLOWED | REJECTED | REJECTED | REJECTED | REJECTED |
| `checkpoint()` | REJECTED | ALLOWED | REJECTED | REJECTED | REJECTED | REJECTED |
| `evaluate()` | no-op (documented, see below) | active check runs | no-op | no-op | no-op | no-op |
| `begin_recovery()` | REJECTED | REJECTED | ALLOWED (own dedicated check) | REJECTED | REJECTED (see below) | REJECTED |
| `record_recovery_result()` | REJECTED | REJECTED | REJECTED | ALLOWED (own dedicated check) | REJECTED | REJECTED |
| `complete()` | REJECTED | ALLOWED from REVIEWING only (state machine `_ALLOWED`) | REJECTED | REJECTED | REJECTED | REJECTED |
| `sync_heartbeat()` | ALLOWED everywhere -- synchronizes already-committed state, adds no domain work | | | | | |

**PLANNING.** Ordinary events rejected -- there is no task in progress yet. `advance()` is
ALSO now rejected from PLANNING: it used to silently bypass `start()`'s own initialization
(see "two additional escapes" below).

**STALLED/RECOVERING.** Ordinary events rejected. Investigated deliberately rather than
guessed (section 3.3): the recovery subsystem has its own dedicated APIs
(`begin_recovery()`, `record_recovery_result()`) for tracking what happens during a
stall/recovery episode. Ordinary activity tracking exists specifically to feed stall
DETECTION (`evaluate()`'s `_stall_detector.evaluate(...)` call) -- which is meaningless
once a stall has already been detected. Recording "ordinary" activity during RECOVERING
would just silently accumulate in `self._activities` with no purpose it was designed for.

**BLOCKED.** Ordinary events rejected. Only the dedicated `begin_recovery()` path may
resume (see F below).

**COMPLETED.** Terminal -- rejected everywhere except `sync_heartbeat()` (section 3.9),
which synchronizes already-committed state rather than adding new domain work.

### Two additional escapes found during matrix construction (not pre-specified)

Building the matrix required asking, for `advance()`, "what happens if it's called from a
state its own target-blocklist doesn't defend against?" -- which surfaced two real, severe
gaps neither literally reproduced by the audit brief:

1. **`advance()` bypassed `start()` from PLANNING.** `advance(RUNNING, ...)` succeeded from
   PLANNING (the state machine's own `_ALLOWED[PLANNING]` includes `RUNNING`, and
   `advance()`'s target-blocklist only excludes `STALLED`/`RECOVERING`/`BLOCKED`/`COMPLETED`
   as TARGETS, never checking the CURRENT state). This left `state == RUNNING` but
   `_started_at is None` and `_current_task == ""` -- and since `evaluate()` no-ops whenever
   `_started_at is None`, this PERMANENTLY DISABLED stall detection for that execution,
   directly undermining AG-01 (Round 1's very first finding).
2. **`advance()` bypassed `record_recovery_result()` from RECOVERING.** `advance(RUNNING,
   ...)` also succeeded from RECOVERING (same reasoning), abandoning a still-`PENDING`
   `RecoveryAttempt` and leaving its recovery episode forever unresolved -- `record_history`
   would never record a `SUCCESS`/`FAILURE` outcome for it.

Both are closed by the SAME fix as the four ordinary methods: `advance()` now requires the
CURRENT state to already be RUNNING/TESTING/REVIEWING, matching its own documented purpose
("RUNNING <-> TESTING <-> REVIEWING") exactly -- it never legitimately needed PLANNING,
STALLED, RECOVERING, or BLOCKED as a starting point.

### `record_escalation()` was completely unguarded

Not just "the wrong states" -- ZERO state check existed. `record_escalation()` before
`start()` (PLANNING) escalated `self.tier` to FRONTIER and committed budget, before any task
had even begun.

### `evaluate()` — deliberately left unchanged

`evaluate()` already no-ops (commits time, returns) outside `_ACTIVE_STATES` -- this is
INTENTIONAL, pre-existing, safe behavior (it never mutates anything beyond the clock
outside active states) and is exercised internally by `record_activity()` only after
`record_activity()`'s OWN new admission check has already passed. A direct external call to
`evaluate()` from an inactive state remains a harmless no-op, unchanged by this round.

### `BLOCKED -> RECOVERING`

The public `ExecutionWatchdog` coordinator has NEVER exposed a resumption method for
BLOCKED: `begin_recovery()` requires STALLED (not BLOCKED) as a precondition, and
`advance()` has always blocked BLOCKED/RECOVERING as targets, both before and after this
round. `BLOCKED -> RECOVERING` remains legal at the LOW-LEVEL `ExecutionStateMachine`
(Round 2 / L5, INTENTIONAL) -- untouched by this round's coordinator-level admission guard.
Verified via a direct low-level transition test; documented here so a future round doesn't
mistake this pre-existing architecture gap for a Round 6 regression.

**Design decision.** ONE internal admission check, `_require_active_state(operation)`,
checked as the FIRST line of `advance()`, `record_activity()`, `record_progress()`,
`record_escalation()`, `checkpoint()` -- before `_validate_time()` or any other input
validation (section 3.5/17): an illegal lifecycle call is rejected on its own terms and
never masked by (or dependent on) an unrelated input problem. Verified explicitly: COMPLETED
+ `record_activity(at_seconds=-10, ...)` raises the lifecycle error, not a timestamp error.

**Fix.** `src/agentgear/watchdog/coordinator.py::ExecutionWatchdog._require_active_state`.

**Zero-mutation verification.** A full snapshot (state, activity/checkpoint/progress
counts, transition history length, clock, tier/reasoning/escalations, budget status,
recovery attempt/history counts) is captured before each rejected call and compared
byte-for-byte after -- across every inactive state x every ordinary operation, plus the
two `advance()` escapes.

**Regression.** `tests/test_round6_hardening.py` -- the full parametrized STATE x EVENT
admission matrix (section 3.10): every ordinary operation tested against every active state
(must succeed) and every inactive state (must reject with zero mutation), plus dedicated
tests for both `advance()` escapes, the BLOCKED-ordinary-events-rejected-but-
`begin_recovery()`-still-legal case, the terminal-COMPLETED invariant, and the lifecycle-
priority-over-timestamp-error test.

---

## AG6-02 — configured persistence root can be replaced by a Windows junction

**Severity.** P1.

**Reproduction (real `mklink /J`, not mocked).**
```python
writer = HeartbeatWriter(state_dir)  # state_dir exists as a normal directory
shutil.rmtree(state_dir)
subprocess.run(["cmd", "/c", "mklink", "/J", state_dir, outside])
writer.write(heartbeat)  # SUCCEEDS -- writes land in `outside`
```
Both the heartbeat JSON and its `.lock` file appeared inside `outside`.

**Root threat model (section 4.1).** Two DIFFERENT containment questions:

- **A.** Did a target INSIDE the configured root escape through a CHILD symlink/junction?
  (Already handled: `assert_path_family_contained`, re-checked on every operation.)
- **B.** Did the CONFIGURED ROOT ITSELF change identity after construction?
  (NOT handled before this round.)

If the root itself is swapped, both the (still-"correct", per its own now-swapped
definition) root AND a target computed relative to it resolve through the SAME junction
consistently -- a same-moment target-vs-root comparison finds nothing wrong, because both
sides moved together. This is why (A) alone was insufficient: it structurally cannot detect
(B).

**Design decision -- `PersistenceRoot` (`src/agentgear/path_security.py`).** At
construction, pins BOTH:
- `lexical_root`: the absolute lexical path (Round 5's `bind_persistence_root`, unchanged).
- `_expected_canonical_root`: `resolve_via_nearest_existing_ancestor(lexical_root)`,
  computed ONCE, at construction time.

`assert_identity_unchanged()` re-computes the SAME resolution on every subsequent operation
and compares against the pinned expectation -- a mismatch means the root's identity changed
since construction, regardless of what any target path resolves to.

**Works for both existing and nonexistent roots (sections 4.3/4.4), by construction, not by
special-casing:**
- **Nonexistent root at construction:** `resolve_via_nearest_existing_ancestor` walks up to
  the nearest existing ancestor, resolves THAT, and appends the still-nonexistent tail
  verbatim -- this becomes the expected identity. A later NORMAL `mkdir` at that lexical
  location re-resolves to the SAME identity (a freshly-created plain directory is its own
  realpath) and passes. A junction planted there instead resolves through to wherever it
  points and fails the comparison.
- **Existing root at construction (including one that is ITSELF already a symlink):**
  `resolve_via_nearest_existing_ancestor` resolves it immediately (no walk-up needed),
  naturally following any symlink already there and pinning ITS resolved target as the
  expected identity -- satisfying section 4.5's preferred design ("allow an already-present
  intentionally configured root link, but pin its resolved identity") for free, with no
  separate code path.

**Fix.** `PersistenceRoot` wired into `HeartbeatWriter._store()` and
`CheckpointStore._segment_dir()` -- both are the SINGLE choke point every other method in
each class funnels through (including `CheckpointStore._execution_lock()`, which calls
`_segment_dir()` to build its own lock path), so the identity check runs before ANY artifact
(mkdir, execution lock, segment lock, JSON, temp, quarantine, heartbeat lock) is created,
satisfying sections 10/11 without a second insertion point.

**Existing child-containment checks are unmodified and remain fully in effect** (section
4.8) -- `PersistenceRoot` is purely additive.

**Verification (real Windows junctions via `cmd /c mklink /J`, never mocked).**
- Existing root swapped for a junction after construction: rejected, zero outside artifacts
  (heartbeat, checkpoint, and end-to-end through `ExecutionWatchdog`).
- Absent root replaced by a junction BEFORE the first write: rejected, zero outside
  artifacts (the case that distinguishes a sound root-identity design from one that only
  protects roots that already existed at construction).
- Absent root, legitimate normal `mkdir` (no attack): still succeeds.
- Relative root + `chdir` (Round 5's own regression) + root junction swap: still rejected --
  proves the lexical-binding fix and the identity-pinning fix compose correctly.
- Corrupt heartbeat file + simultaneous root junction swap: the root-identity check runs
  BEFORE quarantine's own rename, so the swap is rejected first; quarantine cannot escape.
- Nested junction two levels below the root (a CHILD path, not the root itself): still
  caught by the PRE-EXISTING child-containment check, proving the two mechanisms compose
  without one superseding the other.

**Ubuntu/POSIX.** The same code path (`resolve_via_nearest_existing_ancestor` /
`os.path.realpath`) is platform-neutral; `os.symlink` is the POSIX equivalent of
`mklink /J`. CI's Ubuntu matrix runs the full suite, exercising this on a real POSIX
symlink wherever a test isn't explicitly Windows-only (the junction-specific tests are
skipped on non-Windows via `pytest.mark.skipif`, since NTFS junctions have no POSIX
equivalent, but the underlying `PersistenceRoot` mechanism itself is exercised on every
platform through ordinary construction/operation).

**TOCTOU honesty (section 7).** This closes the REPRODUCIBLE check-then-open gap -- a
replaced root is now always caught before the first artifact is created in every tested
scenario. It still uses ordinary path-based checks (`os.path.realpath`), not OS
handle-based sandboxing. A sufficiently-privileged local attacker racing between
`assert_identity_unchanged()`'s check and the very next filesystem call is not provably
impossible to win against on every platform. v0.1.0 does not attempt a handle-based
rewrite -- this is a documented, deliberate scope boundary, not an oversight.

**Regression.** `tests/test_round6_hardening.py` (AG6-02 section): 13 dedicated tests
covering the matrix above, all using real `mklink /J` where Windows-specific.

---

## AG6-03 — existing regular file accepted as `state_dir` until after RUNNING

**Severity.** P2.

**Reproduction.**
```python
open(state_dir, "w").write("not a directory")
w = ExecutionWatchdog("e1", Policy.default(), state_dir=state_dir)  # accepted
w.start(task="x", at_seconds=0.0)  # commits RUNNING, THEN heartbeat write raises FileExistsError
```

**Broken invariant.** A structurally-impossible `state_dir` (an existing regular file) must
be rejected BEFORE `start()` can mutate PLANNING -> RUNNING or consume budget/history --
not discovered as a raw `FileExistsError` deep inside the first heartbeat write, after the
domain transition has already committed (NEW-04's "never rolled back" model means that
transition is now permanent).

**Fix -- folded into the SAME `PersistenceRoot` guard built for AG6-02**, per the explicit
design instruction (section 5.1) to avoid two slightly-different validation rules:
`PersistenceRoot.__init__`/`assert_identity_unchanged()` both call
`_require_directory_or_absent()`, rejecting an existing non-directory immediately with the
new `InvalidPersistenceRootError(PersistenceError)`.

**Error type (section 5.2).** At the PUBLIC `ExecutionWatchdog` constructor, this is caught
and re-raised as `ConfigurationError` -- the constructor's own established, stable public
vocabulary for every other caller-configuration mistake (blank `state_dir`, non-`Policy`
object, ...). At the LOW-LEVEL `HeartbeatWriter`/`CheckpointStore` constructors (section
5.3), `InvalidPersistenceRootError` propagates directly, unwrapped -- consistent with the
EXISTING precedent that `validate_persistence_safe_id`'s `InvalidIdentifierError` already
propagates raw from these same constructors (Round 4/NEW-08), never wrapped in
`ConfigurationError`. No incompatible duplicate rule was introduced: both the watchdog
constructor and the low-level stores call the SAME `PersistenceRoot`/`_require_directory_
or_absent` check; only the exception each layer surfaces to its own callers differs.

**Race case (section 5.5) -- the check is NOT only-once.** `state_dir` absent at
construction; before the first operation, something creates a regular file there instead of
a directory. `assert_identity_unchanged()` re-runs `_require_directory_or_absent()` on
EVERY operation (not only at `__init__`), so this is caught on the first real write, not
silently missed because "we already checked once."

**Regression.** `tests/test_round6_hardening.py`: constructor rejection (watchdog, direct
`HeartbeatWriter`, direct `CheckpointStore`), the post-construction race (absent -> file
before first write), and the existing-root-later-replaced-by-a-file case (distinct code
path from absent-root-becomes-a-file, both verified).

---

## Cross-cutting notes

No test was weakened, removed, or reclassified as "intentional" to make it pass. Sixteen
PRE-EXISTING tests across `test_coordinator.py`, `test_round4_hardening.py`, and
`test_round5_hardening.py` needed updating -- they drove stalls via loops of
`record_activity()` calls that continued past the point where the execution had already
left an active state, which the fixed `record_activity()` now correctly rejects. One
assertion (`total_attempts == 30` in `test_five_successful_recovery_episodes_each_start_
fresh`) changed to `10`: the OLD number silently counted activities recorded AFTER each
episode had already stalled -- itself a symptom of the bug this round fixed, not a
meaningful "total work attempted" figure. Every fix above has an independent reproducer
that failed against baseline `68c829d833ecfd270fa68193b8feae4959406dd7` before the
corresponding change landed. No database, distributed lock service, or OS-handle-based
sandbox was introduced -- AG6-02's fix is one small class (`PersistenceRoot`) reusing the
existing `resolve_via_nearest_existing_ancestor` primitive; AG6-01's fix is one boolean
check (`_require_active_state`) reused across five call sites.
