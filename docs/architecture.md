# AgentGear v0.1.0 Architecture

## Module map

```
agentgear/
├── models.py              # domain types: TaskProfile, ComplexityAssessment, RiskAssessment,
│                           # ModelTier, ReasoningEffort, AgentRole, ExecutionStrategy,
│                           # ExecutionPlan, ExecutionState, ProgressEvent, Checkpoint,
│                           # RecoveryAttempt, BlockedReport, Heartbeat
├── config.py               # Policy and all its validated sub-policies (routing weights/
│                           # thresholds, critical-risk floors, watchdog bounds, budgets,
│                           # tier->model mapping)
├── analysis.py              # TaskProfile -> ComplexityAssessment / RiskAssessment
├── routing.py                # model tier + reasoning effort routing (two independent scores,
│                             #  plus independent critical-signal floors)
├── planning.py                # multi-agent staffing + ExecutionPlan assembly + budget enforcement
├── escalation.py               # evidence-driven escalation, bounded by policy + cumulative ledger
├── budget.py                    # ExecutionBudgetLedger: cumulative token/cost accounting
├── checkpoints.py                 # logical checkpoint persistence (schema-validated)
├── context_provider.py             # ContextProvider interface + Default + optional PromptGraph
├── benchmark_interface.py           # AgentBench-ready EvidenceSource interface (unimplemented)
├── api.py                            # analyze()/plan() convenience wrappers
├── cli.py                             # agentgear analyze|plan|status|simulate
├── path_security.py                    # symlink/junction-aware path containment
├── safe_json_store.py                   # atomic, locked, path-contained JSON persistence
├── _sibling_utils.py                     # optional ecosystem sibling discovery
├── exceptions.py                          # exception hierarchy
└── watchdog/
    ├── state_machine.py                    # ExecutionStateMachine + legal transition table (low-level)
    ├── progress.py                          # ProgressTracker (genuine progress only)
    ├── stall_detection.py                    # StallDetector: multi-signal, never time-only
    ├── recovery.py                            # RecoveryEngine (bounded ladder) + LoopGuard
    ├── blocked.py                              # structured, validated BlockedReport builder
    ├── heartbeat.py                             # lightweight Heartbeat + schema-validated persistence
    └── coordinator.py                           # ExecutionWatchdog: the public runtime-supervision API
```

## Two public layers

```
PLANNING:            TaskProfile -> ExecutionPlan          (api.analyze / api.plan)
RUNTIME SUPERVISION:  ExecutionWatchdog -> lifecycle enforcement
```

These are deliberately separate, not one God object. Planning is a pure function of
`(TaskProfile, Policy)` with no notion of time or in-progress state. Runtime supervision
is inherently stateful (an execution's current phase, its activity history, its budget
spend so far) and event-driven. A caller can use planning without ever touching the
watchdog (e.g. to preview what a task would cost), and can use `ExecutionWatchdog` to
supervise an execution whose plan came from anywhere, including one that didn't go
through `planning.py` at all.

## Responsibility boundaries (why each seam is where it is)

- **`analysis.py` vs `routing.py`**: analysis turns raw task signals into two independent
  scores (complexity, risk); routing turns those scores into two independent decisions
  (tier, reasoning). Splitting them means a future change to *how risk is scored* never
  has to touch *how tiers are chosen*, and vice versa.
- **`routing.py` vs `planning.py`**: routing picks one `ModelProfile` for "the task as a
  whole" (used as the Builder's tier/reasoning); planning decides whether the task needs
  more than a Builder, and if so, what the other roles' tiers should be *relative to* the
  primary model. Planning depends on routing's output; routing has no notion of agents.
  Both consult `routing.critical_signal_reasons` — the same single-source-of-truth
  function — so "does this task have a critical individual risk signal" is answered
  identically whether you're asking about tier/reasoning floors or about whether a
  Reviewer must be staffed.
- **`escalation.py` is separate from both**: it operates mid-execution, on evidence a
  runtime orchestrator collects (failures, uncertainty, stalls), not on the static
  `TaskProfile`. It reuses `routing.estimate_cost` for budget checks but never reuses
  threshold logic from `routing.py`, because escalation moves by fixed ladder steps
  (with a non-sequential critical-risk jump), not by re-scoring the task. When driven
  through `ExecutionWatchdog`, its cost check is answered by the shared
  `ExecutionBudgetLedger` rather than a static one-operation comparison — see "Cumulative
  budget" below.
