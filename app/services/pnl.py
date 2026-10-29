"""Deterministic average-cost P&L from stored broker operations.

The calculation is intentionally explicit about completeness: if the stored
operation window does not explain the current position, the result is flagged
as incomplete instead of being presented as final.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.operation import Operation
from app.models.position import Position

ZERO = Decimal("0")
EXECUTED_STATE = "OPERATION_STATE_EXECUTED"

BUY_TYPES = {
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_BUY_CARD",
    "OPERATION_TYPE_BUY_MARGIN",
    "OPERATION_TYPE_DELIVERY_BUY",
}
SELL_TYPES = {
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_SELL_CARD",
    "OPERATION_TYPE_SELL_MARGIN",
    "OPERATION_TYPE_DELIVERY_SELL",
    "OPERATION_TYPE_BOND_REPAYMENT",
    "OPERATION_TYPE_BOND_REPAYMENT_FULL",
}
FEE_TYPES = {
    "OPERATION_TYPE_BROKER_FEE",
    "OPERATION_TYPE_SERVICE_FEE",
    "OPERATION_TYPE_MARGIN_FEE",
    "OPERATION_TYPE_SUCCESS_FEE",
    "OPERATION_TYPE_OVER_COM",
    "OPERATION_TYPE_CASH_FEE",
}


@dataclass(frozen=True)
class PositionPnL:
    instrument_id: int
    realized_pnl: Decimal
    commission_paid: Decimal
    buy_quantity: Decimal
    sell_quantity: Decimal
    buy_cost: Decimal
    sell_proceeds: Decimal
    incomplete: bool


def _operation_executed(operation: Operation) -> bool:
    return operation.state in ("", EXECUTED_STATE)


def calculate_position_pnl(
    session: Session,
    account: Account,
    *,
    operations_from: datetime,
) -> dict[int, PositionPnL]:
    operations = list(
        session.scalars(
            select(Operation)
            .where(Operation.account_id == account.id)
            .order_by(Operation.occurred_at, Operation.id)
        )
    )
    positions = {
        position.instrument_id: position
        for position in session.scalars(select(Position).where(Position.account_id == account.id))
    }
    totals: dict[int, dict[str, Decimal]] = {}
    for operation in operations:
        if operation.instrument_id is None or not _operation_executed(operation):
            continue
        bucket = totals.setdefault(
            operation.instrument_id,
            {
                "buy_quantity": ZERO,
                "sell_quantity": ZERO,
                "buy_cost": ZERO,
                "sell_proceeds": ZERO,
                "commission": ZERO,
            },
        )
        if operation.operation_type in BUY_TYPES and operation.quantity > 0:
            bucket["buy_quantity"] += Decimal(operation.quantity)
            bucket["buy_cost"] += abs(operation.payment)
        elif operation.operation_type in SELL_TYPES and operation.quantity > 0:
            bucket["sell_quantity"] += Decimal(operation.quantity)
            bucket["sell_proceeds"] += operation.payment
        elif operation.operation_type in FEE_TYPES:
            bucket["commission"] += abs(operation.payment)

    account_window_incomplete = account.opened_date is None or account.opened_date < operations_from
    result: dict[int, PositionPnL] = {}
    for instrument_id, position in positions.items():
        bucket = totals.get(
            instrument_id,
            {
                "buy_quantity": ZERO,
                "sell_quantity": ZERO,
                "buy_cost": ZERO,
                "sell_proceeds": ZERO,
                "commission": ZERO,
            },
        )
        buy_quantity = bucket["buy_quantity"]
        sell_quantity = bucket["sell_quantity"]
        buy_cost = bucket["buy_cost"]
        sell_proceeds = bucket["sell_proceeds"]
        incomplete = account_window_incomplete
        if position.quantity > ZERO and buy_quantity == ZERO:
            incomplete = True
        if sell_quantity > buy_quantity:
            incomplete = True
        if abs(buy_quantity - sell_quantity - position.quantity) > ZERO:
            incomplete = True
        if buy_quantity > ZERO:
            average_cost = buy_cost / buy_quantity
            realized = sell_proceeds - average_cost * sell_quantity
        else:
            realized = ZERO
        pnl = PositionPnL(
            instrument_id=instrument_id,
            realized_pnl=realized,
            commission_paid=bucket["commission"],
            buy_quantity=buy_quantity,
            sell_quantity=sell_quantity,
            buy_cost=buy_cost,
            sell_proceeds=sell_proceeds,
            incomplete=incomplete,
        )
        position.realized_pnl = realized
        position.realized_pnl_incomplete = incomplete
        position.commission_paid = pnl.commission_paid
        result[instrument_id] = pnl
    session.flush()
    return result
