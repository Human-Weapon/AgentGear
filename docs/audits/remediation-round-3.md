# AgentGear Remediation Round 3

Third independent adversarial audit against baseline `1cff32b6ed6bf46163423d86d870ee1c8ddee314`
(Remediation Round 2). Verdict: **C — FIX BEFORE PROMOTING** (0 P0, 0 P1, 4 P2, 2 P3, 1 P4).
This document records the decisions made in response, for future agents who might
otherwise rediscover — or accidentally reverse — the same conclusions.

## AUDIT3-01 — `critical_risk` threshold=0.0 semantics

**Finding.** `CriticalRiskPolicy(security_impact_at=0.0)` was documented (Round 2 / M1) as
"any nonzero signal forces the floor." The actual comparison is `signal >= threshold`, so
`0.0 >= 0.0` is `True` — the floor fires even on a genuinely zero-risk task. The
documentation was wrong, not the code.

**Reproduction.**
```python
policy = Policy(
    critical_risk=CriticalRiskPolicy(
        security_impact_at=0.0, data_impact_at=0.0, irreversibility_at=0.0
    )
)
zero_risk = RiskAssessment(
    score=0.0,
    level=RiskLevel.MINIMAL,
    factors={"security_impact": 0.0, "data_impact": 0.0, "irreversibility": 0.0},
)
critical_signal_reasons(zero_risk, policy)  # -> fires, all three reasons present
```

**Root cause.** The Round 2 note characterized the boundary qualitatively without
reproducing the exact `signal == threshold == 0.0` case.

**Why previous tests missed it.** The existing test only checked a *nonzero* signal
(`0.001`) against `threshold=0.0`, which is consistent with both "any nonzero" and
"unconditional" — it can't distinguish the two.

**Decision.** Preserve the inclusive `signal >= threshold` comparison as-is (explicitly
directed: do not change `>=` to `>`, which would shift behavior at the default `0.85`
boundary too). Fix the documentation instead: `threshold=0.0` means the floor applies
**unconditionally**, on every task including an exactly-zero one — not "nonzero-only."

**Alternatives considered.** Introducing a sentinel (`None` = disabled, `0` = nonzero-only)
was explicitly rejected per the remediation brief — it would add a second meaning to the
same field with no discoverable signal for which one applies, and no evidence anyone
needs it.

**Fix.** `CriticalRiskPolicy`'s docstring corrected in `src/agentgear/config.py`.

**Regression test.** `tests/test_round2_intentional_architecture.py::
test_m1_critical_risk_threshold_zero_means_unconditional_always_apply` and
`test_m1_critical_risk_boundary_table_per_signal` (parametrized boundary table across all
three signals: below/at/above threshold, including the exact `0.0`/`0.0` case).

**Known limitation / trust boundary.** A deployer who wants "sensitive to any real signal
but not a genuinely zero one" needs a threshold strictly above `0.0` (the smallest value
their integration can distinguish from zero) — `0.0` itself is not that setting.

---

## AUDIT3-02 — COMPLETED evidence silently filtered instead of validated

**Finding.** `ExecutionStateMachine.transition()` filtered `evidence` down to non-blank
strings before checking non-emptiness. A wholly-invalid tuple (`(42,)`) was correctly
rejected (filters to empty, trips the "no evidence" check) — but a **mixed** tuple like
`("done", 42)` silently dropped the `42` and stored `("done",)` with no error at all.

**Reproduction.**
```python
sm.transition(ExecutionState.COMPLETED, at_seconds=2.0, evidence=("done", 42))
sm.history[-1].evidence  # -> ("done",) — accepted, no error, 42 vanished silently
```

**Root cause.** `clean_evidence = tuple(e for e in evidence if isinstance(e, str) and e.strip())`
filtered rather than validated. The only gate was "is the *result* non-empty," not "was
every *input* entry valid."

**Why previous tests missed it.** `test_completed_rejects_every_kind_of_meaningless_evidence`
only parametrized over wholly-invalid tuples (`()`, `("",)`, `("   ",)`, `(42,)`) — never
a mix of one valid and one invalid entry, which is exactly the case that exposes
filter-vs-reject.

**Decision.** Evidence is a strict `tuple[str, ...]`, validated **all-or-nothing**: every
entry must be a non-blank string, or the *entire* transition is rejected before any
mutation. Applied uniformly to every transition target (not just COMPLETED) — the only
internal caller that ever passes non-default evidence is `complete()`, so this couldn't
regress anything else, and a single uniform contract for the same field is easier to
reason about than one that behaves differently depending on target state.

