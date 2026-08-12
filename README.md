# AgentGear

**Adaptive compute orchestrator for AI software-engineering agents.**

AgentGear decides **how** a task should be executed: which model capability tier, how much reasoning effort, how many agents, which roles, when to escalate, when to recover, and when to stop because something is stuck. It runs with **zero network access and zero API keys** — routing is a logical, configurable decision, not a call to a real model provider.

Part of the HERMES OSS ecosystem (PromptGraph, AgentGear, SkillGuard, AgentBench, ProjectKaizen). Every tool in the ecosystem is **useful alone, better together** — AgentGear has no required dependency on any sibling project.

## What AgentGear IS

- A **task analyzer**: turns raw signals (files affected, architectural impact, security impact, ambiguity, novelty, reversibility, ...) into an explainable, deterministic complexity/risk score.
- A **model router**: picks a provider-agnostic capability tier (`FAST`/`STANDARD`/`ADVANCED`/`FRONTIER`) and, independently, a reasoning effort (`none`..`max`), using the minimum amount of intelligence and compute capable of meeting the required quality level.
- A **multi-agent planner**: decides whether a task needs a lone Builder or a staffed pipeline (Planner → Researcher(s) → Judge → Builder → Reviewer), and enforces hard compute/cost/agent-count budgets — a plan that would violate policy is never silently returned.
- An **escalation engine**: raises tier/reasoning on evidence (repeated failure, uncertainty, risk, insufficient context, failed tests, stalled execution) — never on elapsed time alone — bounded by a configurable escalation limit and cost budget.
- An **Execution Watchdog**: an explicit state machine (`PLANNING`/`RUNNING`/`TESTING`/`REVIEWING`/`STALLED`/`RECOVERING`/`BLOCKED`/`COMPLETED`) so an execution can never silently "go quiet" and get mistaken for done. Stall detection combines multiple independent signals (never time alone); recovery is bounded and never repeats a strategy; `BLOCKED` always produces a structured report.

## What AgentGear IS NOT

- **Not a context builder.** AgentGear can say "I need context on AUTH, budget 8000 tokens" via the abstract `ContextProvider` interface, but it does not build a context graph. That is [PromptGraph](https://github.com/hermes-oss/promptgraph)'s job.
- **Not a skill/security validator.** AgentGear does not decide whether a skill, plugin, or automation is safe to run. That is SkillGuard's job.
- **Not a benchmarking tool.** AgentGear does not measure which strategy performed best historically, though it defines a stable `EvidenceSource` interface an external benchmarking tool (AgentBench) could implement in the future. Not wired into routing in v0.1.0.
- **Not a project-improvement tool.** It does not suggest what to refactor or clean up — that is ProjectKaizen's job.
- **Not a model provider client.** v0.1.0 never calls OpenAI, Anthropic, Gemini, or any other real API. It produces a *plan*; executing that plan against a real provider is left to the caller.

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

Routing combines complexity and risk into a single score and walks a configurable threshold ladder, picking the **cheapest tier that clears the threshold** — this directly encodes "use the minimum amount of intelligence and compute capable of meeting the required quality level." A risk score at or above 0.85 forces a minimum of `ADVANCED`, regardless of complexity (a one-line change to how session tokens are stored is still routed carefully). `Policy.routing_weights` (cost/quality/latency) shift the thresholds without ever defaulting to the most powerful tier.

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

- `agentgear.watchdog.ExecutionStateMachine` only allows the documented transitions; anything else raises `InvalidStateTransitionError`.
- `COMPLETED` requires non-empty evidence — idle, silence, or "no more tool calls" is never treated as done (`NotCompletedError` otherwise).
- `ProgressTracker` only ever holds genuine `ProgressEvent`s (subtask completed, file meaningfully changed, test status improved, error resolved, ...); raw activity (`ActivityRecord`) is tracked separately, so "busy but not progressing" is representable.
- `StallDetector` combines elapsed time, attempt counts, repeated identical failures, circular attempts, and abnormally slow trivial commands — **time alone never triggers STALLED**.
- `RecoveryEngine` walks a fixed strategy ladder (re-read error → inspect assumptions → split task → change approach → restore checkpoint → restart tool → use another agent → increase reasoning → change model tier → request human intervention), never repeating a strategy, bounded by `Policy.watchdog.max_recovery_attempts`.
- `LoopGuard` independently bounds identical failures, recovery attempts, no-progress cycles, total attempts, and model escalations — none of these limits auto-increases to hide a stuck execution.
- `build_blocked_report(...)` always produces a structured `BlockedReport` (blocker, root cause, last checkpoint, attempts, strategies tried, evidence, recommended human action) — `BLOCKED` is never a bare exception or a quiet stop.
- `HeartbeatWriter` / `CheckpointStore` persist lightweight, atomic, path-contained state so an external observer can answer "what is this execution doing right now" (`agentgear status`).

## Escalation

`agentgear.escalation.decide_escalation(...)` raises tier/reasoning on evidence:

- A single failure does **not** automatically escalate; ≥2 repeated failures do.
- `security_risk`/`architectural_risk` signals jump directly to `FRONTIER` rather than climbing the ladder step by step.
- Bounded by `Policy.watchdog.max_model_escalations` and the cost budget — escalation that would exceed either is refused with a clear reason instead of silently applied.
- **Elapsed time never triggers escalation by itself.**

## Optional integrations

### PromptGraph

```python
from agentgear.context_provider import PromptGraphContextProvider, ContextRequest

provider = PromptGraphContextProvider(my_promptgraph_instance)
package = provider.request(ContextRequest(topic="AUTH", budget_tokens=8000))
```

If PromptGraph is not installed, or the supplied instance doesn't expose a usable `memory.search()`, this degrades gracefully to the same shape `DefaultContextProvider` returns, with a `note` explaining why — it never raises. AgentGear never imports PromptGraph at module load time; availability is checked at call time via `importlib.util.find_spec`.

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
- `PromptGraphContextProvider` requires the caller to construct and pass in a `promptgraph.PromptGraph` instance; AgentGear does not create or own one. Its integration surface (`memory.search`) is best-effort against PromptGraph's current public API and may need updates as PromptGraph evolves.
- The `AgentBench` evidence interface is unimplemented in v0.1.0; routing does not yet learn from historical outcomes.
- v0.1.0 does not execute real agents against real model providers — it produces an `ExecutionPlan`; wiring that plan to actual provider calls is a future adapter layer, deliberately out of scope so AgentGear stays testable and network-free.
- macOS CI is **NOT VERIFIED** — CI runs on Windows and Ubuntu only for v0.1.0.

## Roadmap

- Provider adapter layer to actually execute an `ExecutionPlan` (opt-in, still requiring no mandatory API keys for the rest of the package to function).
- `AgentBench` evidence consumption to tune routing thresholds from observed outcomes.
- Richer `ContextProvider` integrations beyond PromptGraph's technical-memory search.
- Expanded stall-detection fingerprinting (e.g. structural similarity of tool arguments, not just exact fingerprint match).

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
