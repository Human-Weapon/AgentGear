# Changelog

All notable changes to AgentGear are documented in this file.

## [0.1.0] - Unreleased (release candidate) — Remediation Round 6

Not yet tagged pending independent adversarial audit. Addresses findings from a sixth
independent adversarial audit (baseline commit `68c829d`). Full writeups:
`docs/audits/remediation-round-6.md`; cross-round index: `docs/audits/index.md`.

### Changed (user-visible contract changes)

- **`ExecutionWatchdog.record_activity()`, `record_progress()`, `record_escalation()`,
  `checkpoint()`, and `advance()` now enforce lifecycle admission**: these are only legal
  while the execution is RUNNING/TESTING/REVIEWING. Previously `checkpoint()` could be
  called before `start()`, and all four (plus `record_escalation()`, which had no state
  check at all) could still be called after `COMPLETED` or while `BLOCKED`, silently
  mutating history/budget/heartbeat state that should have been immutable. `advance()` also
  used to silently bypass `start()`'s own initialization when called from `PLANNING`
  (permanently disabling stall detection) and could bypass `record_recovery_result()`
  entirely when called from `RECOVERING` (abandoning an unresolved recovery attempt) --
  both are now rejected.
- **`HeartbeatWriter`/`CheckpointStore`/`ExecutionWatchdog(state_dir=...)` now detect the
  configured persistence root itself being replaced** by a symlink/junction after
  construction (previously only a CHILD path beneath an unchanged root was protected;
  the root's own identity was never re-verified, so replacing the root itself bypassed
  containment entirely). Verified with real Windows junctions (`mklink /J`), both for a
  root that existed at construction and one that didn't yet.
- **`state_dir` pointing at an existing regular file is now rejected** -- at
  `ExecutionWatchdog` construction (`ConfigurationError`, before `start()` can commit
  `RUNNING` or spend budget) and at the low-level `HeartbeatWriter`/`CheckpointStore`
  constructors and every subsequent operation (new `InvalidPersistenceRootError`),
  including the race case where the root becomes a regular file only after construction.

## [0.1.0] - Unreleased (release candidate) — Remediation Round 5

Not yet tagged pending independent adversarial audit. Addresses findings from a fifth
independent adversarial audit (baseline commit `f8573ef`). Full writeups:
`docs/audits/remediation-round-5.md`; cross-round index: `docs/audits/index.md`.

### Changed (user-visible contract changes)

- **`CheckpointStore.append()` now enforces a HARD segment-capacity cap under real
  concurrency**, via a new execution-scoped file lock. Previously, several concurrent
  processes could each observe "still room" in the same segment and all append to it,
  overshooting `_SEGMENT_CAPACITY` (documented as merely "advisory" before). Public
  `append()`/`all()`/`latest()` behavior is otherwise unchanged.
- **`ExecutionWatchdog.record_recovery_result()`** now validates `result` (must be exactly
  `RecoveryResult.SUCCESS` or `RecoveryResult.FAILURE` -- `PENDING` is rejected) and
  `evidence` (must be `None` or a non-blank string) BEFORE any state mutation, instead of
  discovering an invalid value deep inside a follow-on construction after the recovery
  attempt, episode, and state machine had already been mutated.
- **`ActivityRecord`** (used by `ExecutionWatchdog.record_activity()`) now validates
  `succeeded`/`is_trivial` as strict `bool` (no int/string coercion) and `error` as `None`
  or a non-blank string.
- **`ExecutionWatchdog.advance()`** now validates its `target` argument is a real
  `ExecutionState` instance -- previously a raw string with the same value (e.g.
  `"testing"`) was silently accepted and corrupted `state` with a plain `str`, breaking
  every later `.value` access. The same guard was added to the low-level
  `ExecutionStateMachine.transition()` and `__init__`, and to
  `ExecutionBudgetLedger.reserve()`'s `kind` parameter (the identical `str`-subclassing
  trap, found during this round's own enum sweep).
