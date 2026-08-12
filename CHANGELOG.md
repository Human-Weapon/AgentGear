# Changelog

All notable changes to AgentGear are documented in this file.

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

### Changed

- Deprecated the standalone blended-risk-only override language in favor of documenting
  both the blended-score override (risk ≥ 0.85 → minimum `ADVANCED`) and the new
  independent per-signal `critical_risk` floors side by side.

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