Two genuinely different failure modes now get two different exception types:
- A **structurally malformed** entry (wrong type, blank) → `InvalidObservationError`
  (an input-validation failure, independent of what transition was requested).
- A **well-formed but empty** tuple on a COMPLETED transition → `NotCompletedError`
  (a business-rule failure: nothing structurally wrong, just nothing supplied).

**Alternatives considered.** Keeping the filter but requiring 100%-valid-or-reject only
for COMPLETED (leaving other transitions filtered) was rejected — it would leave the same
inconsistent-behavior-by-target-state pattern that made this bug hard to notice in the
first place.

**Fix.** `src/agentgear/watchdog/state_machine.py::ExecutionStateMachine.transition()`.

**Regression tests.** `tests/test_state_machine.py::
test_evidence_with_any_blank_or_non_string_entry_is_rejected_wholesale`,
`test_completed_evidence_mixed_valid_and_invalid_is_rejected_not_truncated` (the exact
AUDIT3-02 case), `test_valid_multi_entry_evidence_is_stored_in_full_no_truncation`,
`test_completed_rejects_a_genuinely_empty_evidence_tuple`,
`test_completed_rejects_every_kind_of_structurally_meaningless_evidence`.

---

## AUDIT3-03 — `begin_recovery()` swallowed arbitrary programming errors

**Finding.** `except Exception as exc:  # RecoveryExhaustedError` around
`RecoveryEngine.next_strategy()` caught *any* exception, not just the one documented in
its own comment. A genuine bug (`AttributeError`, `TypeError`, ...) in a caller-supplied
or buggy `RecoveryEngine` would be silently reclassified as a normal-looking `BlockedReport`
("no recovery strategy available: 'NoneType' object has no attribute...") instead of
crashing the way a real bug should.

**Reproduction.**
```python
w._recovery_engine.next_strategy = lambda *a, **kw: (None).nonexistent_attribute
w.begin_recovery(at_seconds=21.0)
# -> BLOCKED, report.root_cause = "'NoneType' object has no attribute 'nonexistent_attribute'"
```

