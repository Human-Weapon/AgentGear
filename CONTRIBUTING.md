# Contributing to AgentGear

Thanks for considering contributing! This is part of the HERMES OSS ecosystem. By participating you agree to the ecosystem principles: **USEFUL ALONE + BETTER TOGETHER**, security by default, auditability, and evidence over confidence.

## Before you start

- **SEARCH before you create** — check whether the feature already exists or belongs in a sibling project. AgentGear's responsibility is **deciding how a task should be executed** (model tier, reasoning effort, agent staffing, escalation, watchdog/recovery). It does not decide *what context* to load (PromptGraph), whether a skill is safe (SkillGuard), what strategy performed best historically (AgentBench), or what to improve in a project (ProjectKaizen).
- **EXTEND before you duplicate** — improve existing modules instead of adding overlapping ones.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,yaml]"
```

## Running quality checks

```bash
pytest                      # run tests
ruff check .                 # lint
ruff format --check .        # formatting
```

## Commit conventions

Use small, focused commits with conventional prefixes:

- `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `perf:`, `security:`, `chore:`

Never commit secrets. Run `git diff` + `ruff` + `pytest` before committing.

## Standards

- **No telemetry.** The package must never phone home or collect data.
- **No network calls, no API keys.** Routing is a logical/configurable decision; AgentGear does not call real model providers in v0.1.0.
- **Standalone by default.** Optional sibling integration (PromptGraph) must degrade gracefully — never import a sibling unconditionally.
- **Determinism.** Same task + same policy must always produce the same `ExecutionPlan`. Add a regression test for anything that could break this.
- **No silent stalls.** Any change to the watchdog/state-machine/recovery path needs a test proving the execution never silently disappears (see `tests/adversarial/`).
- **Hard budgets stay hard.** A change that could make `planning.py` return a plan violating `Policy.budget` is a bug, not a feature — it must raise `BudgetExceededError` instead.
- **Evidence > confidence.** Do not claim performance or correctness without tests. Follow the regression standard: reproduce the bug with a failing test first, then fix it.
- Aim for green: `pytest` + `ruff check` + `ruff format --check` must pass before merge.
