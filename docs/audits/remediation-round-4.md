# AgentGear Remediation Round 4

Fourth independent adversarial audit against baseline `bc531510c93b5a390132b95374efbcd228fb94c8`
(Remediation Round 3). Verdict: **C — FIX BEFORE PROMOTING** (0 P0, 0 P1, 6 P2, 4 P3, 2 P4).
See `docs/audits/index.md` for the cross-round traceability index.

## NEW-01 — decision-critical factor VALUES unvalidated

**Finding.** `RiskAssessment`/`ComplexityAssessment.factors` were deep-frozen (Round 3 /
AUDIT3-04) but the individual VALUES inside were never validated. `security_impact=NaN`
was silently accepted.

**Reproduction.**
```python
risk = RiskAssessment(score=0.05, level=RiskLevel.MINIMAL, factors={"security_impact": math.nan})
critical_signal_reasons(risk, Policy.default())  # -> () -- NaN >= 0.85 is always False
```

**Broken invariant.** A task with an unknown/maximal-risk signal (represented as `NaN`
because the caller couldn't compute a real number) routes as if it were harmless, because
every `signal >= threshold` critical-risk check is silently defeated by `NaN`'s comparison
semantics.

**Root cause.** `factors` was frozen (a mutability fix) but never given the same
finite-[0,1]-not-bool value contract as `score` itself.

**Why previous tests missed it.** AUDIT3-04's tests exercised mutation resistance, not
value validity — nothing constructed a `RiskAssessment` with a genuinely malformed factor
value.

**Decision.** Add `_validate_factors_mapping()`, a single shared validator (not two
subtly-different ones for Complexity vs. Risk) applied to every factor value with the
existing `_validate_unit_interval` contract (finite, [0,1], not bool). Keys must be
non-blank strings; UNKNOWN keys are allowed (forward compatibility — no hardcoded factor
vocabulary), but their values obey the same contract as known ones.

**Alternatives considered.** Restricting to only the currently-consumed key names (reject
unknown keys) was rejected — it would make adding a new factor in a future analysis.py
change a breaking API change for anyone who'd started passing it early.

**Fix.** `src/agentgear/models.py::_validate_factors_mapping`, wired into both
`ComplexityAssessment.__post_init__` and `RiskAssessment.__post_init__`, called BEFORE the
`MappingProxyType` freeze.

**Regression.** `tests/test_round4_hardening.py` — NaN/Infinity/-Infinity/out-of-range/
bool/string/None rejected for every decision-critical key on both models; unknown keys
accepted with valid values, rejected with invalid ones; a NaN-factor construction attempt
is proven unable to ever reach `critical_signal_reasons()`.

**Black-box.** Scenario 8 (`agentgear.models`/`agentgear.routing` imported from the
installed wheel, threshold=0.0 exact-zero case).

---

## NEW-02 — `ExecutionWatchdog` constructor accepted impossible values

**Finding.** Only `execution_id` (non-blank) and a loose `context_budget_tokens <= 0`
check ran in `__init__`. `context_budget_tokens=True` (bool) and `float('nan')` both
silently pass `<= 0` (`True <= 0` is `False`; `nan <= 0` is `False`); a raw string for
`initial_tier`/`initial_reasoning`, or a non-`Policy` object for `policy`, was accepted at
construction and only surfaced as a raw `AttributeError` moments later inside the same
`__init__` (e.g. `policy.watchdog` on a `dict`).

**Reproduction.**
```python
ExecutionWatchdog("x", Policy.default(), context_budget_tokens=True)  # accepted, budget=True
ExecutionWatchdog(
    "x", {"not": "a policy"}
)  # raw AttributeError: 'dict' object has no attribute 'watchdog'
```

**Broken invariant.** A caller mistake should fail with a clear, stable domain error
immediately at construction — not sometimes succeed with a nonsensical value (bool budget)
and sometimes crash with a raw, non-domain exception from deep inside the constructor.

**Root cause.** No boundary validation for `policy`, `initial_tier`, `initial_reasoning`;
`context_budget_tokens`'s check didn't exclude `bool` or restrict to `int`.

**Decision.** Validate every constructor argument BEFORE any state is assigned, all
raising one error family (`ConfigurationError`, matching how `Policy` and its nested
config classes already validate themselves) — not `InvalidObservationError` (reserved for
runtime-reported activity/progress signals, a different semantic category from
construction-time configuration).

**Alternatives considered.** Silently coercing (`ModelTier(value)` from a string) was
explicitly rejected — the brief calls for failing immediately on caller mistakes, not
guessing intent.

**Fix.** `src/agentgear/watchdog/coordinator.py::ExecutionWatchdog.__init__`.

**Regression.** `tests/test_round4_hardening.py` — full boundary table for
`initial_tier`/`initial_reasoning` (string/int/None/bool), `policy` (dict/None/string),
`context_budget_tokens` (bool/float/string/NaN/Infinity/negative/zero), `state_dir`
(non-str), plus a test confirming no partial state is ever left reachable after rejection.

**Black-box.** Scenarios 9–12 (invalid tier/reasoning/budget/policy all rejected against
the installed wheel).

---

## NEW-03 — rejected operations still mutated the clock

**Finding.** `_validate_time()` both validated AND mutated `self._last_observed_at` in one
step. A caller whose operation validated the timestamp successfully but then failed its
OWN later validation (e.g. `complete()` rejecting malformed evidence) had already advanced
the clock — so a subsequent LEGITIMATE retry at an earlier, correct timestamp was
incorrectly rejected as "before the last observed time."

**Reproduction.**
```python
w.complete(
    at_seconds=100.0, evidence=("done", 42)
)  # rejected (bad evidence) -- but clock now at 100.0
w.complete(at_seconds=2.0, evidence=("done",))  # WOULD incorrectly fail: "2.0 is before 100.0"
```

**Broken invariant.** "A rejected operation must not mutate state" — the clock watermark
is state.

**Root cause.** Validation and commitment were the same function call, so a caller had no
way to validate without also committing.

**Decision.** Split `_validate_time()` (pure check, no mutation) from a new
`_commit_time()` (mutates `_last_observed_at`, called only once an operation has fully
succeeded). Every public method calls `_commit_time()` as its own last mutation, AFTER any
step that could still fail (e.g. `complete()` commits only after `_sm.transition()`
succeeds). Explicitly did NOT implement rollback-on-exception (broad `except` restoring
the old value) — the brief calls this out as fragile; check-then-commit is simpler and has
no failure mode to reason about.

**Fix.** `src/agentgear/watchdog/coordinator.py` — every state-mutating public method
(`start`, `advance`, `record_activity`, `record_progress`, `evaluate`, `begin_recovery`,
`record_recovery_result`, `record_escalation`, `checkpoint`, `complete`,
`_transition_to_blocked`).

**Regression.** `tests/test_round4_hardening.py` — the canonical evidence-rejection
scenario, plus the same "reject then retry earlier legitimate timestamp" pattern for
`advance()`, `record_progress()`, `record_recovery_result()`, and `start()`.

**Black-box.** Scenarios 13–14 (rejected complete doesn't advance clock; subsequent valid
complete succeeds).

---

## NEW-04 — heartbeat write failure produced split-brain state

**DURABILITY MODEL CHOSEN: Option B — commit + dirty/sync.**

**Finding.** State transition succeeds in memory; the subsequent heartbeat write fails
(disk full, permission error); the caller receives the raised `OSError`, but by then
in-memory state is COMPLETED while the durable heartbeat still shows REVIEWING, and
`complete()` cannot be called again to "retry" (COMPLETED has no outgoing transitions).

**Reproduction.**
```python
w.advance(ExecutionState.REVIEWING, at_seconds=1.0)
w._heartbeat_writer.write = raises_oserror
w.complete(at_seconds=2.0, evidence=("done",))  # raises OSError
# w.state == COMPLETED (in-memory)
# durable heartbeat file still shows REVIEWING
# w.complete(...) again -> InvalidStateTransitionError: completed -> completed
```

**Broken invariant.** No documented path from "domain transition succeeded, durable mirror
did not" back to a synchronized state, without repeating the domain operation (which is
often impossible) or duplicating history/budget.