**Root cause.** Over-broad `except Exception` where `except RecoveryExhaustedError` was
actually intended (per the clause's own comment).

**Why previous tests missed it.** Every existing exhaustion test used the real,
unmodified `RecoveryEngine`, which only ever raises `RecoveryExhaustedError` from that
call site — nothing exercised a different exception type reaching the same catch.

**Decision.** Narrow the clause to `except RecoveryExhaustedError`. Any other exception
now propagates unchanged, before any state/budget/history mutation (verified: strategy
resolution happens *before* the budget is touched, so an exception there leaves zero
side effects — no reservation, no attempt recorded, no episode closed, state unchanged).

**Side-effect safety verification.** After the exception propagates: budget reservations
count unchanged, committed cost unchanged, `_recovery_attempts` length unchanged,
`recovery_history` length unchanged, state remains `STALLED` (never silently advanced),
`blocked_report` stays `None`.

**Broad-exception sweep (Round 3 / section 5.4).** Two other `except Exception` sites
exist in production code:
- `context_provider.py` (`PromptGraphContextProvider.request`) — wraps only the call to
  an *external*, caller-supplied `promptgraph.PromptGraph.memory.search()`. Unlike this
  finding, it never manufactures a look-alike business outcome: it falls back to the safe
  default AND records the raw exception text in the response's `note` field, so a caller
  can tell something broke. **Classification: INTENTIONAL** (untrusted external
  integration boundary, per AG-08's original design).
- `safe_json_store.py` (`_write` cleanup) — wraps the temp-file write sequence, does
  best-effort temp-file cleanup, then **re-raises unchanged** (`raise` with no argument).
  Nothing is hidden. **Classification: INTENTIONAL** (cleanup-then-propagate, not a
  swallow).
- `_sibling_utils.py` (`load_sibling`) — wraps `importlib.import_module()` for loading an
  **optional** HERMES sibling package; explicitly documented as "never raises due to a
  missing or broken sibling." Not core AgentGear logic, not decision-relevant.
  **Classification: INTENTIONAL**.

No other instance of the AUDIT3-03 pattern (business logic silently absorbing a
programmer bug as an expected domain outcome) was found.

**Fix.** `src/agentgear/watchdog/coordinator.py::begin_recovery()`.

**Regression tests.** `tests/test_coordinator.py::
test_expected_recovery_exhaustion_still_reaches_blocked_cleanly` (RecoveryExhaustedError
path still works) and `test_unexpected_programming_bug_in_recovery_engine_propagates_not_blocked`
(AttributeError propagates, zero side effects).

---

## AUDIT3-04 — shallow `frozen=True`: nested mutable dicts

**Finding.** `frozen=True` only blocks reassigning a dataclass's own fields; it does
nothing to the mutable objects those fields point to. `Policy.model_tier_mapping.mapping`
is a plain `dict`, so mutating either the caller's original source dict *or*
`mapping.mapping` directly, after construction, silently redirects routing decisions for
every future call using that same, supposedly-immutable `Policy` object. The auditor
additionally corrected an inaccurate Round 2 claim that `ComplexityAssessment.factors`/
`RiskAssessment.factors` were "diagnostic-only" — `planning.py` and `routing.py` both read
individual factor keys (`ambiguity`, `architectural_impact`, `prior_failures`,
`security_impact`, `data_impact`, `irreversibility`) to make real staffing/critical-risk
decisions, so the same mutation risk applies to real decisions, not just rationale text.

**Reproduction.**
```python
policy = Policy.default()
plan_a = build_execution_plan(profile, c, r, policy)  # resolved_model == "Luna"
policy.model_tier_mapping.mapping["fast"] = "tampered-model"
plan_b = build_execution_plan(profile, c, r, policy)  # resolved_model == "tampered-model"
```

**Root cause.** Systemic pattern: `frozen=True` dataclasses with `dict`-typed fields,
storing the caller's dict by reference with no copy and no freeze.

**Why previous tests missed it.** `test_planning_is_deterministic` calls the pipeline
twice on already-constructed objects with no mutation in between — it tests repeatability
under normal use, not resistance to adversarial post-construction mutation.

**Decision.** Defensively copy the input into a `types.MappingProxyType` wrapping a fresh
`dict` copy, applied in `__post_init__` via `object.__setattr__` (the frozen-dataclass
escape hatch already used throughout this codebase for post-init normalization). This
closes both attack vectors: the original dict can be freely mutated afterward with zero
effect, and `model.mapping[...] = ...` raises `TypeError`. Applied to
`ModelTierMapping.mapping`, `ComplexityAssessment.factors`, `RiskAssessment.factors`.

**Sweep scope (Round 3 / section 21).** All 30 `@dataclass(frozen=True)` classes in
`src/agentgear/` were enumerated and checked for `dict`/`list`/`set`-typed fields. Besides
the three fixed above, exactly one other candidate was found:
`ProgressEvent.evidence: dict[str, object]`. Unlike `factors`/`mapping`, nothing anywhere
reads a key out of it to make a decision — it is a genuinely diagnostic annotation payload
attached at progress-recording time and never consumed. **Classification: deferred /
INTENTIONAL-as-is** (documented directly on the class) — freezing it would add protection
nothing downstream needs, and Round 3's brief explicitly cautions against an
unbounded "freeze everything" sweep. Every other frozen dataclass in the codebase already
uses only scalars or `tuple[...]` fields (tuples are immutable by type, no fix needed):
`TransitionRecord.evidence`, `BlockedReport.{strategies_tried,evidence,files_affected}`,
`Checkpoint.{completed,pending}`, `ContextRequest.constraints`,
`ContextPackage.{constraints_requested,constraints_applied}`,
`ExecutionStrategy.{agents,execution_order,rationale}`, `ExecutionPlan.rationale`,
`ModelProfile.rationale`, `EscalationDecision.rationale`, `Heartbeat.pending_work`,
`RecoveryEpisode.attempts`.

**Serialization impact.** `MappingProxyType` supports `.items()`/`.get()`/`in`/iteration
identically to `dict`, so no production code needed to change. The CLI's `--json` output
builds fresh plain dicts via dict comprehensions over `.items()` before calling
`json.dumps` (`cli.py`), so JSON serialization was unaffected — verified directly against
`agentgear analyze --json` / `agentgear plan --json` both before and after the change, and
via the full `test_cli.py` suite.

**Fix.** `src/agentgear/config.py::ModelTierMapping.__post_init__`;
`src/agentgear/models.py::ComplexityAssessment.__post_init__` and
`RiskAssessment.__post_init__`.

**Regression tests.** `tests/test_round3_hardening.py` — defensive-copy tests (mutating
the original source dict has no effect), direct-mutation-rejected tests, non-Mapping-input
rejection, and two canonical determinism regressions
(`test_policy_routing_stays_deterministic_despite_mutation_attempts`,
`test_planning_stays_deterministic_despite_factor_mutation_attempts`) that construct once,
attempt mutation, call the pipeline twice, and assert identical output.

---

## AUDIT3-05 — single-writer contract discoverability (P3, documentation only)

**Finding.** `ExecutionWatchdog`/`ExecutionBudgetLedger` hold no internal lock. The ledger
already documented "not thread-safe by design" in its own docstring, but nothing said so
in `ExecutionWatchdog`'s docstring or the README, and AgentGear is explicitly a
multi-agent orchestrator — a reader could reasonably assume the coordinator itself
tolerates concurrent writers.

**Decision.** Documentation only, per the explicit Round 3 scope guidance (P3, not
release-blocking; do not introduce a concurrency refactor to erase a P3). No internal
`RLock` was added — introducing one carries real risk (lock ordering with persistence
writes, reentrancy from within recovery callbacks) for a v0.1.0 library where "serialize
your own writes" is a normal, well-precedented constraint (it mirrors
`ExecutionStateMachine`'s own single-owner design).

**Fix.** Explicit single-writer contract stated in: `ExecutionWatchdog`'s class docstring,
`ExecutionBudgetLedger`'s class docstring, and README's "Known limitations" section.
Persistence (`HeartbeatWriter`, `CheckpointStore`) is explicitly called out as
independently safe for concurrent/multiprocess writers, since that's a different,
already-tested guarantee that shouldn't be confused with the coordinator's.

**Known limitation.** Stays a documented, accepted limitation for v0.1.0 — not tracked as
an open defect.

---

## AUDIT3-06 — `RecoveryEpisode` defensive validation

**Finding.** `RecoveryEpisode` (a public, exported model returned via
`ExecutionWatchdog.recovery_history`) had zero `__post_init__` validation: negative/bool
`episode_number`, `closed_at < opened_at`, non-finite timestamps, and a wrong `outcome`
type were all silently accepted.

**Decision.** Add `__post_init__` validation reflecting the *actual* lifecycle the
coordinator produces — `RecoveryEpisode` is constructed exactly once, at
`_close_recovery_episode`, always already fully closed (real `outcome`, real
`closed_at`). There is no "open episode" representation of this type to model, so no
`outcome=None`/`closed_at=None` intermediate state was invented. `attempts` may
legitimately be an empty tuple (a budget reservation denied *before* any attempt ran
still closes the episode as BLOCKED with zero attempts — confirmed against the real
coordinator path via `test_budget_exhausted_on_third_episode_blocks_correctly`), so
`len(attempts) >= 1` was deliberately **not** added as an invariant — that would have been
a false one.

**Fix.** `src/agentgear/models.py::RecoveryEpisode.__post_init__` — `episode_number` via
the existing `_validate_positive_int` (bool-excluding) helper, `opened_at`/`closed_at` via
`_validate_observation_timestamp` (finite, non-negative) plus an explicit
`closed_at >= opened_at` check, `outcome` type-checked against `RecoveryEpisodeOutcome`,
`attempts` type-checked as `tuple[RecoveryAttempt, ...]`.

**Regression tests.** `tests/test_round3_hardening.py` — accepts a well-formed closed
episode, accepts the real pre-attempt-budget-blocked lifecycle, and rejects each bad field
independently (bad episode_number including `bool`, `closed_at < opened_at`, non-finite
timestamps, wrong outcome type, wrong attempts type).

---

## Cross-cutting: why these four patterns, and what wasn't done

The three defect *classes* the audit named (permissive filtering, broad exception
swallowing, shallow frozen mutability) were each swept bounded to
exported/public/execution-critical code, not the whole repository — per the explicit
Round 3 instruction against an "endless whole-project perfection campaign." Each sweep's
scope and findings are recorded inline above (AUDIT3-02's filtering sweep found no other
instance; AUDIT3-03's exception sweep found three other `except Exception` sites, all
classified intentional with reasoning; AUDIT3-04's mutability sweep enumerated all 30
frozen dataclasses and found exactly one deferred low-risk case).

No test was weakened, removed, or reclassified as "intentional" to make it pass. Every
fix above has an independent reproducer that failed against baseline
`1cff32b6ed6bf46163423d86d870ee1c8ddee314` before the corresponding change landed.