- **Heartbeat freshness contract extended**: `record_activity()` and `checkpoint()` now
  sync the durable heartbeat on every call (previously `record_activity()` only did so via
  a stall-path side effect, and `checkpoint()` never did at all, silently going stale
  while `heartbeat_dirty` stayed `False`). `heartbeat_dirty` is now set BEFORE attempting
  to build or write the projection (not only around the write call), so a failure during
  heartbeat *construction* (not just I/O) is now correctly reported as dirty too.
- **`HeartbeatWriter`/`CheckpointStore`/`ExecutionWatchdog(state_dir=...)`** now bind a
  relative `state_dir` to an absolute path at construction time. Previously a relative
  `state_dir` silently rebound to wherever the process's current working directory
  happened to be at the moment of each read/write, so a later `os.chdir()` elsewhere in
  the process could redirect persistence to an unrelated location without any error.
- **`ExecutionWatchdog(state_dir=...)`** now rejects a blank/whitespace-only string
  (`ConfigurationError`) instead of silently disabling persistence (`""`) or accepting it
  as a literal, almost-certainly-unintended directory name (`"   "`). Only `None`
  disables persistence.
- **`PromptGraphContextProvider`** no longer echoes a search-failure exception's raw
  message into the public `ContextPackage.note` -- only the exception's class name is
  included now, since the message is untrusted data from an external adapter that could
  contain secrets, paths, or other sensitive fragments.
- Corrected the current-facing `README.md`'s PromptGraph link (was still pointing at the
  dead `hermes-oss/promptgraph`); `docs/audits/index.md`'s Round 4 entries, which had
  never been finalized with real commit SHAs, now cite `f8573ef`; a new CI/test check
  rejects any unfinalized placeholder shipping in `docs/audits/index.md` going forward.
- Fixed the CI "standalone" check, which incorrectly asserted optional sibling packages
  (PromptGraph, etc.) must be ABSENT from the environment -- "standalone" means AgentGear
  never *requires* them, not that they must be uninstalled. CI now separately proves true
  isolation (no siblings present) and that a genuinely-importable sibling doesn't change
  core behavior.

## [0.1.0] - Unreleased (release candidate) — Remediation Round 4

Not yet tagged pending independent adversarial audit. Addresses findings from a fourth
independent adversarial audit (baseline commit `bc53151`). Full writeups:
`docs/audits/remediation-round-4.md`; cross-round index: `docs/audits/index.md`.

### Changed (user-visible contract changes)

- **`ComplexityAssessment.factors`/`RiskAssessment.factors`** now validate every value
  (finite, in [0,1], not bool) in addition to being deep-frozen (Round 3). A `NaN`/
  `Infinity`/out-of-range/bool factor value now raises `TaskProfileError` at construction
  instead of silently defeating critical-risk routing checks later.
- **`ExecutionWatchdog.__init__`** now validates `policy` (must be a `Policy`),
  `initial_tier`/`initial_reasoning` (must be real enum members, no string coercion),
  `context_budget_tokens` (strict positive `int`, bool excluded), and `state_dir` (str or
  `None`), all raising `ConfigurationError`. Previously some invalid values were silently
  accepted (e.g. `context_budget_tokens=True`) and others crashed with a raw
  `AttributeError` deep inside the constructor.
- **Rejected coordinator operations no longer advance the internal clock.** Previously, a
  call that failed its own validation (e.g. `complete()` rejecting malformed evidence)
  could still have already advanced the "last observed time" watermark, incorrectly
  causing a subsequent legitimate retry at an earlier timestamp to fail.
- **New heartbeat durability API:** `ExecutionWatchdog.heartbeat_dirty` (property) and
  `ExecutionWatchdog.sync_heartbeat()` (method). If a heartbeat write fails, in-memory
  state is never rolled back (it remains authoritative) and the triggering call still
  raises so the caller learns about the failure — but `heartbeat_dirty` becomes `True`
  and `sync_heartbeat()` idempotently retries the write without repeating any domain
  transition, budget charge, or history entry. `status()` now includes
  `"heartbeat_dirty"`/`"heartbeat_sync_error"`.
