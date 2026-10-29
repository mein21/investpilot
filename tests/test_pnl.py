from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.enums import AssetClass
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.operation import Operation
from app.models.position import Position
from app.services.pnl import calculate_position_pnl
from sqlalchemy.orm import Session, sessionmaker


def seed_position(
    session: Session, clock: FrozenClock, *, opened_days: int
) -> tuple[Account, Position]:
    account = Account(
        tinvest_account_id="acc-pnl",
        opened_date=clock.now() - timedelta(days=opened_days),
        is_sandbox=True,
    )
    instrument = Instrument(
        instrument_uid="uid-pnl",
        ticker="TEST",
        name="Test",
        asset_class=AssetClass.STOCK,
        lot=1,
        currency="RUB",
    )
    session.add_all([account, instrument])
    session.flush()
    position = Position(
        account_id=account.id,
        instrument_id=instrument.id,
        quantity=Decimal("6"),
        average_price=Decimal("100"),
        current_price=Decimal("110"),
        last_synced_at=clock.now(),
    )
    session.add(position)
    session.flush()
    return account, position


def add_operation(
    session: Session,
    clock: FrozenClock,
    account: Account,
    position: Position,
    *,
    operation_id: str,
    operation_type: str,
    quantity: int,
    payment: str,
    days_ago: int,
) -> None:
    session.add(
        Operation(
            tinvest_operation_id=operation_id,
            account_id=account.id,
            instrument_id=position.instrument_id,
            operation_type=operation_type,
            state="OPERATION_STATE_EXECUTED",
            currency="RUB",
            payment=Decimal(payment),
            price=Decimal("100"),
            quantity=quantity,
            occurred_at=clock.now() - timedelta(days=days_ago),
            trades=[],
        )
    )


def test_average_cost_realized_pnl_and_fees(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    with session_factory() as session:
        account, position = seed_position(session, clock, opened_days=10)
        add_operation(
            session,
            clock,
            account,
            position,
            operation_id="buy-1",
            operation_type="OPERATION_TYPE_BUY",
            quantity=10,
            payment="-1000",
            days_ago=8,
        )
        add_operation(
            session,
            clock,
            account,
            position,
            operation_id="sell-1",
            operation_type="OPERATION_TYPE_SELL",
            quantity=4,
            payment="600",
            days_ago=5,
        )
        add_operation(
            session,
            clock,
            account,
            position,
            operation_id="fee-1",
            operation_type="OPERATION_TYPE_BROKER_FEE",
            quantity=0,
            payment="-10",
            days_ago=5,
        )
        session.flush()

        result = calculate_position_pnl(
            session,
            account,
            operations_from=clock.now() - timedelta(days=30),
        )[position.instrument_id]

        assert result.buy_quantity == Decimal("10")
        assert result.sell_quantity == Decimal("4")
        assert result.buy_cost == Decimal("1000")
        assert result.sell_proceeds == Decimal("600")
        assert result.realized_pnl == Decimal("200")
        assert result.commission_paid == Decimal("10")
        assert result.incomplete is False
        assert position.realized_pnl == Decimal("200")
        assert position.realized_pnl_incomplete is False
        assert position.commission_paid == Decimal("10")


def test_pnl_is_incomplete_when_account_history_exceeds_window(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    with session_factory() as session:
        account, position = seed_position(session, clock, opened_days=100)
        add_operation(
            session,
            clock,
            account,
            position,
            operation_id="buy-2",
            operation_type="OPERATION_TYPE_BUY",
            quantity=10,
            payment="-1000",
            days_ago=8,
        )
        add_operation(
            session,
            clock,
            account,
            position,
            operation_id="sell-2",
            operation_type="OPERATION_TYPE_SELL",
            quantity=4,
            payment="600",
            days_ago=5,
        )
        session.flush()

        result = calculate_position_pnl(
            session,
            account,
            operations_from=clock.now() - timedelta(days=30),
        )[position.instrument_id]

        assert result.realized_pnl == Decimal("200")
        assert result.incomplete is True
        assert position.realized_pnl_incomplete is True
