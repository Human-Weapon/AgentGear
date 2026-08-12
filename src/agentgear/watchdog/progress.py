"""Progress / liveness tracking.

A ``ProgressTracker`` only ever holds ``ProgressEvent`` instances — and a
``ProgressEvent`` can only be constructed with one of the meaningful
``ProgressSignalKind`` values (see ``models.py``). Busywork (producing
text, repeating a command, re-reading the same file, saying "still
working") is never a ``ProgressEvent`` at all: callers report raw activity
to the stall detector separately (``ActivityRecord``), and only genuinely
new evidence should be wrapped as a ``ProgressEvent`` and recorded here.

This split is what makes "activity but no progress" (adversarial scenario
A) representable: an execution can have many ``ActivityRecord`` entries
and zero ``ProgressEvent`` entries in the same window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ProgressEvent


@dataclass
class ProgressTracker:
    """Append-only log of genuine progress for one execution."""

    events: list[ProgressEvent] = field(default_factory=list)

    def record(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def last_progress_at(self) -> float | None:
        if not self.events:
            return None
        return max(e.at_seconds for e in self.events)

    def seconds_since_last_progress(self, now: float) -> float | None:
        last = self.last_progress_at
        if last is None:
            return None
        return max(0.0, now - last)

    def events_since(self, at_seconds: float) -> list[ProgressEvent]:
        return [e for e in self.events if e.at_seconds > at_seconds]

    def has_progress_since(self, at_seconds: float) -> bool:
        return len(self.events_since(at_seconds)) > 0