- **Checkpoint storage format changed** from one ever-growing `{execution_id}.
  checkpoints.json` file to a segmented `{execution_id}.checkpoints/segment-NNNNNN.json`
  directory (bounded entries per segment), fixing an O(N²) total-cost growth pattern over
  an execution's lifetime. `CheckpointStore`'s public API (`append`/`all`/`latest`) is
  unchanged; only the on-disk layout changed. No migration path is provided (v0.1.0 has
  not shipped, so the old format was never a released contract).
- **`RecoveryAttempt`** now validates `reason`/`strategy` (non-blank), `attempt_number`
  (positive int, bool excluded), `result` (a real `RecoveryResult`), and `at_seconds`
  (finite, non-negative) at construction, raising `InvalidObservationError`.
- **`HeartbeatWriter`/`CheckpointStore`** now reject a permanently-unsafe `execution_id`
  (over 150 characters, or containing characters illegal in a filename) immediately with
  a new `InvalidIdentifierError`, instead of eventually failing after a ~10-second lock
  retry loop with a confusing `StorageLockError`.
- Fixed a real multiprocess race (found via this round's own multi-process
  verification, at 10 concurrent checkpoint appenders): an unlocked read racing
  against another process's concurrent atomic file replace could transiently see a
  `PermissionError` on Windows and get misclassified as file corruption, quarantining
  a perfectly healthy checkpoint segment. Reads now retry briefly on a transient OS
  error before concluding corruption; a genuinely unreadable file still quarantines
  as before.
- Fixed a path-containment bug where a `state_dir` that doesn't exist yet (even one level
  deep) caused every heartbeat/checkpoint write to fail with a spurious
  `PathEscapeError`; reads on a nonexistent `state_dir` now cleanly return "no state
  found" rather than raising. Traversal/junction/symlink rejection is unaffected.
- `PromptGraphContextProvider` no longer assumes its adapter's `search()` result supports
  `len()`, and now catches exceptions raised during iteration (not just from the initial
  call), so a generator-based or misbehaving adapter degrades to the documented fallback
  instead of crashing or, for an unbounded generator, hanging.
- Corrected project metadata: `pyproject.toml`'s `Homepage`/`Issues`/author now point to
  the real repository (`Human-Weapon/AgentGear`) instead of a placeholder; `SECURITY.md`
  now points to GitHub's private vulnerability reporting instead of a TBD email; the
  source distribution (sdist) now includes `SECURITY.md`/`CHANGELOG.md`/
  `CONTRIBUTING.md`/`docs/**/*.md`, which were previously silently omitted.
- Self-adversarial pass (beyond the 10 pre-specified findings): `checkpoint()` now
  persists a checkpoint durably before mirroring it into the in-memory cache, so a
  persistence failure can no longer leave `self._checkpoints` claiming a checkpoint exists
  that was never actually written. `ExecutionWatchdog.__init__` now eagerly rejects a
  filesystem-unsafe `execution_id` when `state_dir` is provided, instead of silently
  accepting it and only discovering the problem on the first `start()` call — by which
  point the state machine had already transitioned to `RUNNING` and budget had already
  been committed, an irreversible mutation left permanently stuck with an unsyncable dirty
  heartbeat, since a bad identifier (unlike a full disk) is never transient.

## [0.1.0] - Unreleased (release candidate) — Remediation Round 3

Not yet tagged pending independent adversarial audit. Addresses findings from a third
independent adversarial audit (baseline commit `1cff32b`). Full writeups:
`docs/audits/remediation-round-3.md`.

### Changed (user-visible contract changes)

- **`ExecutionStateMachine.transition(evidence=...)`** now validates evidence
  **strictly**: every entry must be a non-blank string, or the *entire* call is rejected
  (`InvalidObservationError`) before any state mutation. Previously, invalid entries
  mixed in with valid ones were silently dropped rather than raising — e.g.
  `evidence=("done", 42)` used to succeed and quietly store only `("done",)`. A
  well-formed but empty evidence tuple on a `COMPLETED` transition still raises the more
  specific `NotCompletedError`, as before.
