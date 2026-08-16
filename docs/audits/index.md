# AgentGear Audit Index

Traceability map for every finding raised across six independent adversarial
audit rounds. "Round" = which audit surfaced it; "Fix commit" = the AgentGear
remediation commit that closed it (short hash, on `main`). See each round's
own document (`docs/audits/remediation-round-N.md`, N=3,4,5,6 -- rounds 1 and
2 predate this index and are summarized here from their commit messages) for
full reproduction/root-cause/decision detail.

Commit reference:
- `5330554` — v0.1.0 release candidate (pre-audit baseline)
- `dbdcaa9` — Remediation Round 1
- `51bfac8` / `1cff32b` — Remediation Round 2
- `bc53151` — Remediation Round 3
- `f8573ef` — Remediation Round 4
- `a7e59b7` — Remediation Round 5 (fix commit); `8027e9e` finalized this index for it; `68c829d` corrected AG5-09's own description afterward
- (this round) — Remediation Round 6

## Round 1 (baseline `5330554`)

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| AG-01 | Stall detection never fires for an execution that never made progress (no `started_at` boundary) | FIXED | `dbdcaa9` | `tests/test_stall_detection.py::test_ag01_*` |
| AG-02 | Stall signals computed over entire activity history instead of scoped to the progress boundary | FIXED | `dbdcaa9` | `tests/test_stall_detection.py::test_ag02_*` |
| AG-03 | A maxed-out individual risk signal could be diluted by the blended score | FIXED | `dbdcaa9` | `tests/test_planning.py::test_ag03_*`, `tests/test_routing.py::test_critical_risk_*` |
| AG-04 | Escalation cost check compared only the new operation's cost, not cumulative spend | FIXED | `dbdcaa9` | `tests/test_budget.py::test_cumulative_reservations_are_denied_once_ceiling_reached` |
| AG-05 | No single public coordinator enforcing the state machine end-to-end | FIXED | `dbdcaa9` | `tests/test_coordinator.py` (entire file is the regression) |
| AG-06 | Progress/escalation signals accepted invalid values (negative, NaN/Infinity, out-of-range) | FIXED | `dbdcaa9` | `tests/test_stall_detection.py::test_activity_record_rejects_*` |
| AG-07 | Valid JSON but invalid schema not distinguished from corruption | FIXED | `dbdcaa9` | `tests/test_heartbeat.py`, `tests/test_checkpoints.py` (schema-integrity sections) |
| AG-08 | `ContextProvider` budget accounting and constraints provenance not honest | FIXED | `dbdcaa9` | `tests/test_context_provider.py` (AG-08 section) |
| AG-09 | `BLOCKED` reachable without a validated, non-blank report | FIXED | `dbdcaa9` | `tests/test_blocked.py` (AG-09 section) |
| P3 | `latency_weight` accepted but had no observable effect on routing | FIXED | `dbdcaa9` | `tests/test_routing.py::test_latency_weight_*` |
| — | Standalone test suite depended on sibling-project environment state | FIXED | `dbdcaa9` | (test-infra fix, no dedicated regression file) |

