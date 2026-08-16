"""Stall detection: combine multiple independent signals so no single
heuristic (especially elapsed time) can trigger a false STALLED, and so a
genuinely stuck agent cannot hide behind a single narrow check.

Signals combined (principle #16):
  * elapsed time since the progress boundary (see below)
  * number of activity attempts since the progress boundary
  * repeated identical failures (same command/error fingerprint)
  * circular attempts (same fingerprint recurring without new evidence)
  * a trivial command taking abnormally long, repeatedly

Time alone never triggers STALLED: the time-based trigger additionally
requires a minimum number of attempted (and evidence-free) activities in
that same window.

**Progress boundary.** Every signal above is evaluated only against
activity *after* the progress boundary — never the full history. The
boundary is ``last_progress_at`` when the execution has ever made real
progress, or ``started_at`` (the execution/observation start) when it has
not. Without this, two bugs are possible: (1) an execution that has
*never* made progress can never be flagged stalled, because there is no
``last_progress_at`` to measure elapsed time from; (2) once genuine
progress resets the clock, stale pre-progress activity (old failures, old
repeated fingerprints) could still count toward a stall verdict computed
after that progress happened. ``started_at`` closes gap (1); scoping every
signal to activity strictly after the boundary closes gap (2).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import WatchdogPolicy
from ..exceptions import InvalidObservationError
from ..models import _validate_non_blank_str, _validate_observation_timestamp


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "at_seconds", _validate_observation_timestamp("at_seconds", self.at_seconds)
        )
        object.__setattr__(
            self, "fingerprint", _validate_non_blank_str("fingerprint", self.fingerprint)
        )
        object.__setattr__(
            self,
            "duration_seconds",
            _validate_observation_timestamp("duration_seconds", self.duration_seconds),
        )
        # Round 5 / AG5-03: `succeeded`/`is_trivial` must be a genuine
        # `bool`, not a truthy stand-in like `1`/`"false"` -- the stall
        # detector reads `succeeded` to count trailing-identical-failures
        # (stall_detection.py) and `is_trivial` to gate the trivial-slow-
        # command signal, so an accidentally-truthy string like `"false"`
        # (which IS non-empty, hence truthy) would silently corrupt those
        # verdicts. No coercion is documented for either field, so both
        # are rejected outright rather than guessed at.
        if not isinstance(self.succeeded, bool):
            raise InvalidObservationError(
                f"succeeded must be a bool, got {type(self.succeeded).__name__}"
            )
        if not isinstance(self.is_trivial, bool):
            raise InvalidObservationError(
                f"is_trivial must be a bool, got {type(self.is_trivial).__name__}"
            )
        if self.error is not None:
            object.__setattr__(self, "error", _validate_non_blank_str("error", self.error))


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
        started_at: float,
        last_progress_at: float | None,
        recent_activities: Sequence[ActivityRecord],
    ) -> StallVerdict:
        """Evaluate whether the execution should be considered stalled.

        ``started_at`` is the execution/observation start boundary — it is
        always required, even once real progress has happened, so the
        progress boundary is never allowed to collapse to "no boundary at
        all" (see module docstring).
        """
        if not self.policy.enabled:
            return StallVerdict(is_stalled=False, reasons=())

        reasons: list[str] = []
        p = self.policy

        boundary = last_progress_at if last_progress_at is not None else started_at
        # Round 2 / M8/L8: strictly-after, matching progress.py's
        # ``events_since`` contract ("activity at exactly the boundary is
        # not considered new after it"). Using `>=` here would resurrect a
        # stale activity recorded at exactly the progress/start instant as
        # "since progress" the moment the boundary is set to that same
        # timestamp, silently weakening the AG-02 boundary-reset guarantee.
        since_boundary = [a for a in recent_activities if a.at_seconds > boundary]

        elapsed = max(0.0, now - boundary)
        time_exceeded = elapsed >= p.no_progress_seconds
        attempts_exceeded = len(since_boundary) >= p.no_progress_cycles
        if time_exceeded and attempts_exceeded:
            reasons.append(
                f"no progress for {elapsed:.1f}s across {len(since_boundary)} attempts "
                f"(>= {p.no_progress_seconds:.0f}s and >= {p.no_progress_cycles} attempts)"
            )

        trailing_failures = 0
        for a in reversed(since_boundary):
            if a.succeeded:
                break
            trailing_failures += 1
        if trailing_failures >= p.max_identical_failures:
            trailing = since_boundary[-trailing_failures:]
            fingerprints = {a.fingerprint for a in trailing}
            if len(fingerprints) == 1:
                reasons.append(
                    f"{trailing_failures} consecutive identical failures "
                    f"(fingerprint={next(iter(fingerprints))!r})"
                )

        # Round 2 / L7: counts EVERY activity by fingerprint regardless of
        # ``succeeded`` -- deliberately not filtered to failures only. An
        # agent that keeps re-running the same successful-but-useless
        # command (re-reading the same file, re-listing the same
        # directory) over and over is exhibiting circular/busywork
        # behavior just as much as one that keeps failing identically; a
        # "successful" repeated no-op is not evidence of real progress.
        fingerprint_counts = Counter(a.fingerprint for a in since_boundary)
        for fingerprint, count in fingerprint_counts.items():
            if count >= max(3, p.max_identical_failures + 1):
                reasons.append(
                    f"circular attempts: fingerprint {fingerprint!r} repeated {count} times "
                    "without new evidence"
                )
                break

        trivial_slow = [
            a
            for a in since_boundary
            if a.is_trivial and a.duration_seconds >= p.trivial_command_timeout_seconds
        ]
        if len(trivial_slow) >= 2:
            reasons.append(
                f"{len(trivial_slow)} trivial commands each took >= "
                f"{p.trivial_command_timeout_seconds:.0f}s (abnormal for trivial work)"
            )

        return StallVerdict(is_stalled=bool(reasons), reasons=tuple(reasons))