- **`ExecutionWatchdog.begin_recovery()`** now only converts a `RecoveryExhaustedError`
  from `RecoveryEngine.next_strategy()` into a `BLOCKED` report. Any other exception type
  (e.g. a bug in a custom `RecoveryEngine`) now propagates as a real exception instead of
  being silently reported as ordinary recovery exhaustion.
- **`Policy.model_tier_mapping`, `ComplexityAssessment.factors`, `RiskAssessment.factors`**
  are now deep-frozen: the constructor defensively copies the input, and the exposed
  mapping rejects direct item assignment (`TypeError`). Previously these were plain
  mutable `dict`s nested inside otherwise-frozen dataclasses, so a caller mutating either
  the original source dict or the exposed field after construction could silently change
  routing/planning output for subsequent calls using the same object.
- **`RecoveryEpisode`** (returned via `ExecutionWatchdog.recovery_history`) now validates
  its fields on construction (positive `episode_number`, finite timestamps with
  `closed_at >= opened_at`, a real `RecoveryEpisodeOutcome`, a proper
  `tuple[RecoveryAttempt, ...]`).
- `CriticalRiskPolicy`'s documentation corrected: a `*_at` threshold of `0.0` means the
  critical-risk floor applies **unconditionally** (every signal value, including an
  exactly-zero one, satisfies `signal >= 0.0`) — not "any nonzero signal," as previously
  (incorrectly) documented. No behavior changed, only the description of existing
  behavior.
- `ExecutionWatchdog`/`ExecutionBudgetLedger` are now explicitly documented as
  single-writer, not-thread-safe coordinators for v0.1.0 (no behavior change).

## [0.1.0] - Unreleased (release candidate) — Remediation Round 1

Not yet tagged pending independent adversarial audit. This round addresses the findings
from the first independent adversarial audit (baseline commit `5330554`).

### Fixed

- **AG-01** — Stall detection could never fire for an execution that had *never* made
  genuine progress, because the "no progress" trigger measured elapsed time from
  `last_progress_at`, which was `None` in that exact case. `StallDetector.evaluate` now
  requires an explicit `started_at` execution/observation boundary, so "active for N
  seconds and N attempts without ever producing progress" is detectable even when every
  activity fingerprint is unique.
- **AG-02** — Stall signals (repeated failures, circular attempts, slow trivial commands)
  were computed over an execution's *entire* activity history, so stale pre-progress
  activity could still count toward a stall verdict computed after genuine progress had
  already reset the clock. All signals are now scoped to activity strictly after the
  progress boundary (`last_progress_at`, or `started_at` if there has been none).
- **AG-03** — A single maxed-out individual risk signal (`security_impact=1.0`,
  `data_impact=1.0`, or full irreversibility) on an otherwise trivial task could be
  diluted by the blended risk score into `FAST`/`low`/single-Builder/no-review. A new
  `Policy.critical_risk` policy floors tier/reasoning and forces review independently of
  the blended score whenever any one of these signals crosses its own threshold.
- **AG-04** — Escalation's cost check compared only the *new* operation's cost against
  the static policy ceiling, so a sequence of individually-affordable escalations could
  jointly blow the budget. A new `ExecutionBudgetLedger` (`budget.py`) tracks cumulative
  reserved/committed tokens and cost across the initial plan and every escalation/recovery
  attempt in one shared pool; `decide_escalation` consults it when supplied.
  `ExecutionWatchdog.from_plan(...)` now commits the entire initial multi-agent plan at
  `start()`, and every accepted recovery attempt reserves/commits a conservative
  context-sized charge before it begins. A committed reservation can no longer be
  released, so spent budget cannot be reused to bypass a hard ceiling.