**Root cause.** `_write_heartbeat()` had no failure handling at all; a raised exception
there propagated unchanged with no bookkeeping of what had already durably landed.

**Alternatives considered.**
- **Option A (persist-before-commit):** validate the whole transition + prospective
  heartbeat without mutation, persist, THEN commit in-memory (designed to be
  non-failing at that point). Rejected: AgentGear's state-machine transitions aren't
  purely-computed-then-applied in a way that makes the second stage provably
  non-failing without a much larger refactor, and it still leaves a gap if the
  in-memory commit crashes AFTER a successful persist (durable state would then be
  AHEAD of memory, which is arguably a worse and higher-risk failure mode for an
  execution-liveness watchdog than "memory ahead of a best-effort mirror").
- **Ad-hoc rollback** (catch the heartbeat exception, unwind the state machine
  transition, budget, history): explicitly rejected per the remediation brief — it
  risks corrupting exactly the invariants (history, clock, budget, recovery episodes)
  this whole round exists to protect, for a benefit (perfect heartbeat/state sync) that
  Option B achieves anyway via an explicit resync step.

**Decision (Option B).** The in-memory state machine remains the SOLE authority. A failed
heartbeat write sets `heartbeat_dirty = True`, records the error, and re-raises the
ORIGINAL exception unchanged (the immediate caller learns about it) — but never rolls
back. `sync_heartbeat()` is a new public, idempotent method: retries writing the CURRENT
in-memory state as a heartbeat, touching nothing else (no transition, no budget, no
history, no attempt count), safe to call any number of times including when nothing is
dirty. `status()["heartbeat_dirty"]`/`["heartbeat_sync_error"]` expose the condition
explicitly.