## Round 2 (baseline `51bfac8`)

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| C1 | Recovery attempt/strategy state leaked across STALL episodes instead of resetting per-episode | FIXED | `1cff32b` | `tests/test_coordinator.py::test_five_successful_recovery_episodes_each_start_fresh` |
| C2 | `max_recovery_attempts=N` off-by-one: increment-before-check meant N-1 real attempts | FIXED | `1cff32b` | `tests/test_recovery.py::test_can_start_recovery_attempt_boundary_table`, `tests/test_coordinator.py::test_max_recovery_attempts_one_canonical_regression` |
| H1 | `AgentAssignment.count` accepted `bool` (subclass-of-int trap) | FIXED | `1cff32b` | `tests/test_models.py::test_agent_assignment_rejects_non_strict_positive_int_count` |
| H2 | `ContextRequest.constraints` had no type contract (bare string iterable char-by-char) | FIXED | `1cff32b` | `tests/test_context_provider.py::test_context_request_rejects_invalid_constraints_shape` |
| H3 | COMPLETED evidence validated then discarded, never retrievable | FIXED | `1cff32b` | `tests/test_state_machine.py::test_completed_evidence_is_preserved_in_history` |
| H4 | Schema-valid-but-domain-invalid heartbeat/checkpoint leaked raw domain exceptions instead of quarantining | FIXED | `1cff32b` | `tests/test_heartbeat.py::test_whitespace_only_nullable_field_is_quarantined_not_raised_raw` |
| M7 | `ComplexityAssessment`/`RiskAssessment.score` accepted out-of-range/NaN/Infinity/bool | FIXED | `1cff32b` | `tests/test_models.py::test_*_rejects_impossible_scores` |
| M8/L8 | `StallDetector` used `>=` at the progress boundary while `ProgressTracker` used `>` | FIXED | `1cff32b` | `tests/test_stall_detection.py::test_m8_*` |
| M9 | `BlockedReport` validated only by its builder function, not at the model boundary itself | FIXED | `1cff32b` | `tests/test_blocked.py::test_blocked_report_direct_construction_*` |
| M10 | **LoopGuard / global limit boundary semantics.** See dedicated section below. | FIXED | `1cff32b` | `tests/test_recovery.py::test_loop_guard_total_attempts_boundary_table`, `test_can_start_recovery_attempt_boundary_table` |
| L2 | Suspected factor-dict key mismatch between `analysis.py` producers and `routing.py`/`planning.py` consumers | NOT REPRODUCIBLE | — | `tests/test_planning.py::test_l2_individual_factor_key_forces_multi_agent_below_blended_thresholds` (isolating regression proving no mismatch, and would catch a future one) |
| M1 | `critical_risk` threshold=0.0 semantics | INTENTIONAL (Round 2), **documentation corrected in Round 3 as AUDIT3-01** | `bc53151` | `tests/test_round2_intentional_architecture.py::test_m1_*` |
| M2 | Equal routing thresholds collapse a tier | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_m2_*` |
| M3 | `multi_agent_*_threshold=0.0` forces staffing unconditionally | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_m3_*` |
| M4/M5/M6 | Role-specific fixed reasoning (Planner LOW / Judge HIGH / Reviewer MEDIUM) not coupled to Builder | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_m4_m5_m6_*` |
| L1 | Blended-score vs. individual-signal critical-risk overrides look duplicative | INTENTIONAL (defense-in-depth) | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_l1_*` |
| L3 | `architectural_impact` contributes to both complexity and risk | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_l3_*` |
| L5 | `BLOCKED.is_terminal == False` | INTENTIONAL (BLOCKED→RECOVERING is legal) | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_l5_*` |
| L6 | Low-level watchdog primitives exported alongside the coordinator | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_l6_*` |
| L7 | Successful repeated identical activity still counts toward circular-attempt detection | INTENTIONAL | `1cff32b` | `tests/test_round2_intentional_architecture.py::test_l7_*` |
| latency_weight | Re-verified post-Round-2-refactor | FIXED (re-confirmed, no regression) | `1cff32b` | `tests/test_routing.py::test_latency_weight_*` |

### M10 in full: LoopGuard / global limit boundary semantics

"M10" is the umbrella label for auditing **every** loop-protection counter
for the same off-by-one class of bug as C2, and defining exactly what `N`
means for each. Resolved contract, "N allowed, N+1 rejected" throughout:

- `max_identical_failures`: N consecutive identical trailing failures tolerated; the (N+1)th triggers the signal.
- `max_recovery_attempts`: N attempts permitted within one recovery **episode**; the (N+1)th `begin_recovery()` call in that episode is denied.
- `max_no_progress_cycles`: N no-progress activity cycles since the boundary tolerated; exceeding N trips the global bound.
- `max_total_attempts`: N total attempts across the **whole execution** (all episodes combined); attempt N+1 trips it, never resettable by episode success.
- `max_model_escalations`: N escalations permitted execution-wide; escalation N+1 trips it, unaffected by recovery episodes.

Verified consistent across `StallDetector`, `LoopGuard`, and
`ExecutionWatchdog` (no path reinterprets the limit independently) via
`tests/test_recovery.py`'s boundary-table tests and
`tests/test_coordinator.py`'s public-API-level canonical regressions.

## Round 3 (baseline `1cff32b`, verdict C — fix before promoting: 0 P0, 0 P1, 4 P2, 2 P3, 1 P4)

Full writeup: `docs/audits/remediation-round-3.md`.

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| AUDIT3-01 | `critical_risk` threshold=0.0 documented as "any nonzero" when it actually means "unconditional" | FIXED (documentation) | `bc53151` | `tests/test_round2_intentional_architecture.py::test_m1_critical_risk_boundary_table_per_signal` |
| AUDIT3-02 | COMPLETED evidence with a mixed valid/invalid tuple silently filtered instead of rejected | FIXED | `bc53151` | `tests/test_state_machine.py::test_completed_evidence_mixed_valid_and_invalid_is_rejected_not_truncated` |
| AUDIT3-03 | `begin_recovery()`'s `except Exception` swallowed arbitrary programming errors as normal BLOCKED | FIXED | `bc53151` | `tests/test_coordinator.py::test_unexpected_programming_bug_in_recovery_engine_propagates_not_blocked` |
| AUDIT3-04 | `frozen=True` dataclasses with nested mutable dict fields could be mutated post-construction | FIXED | `bc53151` | `tests/test_round3_hardening.py` (AUDIT3-04 section) |
| AUDIT3-05 | `ExecutionWatchdog`/`ExecutionBudgetLedger` single-writer contract undocumented | FIXED (documentation, P3) | `bc53151` | (docstring/README only, no test) |
| AUDIT3-06 | `RecoveryEpisode` had zero field validation | FIXED (P3) | `bc53151` | `tests/test_round3_hardening.py` (AUDIT3-06 section) |

## Round 4 (baseline `bc53151`, verdict C — fix before promoting: 0 P0, 0 P1, 6 P2, 4 P3, 2 P4)

Full writeup: `docs/audits/remediation-round-4.md`.

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| NEW-01 | `RiskAssessment`/`ComplexityAssessment` factor VALUES unvalidated (NaN defeats `signal >= threshold`) | FIXED | `f8573ef` | `tests/test_round4_hardening.py` (NEW-01 section) |
| NEW-02 | `ExecutionWatchdog.__init__` accepted invalid tier/reasoning/policy/budget, failing later with a raw error | FIXED | `f8573ef` | `tests/test_round4_hardening.py` (NEW-02 section) |
| NEW-03 | Rejected operations (e.g. malformed COMPLETED evidence) still advanced the coordinator's clock | FIXED | `f8573ef` | `tests/test_round4_hardening.py` (NEW-03 section) |
| NEW-04 | Heartbeat write failure left in-memory state (COMPLETED) and durable heartbeat (REVIEWING) split-brain, with no recovery path | FIXED (commit + dirty/sync durability model) | `f8573ef` | `tests/test_round4_hardening.py` (NEW-04 section) |
| NEW-05 | `PromptGraphContextProvider` assumed `len(results)` and didn't catch mid-iteration exceptions on a generator | FIXED | `f8573ef` | `tests/test_context_provider.py` (NEW-05 section) |
| NEW-06 | Checkpoint storage read+rewrote the ENTIRE history on every append (O(N) per append, O(N²) total) | FIXED (segmented storage) | `f8573ef` | `tests/test_checkpoints.py` (NEW-06 section) |
| NEW-07 | `RecoveryAttempt` had zero field validation | FIXED (P3) | `f8573ef` | `tests/test_round4_hardening.py` (NEW-07 section) |
| NEW-08 | Nonexistent `state_dir` produced a misleading `PathEscapeError`; a first write to a brand-new dir was completely broken; a pathologically long `execution_id` entered a ~10s lock-retry loop | FIXED | `f8573ef` | `tests/test_round4_hardening.py` (NEW-08 section) |
| NEW-09 | Release metadata pointed at a dead `hermes-oss/agentgear` repo and a fake `.local` email; sdist was missing SECURITY.md/CHANGELOG.md/CONTRIBUTING.md | FIXED (P4) | `f8573ef` | CI step "Inspect release metadata" in `.github/workflows/ci.yml` |
| NEW-10 | This index did not exist | FIXED | `f8573ef` | this file |

**Self-adversarial additions (task #67 / section 33, discovered during this round's own verification, not pre-specified by the audit brief):**

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| R4-SA-01 | `checkpoint()` mutated in-memory `self._checkpoints` before the durable `CheckpointStore.append()` call, so a persistence failure left a phantom in-memory checkpoint | FIXED | `f8573ef` | `tests/test_round4_hardening.py::test_checkpoint_persists_before_updating_in_memory_cache` |
| R4-SA-02 | `ExecutionWatchdog.__init__` accepted a filesystem-unsafe `execution_id` even with `state_dir` set; only discovered on the first `start()`, after an irreversible RUNNING transition + budget commit | FIXED | `f8573ef` | `tests/test_round4_hardening.py::test_constructor_eagerly_rejects_filesystem_unsafe_execution_id_when_durable`, `test_constructor_allows_filesystem_unsafe_execution_id_without_state_dir` |

---

## Round 5 (baseline `f8573ef`, verdict C — fix before promoting: 0 P0, 0 P1, 6 P2, 5 P3, 0 P4)

Full writeup: `docs/audits/remediation-round-5.md`.

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| AG5-01 | `CheckpointStore.append()`'s segment-capacity check was advisory, not a hard bound, under real concurrency | FIXED (execution-scoped lock) | `a7e59b7` | `tests/test_persistence_concurrency.py::test_checkpoint_segment_capacity_is_a_hard_cap_under_real_concurrency`, `tests/test_checkpoints.py` (AG5-01 section) |
| AG5-02 | `record_recovery_result()` mutated recovery state before validating `result`/`evidence` | FIXED | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-02 section) |
| AG5-03 | `ActivityRecord` accepted non-bool `succeeded`/`is_trivial` and malformed `error`; heartbeat dirty flag didn't cover construction failures | FIXED | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-03 section) |
| AG5-04 | `ExecutionState` subclasses `str`; `advance("testing")` poisoned the state machine with a raw string | FIXED | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-04 section) |
| AG5-05 | Heartbeat went stale (dirty=False but disk out of date) after ordinary `record_activity()`/`checkpoint()` calls | FIXED | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-05 section) |
| AG5-06 | Relative `state_dir` silently rebound to the process's CWD at operation time, not construction time | FIXED | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-06 section) |
| AG5-07 | `PromptGraphContextProvider` echoed a raw adapter exception message (potential secret leak) into `ContextPackage.note` | FIXED (P3) | `a7e59b7` | `tests/test_context_provider.py::test_promptgraph_provider_search_exception_message_is_never_echoed` |
| AG5-08 | Stale `hermes-oss/promptgraph` link in current-facing README.md | FIXED (P3) | `a7e59b7` | `tests/test_release_metadata.py` |
| AG5-09 | `docs/audits/index.md` shipped with unfinalized `(this round)` placeholders for Round 4's already-fixed findings | FIXED (two-commit workflow) | `a7e59b7` | `tests/test_release_metadata.py::test_audit_index_has_no_unfinalized_placeholder_shas` |
| AG5-10 | `state_dir=""` silently disabled persistence instead of raising | FIXED (P3) | `a7e59b7` | `tests/test_round5_hardening.py` (AG5-10 section) |
| AG5-11 | CI's "standalone" check incorrectly asserted optional siblings must be ABSENT, not merely optional | FIXED (P3) | `a7e59b7` | `tests/test_standalone.py::test_core_plan_pipeline_unaffected_by_an_actually_importable_sibling`, `.github/workflows/ci.yml` |

**Self-adversarial additions (cross-cutting enum sweep, discovered during this round's own
verification, not pre-specified by the audit brief):**

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| R5-SA-01 | `ExecutionStateMachine.__init__` accepted `state="running"` directly (same str-enum trap as AG5-04, at the constructor rather than `transition()`) | FIXED | `a7e59b7` | `tests/test_round5_hardening.py::test_state_machine_constructor_rejects_raw_string_state` |
| R5-SA-02 | `ExecutionBudgetLedger.reserve(kind=...)` accepted a raw string for `ReservationKind`, poisoning the resulting `BudgetReservation.kind` | FIXED | `a7e59b7` | `tests/test_round5_hardening.py::test_budget_ledger_reserve_rejects_raw_string_kind` |

---

## Round 6 (baseline `68c829d`, verdict D — not release ready: 0 P0, 2 P1, 1 P2, 0 P3, 0 P4)

Full writeup: `docs/audits/remediation-round-6.md`.

| ID | Description | Classification | Fix commit | Regression test(s) |
|---|---|---|---|---|
| AG6-01 | Public watchdog events (`record_activity`, `record_progress`, `record_escalation`, `checkpoint`, `advance`) accepted outside the lifecycle they require -- before `start()`, after `COMPLETED`, or while `BLOCKED`; `advance()` could also bypass `start()`/`record_recovery_result()` entirely | FIXED (P1) | (this round) | `tests/test_round6_hardening.py` (AG6-01 section, full state x event admission matrix) |
| AG6-02 | The configured persistence root itself (not just a child path beneath it) could be replaced by a Windows junction/symlink after construction, bypassing containment entirely | FIXED (P1, `PersistenceRoot` root-identity guard) | (this round) | `tests/test_round6_hardening.py` (AG6-02 section, real `mklink /J` tests) |
| AG6-03 | An existing regular file supplied as `state_dir` was accepted until the first heartbeat write raised a raw `FileExistsError` after `start()` had already committed `RUNNING` | FIXED (P2) | (this round) | `tests/test_round6_hardening.py` (AG6-03 section) |

---

*This index is maintained alongside each remediation round. When starting a
new round, add a new section above rather than rewriting history in the
existing rows.*