- **AG-06** — Escalation and progress signals (`repeated_failures`, `uncertainty`,
  `elapsed_seconds`, boolean flags, timestamps, fingerprints, descriptions) were not
  validated, so out-of-range, negative, or NaN/Infinity values were silently accepted and
  could force incorrect decisions or keep an execution "alive" on bogus progress. All of
  these now raise `InvalidObservationError` on construction; the CLI surfaces this as a
  concise `error:` message with exit code 1, never a traceback.
- **AG-07** — Heartbeat and checkpoint files that were technically valid JSON but didn't
  match the expected schema (`{}`, `[]`, wrong root type, missing/wrong-typed fields, an
  unrecognized `ExecutionState`, or a literal `null` body indistinguishable from "file
  doesn't exist") raised raw `KeyError`/`ValueError`/`TypeError`, or in the `null` case
  were silently treated as if nothing had ever been written. Both stores now validate
  their schema on every read, quarantining malformed files and raising
  `CorruptStorageError` — `agentgear status` reports this cleanly, never with a traceback.
- **AG-08** — `ContextPackage.used_tokens` summed each chunk's token estimate before
  joining them with separators, so the actual returned `content` could exceed
  `budget_tokens` once separator overhead was included. `used_tokens` is now always the
  token estimate of the real joined `content`, checked before each chunk is accepted.
  `constraints_applied` also used to echo back whatever was requested, even though no
  provider enforces any constraint in v0.1.0; it is now always `()`, with
  `constraints_requested` recording what was actually asked for.
- **AG-09** — `BlockedReport` accepted blank `blocker`/`root_cause`/recommended-action
  strings and non-int `attempts`. `build_blocked_report` now validates all of these
  (`InvalidBlockedReportError`), and the new `ExecutionWatchdog` coordinator (see below)
  makes it structurally impossible to reach `BLOCKED` through any path that doesn't
  build and validate a report first.
- **P3** — `RoutingWeights.latency_weight` was normalized but never used in the threshold
  shift, so any value produced identical routing to `latency_weight=0`. It now shares
  `cost_weight`'s direction in the bias term (both favor cheaper/faster tiers; quality
  favors richer ones), documented in `routing._threshold_shift`.
- Test quality: `tests/test_standalone.py` used to assert that PromptGraph/SkillGuard/
  AgentBench/ProjectKaizen were *not installed* in the running environment, which would
  fail in any environment that happens to have a sibling installed even though AgentGear
  has no mandatory dependency on it. It now proves standalone-ness statically (no
  sibling is imported at module level, verified via AST inspection) and behaviorally
  (the "unavailable" degradation path is exercised via monkeypatching), independent of
  what's actually installed.

### Added

- **`ExecutionWatchdog`** (`watchdog/coordinator.py`, exported as `agentgear.ExecutionWatchdog`):
  the public runtime-supervision coordinator composing the state machine, progress
  tracker, stall detector, recovery engine, loop guard, budget ledger, heartbeat writer,
  and checkpoint store behind a small event-oriented API (`start`, `record_activity`,
  `record_progress`, `evaluate`, `begin_recovery`, `record_recovery_result`, `checkpoint`,
  `complete`, `status`). Callers no longer call `ExecutionStateMachine.transition`
  directly; the coordinator enforces every transition, including automatically driving
  RUNNING → STALLED → RECOVERING (or BLOCKED) when `record_activity`/`evaluate` detects a
  stall. AgentGear now has two public layers: PLANNING (`TaskProfile` → `ExecutionPlan`)
  and RUNTIME SUPERVISION (`ExecutionWatchdog`).
- `ExecutionBudgetLedger` / `ReservationKind` / `ReservationState` (`budget.py`), exported
  from the top-level package.
- `ExecutionWatchdog.from_plan(...)`, the budget-safe bridge from the planning API to
  runtime supervision. It binds the full initial plan estimate to the coordinator's
  execution-wide ledger.
- `Policy.critical_risk` (`CriticalRiskPolicy`): configurable per-signal critical-risk
  floors independent of the blended risk score.
