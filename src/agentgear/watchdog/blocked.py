"""Structured BLOCKED reporting (principle #19: no silence on failure).

Reaching BLOCKED must always produce a ``BlockedReport`` — never a bare
exception, never a quiet stop. The report is deliberately structured (not
free text) so a human or a downstream tool can act on it immediately.
"""

from __future__ import annotations

from ..models import BlockedReport, Checkpoint


def build_blocked_report(
    *,
    blocker: str,
    root_cause: str,
    last_successful_checkpoint: Checkpoint | None,
    attempts: int,
    strategies_tried: tuple[str, ...],
    evidence: tuple[str, ...],
    files_affected: tuple[str, ...] = (),
    recommended_human_action: str | None = None,
) -> BlockedReport:
    if attempts < 0:
        raise ValueError("attempts must be >= 0")

    if recommended_human_action is None:
        recommended_human_action = _default_recommendation(strategies_tried)

    return BlockedReport(
        blocker=blocker,
        root_cause=root_cause,
        last_successful_checkpoint=last_successful_checkpoint,
        attempts=attempts,
        strategies_tried=strategies_tried,
        evidence=evidence,
        files_affected=files_affected,
        recommended_human_action=recommended_human_action,
    )


def _default_recommendation(strategies_tried: tuple[str, ...]) -> str:
    if not strategies_tried:
        return (
            "No automated recovery was attempted. A human should review the last checkpoint "
            "and decide how to proceed."
        )
    if "request_human_intervention" in strategies_tried:
        return (
            "All automated recovery strategies were exhausted, including a request for human "
            "intervention. A human must review the evidence and either unblock manually or "
            "restart the execution from the last successful checkpoint."
        )
    tried = ", ".join(strategies_tried)
    return (
        f"Automated recovery tried [{tried}] without success. A human should review the "
        "evidence, address the root cause, and either resume (STALLED/BLOCKED -> RECOVERING) "
        "or restart from the last successful checkpoint."
    )