- **`budget.py` is its own module, not folded into `escalation.py` or `planning.py`**:
  the ledger has to outlive any single planning or escalation call — it's constructed
  once per execution (by `ExecutionWatchdog.__init__`) and threaded through everything
  that spends against it. Giving it its own module makes that lifetime explicit and
  keeps `escalation.py` focused on *deciding* whether to escalate, not on *accounting*.
- **`watchdog/` is a self-contained subsystem**: the low-level pieces (state machine,
  progress tracking, stall detection, recovery, heartbeats) only depend on `models.py`,
  `config.py`, `budget.py`, and `exceptions.py` — never on `routing.py` or `planning.py`.
  `watchdog/coordinator.py` (`ExecutionWatchdog`) composes all of them into the one
  supported public entry point; application code should use the coordinator, not the
  low-level pieces directly, even though those remain independently testable and usable
  for advanced/custom orchestration.
- **`checkpoints.py` vs `watchdog/heartbeat.py`**: a checkpoint is an append-only history
  of "how far did this get" (phase, completed/pending subtasks); a heartbeat is a single
  overwritten "what is it doing right now" record. Different write patterns, so different
  stores, both built on the same `safe_json_store.SafeJsonStore` primitive, and both
  validate their on-disk schema on every read — a file that parses as JSON but doesn't
  match the expected shape is quarantined and reported as `CorruptStorageError`, never
  handed back as if it were valid.
- **`context_provider.py` / `benchmark_interface.py` are interface-only seams**: AgentGear
  depends on the abstract shape, never on a concrete sibling implementation. This is what
  makes "useful alone, better together" enforceable rather than aspirational — deleting
  PromptGraph and AgentBench from `site-packages` cannot break AgentGear's own test suite.
  `tests/test_standalone.py` proves this two ways: statically (no file in the package
  imports a sibling at module level) and behaviorally (the "sibling unavailable" path is
  exercised via monkeypatching, not by assuming the test environment lacks the sibling —
  an auditor's environment may well have PromptGraph installed, and that must never make
  the standalone tests fail).

## Determinism

`analysis.assess_complexity/assess_risk`, `routing.route`, and `planning.build_execution_plan`
are pure functions of their inputs (`TaskProfile`, `Policy`) — no randomness, no wall-clock
reads, no I/O. `tests/test_determinism.py` asserts that the same `TaskProfile` + `Policy`
(including re-constructed from an equivalent dict) always yields an equal `ExecutionPlan`.

`ExecutionWatchdog` and the watchdog subsystem it composes are explicitly *not* required to
be pure: state transitions are driven by caller-supplied `at_seconds` (never a real clock
read internally) and are enforced to be non-decreasing (`InvalidObservationError` otherwise)
— deterministic and testable without a fake-clock dependency, while still modeling real
elapsed-time-sensitive behavior (stall detection).

## Cumulative budget

`ExecutionBudgetLedger` (`budget.py`) is the single source of truth for "how much of this
execution's budget has been spent" across its entire lifetime — the initial plan, every
escalation, every recovery attempt, and every agent dispatch reserve/commit against the
SAME ledger instance (owned by `ExecutionWatchdog`). `reserve()` is atomic: it either
creates a reservation that fits under both the token and cost ceilings, or raises
`BudgetExceededError` and changes nothing. This is why two escalations that are each
individually affordable can still be correctly denied on the second one — the ledger
knows what the first one already committed; a stateless per-call check against the static
policy ceiling cannot.

`ExecutionWatchdog.from_plan(execution_id, execution_plan, policy)` is the runtime entry
point when planning has already produced an `ExecutionPlan`: its `start()` transaction
commits the plan's full multi-agent token/cost estimate before entering `RUNNING`.
Recovery attempts likewise reserve and commit a conservative context-sized charge before
they are started; when no such charge fits, the coordinator emits `BLOCKED` rather than
running an unbudgeted recovery. A directly constructed coordinator intentionally has no
implicit plan charge; its caller must provide a known initial charge to `start()`.

## Hard budgets

`planning.build_execution_plan` raises `BudgetExceededError` — never returns a plan that
violates `Policy.budget` — for: agent count, context token budget, estimated cost, and
estimated total tokens. `escalation.decide_escalation` checks the cumulative
`ExecutionBudgetLedger` when one is supplied (always true when driven through
`ExecutionWatchdog`), falling back to a static single-operation check against
`Policy.budget.max_estimated_cost` only for standalone use with no execution in progress.
Both paths are covered by dedicated tests (`tests/test_planning.py`,
`tests/test_escalation.py`, `tests/test_budget.py`, `tests/test_coordinator.py`).