**Failure semantics.** Every state-mutating method now commits the clock/state BEFORE
attempting the heartbeat write (reordered so a heartbeat failure can never block an
already-decided domain outcome from being fully committed in memory).

**Recovery/sync path.** `sync_heartbeat()` — see above.

**Idempotency.** Verified: calling `sync_heartbeat()` after a failure, then again after
another failure, then again after success, produces exactly the durable state that
matches in-memory, with zero duplicate transitions/history/budget/attempts at any point.

**Regression.** `tests/test_round4_hardening.py` — failed write doesn't roll back the
transition; `sync_heartbeat()` recovers without repeating anything; fails-twice-then-
recovers-on-third-attempt; no-op when nothing dirty; dirty status survives until a REAL
successful write clears it (not incorrectly cleared by an unrelated success); failure
injected across `start()`/`complete()`/the BLOCKED path/a recovery transition.

**Black-box.** Scenarios 15–17 (heartbeat failure creates a documented recoverable
condition, sync succeeds, retry doesn't duplicate anything).

**CLI status limitation (documented, not a defect).** A separate-process reader (the CLI)
can only ever see the last successfully WRITTEN heartbeat — it has no way to know a more
recent in-memory state exists if that write failed. Only the in-process caller holding the
`ExecutionWatchdog` can see `heartbeat_dirty` and resolve it. README and the coordinator's
own docstring both state this explicitly now.

---

## NEW-05 — PromptGraph adapter unsafe on non-list iterables

**Finding.** `for entry in results or []:` assumed `results` was falsy-checkable in a
useful way and, worse, the closing `note` string called `len(results or [])` — which
raises `TypeError` on a generator (no `__len__`). Separately, only the INITIAL
`search()` call was wrapped in `try/except`; an exception raised DURING iteration of a
lazily-evaluated generator (deferred past the call itself) escaped uncaught. An infinite
generator of empty-content entries never tripped the budget-based `break`, so nothing
bounded consumption if the adapter ignored `limit`.

**Reproduction.**
```python
def search(query, limit=10):
    yield {"content": "first"}
    raise RuntimeError("boom mid-iteration")


provider.request(request)  # RuntimeError escapes, uncaught, crashes the caller
```
```python
def search(query, limit=10):
    while True:
        yield {"content": ""}  # never triggers the budget break


provider.request(request)  # would hang forever without a bound
```

**Broken invariant.** "Optional sibling integration must never crash or hang core
AgentGear" (the entire point of `PromptGraphContextProvider`'s fallback design).

**Root cause.** The `try/except` boundary was drawn too narrowly (around the call, not the
full consumption), and `len()` was called on a value never guaranteed to support it.

**Decision.** Widen the safety boundary to cover search() + iteration + per-item
conversion as ONE unit; bound consumption independently via `itertools.islice(results,
search_limit)` regardless of whether the far side honors `limit` itself; replace the
`len(results or [])` note with a `considered` counter incremented per actually-consumed
item (bounded by `search_limit`, so it never requires materializing or measuring the full
source).

**Fix.** `src/agentgear/context_provider.py::PromptGraphContextProvider.request()`.

**Regression.** `tests/test_context_provider.py` — list/tuple/generator all work; an
effectively-infinite generator is bounded by `search_limit` (no hang); exceptions before
the first yield AND after one yield both fall back safely; malformed items (None, int,
bare string, dict missing `content`, an item whose `__str__` itself raises) never crash;
`None`/non-iterable return values fall back safely; Unicode content handled;
`budget_tokens=1` and `search_limit=1` both work; the AG-08 budget invariant re-verified
across the normal, fallback, and mid-iteration-failure paths.

**Black-box.** Scenarios 18–21 (list, generator, generator failure fallback, infinite
iterable bounded by `search_limit`).

---

## NEW-06 — checkpoint storage was O(N²) over an execution's lifetime

**OLD COMPLEXITY:** every `append()` read the ENTIRE existing history (JSON parse), added
one entry, re-serialized the WHOLE (now one-longer) list, and atomically rewrote it —
O(history length) work per append, O(N²) total work across N appends.

**NEW STORAGE ARCHITECTURE:** segmented directory per execution
(`{execution_id}.checkpoints/segment-NNNNNN.json`), each segment holding at most
`_SEGMENT_CAPACITY` (100) checkpoints. `append()` only ever reads/rewrites the CURRENT
(newest, not-yet-full) segment, rolling over to a fresh one once full. `latest()` only
ever needs the single newest segment. `all()` still reads every segment (unavoidable —
returning full history requires touching all of it) but as a simple linear concatenation.

**PER-APPEND BOUND:** O(`_SEGMENT_CAPACITY`) — a fixed constant, independent of total
history length. Proven structurally (not by wall-clock timing, which varies by
machine/antivirus/filesystem) in `test_structural_per_append_cost_is_bounded_by_
segment_capacity_not_history_length`: after 500 appends, exactly 5 segment files exist,
each holding at most 100 entries.

**DURABILITY:** unchanged per-segment — each segment is its own `SafeJsonStore` (atomic
replace, fsync, real cross-process file lock). Every successfully-acknowledged append
remains recoverable (re-verified: `all()` after 500 appends returns exactly 500, in
order, and `latest()` returns the 500th).

**MULTIPROCESS:** re-verified with REAL spawned processes at 2, 5, and 10 concurrent
appenders, sized so they collectively span multiple segment rollovers — the new race
surface this design introduces (two processes both deciding to roll over to the same next
segment). No entry lost at any concurrency level; `SafeJsonStore.update()`'s existing
lock-then-re-read-then-mutate pattern already makes the actual append correct regardless
of what a process's earlier "which segment is active" peek assumed — a segment may rarely
end up modestly over `_SEGMENT_CAPACITY` under a genuine race (harmless, documented), but
no entry is ever lost or placed in the wrong file.

**CORRUPTION:** re-verified for the first, middle, and last of three full segments —
quarantining the corrupt one never touches (not even re-reads/re-writes) any other,
healthy segment; `all()` reports the corruption via `CorruptStorageError` (same as before,
whole-history-fails-on-any-corruption contract preserved) but the quarantine itself is
now strictly more surgical than the single-file design ever could be.

**PATH SAFETY:** re-verified — the new segment directory and every segment file inside it
stay contained under `trusted_root`; a malicious `execution_id` (`../escape`,
`a/../../escape`) is rejected with zero artifacts created outside `state_dir`, exactly as
before.

**MIGRATION:** none. v0.1.0 has not shipped; the single-file format is replaced outright,
not dual-supported (an explicit brief instruction — no half-support for two formats).

**Regression.** `tests/test_checkpoints.py` — structural 500-append proof, `latest()`
reads only the newest segment (proven by corrupting an OLDER segment and confirming
`latest()` is unaffected), rollover-at-exactly-capacity, corrupt-first/middle/last-segment.
`tests/test_persistence_concurrency.py` — 2/5/10 real spawned-process appenders.

**Black-box.** Scenarios 22–24 (append 500, `all()` returns all 500, `latest()` correct).

**Self-adversarial addendum (discovered during this round's own verification, not
pre-specified by the audit brief):** running the real-multiprocess regression at 10
concurrent appenders surfaced a genuine NEW race the segmented design introduced.
`append()`'s target-segment peek does an UNLOCKED `store.read()` that can interleave with
another process's LOCKED `write_atomic()` (temp-write + atomic replace) on the same
segment file. On Windows this transiently raises `PermissionError` while the replace is
in flight — but `SafeJsonStore.read()` treated ANY `OSError` from the underlying
`read_text()` call as evidence of corruption and quarantined (renamed away) a perfectly
healthy segment file. Fixed by adding a bounded retry specifically around the raw file
read (mirroring the existing `_replace_with_retry` pattern already used for writes) —
transient `OSError` is retried a short, bounded number of times before falling through to
quarantine; a genuinely corrupt/unreadable file after retries is still caught exactly as
before. Fix: `src/agentgear/safe_json_store.py::SafeJsonStore._read_text_with_retry`.
Regression: `tests/test_safe_json_store.py::test_read_retries_transient_permission_error_
instead_of_quarantining` and `test_read_gives_up_after_exhausting_retries_and_quarantines`
(the negative case — a persistent failure must still quarantine). Re-ran the 10-worker
multiprocess test 3 additional times after the fix with zero failures (was reliably
reproducible before it).

---

## NEW-07 — `RecoveryAttempt` had zero domain validation

**Reproduction.**
```python
RecoveryAttempt(reason="", strategy="", attempt_number=True, result="not-an-enum", at_seconds=-5.0)
# accepted, no error
```

**Fix.** `src/agentgear/models.py::RecoveryAttempt.__post_init__` — `reason`/`strategy`
non-blank, `attempt_number` a strict positive int (bool excluded), `result` a real
`RecoveryResult`, `at_seconds` finite non-negative. All raise `InvalidObservationError`
(this is a watchdog/observation model, not a `TaskProfile`-family one, so it does NOT
reuse `_validate_positive_int`, which raises `TaskProfileError` — one consistent error
family per model).

**Zero-attempt episode.** Explicitly NOT regressed: `RecoveryEpisode(attempts=(), outcome=BLOCKED, ...)`
remains valid (a budget denial before any attempt ran legitimately closes an episode with
zero attempts) — `RecoveryAttempt`'s new validation only constrains attempts that
actually exist; no `len(attempts) >= 1` invariant was added anywhere.

**Regression.** `tests/test_round4_hardening.py` — full boundary table (blank
reason/strategy, bad attempt_number including bool, wrong result type, bad timestamp),
plus the zero-attempt-BLOCKED-episode invariant re-verified both via direct model
construction and through the real coordinator path.

**Black-box.** Scenarios 25–26.

---

## NEW-08 — path/identifier UX and retry behavior

**NONEXISTENT ROOT:** `HeartbeatWriter.read()`/`write()` on a `state_dir` that doesn't
exist yet used to raise a misleading `PathEscapeError` — and, far more severely, the FIRST
EVER write to a brand-new (even single-level) nonexistent `state_dir` was completely
broken the same way, since `SafeJsonStore.__init__` runs an unconditional containment
check at construction time, before `write()`'s own logic ever runs.

**Root cause.** `assert_path_family_contained()` resolved `trusted_root` directly via
`os.path.realpath()` with no "walk up to nearest existing ancestor" handling — only the
TARGET path had that logic. When both root and target are (independently) nonexistent,
`os.path.realpath()`'s behavior for fully-nonexistent multi-level paths is not consistent
enough to compare the two, producing spurious escape errors — or, given different path
shapes, could in principle have gone the other way (a real escape resolving as
"contained").

**Decision.** Added `resolve_via_nearest_existing_ancestor()`: walks up to the nearest
EXISTING ancestor, canonicalizes only that real portion (still following any actual
symlink/junction in it), and re-appends the nonexistent trailing components VERBATIM
(never fed to `realpath` — a nonexistent component cannot itself be a symlink, so there is
nothing to resolve). Applied consistently to BOTH root and target in
`validate_contained()`/`assert_path_family_contained()`. This fixes the crash AND is
strictly more correct than before (previously undefined behavior for the nonexistent
case; now well-defined and symlink-aware regardless of what exists yet). Re-verified:
traversal/junction/symlink rejection is UNCHANGED and still holds, including when combined
with a not-yet-created `state_dir`.

**EXECUTION ID:** a new `validate_persistence_safe_id()` (Option A from the brief — a
direct bounded-safe-filename-component check, not an ID-derivation scheme) rejects
non-blank-but-empty, over-150-character, and filename-illegal-character (`<>:"/\|?*` plus
control characters) identifiers, called at the single choke point each of
`HeartbeatWriter`/`CheckpointStore` already funnels every `execution_id` through. Windows
reserved device names (CON/NUL/...) and trailing dot/space edge cases were deliberately
NOT chased — real but much rarer, and out of proportion for v0.1.0 given the actually-
reproduced failures were length and illegal characters.

**RETRY POLICY:** unchanged (still retries `PermissionError` for atomic-replace, and waits
up to the existing 10s lock-acquisition timeout) — no errno-classification framework was
built (explicitly out of scope). Instead, the PRACTICAL trigger for ever reaching a
permanent failure in that retry loop — a pathologically long `execution_id` — is now
caught immediately by `validate_persistence_safe_id()`, before any filesystem call.
Confirmed: a 300-character `execution_id` now fails in <1s (was ~10s).

**PATH SECURITY:** re-verified unaffected — traversal rejection, and zero artifacts
escaping `state_dir`, hold in every combination (existing root + malicious id, nonexistent
root + malicious id).

**Regression.** `tests/test_round4_hardening.py` — brand-new nested nonexistent state_dir
write succeeds; read on nonexistent root returns "no state" without creating anything;
traversal still rejected when state_dir itself doesn't exist yet; 300-char id fails in
<1s; illegal-character/oversized ids rejected for both heartbeat and checkpoints; normal
ids unaffected.

**Black-box.** Scenarios 27–28.

---

## NEW-09 — release metadata / sdist / security contact

**PYPROJECT URLS:** `Homepage`/`Issues` corrected from the dead `hermes-oss/agentgear` to
the real `https://github.com/Human-Weapon/AgentGear`(`/issues`). Author corrected from
`{name = "HERMES OSS", email = "oss@hermes.local"}` (a fabricated domain) to
`{name = "Human-Weapon"}` — no email invented, per the explicit "do not invent an email
address" instruction.

**SECURITY CONTACT:** `SECURITY.md`'s "address TBD once the repo is published" replaced
with GitHub's private vulnerability reporting
(`https://github.com/Human-Weapon/AgentGear/security/advisories/new`) — a real, usable,
already-available mechanism requiring no new infrastructure.

**WHEEL METADATA:** re-verified by extracting the built wheel's `METADATA` and asserting
it contains the real repo URL and neither `hermes-oss/agentgear` nor the fake `.local`
email.

**SDIST CONTENTS:** were MISSING `SECURITY.md`, `CHANGELOG.md`, `CONTRIBUTING.md` entirely
(setuptools only auto-includes `README.md`/`LICENSE`) — despite `README.md` itself linking
to `SECURITY.md` and `CONTRIBUTING.md`. Added `MANIFEST.in` explicitly including those
three files plus `docs/**/*.md` (referenced by `CHANGELOG.md`). Re-verified: rebuilt
sdist now contains all required docs.

**Regression.** New CI step "Inspect release metadata" in `.github/workflows/ci.yml`
(runs after `python -m build`, inspects the actual built wheel `METADATA` and sdist
`tarfile` contents programmatically — no network dependency).

**Black-box.** Scenarios 29–30.

---

## NEW-10 — audit traceability

`docs/audits/index.md` created: maps every finding from all four rounds (AG-01 through
AG-09, C1/C2, H1–H4, M1–M10 including the explicit M10 boundary-semantics table, L1–L8,
latency_weight, AUDIT3-01 through AUDIT3-06, NEW-01 through NEW-10) to its classification,
fix commit, and regression test location. M10's exact definition ("N allowed, N+1
rejected" per counter, enumerated individually for all five loop-guard limits) is recorded
there in full, closing the traceability gap this finding identified.

---

## Public boundary + validate-before-mutate sweep (section 14/15/33, beyond the 10 pre-specified findings)

Systematically reviewed every state-mutating method on the public `ExecutionWatchdog`
surface for "can validation/persistence fail after an earlier mutation has already
happened," and every public constructor on the exported API (`ExecutionWatchdog`,
`ExecutionBudgetLedger`, the `Policy`/`config` dataclass tree, the `models` dataclasses,
`api.analyze`/`api.plan`) for missing input validation. Two additional gaps were found this
way, neither pre-specified by the audit brief's 10 findings:

