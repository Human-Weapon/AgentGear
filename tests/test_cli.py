from __future__ import annotations

import json

import pytest

from agentgear.checkpoints import CheckpointStore
from agentgear.cli import main
from agentgear.exceptions import AgentGearError
from agentgear.models import Checkpoint, ExecutionState
from agentgear.watchdog.heartbeat import HeartbeatWriter, build_heartbeat


def test_analyze_json_exit_zero(capsys) -> None:
    code = main(["analyze", "--task", "rename a variable", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert "complexity" in out
    assert "risk" in out


def test_plan_json_exit_zero(capsys) -> None:
    code = main(["plan", "--task", "add an endpoint", "--files", "3", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["primary_model"]["tier"] in ("fast", "standard", "advanced", "frontier")
    assert "rationale" in out


def test_plan_human_output_is_not_json(capsys) -> None:
    code = main(["plan", "--task", "add an endpoint"])
    assert code == 0
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "task:" in out


def test_missing_task_argument_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["plan"])
    assert exc_info.value.code == 2


def test_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-real-command"])
    assert exc_info.value.code == 2


def test_missing_config_file_exits_one(capsys) -> None:
    code = main(["plan", "--task", "x", "--config", "does-not-exist.json"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_budget_exceeded_via_config_exits_one(tmp_path, capsys) -> None:
    config = tmp_path / "policy.json"
    config.write_text(json.dumps({"budget": {"max_context_budget_tokens": 1}}), encoding="utf-8")
    code = main(["plan", "--task", "x", "--config", str(config)])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_debug_flag_reraises_instead_of_swallowing() -> None:
    with pytest.raises(AgentGearError):
        main(["--debug", "plan", "--task", "x", "--config", "does-not-exist.json"])


def test_status_missing_execution_exits_one(tmp_path, capsys) -> None:
    code = main(["status", "--state-dir", str(tmp_path), "--execution-id", "nope"])
    assert code == 1
    assert "no heartbeat found" in capsys.readouterr().err


def test_status_corrupt_heartbeat_exits_one_cleanly(tmp_path, capsys) -> None:
    (tmp_path / "exec-1.heartbeat.json").write_text("{}", encoding="utf-8")
    code = main(["status", "--state-dir", str(tmp_path), "--execution-id", "exec-1"])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_status_reports_heartbeat_and_checkpoint(tmp_path, capsys) -> None:
    writer = HeartbeatWriter(tmp_path)
    writer.write(
        build_heartbeat(
            execution_id="exec-1",
            state=ExecutionState.RUNNING,
            current_task="task",
            current_subtask=None,
            last_real_progress_at=1.0,
            last_progress_evidence=None,
            attempt_count=1,
            current_strategy=None,
            last_error=None,
        )
    )
    CheckpointStore(tmp_path).append(
        Checkpoint(execution_id="exec-1", phase="build", at_seconds=1.0)
    )
    code = main(["status", "--state-dir", str(tmp_path), "--execution-id", "exec-1", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "running"
    assert out["latest_checkpoint"]["phase"] == "build"


def test_simulate_reports_escalation_decision(capsys) -> None:
    code = main(
        [
            "simulate",
            "--task",
            "x",
            "--repeated-failures",
            "2",
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["should_escalate"] is True


def test_cli_works_with_no_network_or_api_keys(monkeypatch, capsys) -> None:
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    code = main(["plan", "--task", "x", "--json"])
    assert code == 0


def test_simulate_rejects_out_of_range_uncertainty_cleanly(capsys) -> None:
    code = main(["simulate", "--task", "x", "--uncertainty", "999.0"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "uncertainty" in err
