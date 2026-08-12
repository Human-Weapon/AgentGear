from __future__ import annotations

import pytest

from agentgear.budget import ExecutionBudgetLedger, ReservationKind, ReservationState
from agentgear.exceptions import BudgetExceededError, ConfigurationError


def test_fresh_ledger_has_full_remaining_budget() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    assert ledger.remaining_tokens == 1000
    assert ledger.remaining_cost == 1.0
    assert ledger.committed_tokens == 0
    assert ledger.reserved_tokens == 0


@pytest.mark.parametrize(
    "kwargs", [{"max_tokens": 0}, {"max_tokens": -1}, {"max_cost": 0.0}, {"max_cost": -1.0}]
)
def test_ledger_rejects_invalid_ceilings(kwargs: dict) -> None:
    base = {"max_tokens": 100, "max_cost": 1.0}
    base.update(kwargs)
    with pytest.raises(ConfigurationError):
        ExecutionBudgetLedger(**base)


def test_reserve_within_budget_succeeds() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    reservation = ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=200, cost=0.1)
    assert reservation.state == ReservationState.RESERVED
    assert ledger.remaining_tokens == 800
    assert ledger.remaining_cost == pytest.approx(0.9)


def test_reserve_exceeding_token_ceiling_raises_and_leaves_ledger_unchanged() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=100, max_cost=10.0)
    with pytest.raises(BudgetExceededError):
        ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=101, cost=0.01)
    assert ledger.remaining_tokens == 100
    assert ledger.reserved_tokens == 0


def test_reserve_exceeding_cost_ceiling_raises_and_leaves_ledger_unchanged() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=10_000, max_cost=0.05)
    with pytest.raises(BudgetExceededError):
        ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=100, cost=0.06)
    assert ledger.remaining_cost == 0.05
    assert ledger.reserved_cost == 0.0


def test_commit_does_not_change_totals() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    r = ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=200, cost=0.1)
    before = ledger.remaining_tokens, ledger.remaining_cost
    ledger.commit(r.reservation_id)
    after = ledger.remaining_tokens, ledger.remaining_cost
    assert before == after
    assert ledger.committed_tokens == 200
    assert ledger.reserved_tokens == 0


def test_release_frees_budget() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    r = ledger.reserve(kind=ReservationKind.ESCALATION, tokens=500, cost=0.5)
    assert ledger.remaining_tokens == 500
    ledger.release(r.reservation_id)
    assert ledger.remaining_tokens == 1000
    assert ledger.remaining_cost == 1.0


def test_committing_unknown_reservation_raises() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=100, max_cost=1.0)
    with pytest.raises(BudgetExceededError):
        ledger.commit("res-does-not-exist")


def test_cumulative_reservations_are_denied_once_ceiling_reached() -> None:
    """The exact AG-04 scenario: initial=A, escalation1=B, escalation2=C.
    A+B fits under the limit; A+B+C does not. The SECOND escalation must
    be denied even though C alone, taken in isolation, would fit."""
    ledger = ExecutionBudgetLedger(max_tokens=1_000_000, max_cost=0.02)
    a_cost, b_cost, c_cost = 0.001, 0.004, 0.018  # A+B=0.005 < 0.02 < A+B+C=0.023

    initial = ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=1000, cost=a_cost)
    ledger.commit(initial.reservation_id)

    escalation_1 = ledger.reserve(kind=ReservationKind.ESCALATION, tokens=1000, cost=b_cost)
    ledger.commit(escalation_1.reservation_id)
    assert ledger.committed_cost == pytest.approx(a_cost + b_cost)

    assert ledger.can_afford(tokens=1000, cost=c_cost) is False
    with pytest.raises(BudgetExceededError):
        ledger.reserve(kind=ReservationKind.ESCALATION, tokens=1000, cost=c_cost)

    # Denial must not have mutated the ledger.
    assert ledger.committed_cost == pytest.approx(a_cost + b_cost)


def test_agent_dispatch_reservations_are_counted() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=10_000, max_cost=10.0)
    ledger.reserve(kind=ReservationKind.AGENT_DISPATCH, tokens=100, cost=0.01, label="builder")
    ledger.reserve(kind=ReservationKind.AGENT_DISPATCH, tokens=100, cost=0.01, label="reviewer")
    assert ledger.agent_dispatches == 2


def test_status_reports_consistent_snapshot() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    r = ledger.reserve(kind=ReservationKind.RECOVERY, tokens=100, cost=0.1)
    ledger.commit(r.reservation_id)
    status = ledger.status()
    assert status["committed_tokens"] == 100
    assert status["remaining_tokens"] == 900
    assert status["remaining_cost"] == pytest.approx(0.9)


def test_reserve_rejects_negative_tokens_or_cost() -> None:
    ledger = ExecutionBudgetLedger(max_tokens=1000, max_cost=1.0)
    with pytest.raises(ConfigurationError):
        ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=-1, cost=0.1)
    with pytest.raises(ConfigurationError):
        ledger.reserve(kind=ReservationKind.INITIAL_PLAN, tokens=10, cost=-0.1)
