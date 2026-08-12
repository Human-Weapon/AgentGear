"""Stall detection: combine multiple independent signals so no single
heuristic (especially elapsed time) can trigger a false STALLED, and so a
genuinely stuck agent cannot hide behind a single narrow check.

Signals combined (principle #16):
  * elapsed time since last real progress
  * number of activity attempts since last real progress
  * repeated identical failures (same command/error fingerprint)
  * circular attempts (same fingerprint recurring without new evidence)
  * a trivial command taking abnormally long, repeatedly

Time alone never triggers STALLED: the time-based trigger additionally
requires a minimum number of attempted (and evidence-free) activities in
that same window.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import WatchdogPolicy


@dataclass(frozen=True)
class ActivityRecord:
    """One raw unit of agent activity (a tool call, a command, a retry).

    ``fingerprint`` should identify *what was attempted* (e.g. a hash of
    command + normalized args) so repeated/circular attempts can be
    detected even when free-text output differs each time.
    """

    at_seconds: float
    fingerprint: str
    succeeded: bool
    is_trivial: bool = False
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class StallVerdict:
    is_stalled: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


class StallDetector:
    def __init__(self, policy: WatchdogPolicy) -> None:
        self.policy = policy

    def evaluate(
        self,
        *,
        now: float,
        last_progress_at: float | None,
        recent_activities: Sequence[ActivityRecord],
    ) -> StallVerdict:
        if not self.policy.enabled:
            return StallVerdict(is_stalled=False, reasons=())

        reasons: list[str] = []
        p = self.policy

        since_progress = [
            a
            for a in recent_activities
            if last_progress_at is None or a.at_seconds > last_progress_at
        ]

        elapsed = None if last_progress_at is None else max(0.0, now - last_progress_at)
        time_exceeded = elapsed is not None and elapsed >= p.no_progress_seconds
        attempts_exceeded = len(since_progress) >= p.no_progress_cycles
        if time_exceeded and attempts_exceeded:
            reasons.append(
                f"no progress for {elapsed:.1f}s across {len(since_progress)} attempts "
                f"(>= {p.no_progress_seconds:.0f}s and >= {p.no_progress_cycles} attempts)"
            )

        trailing_failures = 0
        for a in reversed(recent_activities):
            if a.succeeded:
                break
            trailing_failures += 1
        if trailing_failures >= p.max_identical_failures:
            trailing = recent_activities[-trailing_failures:]
            fingerprints = {a.fingerprint for a in trailing}
            if len(fingerprints) == 1:
                reasons.append(
                    f"{trailing_failures} consecutive identical failures "
                    f"(fingerprint={next(iter(fingerprints))!r})"
                )

        fingerprint_counts = Counter(a.fingerprint for a in recent_activities)
        for fingerprint, count in fingerprint_counts.items():
            if count >= max(3, p.max_identical_failures + 1):
                reasons.append(
                    f"circular attempts: fingerprint {fingerprint!r} repeated {count} times "
                    "without new evidence"
                )
                break

        trivial_slow = [
            a
            for a in recent_activities
            if a.is_trivial and a.duration_seconds >= p.trivial_command_timeout_seconds
        ]
        if len(trivial_slow) >= 2:
            reasons.append(
                f"{len(trivial_slow)} trivial commands each took >= "
                f"{p.trivial_command_timeout_seconds:.0f}s (abnormal for trivial work)"
            )

        return StallVerdict(is_stalled=bool(reasons), reasons=tuple(reasons))
