# AgentGear v0.1.0 Architecture

## Module map

```
agentgear/
├── models.py              # domain types: TaskProfile, ComplexityAssessment, RiskAssessment,
│                           # ModelTier, ReasoningEffort, AgentRole, ExecutionStrategy,
│                           # ExecutionPlan, ExecutionState, ProgressEvent, Checkpoint,
│                           # RecoveryAttempt, BlockedReport, Heartbeat
├── config.py               # Policy and all its validated sub-policies (routing weights/
│                           # thresholds, watchdog bounds, budgets, tier->model mapping)
├── analysis.py              # TaskProfile -> ComplexityAssessment / RiskAssessment
├── routing.py                # model tier + reasoning effort routing (two independent scores)
├── planning.py                # multi-agent staffing + ExecutionPlan assembly + budget enforcement
├── escalation.py               # evidence-driven escalation, bounded by policy + cost
├── checkpoints.py               # logical checkpoint persistence
├── context_provider.py           # ContextProvider interface + Default + optional PromptGraph
├── benchmark_interface.py         # AgentBench-ready EvidenceSource interface (unimplemented)
├── api.py                          # analyze()/plan() convenience wrappers
├── cli.py                           # agentgear analyze|plan|status|simulate
├── path_security.py                  # symlink/junction-aware path containment
├── safe_json_store.py                 # atomic, locked, path-contained JSON persistence
├── _sibling_utils.py                   # optional ecosystem sibling discovery
├── exceptions.py                        # exception hierarchy
└── watchdog/
    ├── state_machine.py                  # ExecutionStateMachine + legal transition table
    ├── progress.py                        # ProgressTracker (genuine progress only)
    ├── stall_detection.py                  # StallDetector: multi-signal, never time-only
    ├── recovery.py                          # RecoveryEngine (bounded ladder) + LoopGuard
    ├── blocked.py                            # structured BlockedReport builder
    └── heartbeat.py                           # lightweight Heartbeat + persistence
```

## Responsibility boundaries (why each seam is where it is)

- **`analysis.py` vs `routing.py`**: analysis turns raw task signals into two independent
  scores (complexity, risk); routing turns those scores into two independent decisions
  (tier, reasoning). Splitting them means a future change to *how risk is scored* never
  has to touch *how tiers are chosen*, and vice versa.
- **`routing.py` vs `planning.py`**: routing picks one `ModelProfile` for "the task as a
  whole" (used as the Builder's tier/reasoning); planning decides whether the task needs
  more than a Builder, and if so, what the other roles' tiers should be *relative to* the
  primary model. Planning depends on routing's output; routing has no notion of agents.
- **`escalation.py` is separate from both**: it operates mid-execution, on evidence a
  runtime orchestrator collects (failures, uncertainty, stalls), not on the static
  `TaskProfile`. It reuses `routing.estimate_cost` for budget checks but never reuses
  threshold logic from `routing.py`, because escalation moves by fixed ladder steps
  (with a non-sequential critical-risk jump), not by re-scoring the task.
- **`watchdog/` is a self-contained subsystem**: state machine, progress tracking, stall
  detection, recovery, and heartbeats only depend on `models.py`, `config.py`, and
  `exceptions.py` — never on `routing.py` or `planning.py`. A caller can use the watchdog
  to supervise an execution whose plan came from anywhere (including a hand-written one),
  which is why it is importable as `agentgear.watchdog` independently.
- **`checkpoints.py` vs `watchdog/heartbeat.py`**: a checkpoint is an append-only history
  of "how far did this get" (phase, completed/pending subtasks); a heartbeat is a single
  overwritten "what is it doing right now" record. Different write patterns, so different
  stores, both built on the same `safe_json_store.SafeJsonStore` primitive.
- **`context_provider.py` / `benchmark_interface.py` are interface-only seams**: AgentGear
  depends on the abstract shape, never on a concrete sibling implementation. This is what
  makes "useful alone, better together" enforceable rather than aspirational — deleting
  PromptGraph and AgentBench from `site-packages` cannot break AgentGear's own test suite
  (see `tests/test_standalone.py`, which runs in an environment where neither is installed).

## Determinism

`analysis.assess_complexity/assess_risk`, `routing.route`, and `planning.build_execution_plan`
are pure functions of their inputs (`TaskProfile`, `Policy`) — no randomness, no wall-clock
reads, no I/O. `tests/test_determinism.py` asserts that the same `TaskProfile` + `Policy`
(including re-constructed from an equivalent dict) always yields an equal `ExecutionPlan`.

The watchdog is the one subsystem that is explicitly *not* required to be pure: state
transitions are driven by caller-supplied `at_seconds` (never a real clock read internally),
which keeps it deterministic and testable without a fake-clock dependency, while still
modeling real elapsed-time-sensitive behavior (stall detection).

## Hard budgets

`planning.build_execution_plan` raises `BudgetExceededError` — never returns a plan that
violates `Policy.budget` — for: agent count, context token budget, estimated cost, and
estimated total tokens. `escalation.decide_escalation` independently checks the cost budget
before proposing an escalation. Both paths are covered by dedicated tests
(`tests/test_planning.py`, `tests/test_escalation.py`).
