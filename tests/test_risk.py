from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import AssetClass, ExecutionMode, OrderSide, OrderStatus, TradingMode
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.account import Account
from app.models.goal import Goal
from app.models.instrument import Instrument
from app.models.order import Order
from app.risk import RiskEngine
from app.services.goals import AllocationInput, GoalInput, GoalService
from app.services.sync import SyncService
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def prepare(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    *,
    settings: Settings | None = None,
    reserve: Decimal = Decimal("0"),
) -> tuple[SyncService, RiskEngine, Goal]:
    settings = settings or Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_sandbox=True,
    )
    sync = SyncService(
        session_factory,
        MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    )
    sync.run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        goal = GoalService().create_goal(
            session,
            GoalInput(
                name="Test goal",
                target_amount=Decimal("2000000"),
                currency="RUB",
                target_date=clock.now().date() + timedelta(days=365),
                min_cash_reserve=reserve,
            ),
            [
                AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("50"), Decimal("60")),
                AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("20"), Decimal("25")),
                AllocationInput(AssetClass.ETF, Decimal("0"), Decimal("5"), Decimal("10")),
                AllocationInput(AssetClass.CURRENCY, Decimal("5"), Decimal("10"), Decimal("15")),
                AllocationInput(AssetClass.CASH, Decimal("10"), Decimal("15"), Decimal("20")),
            ],
            account_id=account.id,
            today=clock.now().date(),
        )
        session.commit()
        goal_id = goal.id
    with session_factory() as session:
        loaded = session.get(Goal, goal_id)
        assert loaded is not None
        return sync, RiskEngine(settings, clock=clock), loaded


def bond(session: Session) -> tuple[Account, Instrument, Order]:
    account = session.scalar(select(Account))
    instrument = session.scalar(select(Instrument).where(Instrument.ticker == "SU26248RMFS3"))
    assert account is not None and instrument is not None
    return account, instrument, None  # type: ignore[return-value]


def test_buy_and_sell_pass_when_limits_are_satisfied(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _sync, engine, goal = prepare(session_factory, clock)
    with session_factory() as session:
        account, instrument, _ = bond(session)
        position = instrument.positions[0]
        decision = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        assert decision.allowed, [item.code for item in decision.blocking_rules]
        assert decision.estimated_amount == Decimal("9600.00")

        sell = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.SELL,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        assert sell.allowed, [item.code for item in sell.blocking_rules]
        assert sell.cash_after > decision.cash_after


def test_stale_quote_and_price_deviation_are_blocking(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _sync, engine, goal = prepare(session_factory, clock)
    with session_factory() as session:
        account, instrument, _ = bond(session)
        decision = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("1100"),
            quote_at=clock.now() - timedelta(seconds=600),
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        codes = {item.code for item in decision.blocking_rules}
        assert "QUOTE_FRESH" in codes
        assert "PRICE_DEVIATION" in codes


def test_cash_reserve_and_issuer_limit_are_blocking(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        max_issuer_percent=Decimal("15"),
        default_min_cash_reserve=Decimal("200000"),
    )
    _sync, engine, goal = prepare(
        session_factory,
        clock,
        settings=settings,
        reserve=Decimal("200000"),
    )
    with session_factory() as session:
        account, instrument, _ = bond(session)
        position = instrument.positions[0]
        decision = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        codes = {item.code for item in decision.blocking_rules}
        assert "CASH_RESERVE" in codes
        assert "MAX_ISSUER_PERCENT" in codes


def test_daily_order_count_blocks_new_order(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        max_daily_order_count=1,
    )
    _sync, engine, goal = prepare(session_factory, clock, settings=settings)
    with session_factory() as session:
        account, instrument, _ = bond(session)
        position = instrument.positions[0]
        session.add(
            Order(
                account_id=account.id,
                instrument_id=instrument.id,
                client_order_id="existing-order",
                side=OrderSide.BUY,
                status=OrderStatus.SENT,
                execution_mode=ExecutionMode.SANDBOX,
                is_sandbox=True,
                lots=1,
                lot_size=instrument.lot,
                limit_price=Decimal("95.5"),
                currency="RUB",
                base_currency="RUB",
                estimated_amount=Decimal("95.5"),
                estimated_amount_base=Decimal("95.5"),
                commission_estimate=Decimal("2.88"),
                commission_estimate_base=Decimal("2.88"),
                sent_at=clock.now(),
            )
        )
        session.flush()
        decision = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        assert "MAX_DAILY_ORDER_COUNT" in {item.code for item in decision.blocking_rules}


def test_read_only_account_blocks_real_orders_but_not_recommendations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_sandbox=False,
        real_trading_enabled=True,
        trading_mode="CONFIRM",
    )
    _sync, engine, goal = prepare(session_factory, clock, settings=settings)
    with session_factory() as session:
        account, instrument, _ = bond(session)
        account.access_level = "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
        position = instrument.positions[0]
        session.flush()

        blocked = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.CONFIRM,
            goal=goal,
        )
        assert "ACCOUNT_TRADING_ACCESS" in {item.code for item in blocked.blocking_rules}

        recommendation_only = engine.evaluate(
            session,
            account_id=account.id,
            instrument=instrument,
            side=OrderSide.BUY,
            lots=10,
            price=Decimal("95.5"),
            quote_at=position.current_price_at,
            mode=TradingMode.RECOMMEND,
            goal=goal,
            require_order_mode=False,
        )
        assert recommendation_only.allowed
