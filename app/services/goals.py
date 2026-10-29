"""Goal validation, target allocation input and progress calculations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AssetClass, RiskProfile
from app.models.goal import Goal, GoalAllocation

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TARGET_SUM_TOLERANCE = Decimal("0.01")


class GoalValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AllocationInput:
    asset_class: AssetClass
    min_percent: Decimal
    target_percent: Decimal
    max_percent: Decimal


@dataclass(frozen=True)
class GoalInput:
    name: str
    target_amount: Decimal
    currency: str
    target_date: date
    regular_contribution: Decimal = ZERO
    contribution_frequency: str = "MONTHLY"
    min_cash_reserve: Decimal = ZERO
    max_drawdown_percent: Decimal = Decimal("20")
    risk_profile: RiskProfile = RiskProfile.BALANCED
    description: str = ""
    stop_loss_enabled: bool = False
    stop_loss_percent: Decimal | None = None
    take_profit_enabled: bool = False
    take_profit_percent: Decimal | None = None


@dataclass(frozen=True)
class GoalProgress:
    goal_id: int
    target_amount: Decimal
    current_value: Decimal
    progress_percent: Decimal
    remaining_amount: Decimal
    days_left: int
    months_left: int
    required_monthly: Decimal
    regular_contribution: Decimal
    contribution_gap: Decimal
    is_reached: bool
    is_overdue: bool


def _validate_percent(value: Decimal, name: str, *, allow_zero: bool = True) -> None:
    if value < ZERO or value > HUNDRED:
        raise GoalValidationError(f"{name} must be between 0 and 100")
    if not allow_zero and value == ZERO:
        raise GoalValidationError(f"{name} must be positive")


def validate_allocations(allocations: Iterable[AllocationInput]) -> tuple[AllocationInput, ...]:
    items = list(allocations)
    if not items:
        raise GoalValidationError("at least one target allocation is required")
    classes: set[AssetClass] = set()
    target_sum = ZERO
    for item in items:
        if item.asset_class in classes:
            raise GoalValidationError(f"duplicate allocation for {item.asset_class.value}")
        classes.add(item.asset_class)
        _validate_percent(item.min_percent, "min_percent")
        _validate_percent(item.target_percent, "target_percent")
        _validate_percent(item.max_percent, "max_percent")
        if not (item.min_percent <= item.target_percent <= item.max_percent):
            raise GoalValidationError(f"{item.asset_class.value}: min <= target <= max is required")
        target_sum += item.target_percent
    if abs(target_sum - HUNDRED) > TARGET_SUM_TOLERANCE:
        raise GoalValidationError(f"target percentages must sum to 100, got {target_sum}")
    return tuple(items)


class GoalService:
    def create_goal(
        self,
        session: Session,
        data: GoalInput,
        allocations: Iterable[AllocationInput],
        *,
        account_id: int | None = None,
        today: date | None = None,
    ) -> Goal:
        self._validate_goal(data, today=today)
        validated = validate_allocations(allocations)
        goal = Goal(
            account_id=account_id,
            name=data.name.strip(),
            description=data.description,
            target_amount=data.target_amount,
            currency=data.currency.upper(),
            target_date=data.target_date,
            regular_contribution=data.regular_contribution,
            contribution_frequency=data.contribution_frequency,
            min_cash_reserve=data.min_cash_reserve,
            max_drawdown_percent=data.max_drawdown_percent,
            risk_profile=data.risk_profile,
            stop_loss_enabled=data.stop_loss_enabled,
            stop_loss_percent=data.stop_loss_percent,
            take_profit_enabled=data.take_profit_enabled,
            take_profit_percent=data.take_profit_percent,
        )
        session.add(goal)
        session.flush()
        self._replace_allocations(session, goal, validated)
        session.flush()
        return goal

    def update_goal(
        self,
        session: Session,
        goal: Goal,
        data: GoalInput,
        allocations: Iterable[AllocationInput] | None = None,
        *,
        today: date | None = None,
    ) -> Goal:
        self._validate_goal(data, today=today)
        goal.name = data.name.strip()
        goal.description = data.description
        goal.target_amount = data.target_amount
        goal.currency = data.currency.upper()
        goal.target_date = data.target_date
        goal.regular_contribution = data.regular_contribution
        goal.contribution_frequency = data.contribution_frequency
        goal.min_cash_reserve = data.min_cash_reserve
        goal.max_drawdown_percent = data.max_drawdown_percent
        goal.risk_profile = data.risk_profile
        goal.stop_loss_enabled = data.stop_loss_enabled
        goal.stop_loss_percent = data.stop_loss_percent
        goal.take_profit_enabled = data.take_profit_enabled
        goal.take_profit_percent = data.take_profit_percent
        if allocations is not None:
            validated = validate_allocations(allocations)
            self._replace_allocations(session, goal, validated)
        session.flush()
        return goal

    def set_allocations(
        self,
        session: Session,
        goal: Goal,
        allocations: Iterable[AllocationInput],
    ) -> tuple[GoalAllocation, ...]:
        validated = validate_allocations(allocations)
        self._replace_allocations(session, goal, validated)
        session.flush()
        return tuple(goal.allocations)

    def progress(
        self,
        goal: Goal,
        current_value: Decimal,
        *,
        now: datetime,
    ) -> GoalProgress:
        target = goal.target_amount
        current = max(ZERO, current_value)
        remaining = max(ZERO, target - current)
        progress = current / target * HUNDRED if target > ZERO else ZERO
        days_left = (goal.target_date - now.date()).days
        months_left = max(0, ceil(days_left / 30)) if days_left > 0 else 0
        required_monthly = remaining / Decimal(months_left) if months_left > 0 else remaining
        contribution_gap = max(
            ZERO,
            required_monthly - goal.regular_contribution,
        )
        return GoalProgress(
            goal_id=goal.id,
            target_amount=target,
            current_value=current,
            progress_percent=min(progress, HUNDRED),
            remaining_amount=remaining,
            days_left=days_left,
            months_left=months_left,
            required_monthly=required_monthly,
            regular_contribution=goal.regular_contribution,
            contribution_gap=contribution_gap,
            is_reached=current >= target,
            is_overdue=days_left < 0 and current < target,
        )

    def allocation_map(
        self,
        goal: Goal,
    ) -> dict[AssetClass, tuple[Decimal, Decimal, Decimal]]:
        return {
            item.asset_class: (
                item.min_percent,
                item.target_percent,
                item.max_percent,
            )
            for item in goal.allocations
        }

    def _replace_allocations(
        self,
        session: Session,
        goal: Goal,
        allocations: tuple[AllocationInput, ...],
    ) -> None:
        for existing in list(
            session.scalars(select(GoalAllocation).where(GoalAllocation.goal_id == goal.id))
        ):
            session.delete(existing)
        session.flush()
        for item in allocations:
            session.add(
                GoalAllocation(
                    goal_id=goal.id,
                    asset_class=item.asset_class,
                    min_percent=item.min_percent,
                    target_percent=item.target_percent,
                    max_percent=item.max_percent,
                )
            )

    @staticmethod
    def _validate_goal(data: GoalInput, *, today: date | None = None) -> None:
        if not data.name.strip():
            raise GoalValidationError("goal name is required")
        if data.target_amount <= ZERO:
            raise GoalValidationError("target amount must be positive")
        if data.regular_contribution < ZERO:
            raise GoalValidationError("regular contribution cannot be negative")
        if data.min_cash_reserve < ZERO:
            raise GoalValidationError("minimum cash reserve cannot be negative")
        _validate_percent(data.max_drawdown_percent, "max_drawdown_percent")
        if today is not None and data.target_date < today:
            raise GoalValidationError("target date cannot be in the past")
        if len(data.currency.strip()) != 3:
            raise GoalValidationError("goal currency must be a 3-letter code")
        if data.stop_loss_enabled:
            if data.stop_loss_percent is None:
                raise GoalValidationError("stop_loss_percent is required")
            _validate_percent(data.stop_loss_percent, "stop_loss_percent", allow_zero=False)
        if data.take_profit_enabled:
            if data.take_profit_percent is None:
                raise GoalValidationError("take_profit_percent is required")
            _validate_percent(
                data.take_profit_percent,
                "take_profit_percent",
                allow_zero=False,
            )
