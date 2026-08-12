"""Explicit execution state machine.

Only the transitions listed in ``_ALLOWED`` are legal; anything else raises
``InvalidStateTransitionError``. ``COMPLETED`` additionally requires
non-empty evidence (principle #20: Idle/silence/no-tool-calls is never
COMPLETED) — the caller must pass concrete evidence strings, e.g. resolved
acceptance criteria, at the moment of transition.

Callers supply ``at_seconds`` explicitly rather than the state machine
reading a real clock: this keeps transitions pure and deterministic, and
tests do not need a fake-clock dependency to exercise time ordering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..exceptions import InvalidStateTransitionError, NotCompletedError
from ..models import ExecutionState

_ALLOWED: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PLANNING: frozenset({ExecutionState.RUNNING, ExecutionState.STALLED}),
    ExecutionState.RUNNING: frozenset(
        {ExecutionState.TESTING, ExecutionState.REVIEWING, ExecutionState.STALLED}
    ),
    ExecutionState.TESTING: frozenset(
        {ExecutionState.RUNNING, ExecutionState.REVIEWING, ExecutionState.STALLED}
    ),
    ExecutionState.REVIEWING: frozenset(
        {ExecutionState.RUNNING, ExecutionState.COMPLETED, ExecutionState.STALLED}
    ),
    ExecutionState.STALLED: frozenset({ExecutionState.RECOVERING}),
    ExecutionState.RECOVERING: frozenset(
        {
            ExecutionState.PLANNING,
            ExecutionState.RUNNING,
            ExecutionState.TESTING,
            ExecutionState.REVIEWING,
            ExecutionState.STALLED,
            ExecutionState.BLOCKED,
        }
    ),
    ExecutionState.BLOCKED: frozenset({ExecutionState.RECOVERING}),
    ExecutionState.COMPLETED: frozenset(),
}


@dataclass(frozen=True)
class TransitionRecord:
    from_state: ExecutionState
    to_state: ExecutionState
    at_seconds: float
    note: str = ""


@dataclass
class ExecutionStateMachine:
    """Tracks the single current ``ExecutionState`` for one execution_id."""

    execution_id: str
    state: ExecutionState = ExecutionState.PLANNING
    history: list[TransitionRecord] = field(default_factory=list)

    def can_transition(self, target: ExecutionState) -> bool:
        return target in _ALLOWED[self.state]

    def transition(
        self,
        target: ExecutionState,
        *,
        at_seconds: float,
        evidence: tuple[str, ...] = (),
        note: str = "",
    ) -> None:
        if not self.can_transition(target):
            raise InvalidStateTransitionError(
                f"execution '{self.execution_id}': cannot transition "
                f"{self.state.value} -> {target.value}; legal targets from "
                f"{self.state.value} are {sorted(s.value for s in _ALLOWED[self.state])}"
            )
        if self.history and at_seconds < self.history[-1].at_seconds:
            raise InvalidStateTransitionError(
                f"execution '{self.execution_id}': at_seconds={at_seconds} is before the "
                f"previous transition's at_seconds={self.history[-1].at_seconds}; transitions "
                "must be reported in non-decreasing time order"
            )
        if target == ExecutionState.COMPLETED:
            clean_evidence = [e for e in evidence if isinstance(e, str) and e.strip()]
            if not clean_evidence:
                raise NotCompletedError(
                    f"execution '{self.execution_id}': cannot transition to COMPLETED without "
                    "non-empty evidence of satisfied acceptance criteria"
                )
        record = TransitionRecord(
            from_state=self.state, to_state=target, at_seconds=at_seconds, note=note
        )
        self.history.append(record)
        self.state = target

    @property
    def is_terminal(self) -> bool:
        return self.state == ExecutionState.COMPLETED
