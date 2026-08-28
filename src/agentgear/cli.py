"""AgentGear CLI.

Every command works with zero network access and zero API keys: routing
is a logical/configurable decision, not a call to a real model provider.

Exit codes:
  0  success
  1  a known AgentGear error (bad config, budget exceeded, invalid task, ...)
  2  CLI usage error (argparse's own convention)
Tracebacks are suppressed unless ``--debug`` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .analysis import assess_complexity, assess_risk
from .checkpoints import CheckpointStore
from .config import Policy
from .escalation import EscalationSignals, decide_escalation
from .exceptions import AgentGearError
from .models import TaskProfile
from .planning import build_execution_plan
from .watchdog.heartbeat import HeartbeatWriter


def _add_task_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True, help="Short description of the task")
    parser.add_argument("--files", type=int, default=1, help="Files affected (default: 1)")
    parser.add_argument("--modules", type=int, default=1, help="Modules affected (default: 1)")
    parser.add_argument(
        "--architectural", type=float, default=0.0, help="Architectural impact 0..1"
    )
    parser.add_argument("--security", type=float, default=0.0, help="Security impact 0..1")
    parser.add_argument("--data-impact", type=float, default=0.0, help="Data impact 0..1")
    parser.add_argument("--ambiguity", type=float, default=0.0, help="Ambiguity 0..1")
    parser.add_argument("--novelty", type=float, default=0.0, help="Novelty 0..1")
    parser.add_argument("--reversibility", type=float, default=1.0, help="Reversibility 0..1")
    parser.add_argument("--coverage", type=float, default=0.5, help="Existing test coverage 0..1")
    parser.add_argument("--prior-failures", type=int, default=0, help="Prior failed attempts")
    parser.add_argument(
        "--expected-tokens", type=int, default=2000, help="Expected output size in tokens"
    )


def _task_profile_from_args(args: argparse.Namespace) -> TaskProfile:
    return TaskProfile(
        description=args.task,
        files_affected=args.files,
        modules_affected=args.modules,
        architectural_impact=args.architectural,
        security_impact=args.security,
        data_impact=args.data_impact,
        ambiguity=args.ambiguity,
        novelty=args.novelty,
        reversibility=args.reversibility,
        existing_test_coverage=args.coverage,
        prior_failures=args.prior_failures,
        expected_output_tokens=args.expected_tokens,
    )


def _load_policy(config_path: str | None) -> Policy:
    if not config_path:
        return Policy.default()
    path = Path(config_path)
    if not path.exists():
        raise AgentGearError(f"config file not found: {config_path}")
    if path.suffix in (".yaml", ".yml"):
        return Policy.from_yaml(str(path))
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return Policy.from_dict(data)
    raise AgentGearError(f"unsupported config file extension: {path.suffix} (use .json/.yaml/.yml)")


def _print(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    _print_human(data)


def _print_human(data: dict[str, Any], indent: int = 0) -> None:
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{pad}{key}:")
            _print_human(value, indent + 1)
        elif isinstance(value, list):
            print(f"{pad}{key}:")
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    print(f"{pad}  - [{i}]")
                    _print_human(item, indent + 2)
                else:
                    print(f"{pad}  - {item}")
        else:
            print(f"{pad}{key}: {value}")


def _cmd_analyze(args: argparse.Namespace) -> int:
    profile = _task_profile_from_args(args)
    complexity = assess_complexity(profile)
    risk = assess_risk(profile)
    _print(
        {
            "task": profile.description,
            "complexity": {
                "score": round(complexity.score, 3),
                "level": complexity.level.value,
                "rationale": complexity.rationale,
                "factors": {k: round(v, 3) for k, v in complexity.factors.items()},
            },
            "risk": {
                "score": round(risk.score, 3),
                "level": risk.level.value,
                "rationale": risk.rationale,
                "factors": {k: round(v, 3) for k, v in risk.factors.items()},
            },
        },
        args.json,
    )
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    profile = _task_profile_from_args(args)
    policy = _load_policy(args.config)
    complexity = assess_complexity(profile)
    risk = assess_risk(profile)
    exec_plan = build_execution_plan(profile, complexity, risk, policy)

    _print(
        {
            "task": profile.description,
            "complexity": {"score": round(complexity.score, 3), "level": complexity.level.value},
            "risk": {"score": round(risk.score, 3), "level": risk.level.value},
            "primary_model": {
                "tier": exec_plan.primary_model.tier.value,
                "reasoning": exec_plan.primary_model.reasoning.value,
                "resolved_model": exec_plan.primary_model.resolved_model,
            },
            "agents": [
                {
                    "role": a.role.value,
                    "tier": a.tier.value,
                    "reasoning": a.reasoning.value,
                    "count": a.count,
                }
                for a in exec_plan.strategy.agents
            ],
            "judge_required": exec_plan.strategy.judge_required,
            "execution_order": [r.value for r in exec_plan.strategy.execution_order],
            "context_budget_tokens": exec_plan.context_budget_tokens,
            "max_estimated_cost": round(exec_plan.max_estimated_cost, 4),
            "max_agents": exec_plan.max_agents,
            "review_required": exec_plan.review_required,
            "escalation_policy": exec_plan.escalation_policy_summary,
            "recovery_policy": exec_plan.recovery_policy_summary,
            "actionable_context": {
                "affected_files": list(exec_plan.actionable_context.affected_files),
                "dependencies": list(exec_plan.actionable_context.dependencies),
                "acceptance_criteria": list(exec_plan.actionable_context.acceptance_criteria),
                "verification": list(exec_plan.actionable_context.verification),
                "rollback_strategy": exec_plan.actionable_context.rollback_strategy,
            },
            "rationale": list(exec_plan.rationale),
        },
        args.json,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    writer = HeartbeatWriter(args.state_dir)
    heartbeat = writer.read(args.execution_id)
    if heartbeat is None:
        print(f"no heartbeat found for execution_id={args.execution_id!r}", file=sys.stderr)
        return 1

    store = CheckpointStore(args.state_dir)
    latest_checkpoint = store.latest(args.execution_id)

    _print(
        {
            "execution_id": heartbeat.execution_id,
            "state": heartbeat.state.value,
            "current_task": heartbeat.current_task,
            "current_subtask": heartbeat.current_subtask,
            "last_real_progress_at": heartbeat.last_real_progress_at,
            "last_progress_evidence": heartbeat.last_progress_evidence,
            "attempt_count": heartbeat.attempt_count,
            "current_strategy": heartbeat.current_strategy,
            "last_error": heartbeat.last_error,
            "pending_work": list(heartbeat.pending_work),
            "latest_checkpoint": (
                {
                    "phase": latest_checkpoint.phase,
                    "completed": list(latest_checkpoint.completed),
                    "pending": list(latest_checkpoint.pending),
                }
                if latest_checkpoint
                else None
            ),
        },
        args.json,
    )
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    profile = _task_profile_from_args(args)
    policy = _load_policy(args.config)
    complexity = assess_complexity(profile)
    risk = assess_risk(profile)
    exec_plan = build_execution_plan(profile, complexity, risk, policy)

    signals = EscalationSignals(
        repeated_failures=args.repeated_failures,
        uncertainty=args.uncertainty,
        architectural_risk=args.architectural_risk,
        security_risk=args.security_risk,
        insufficient_context=args.insufficient_context,
        failed_tests=args.failed_tests,
        stalled=args.stalled,
    )
    decision = decide_escalation(
        current_tier=exec_plan.primary_model.tier,
        current_reasoning=exec_plan.primary_model.reasoning,
        escalations_used=args.escalations_used,
        signals=signals,
        policy=policy,
        context_budget_tokens=exec_plan.context_budget_tokens,
    )

    _print(
        {
            "initial_tier": exec_plan.primary_model.tier.value,
            "initial_reasoning": exec_plan.primary_model.reasoning.value,
            "should_escalate": decision.should_escalate,
            "reason": decision.reason,
            "next_tier": decision.next_tier.value if decision.next_tier else None,
            "next_reasoning": decision.next_reasoning.value if decision.next_reasoning else None,
            "rationale": list(decision.rationale),
        },
        args.json,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentgear", description="Adaptive compute orchestrator")
    parser.add_argument("--debug", action="store_true", help="Show full tracebacks on error")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_analyze = subparsers.add_parser("analyze", help="Analyze a task's complexity and risk")
    _add_task_args(p_analyze)
    p_analyze.add_argument("--json", action="store_true")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_plan = subparsers.add_parser("plan", help="Generate a full execution plan for a task")
    _add_task_args(p_plan)
    p_plan.add_argument("--config", help="Path to a .json/.yaml policy config file")
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=_cmd_plan)

    p_status = subparsers.add_parser("status", help="Show the latest heartbeat/checkpoint")
    p_status.add_argument("--state-dir", required=True, help="Directory holding watchdog state")
    p_status.add_argument("--execution-id", required=True)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_status)

    p_sim = subparsers.add_parser(
        "simulate", help="Simulate an escalation decision without any real provider"
    )
    _add_task_args(p_sim)
    p_sim.add_argument("--config", help="Path to a .json/.yaml policy config file")
    p_sim.add_argument("--repeated-failures", type=int, default=0)
    p_sim.add_argument("--uncertainty", type=float, default=0.0)
    p_sim.add_argument("--architectural-risk", action="store_true")
    p_sim.add_argument("--security-risk", action="store_true")
    p_sim.add_argument("--insufficient-context", action="store_true")
    p_sim.add_argument("--failed-tests", action="store_true")
    p_sim.add_argument("--stalled", action="store_true")
    p_sim.add_argument("--escalations-used", type=int, default=0)
    p_sim.add_argument("--json", action="store_true")
    p_sim.set_defaults(func=_cmd_simulate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AgentGearError as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, TypeError) as exc:
        if args.debug:
            raise
        print(f"error: invalid input: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
