# AgentGear

**Adaptive compute orchestrator for AI software-engineering agents.**

AgentGear decides **how** a task should be executed: which model capability tier, how much reasoning effort, how many agents, which roles, when to escalate, when to recover, and when to stop because something is stuck. It runs with **zero network access and zero API keys** — routing is a logical, configurable decision, not a call to a real model provider.

AgentGear has two public layers:

```
PLANNING:            TaskProfile -> ExecutionPlan          (agentgear.plan)
RUNTIME SUPERVISION:  ExecutionWatchdog -> lifecycle enforcement
```

Part of the HERMES OSS ecosystem ([PromptGraph](https://github.com/Human-Weapon/PromptGraph), AgentGear, [SkillGuard](https://github.com/Human-Weapon/SkillGuard), [AgentBench](https://github.com/Human-Weapon/AgentBench), [ProjectKaizen](https://github.com/Human-Weapon/ProjectKaizen)). Every tool in the ecosystem is **useful alone, better together** — AgentGear has no required dependency on any sibling project.

## What AgentGear IS

- A **task analyzer**: turns raw signals (files affected, architectural impact, security impact, ambiguity, novelty, reversibility, ...) into an explainable, deterministic complexity/risk score.
- A **model router**: picks a provider-agnostic capability tier (`FAST`/`STANDARD`/`ADVANCED`/`FRONTIER`) and, independently, a reasoning effort (`none`..`max`), using the minimum amount of intelligence and compute capable of meeting the required quality level. A single maxed-out risk signal (e.g. `security_impact=1.0`) floors the tier/reasoning independently of the blended score, so it can never be diluted away.
- A **multi-agent planner**: decides whether a task needs a lone Builder or a staffed pipeline (Planner → Researcher(s) → Judge → Builder → Reviewer), and enforces hard compute/cost/agent-count budgets — a plan that would violate policy is never silently returned.
- An **escalation engine**: raises tier/reasoning on evidence (repeated failure, uncertainty, risk, insufficient context, failed tests, stalled execution) — never on elapsed time alone — bounded by a configurable escalation limit and a *cumulative*, execution-wide cost/token ledger (`ExecutionBudgetLedger`) shared across the initial plan, every escalation, and every recovery attempt.
- An **`ExecutionWatchdog`** runtime coordinator: a small event-oriented API (`start`, `record_activity`, `record_progress`, `evaluate`, `begin_recovery`, `record_recovery_result`, `checkpoint`, `complete`, `status`) that *enforces* the underlying state machine (`PLANNING`/`RUNNING`/`TESTING`/`REVIEWING`/`STALLED`/`RECOVERING`/`BLOCKED`/`COMPLETED`) so an execution can never silently "go quiet" and get mistaken for done. Callers never call the low-level state machine directly. Stall detection combines multiple independent signals (never time alone); recovery is bounded and never repeats a strategy; `BLOCKED` is only reachable with a validated, non-blank structured report attached.

## What AgentGear IS NOT

- **Not a context builder.** AgentGear can say "I need context on AUTH, budget 8000 tokens" via the abstract `ContextProvider` interface, but it does not build a context graph. That is [PromptGraph](https://github.com/Human-Weapon/PromptGraph)'s job.
- **Not a skill/security validator.** AgentGear does not decide whether a skill, plugin, or automation is safe to run. That is SkillGuard's job.
- **Not a benchmarking tool.** AgentGear does not measure which strategy performed best historically, though it defines a stable `EvidenceSource` interface an external benchmarking tool (AgentBench) could implement in the future. Not wired into routing in v0.1.0.
- **Not a project-improvement tool.** It does not suggest what to refactor or clean up — that is ProjectKaizen's job.
- **Not a model provider client.** v0.1.0 never calls OpenAI, Anthropic, Gemini, or any other real API. `ExecutionWatchdog` supervises the *state* of an execution an external runtime drives (records activity/progress it's told about, decides when to stall/recover/block) — it never spawns, calls, or owns a provider process itself.

## Install

```bash
pip install agentgear            # core (stdlib only)
pip install agentgear[yaml]      # + YAML policy config files
pip install agentgear[dev]       # + pytest, ruff, build
```

Requires Python 3.10+. No network access or API keys are ever required.

## Standalone usage

```python
import agentgear

task = agentgear.TaskProfile(
    description="Refactor authentication across 8 files",
    files_affected=8,
    modules_affected=3,
    architectural_impact=0.4,
    security_impact=0.6,
    ambiguity=0.3,
    existing_test_coverage=0.5,
)

plan = agentgear.plan(task)  # uses Policy.default()

print(plan.primary_model.tier, plan.primary_model.reasoning)
for agent in plan.strategy.agents:
    print(agent.role, agent.tier, agent.reasoning, agent.count)
print(plan.rationale)
```

Or from the CLI:

```bash
agentgear plan --task "Refactor authentication across 8 files" \
  --files 8 --modules 3 --architectural 0.4 --security 0.6 --ambiguity 0.3
```

```bash
agentgear analyze --task "Rename a local variable" --json
agentgear status --state-dir ./.agentops/metrics --execution-id my-run-1
agentgear simulate --task "Fix a flaky test" --repeated-failures 2 --json
```

## Model routing

AgentGear routes to one of four **provider-agnostic** capability tiers:

```
FAST < STANDARD < ADVANCED < FRONTIER
```

Tiers are mapped to real model/provider names only through `Policy.model_tier_mapping` — the router never hardcodes a model name. Example initial policy:

```yaml
model_tier_mapping:
  fast: Luna
  standard: Luna
  advanced: Terra
  frontier: Sol
```

This is a *policy*, not a rule baked into the router — point it at any provider you like.

Routing combines complexity and risk into a single score and walks a configurable threshold ladder, picking the **cheapest tier that clears the threshold** — this directly encodes "use the minimum amount of intelligence and compute capable of meeting the required quality level." A blended risk score at or above 0.85 forces a minimum of `ADVANCED`. `Policy.routing_weights` (cost/quality/latency — latency shares cost's direction, both favor cheaper/faster tiers; quality favors richer ones) shift the thresholds without ever defaulting to the most powerful tier.

Independently of that blended score, `Policy.critical_risk` floors the tier/reasoning (and forces a Reviewer) whenever a single raw signal — `security_impact`, `data_impact`, or irreversibility — crosses its own threshold (default 0.85), even on an otherwise trivial task:

```python
from agentgear import Policy, TaskProfile, plan

task = TaskProfile(description="Change how session tokens are stored", security_impact=1.0)
result = plan(task, Policy())
assert result.primary_model.tier.value in ("advanced", "frontier")
assert result.review_required is True
```

A blended score alone can dilute one maxed-out signal into a merely "moderate" average; `critical_risk` exists specifically so that never happens.

For the same reason, the public `assess_risk()` result labels a maximum
individual security, data, or irreversibility signal `CRITICAL` and names it
in the rationale. Its numeric `score` remains the documented weighted
heuristic; do not interpret that score alone as the complete risk decision.

## Reasoning levels

Reasoning effort (`none`, `low`, `medium`, `high`, `xhigh`, `max`) is a **separate dimension** from model tier, using its own score blend (weighted toward risk) and its own threshold set (`Policy.reasoning_thresholds`). `tier=X, reasoning=high` is never assumed equivalent to `tier=Y, reasoning=low` for a different tier — the two dimensions are computed and configured independently.

| Task | Tier | Reasoning |
|---|---|---|
| Rename a variable | FAST | low |
| Normal implementation | STANDARD | medium |
| Complex debugging | ADVANCED | high |
| Repository architecture | FRONTIER | medium |
| Exceptional architectural/security problem | FRONTIER | high/xhigh |

(Illustrative under default policy at typical signal values — your thresholds may route differently, which is the point.)

## Multi-agent planning

- **Single agent** (a lone Builder) for low-complexity, low-risk, unambiguous work.
- A **Planner** joins when architectural impact is high.
- **Researcher(s)** join when ambiguity/novelty is high enough that evidence-gathering before building is worth it — two researchers only when ambiguity is high enough to expect genuinely divergent proposals.
- A **Judge** joins only when there is something to judge: ≥2 researcher proposals, or risk high enough to warrant independent evaluation before the Builder acts.
- A **Reviewer** closes every multi-agent pipeline (Builder implements → Reviewer verifies) — never added to a lone trivial Builder run.

`Policy.budget.max_agents` (and the other hard budgets — context tokens, estimated cost, estimated tokens) are enforced by raising `BudgetExceededError`. AgentGear never silently returns a plan that violates policy; a task whose honest staffing needs exceed your budget must be blocked and told so, or your budget must be raised.

## The Execution Watchdog

The core reliability feature, present from v0.1.0. **Never stop silently.**

```
PLANNING → RUNNING → TESTING/REVIEWING → COMPLETED
              ↓            ↓
           STALLED ──→ RECOVERING ──→ BLOCKED
```

Drive it entirely through `agentgear.ExecutionWatchdog` — you never call the low-level state machine yourself; the coordinator enforces every transition:

```python
from agentgear import ExecutionWatchdog, Policy
from agentgear.models import ExecutionState, ProgressSignalKind, RecoveryResult

watchdog = ExecutionWatchdog("run-1", Policy(), state_dir="./.agentops/metrics")
watchdog.start(task="Refactor authentication", at_seconds=0.0)

watchdog.record_progress(
    at_seconds=5.0, kind=ProgressSignalKind.FILE_CHANGED, description="edited auth.py"
)
watchdog.record_activity(at_seconds=6.0, fingerprint="pytest -k auth", succeeded=True)

# A stall (busy activity with no genuine progress) is detected automatically
# by record_activity()/evaluate() and drives RUNNING -> STALLED -> RECOVERING
# for you -- you never have to remember to check for it yourself.
if watchdog.state == ExecutionState.RECOVERING:
    watchdog.record_recovery_result(at_seconds=7.0, result=RecoveryResult.SUCCESS)

watchdog.checkpoint(at_seconds=8.0, phase="implementation", completed=("auth.py",))
watchdog.advance(ExecutionState.REVIEWING, at_seconds=9.0)
watchdog.complete(at_seconds=10.0, evidence=("all tests pass",))

print(watchdog.status())
```

When supervision begins from an `ExecutionPlan`, construct it with
`ExecutionWatchdog.from_plan(...)` instead. That binds the coordinator to the
plan's chosen tier, reasoning, context budget, and complete initial
multi-agent token/cost estimate; `start()` then commits that estimate before
the execution becomes `RUNNING`. A directly constructed watchdog is for an
external runtime that has no plan object and must provide any known initial
charge explicitly through `start(initial_tokens=..., initial_cost=...)`.

What the coordinator composes and enforces on your behalf:

- Only the documented state transitions are ever legal — the coordinator makes every one of them internally; nothing in application code calls `ExecutionStateMachine.transition` directly.
- `complete()` requires non-empty evidence — idle, silence, or "no more tool calls" is never treated as done (`NotCompletedError` otherwise).
- `record_activity()` re-evaluates for a stall on every call and automatically drives RUNNING/TESTING/REVIEWING → STALLED → RECOVERING (or straight to a validated BLOCKED, if recovery is already exhausted) — stall detection combines elapsed time (measured from the execution's start if it has *never* made progress, never leaving that case undetectable), attempt counts, repeated identical failures, circular attempts, and abnormally slow trivial commands, all scoped to activity *after* the most recent genuine progress. **Time alone never triggers STALLED.**
- Recovery walks a fixed strategy ladder (re-read error → inspect assumptions → split task → change approach → restore checkpoint → restart tool → use another agent → increase reasoning → change model tier → request human intervention), never repeating a strategy, bounded by `Policy.watchdog.max_recovery_attempts` and an independent `LoopGuard` (identical failures, no-progress cycles, total attempts, model escalations).
- `BLOCKED` is reachable through exactly one internal path, and it always attaches a validated `BlockedReport` (non-blank blocker/root cause/recommended action, coherent evidence and strategies) — there is no way to reach BLOCKED with an empty or missing report.
- `checkpoint()` / the heartbeat written after every call persist lightweight, atomic, schema-validated, path-contained state (`state_dir=...`) so an external observer can answer "what is this execution doing right now" (`agentgear status`) — a heartbeat or checkpoint file that fails schema validation is quarantined and reported as `CorruptStorageError`, never silently misread. A separate-process reader (like the CLI) can only ever see the last successfully *written* heartbeat, not the in-process `ExecutionWatchdog`'s live in-memory state — if a heartbeat write itself failed, the in-process caller sees that immediately (see `heartbeat_dirty`/`sync_heartbeat()` on `ExecutionWatchdog`), but an external reader has no way to know the durable file is stale until it's resynchronized.

## Escalation

`watchdog.record_escalation(...)` (or `agentgear.escalation.decide_escalation(...)` directly) raises tier/reasoning on evidence:

- A single failure does **not** automatically escalate; ≥2 repeated failures do.
- `security_risk`/`architectural_risk` signals jump directly to `FRONTIER` rather than climbing the ladder step by step.
- Bounded by `Policy.watchdog.max_model_escalations` and, when driven through `ExecutionWatchdog`, a shared `ExecutionBudgetLedger` covering the *entire* execution — the initial plan, every prior escalation, and every recovery attempt draw from the same cumulative pool, so two escalations that are each individually affordable can still be correctly denied once what's already been spent is accounted for.
- **Elapsed time never triggers escalation by itself.**
- Every signal (`repeated_failures`, `uncertainty`, ...) is validated on construction — out-of-range, NaN/Infinity, or negative values raise `InvalidObservationError` instead of silently corrupting a decision.

## Optional integrations

### PromptGraph

```python
from agentgear.context_provider import PromptGraphContextProvider, ContextRequest

provider = PromptGraphContextProvider(my_promptgraph_instance)
package = provider.request(ContextRequest(topic="AUTH", budget_tokens=8000))
```

If PromptGraph is not installed, or the supplied instance doesn't expose a usable `memory.search()`, this degrades gracefully to the same shape `DefaultContextProvider` returns, with a `note` explaining why — it never raises. AgentGear never imports PromptGraph at module load time; availability is checked at call time via `importlib.util.find_spec`.

`ContextPackage.used_tokens` is always the token estimate of the *actual* returned `content` (including chunk separators), so `used_tokens <= budget_tokens` holds by construction — never a sum of pre-join per-chunk estimates that could understate the real size. `ContextRequest.constraints` are echoed back as `constraints_requested`; `constraints_applied` is always `()` in v0.1.0, since no provider actually enforces them yet — AgentGear never claims credit for filtering that didn't happen.

### AgentBench (interface only)

`agentgear.benchmark_interface.EvidenceSource` defines the shape of evidence (success rate, cost, latency, regression rate, stall rate, recovery rate) a future AgentBench integration could feed into policy tuning. **Not implemented and not wired into routing in v0.1.0.**

## Configuration

Everything with a magic number lives in `agentgear.config.Policy` and is validated on construction (negative budgets, contradictory thresholds, unknown reasoning effort, non-finite floats, zero retries, etc. all raise `ConfigurationError` immediately):

```python
from agentgear import Policy

policy = Policy.from_dict(
    {
        "budget": {"max_agents": 6, "max_estimated_cost": 2.0},
        "watchdog": {"max_recovery_attempts": 5},
        "critical_risk": {"security_impact_at": 0.8, "min_tier": "frontier"},
        "model_tier_mapping": {
            "fast": "Luna",
            "standard": "Luna",
            "advanced": "Terra",
            "frontier": "Sol",
        },
    }
)
```

or from a YAML/JSON file (`agentgear plan --config policy.yaml`, requires `agentgear[yaml]` for YAML).

## Security

- No network calls, ever. No API keys, no credentials, no `.env` reading.
- No arbitrary shell execution, no package installation, no writes outside a caller-supplied state directory.
- All persistent writes (heartbeats, checkpoints) go through a path-contained, atomic, concurrency-safe JSON store (symlink/junction-aware; see [SECURITY.md](SECURITY.md)).
- Optional sibling integration degrades gracefully and is never imported unconditionally.

See [SECURITY.md](SECURITY.md) for the full policy and how to report a vulnerability.

## Known limitations

- Complexity/risk scoring is a documented, configurable **heuristic** — it is explainable and deterministic, not a scientifically validated measure of "true" task difficulty.
- The relative cost model (`routing.RELATIVE_COST_PER_1K_TOKENS`) varies by model **tier only**, not by reasoning effort — escalating reasoning without changing tier is treated as cost-neutral by budget/escalation checks, even though higher reasoning effort has real compute cost in practice.
- `PromptGraphContextProvider` requires the caller to construct and pass in a `promptgraph.PromptGraph` instance; AgentGear does not create or own one. Its integration surface (`memory.search`) is best-effort against PromptGraph's current public API and may need updates as PromptGraph evolves. It also implements no constraint enforcement in v0.1.0 (`constraints_applied` is always `()`).
- The `AgentBench` evidence interface is unimplemented in v0.1.0; routing does not yet learn from historical outcomes.
- v0.1.0 does not execute real agents against real model providers — `ExecutionWatchdog` supervises the state of an execution an external runtime drives; wiring it to actual provider calls is a future adapter layer, deliberately out of scope so AgentGear stays testable and network-free.
- `ExecutionWatchdog`'s clock is caller-supplied (`at_seconds`), not a real wall clock: it guarantees timestamps are never reported out of order, but it cannot independently verify a caller is reporting truthful elapsed time — the same trust boundary as any event-sourced system with an external clock.
- `ExecutionWatchdog` (and the `ExecutionBudgetLedger` it owns) is a **single-writer coordinator** in v0.1.0 — it holds no internal lock, and its methods are not safe to call concurrently against the same instance from multiple threads or processes. If multiple agents/workers report events for one execution, serialize their submission externally before it reaches one `ExecutionWatchdog`. This is independent of the persistence layer (`HeartbeatWriter`, `CheckpointStore`), which IS safe for concurrent/multiprocess writers.
- `complete(evidence=...)` and progress/activity reporting validate structure and presence (non-blank strings, a real evidence tuple, chronological timestamps) but cannot independently verify that a caller's textual claim ("tests pass") is actually true, or that reported "progress"/"recovery success" reflects genuine forward movement on the underlying task rather than, say, a recovery mechanism regaining the ability to act without the original problem being fixed. AgentGear preserves and enforces the *structure* of the audit trail; the runtime/provider integration supplying evidence is responsible for its *honesty*.
- macOS CI is **NOT VERIFIED** — CI runs on Windows and Ubuntu only for v0.1.0.

## Roadmap

- Provider adapter layer to actually execute an `ExecutionPlan` (opt-in, still requiring no mandatory API keys for the rest of the package to function).
- `AgentBench` evidence consumption to tune routing thresholds from observed outcomes.
- Richer `ContextProvider` integrations beyond PromptGraph's technical-memory search, including real constraint enforcement.
- Expanded stall-detection fingerprinting (e.g. structural similarity of tool arguments, not just exact fingerprint match).
- A reasoning-effort-aware cost model (today only tier affects the relative cost estimate).

## Development

```bash
git clone <this-repo> && cd agentgear
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,yaml]"
pytest
ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