**checkpoint() persisted after mutating in-memory state, not before.**

**Reproduction.**
```python
w = ExecutionWatchdog("e1", Policy.default(), state_dir=some_dir)
w.start(task="t", at_seconds=0.0)
w._checkpoint_store.append = lambda cp: (_ for _ in ()).throw(OSError("disk full"))
w.checkpoint(at_seconds=1.0, phase="p1")  # raises OSError, but...
w._checkpoints  # ...already contains the phantom checkpoint that was never durably written
```

**Broken invariant.** `_transition_to_blocked()` reads `self._checkpoints[-1]` as
`BlockedReport.last_successful_checkpoint`, and `_write_heartbeat()` reads it for
`pending_work` — both would reference a checkpoint that does not exist on disk and cannot
survive a crash, after a persistence failure the caller was explicitly told about via the
raised exception.

**Fix.** `src/agentgear/watchdog/coordinator.py::ExecutionWatchdog.checkpoint` — reordered
to call `self._checkpoint_store.append(cp)` (the fallible, durable operation) BEFORE
`self._checkpoints.append(cp)` (the in-memory mirror), consistent with every other public
mutating method's validate/persist-then-commit ordering (NEW-03).

**Regression.**
`tests/test_round4_hardening.py::test_checkpoint_persists_before_updating_in_memory_cache`
— injects a failing `_checkpoint_store.append`, asserts `checkpoint()` raises and
`self._checkpoints` stays empty.

