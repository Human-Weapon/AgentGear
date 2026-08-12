"""Execution-wide cumulative budget accounting (AG-04).

A single ``ExecutionBudgetLedger`` instance is shared across an entire
execution's lifetime — the initial plan, every escalation, every recovery
attempt, and every agent dispatch all draw from the SAME pool. This is
deliberately a different concern from ``Policy.budget`` (which only
describes the *ceilings*): the ledger tracks what has actually been
reserved/committed against those ceilings over time.

Core invariant: no operation is ever approved if
``already reserved/committed + new operation estimate > hard ceiling``.
``reserve()`` is the only way to spend budget, and it is atomic — it
either creates a reservation that fits, or raises ``BudgetExceededError``
and changes nothing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum

from .exceptions import BudgetExceededError, ConfigurationError


class ReservationKind(str, Enum):
    INITIAL_PLAN = "initial_plan"
    ESCALATION = "escalation"
    RECOVERY = "recovery"
    AGENT_DISPATCH = "agent_dispatch"


class ReservationState(str, Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(frozen=True)
class BudgetReservation:
    """A hold against the ledger's ceilings. ``RESERVED`` and
    ``COMMITTED`` both count against the ceiling (a reservation isn't
    "free" until it's released); ``COMMITTED`` marks it as actually spent
    rather than merely provisional.
    """

    reservation_id: str
    kind: ReservationKind
    tokens: int
    cost: float
    state: ReservationState
    label: str = ""


def _require_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 1:
        raise ConfigurationError(f"{name} must be >= 1, got {value}")
    return value


def _require_positive_float(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number, got {type(value).__name__}")
    if value <= 0:
        raise ConfigurationError(f"{name} must be > 0, got {value}")
    return float(value)


def _require_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ConfigurationError(f"{name} must be >= 0, got {value}")
    return value


def _require_non_negative_float(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number, got {type(value).__name__}")
    if value < 0:
        raise ConfigurationError(f"{name} must be >= 0, got {value}")
    return float(value)


class ExecutionBudgetLedger:
    """Tracks cumulative token/cost usage for one execution against hard
    ceilings. Not thread-safe by design — one execution, one ledger,
    one owning coordinator (mirrors ``ExecutionStateMachine``).
    """

    def __init__(self, *, max_tokens: int, max_cost: float) -> None:
        self.max_tokens = _require_positive_int("max_tokens", max_tokens)
        self.max_cost = _require_positive_float("max_cost", max_cost)
        self._reservations: dict[str, BudgetReservation] = {}
        self._id_counter = itertools.count(1)
        self.agent_dispatches = 0

    def _in_use(self) -> tuple[int, float]:
        tokens = sum(
            r.tokens for r in self._reservations.values() if r.state != ReservationState.RELEASED
        )
        cost = sum(
            r.cost for r in self._reservations.values() if r.state != ReservationState.RELEASED
        )
        return tokens, cost

    @property
    def committed_tokens(self) -> int:
        return sum(
            r.tokens for r in self._reservations.values() if r.state == ReservationState.COMMITTED
        )

    @property
    def committed_cost(self) -> float:
        return sum(
            r.cost for r in self._reservations.values() if r.state == ReservationState.COMMITTED
        )

    @property
    def reserved_tokens(self) -> int:
        return sum(
            r.tokens for r in self._reservations.values() if r.state == ReservationState.RESERVED
        )

    @property
    def reserved_cost(self) -> float:
        return sum(
            r.cost for r in self._reservations.values() if r.state == ReservationState.RESERVED
        )

    @property
    def remaining_tokens(self) -> int:
        in_use_tokens, _ = self._in_use()
        return self.max_tokens - in_use_tokens

    @property
    def remaining_cost(self) -> float:
        _, in_use_cost = self._in_use()
        return self.max_cost - in_use_cost

    def can_afford(self, *, tokens: int, cost: float) -> bool:
        _require_non_negative_int("tokens", tokens)
        _require_non_negative_float("cost", cost)
        in_use_tokens, in_use_cost = self._in_use()
        return (in_use_tokens + tokens <= self.max_tokens) and (in_use_cost + cost <= self.max_cost)

    def reserve(
        self, *, kind: ReservationKind, tokens: int, cost: float, label: str = ""
    ) -> BudgetReservation:
        """Atomically reserve ``tokens``/``cost`` against the ledger.

        Raises ``BudgetExceededError`` and leaves the ledger unchanged if
        the reservation would push cumulative usage past either ceiling.
        """
        _require_non_negative_int("tokens", tokens)
        _require_non_negative_float("cost", cost)
        if not self.can_afford(tokens=tokens, cost=cost):
            in_use_tokens, in_use_cost = self._in_use()
            raise BudgetExceededError(
                f"cannot reserve {tokens} tokens / {cost:.4f} cost for {kind.value}"
                f"{f' ({label})' if label else ''}: already using {in_use_tokens} tokens / "
                f"{in_use_cost:.4f} cost against ceilings of {self.max_tokens} tokens / "
                f"{self.max_cost:.4f} cost"
            )
        reservation_id = f"res-{next(self._id_counter)}"
        reservation = BudgetReservation(
            reservation_id=reservation_id,
            kind=kind,
            tokens=tokens,
            cost=cost,
            state=ReservationState.RESERVED,
            label=label,
        )
        self._reservations[reservation_id] = reservation
        if kind == ReservationKind.AGENT_DISPATCH:
            self.agent_dispatches += 1
        return reservation

    def commit(self, reservation_id: str) -> None:
        """Mark a reservation as actually spent (no change to totals —
        committed reservations already counted against the ceiling)."""
        reservation = self._require_reservation(reservation_id)
        self._reservations[reservation_id] = _replace_state(reservation, ReservationState.COMMITTED)

    def release(self, reservation_id: str) -> None:
        """Free a reservation that was never spent, returning its
        tokens/cost to the available pool."""
        reservation = self._require_reservation(reservation_id)
        self._reservations[reservation_id] = _replace_state(reservation, ReservationState.RELEASED)

    def _require_reservation(self, reservation_id: str) -> BudgetReservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise BudgetExceededError(f"unknown reservation_id: {reservation_id!r}") from None

    def status(self) -> dict[str, object]:
        in_use_tokens, in_use_cost = self._in_use()
        return {
            "max_tokens": self.max_tokens,
            "max_cost": self.max_cost,
            "committed_tokens": self.committed_tokens,
            "committed_cost": self.committed_cost,
            "reserved_tokens": self.reserved_tokens,
            "reserved_cost": self.reserved_cost,
            "remaining_tokens": self.max_tokens - in_use_tokens,
            "remaining_cost": self.max_cost - in_use_cost,
            "agent_dispatches": self.agent_dispatches,
            "reservation_count": len(self._reservations),
        }


def _replace_state(reservation: BudgetReservation, state: ReservationState) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=reservation.reservation_id,
        kind=reservation.kind,
        tokens=reservation.tokens,
        cost=reservation.cost,
        state=state,
        label=reservation.label,
    )