- Real Windows-junction filesystem containment tests (direct, nested, and
  post-construction-swap/TOCTOU) and real multi-process concurrency tests (2 and 5
  concurrent OS processes) for `HeartbeatWriter` and `CheckpointStore`.

### Fixed (found via the new multi-process hardening tests above)

- `SafeJsonStore.write_atomic` could intermittently raise `PermissionError` (Windows
  `WinError 5`) on the final rename/replace even while correctly holding its own file
  lock — real multi-process runs surfaced this under load, almost certainly antivirus or
  the search indexer briefly opening the just-written file. The replace step now retries
  a bounded number of times with a short backoff before giving up; this never masks a
  real containment/logic error, which still raise immediately.
- A schema-invalid but syntactically valid heartbeat/checkpoint file could be silently
  overwritten by a later write/append. `SafeJsonStore` now accepts an optional document
  validator, and both persistence users validate under the same lock before mutating;
  corrupt state is quarantined and reported instead of discarded.

### Changed

- Deprecated the standalone blended-risk-only override language in favor of documenting
  both the blended-score override (risk ≥ 0.85 → minimum `ADVANCED`) and the new
  independent per-signal `critical_risk` floors side by side.

### Additional hardening in this branch

- Public risk analysis now reports `CRITICAL` whenever security impact, data impact,
  or irreversibility reaches the conservative individual-signal threshold. This prevents
  a diluted weighted score from presenting a maximum raw risk as `low`.

### Known limitations

See [README.md#known-limitations](README.md#known-limitations).

---

## [0.1.0] - Unreleased (initial release candidate)

Initial release candidate, since remediated above. Not yet tagged pending independent
adversarial audit.

### Added

- Deterministic, explainable task analysis (`ComplexityAssessment`, `RiskAssessment`) from a normalized `TaskProfile`.
- Provider-agnostic model tier routing (`FAST`/`STANDARD`/`ADVANCED`/`FRONTIER`) mapped to real models only via configuration.
- Independent reasoning-effort routing (`NONE`..`MAX`) using its own score blend and threshold set.
- Cost-aware routing: cheapest sufficient tier is always selected; routing weights (cost/quality/latency) shift thresholds without ever defaulting to the most powerful tier.
- Multi-agent execution planning: Planner/Researcher/Judge/Builder/Reviewer staffing rules, with hard compute budgets (`Policy.budget`) enforced by raising `BudgetExceededError` rather than returning a violating plan.
- Escalation engine: evidence-driven (repeated failure, uncertainty, risk, insufficient context, failed tests, stalled execution) escalation bounded by `max_model_escalations` and cost budget; never escalates on elapsed time alone.
- Execution Watchdog:
  - Explicit `ExecutionState` state machine (`PLANNING`/`RUNNING`/`TESTING`/`REVIEWING`/`STALLED`/`RECOVERING`/`BLOCKED`/`COMPLETED`) with validated transitions.
  - `COMPLETED` requires non-empty evidence — idle/silence can never be mistaken for done.
  - Progress tracking separates genuine `ProgressEvent`s from raw activity, so "busy but not progressing" is representable.
  - Stall detection combines elapsed time, attempt counts, repeated identical failures, circular attempts, and abnormally slow trivial commands — never triggers on time alone.
  - Bounded recovery ladder (`RecoveryEngine`) that never repeats a strategy, plus `LoopGuard` loop-protection counters.
  - Structured `BlockedReport` — BLOCKED always produces a report, never a silent stop.
  - Lightweight `Heartbeat` + `Checkpoint` persistence via a path-contained, atomic, concurrency-safe JSON store.
- Optional, best-effort `PromptGraphContextProvider` (degrades to `DefaultContextProvider` when PromptGraph is not installed or its API doesn't behave as expected).
- `AgentBench`-ready `EvidenceSource` interface (not implemented or wired into routing in v0.1.0).
- CLI: `agentgear analyze`, `agentgear plan`, `agentgear status`, `agentgear simulate` — all work with zero network access and zero API keys.

### Known limitations

See [README.md#known-limitations](README.md#known-limitations).