**execution_id's filesystem-safety was only discovered on the first durable write, not at construction.**

**Reproduction.**
```python
w = ExecutionWatchdog("bad<id>", Policy.default(), state_dir=some_dir)  # succeeds silently
w.start(task="t", at_seconds=0.0)  # raises InvalidIdentifierError from inside _write_heartbeat
w._sm.state  # RUNNING -- the state transition and budget commit already happened
w.heartbeat_dirty  # True, permanently -- sync_heartbeat() will keep failing forever,
# since the identifier problem is not transient like a full disk
```
This is a strictly worse outcome than the transient-failure case NEW-04's dirty/sync model
was designed for: a transient failure recovers via `sync_heartbeat()`; a permanently-unsafe
identifier can never recover, yet the watchdog silently accepted the identifier at
construction and only revealed the problem after an irreversible domain mutation.

**Fix.** `src/agentgear/watchdog/coordinator.py::ExecutionWatchdog.__init__` — when
`state_dir` is provided, calls `validate_persistence_safe_id("execution_id", execution_id)`
before any `self.x = ...` assignment, alongside NEW-02's other constructor checks. Scoped
strictly to the durable case: an in-memory-only watchdog (`state_dir=None`) never touches
the filesystem and must not reject an execution_id purely for being filesystem-unsafe.

**Regression.**
`tests/test_round4_hardening.py::test_constructor_eagerly_rejects_filesystem_unsafe_execution_id_when_durable`
and `test_constructor_allows_filesystem_unsafe_execution_id_without_state_dir`.

**Black-box.** Both fixes covered by the 15-scenario black-box script run against both the
installed wheel and sdist (see Verification below).

---

## Cross-cutting notes

No test was weakened, removed, or reclassified as "intentional" to make it pass. Every fix
above has an independent reproducer that failed against baseline
`bc531510c93b5a390132b95374efbcd228fb94c8` before the corresponding change landed. No
distributed transactions, database, generic event-sourcing framework, or DI framework was
introduced — the durability model (NEW-04) is a documented dirty-flag-plus-idempotent-
resync, and the storage redesign (NEW-06) is bounded flat files, both scoped to what was
actually needed.
