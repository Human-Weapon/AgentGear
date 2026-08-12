"""Structured BLOCKED reporting (principle #19: no silence on failure).

Reaching BLOCKED must always produce a ``BlockedReport`` — never a bare
exception, never a quiet stop. The report is deliberately structured (not
free text) so a human or a downstream tool can act on it immediately.

AG-09: a report with a blank blocker/root_cause/recommendation, or a
negative attempt count, is not "structured" — it's a shell that looks like
a report but carries nothing a human can act on. ``build_blocked_report``
rejects those the same way any other AgentGear boundary rejects
nonsensical input, so BLOCKED can never be reached with an empty report.
"""

from __future__ import annotations

from ..exceptions import InvalidBlockedReportError
from ..models import BlockedReport, Checkpoint


def _require_non_blank(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidBlockedReportError(f"{name} must be a non-empty, non-blank string")
    return value


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
    _require_non_blank("blocker", blocker)
    _require_non_blank("root_cause", root_cause)

    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise InvalidBlockedReportError(f"attempts must be an int, got {type(attempts).__name__}")
    if attempts < 0:
        raise InvalidBlockedReportError(f"attempts must be >= 0, got {attempts}")

    for label, items in (("strategies_tried", strategies_tried), ("evidence", evidence)):
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise InvalidBlockedReportError(f"{label} must contain only non-blank strings")

    if recommended_human_action is None:
        recommended_human_action = _default_recommendation(strategies_tried)
    else:
        _require_non_blank("recommended_human_action", recommended_human_action)

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
