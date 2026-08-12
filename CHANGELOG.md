# Changelog

All notable changes to AgentGear are documented in this file.

## [0.1.0] - Unreleased (release candidate)

Initial release candidate. Not yet tagged pending independent adversarial audit.

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
