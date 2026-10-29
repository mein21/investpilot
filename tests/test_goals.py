from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from app.core.clock import FrozenClock
from app.core.enums import AssetClass
from app.models.goal import Goal
from app.services.goals import (
    AllocationInput,
    GoalInput,
    GoalService,
    GoalValidationError,
    validate_allocations,
)
from sqlalchemy.orm import Session, sessionmaker


def goal_input(clock: FrozenClock, *, target: str = "1000") -> GoalInput:
    return GoalInput(
        name="Car",
        target_amount=Decimal(target),
        currency="RUB",
        target_date=clock.now().date() + timedelta(days=30),
    )


def valid_allocations() -> list[AllocationInput]:
    return [
        AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("60"), Decimal("80")),
        AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("20"), Decimal("30")),
        AllocationInput(AssetClass.CASH, Decimal("10"), Decimal("20"), Decimal("30")),
    ]


def test_goal_creation_and_progress(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = GoalService()
    with session_factory() as session:
        goal = service.create_goal(
            session,
            goal_input(clock),
            valid_allocations(),
            today=clock.now().date(),
        )
        session.flush()
        assert goal.id is not None
        assert len(goal.allocations) == 3
        assert service.allocation_map(goal)[AssetClass.STOCK] == (
            Decimal("40"),
            Decimal("60"),
            Decimal("80"),
        )
        progress = service.progress(goal, Decimal("400"), now=clock.now())
        assert progress.progress_percent == Decimal("40.0")
        assert progress.remaining_amount == Decimal("600.000")
        assert progress.days_left == 30
        assert progress.months_left == 1
        assert progress.required_monthly == Decimal("600.000")
        assert not progress.is_reached
        assert not progress.is_overdue


def test_goal_update_replaces_allocations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = GoalService()
    with session_factory() as session:
        goal = service.create_goal(
            session,
            goal_input(clock),
            valid_allocations(),
            today=clock.now().date(),
        )
        service.update_goal(
            session,
            goal,
            goal_input(clock, target="2000"),
            [
                AllocationInput(
                    AssetClass.STOCK,
                    Decimal("50"),
                    Decimal("100"),
                    Decimal("100"),
                )
            ],
            today=clock.now().date(),
        )
        session.flush()
        stored = session.get(Goal, goal.id)
        assert stored is not None
        assert stored.target_amount == Decimal("2000")
        assert len(stored.allocations) == 1


@pytest.mark.parametrize(
    "allocations",
    [
        [AllocationInput(AssetClass.STOCK, Decimal("0"), Decimal("90"), Decimal("100"))],
        [
            AllocationInput(AssetClass.STOCK, Decimal("20"), Decimal("10"), Decimal("30")),
            AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("90"), Decimal("100")),
        ],
        [
            AllocationInput(AssetClass.STOCK, Decimal("0"), Decimal("50"), Decimal("100")),
            AllocationInput(AssetClass.STOCK, Decimal("0"), Decimal("50"), Decimal("100")),
        ],
    ],
)
def test_invalid_allocations_are_rejected(
    allocations: list[AllocationInput],
) -> None:
    with pytest.raises(GoalValidationError):
        validate_allocations(allocations)


def test_goal_target_date_cannot_be_in_the_past(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    data = GoalInput(
        name="Late goal",
        target_amount=Decimal("1000"),
        currency="RUB",
        target_date=clock.now().date() - timedelta(days=1),
    )
    with session_factory() as session, pytest.raises(GoalValidationError):
        GoalService().create_goal(
            session,
            data,
            valid_allocations(),
            today=clock.now().date(),
        )
