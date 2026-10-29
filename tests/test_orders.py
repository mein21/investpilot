from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import (
    AssetClass,
    ExecutionMode,
    OrderStatus,
    OrderType,
    RecommendationAction,
    RecommendationStatus,
    TradingMode,
)
from app.integrations.tinvest.exceptions import TInvestTimeoutError
from app.integrations.tinvest.mock import MockOrderBehavior, MockTInvestClient
from app.integrations.tinvest.types import OrderRequest
from app.models.account import Account
from app.models.goal import Goal
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.position import CashBalance, Position
from app.models.recommendation import Recommendation
from app.risk.engine import RiskDecision
from app.services.goals import AllocationInput, GoalInput, GoalService
from app.services.orders import OrderBlockedError, OrderError, OrderService
from app.services.recommendations import RecommendationService
from app.services.sync import SyncService
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


def settings() -> Settings:
    return Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_sandbox=True,
        trading_mode="CONFIRM",
    )


def prepare(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    client: MockTInvestClient | None = None,
    settings_override: Settings | None = None,
) -> tuple[Account, Recommendation, MockTInvestClient, OrderService]:
    cfg = settings_override or settings()
    client = client or MockTInvestClient(clock=clock)
    SyncService(session_factory, client, cfg, clock=clock).run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        GoalService().create_goal(
            session,
            GoalInput(
                name="Test goal",
                target_amount=Decimal("2000000"),
                currency="RUB",
                target_date=clock.now().date() + timedelta(days=365),
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
        run = RecommendationService(cfg, clock=clock).generate(
            session,
            account_id=account.id,
        )
        session.commit()
        sell = next(
            item
            for item in run.recommendations
            if item.action is RecommendationAction.SELL and item.ticker == "GAZP"
        )
        return account, sell, client, OrderService(client, cfg, clock=clock)


def test_approve_send_and_sync_full_fill(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _account, recommendation, client, orders = prepare(session_factory, clock)
    with session_factory() as session:
        order = orders.approve(session, recommendation.id, user="tester")
        session.commit()
        assert order.status is OrderStatus.APPROVED
        assert order.execution_mode is ExecutionMode.SANDBOX
        assert len(order.client_order_id) == 36
        stored_rec = session.get(Recommendation, recommendation.id)
        assert stored_rec is not None
        assert stored_rec.status is RecommendationStatus.APPROVED

    with session_factory() as session:
        sent = orders.send(session, order.id, user="tester")
        session.commit()
        assert sent.status is OrderStatus.EXECUTED
        assert sent.broker_order_id
        assert sent.lots_executed == 20
        assert sent.executed_amount == Decimal("30000.00")
        assert sent.average_executed_price == Decimal("150")
        assert sent.executed_commission == Decimal("90.00")
        assert len(sent.executions) == 1
        assert sent.completed_at is not None
        stored_rec = session.get(Recommendation, recommendation.id)
        assert stored_rec is not None
        assert stored_rec.status is RecommendationStatus.EXECUTED

    SyncService(
        session_factory,
        client,
        settings(),
        clock=clock,
    ).run()
    with session_factory() as session:
        position = session.scalar(
            select(Position).join(Instrument).where(Instrument.ticker == "GAZP")
        )
        assert position is not None
        assert position.quantity == Decimal("1800")
        rub = session.scalar(select(CashBalance).where(CashBalance.currency == "RUB"))
        assert rub is not None
        assert rub.amount == Decimal("179910.00")


def test_forced_exit_can_break_class_minimum(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    cfg = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_sandbox=True,
        trading_mode="CONFIRM",
        max_order_amount=Decimal("200000"),
        max_daily_order_amount=Decimal("200000"),
    )
    client = MockTInvestClient(clock=clock)
    SyncService(session_factory, client, cfg, clock=clock).run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        GoalService().create_goal(
            session,
            GoalInput(
                name="Stop goal",
                target_amount=Decimal("2000000"),
                currency="RUB",
                target_date=clock.now().date() + timedelta(days=365),
                stop_loss_enabled=True,
                stop_loss_percent=Decimal("5"),
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
        instrument = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
        assert instrument is not None
        instrument.last_price = Decimal("200")
        instrument.last_price_at = clock.now()
        position = session.scalar(select(Position).where(Position.instrument_id == instrument.id))
        assert position is not None
        position.current_price = Decimal("200")
        position.current_price_at = clock.now()
        session.commit()
        run = RecommendationService(cfg, clock=clock).generate(session, account_id=account.id)
        session.commit()
        recommendation = next(
            item
            for item in run.recommendations
            if item.ticker == "SBER" and item.action is RecommendationAction.SELL
        )
        assert any(rule["code"] == "MANDATORY_EXIT" for rule in recommendation.triggered_rules)
        order = OrderService(client, cfg, clock=clock).approve(
            session,
            recommendation.id,
            user="tester",
        )
        session.commit()
        assert order.status is OrderStatus.APPROVED
        assert order.lots == 100


def test_expired_recommendation_cannot_be_approved(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _account, recommendation, _client, orders = prepare(session_factory, clock)
    clock.advance(minutes=31)
    with session_factory() as session:
        with pytest.raises(OrderError, match="истёк"):
            orders.approve(session, recommendation.id, user="tester")
        session.commit()
    with session_factory() as session:
        stored = session.get(Recommendation, recommendation.id)
        assert stored is not None
        assert stored.status is RecommendationStatus.EXPIRED
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_price_move_blocks_approval(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _account, recommendation, client, orders = prepare(session_factory, clock)
    client.set_price("mock-uid-gazp", Decimal("165"))
    SyncService(
        session_factory,
        client,
        settings(),
        clock=clock,
    ).run()
    with session_factory() as session, pytest.raises(OrderBlockedError) as caught:
        orders.approve(session, recommendation.id, user="tester")
    codes = {item.code for item in caught.value.decision.blocking_rules}
    assert "PRICE_DEVIATION" in codes
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        stored = session.get(Recommendation, recommendation.id)
        assert stored is not None
        assert stored.status is RecommendationStatus.NEW


def test_timeout_marks_order_outcome_unknown(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    class TimeoutClient(MockTInvestClient):
        def post_order(self, request: OrderRequest):  # type: ignore[override]
            raise TInvestTimeoutError("simulated timeout")

    client = TimeoutClient(clock=clock)
    _account, recommendation, client, orders = prepare(
        session_factory,
        clock,
        client=client,
    )
    with session_factory() as session:
        order = orders.approve(session, recommendation.id, user="tester")
        session.commit()
        order_id = order.id
    with session_factory() as session:
        sent = orders.send(session, order_id, user="tester")
        session.commit()
        assert sent.status is OrderStatus.SUBMIT_UNKNOWN
        assert "timeout" in (sent.warning_message or "")
        assert sent.broker_order_id is None
        stored = session.get(Recommendation, recommendation.id)
        assert stored is not None
        assert stored.status is RecommendationStatus.SENT
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        with pytest.raises(OrderError, match="не одобрена"):
            orders.send(session, order_id, user="tester")


def test_broker_rejection_marks_order_and_recommendation_failed(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    client.set_order_behavior(MockOrderBehavior.REJECT)
    _account, recommendation, client, orders = prepare(
        session_factory,
        clock,
        client=client,
    )
    with session_factory() as session:
        order = orders.approve(session, recommendation.id, user="tester")
        session.commit()
        order_id = order.id
    with session_factory() as session:
        sent = orders.send(session, order_id, user="tester")
        session.commit()
        assert sent.status is OrderStatus.REJECTED
        assert sent.error_message == "mock rejection"
        stored = session.get(Recommendation, recommendation.id)
        assert stored is not None
        assert stored.status is RecommendationStatus.FAILED


def test_bond_order_price_converts_percent_of_nominal() -> None:
    from app.services.valuation import instrument_order_price

    assert instrument_order_price(
        AssetClass.BOND,
        Decimal("99.99"),
        nominal=Decimal("7.4"),
    ) == Decimal("7.39926")
    assert instrument_order_price(AssetClass.STOCK, Decimal("99.99")) == Decimal("99.99")


def test_bond_approval_uses_currency_limit_price(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    _account, _recommendation, client, _orders = prepare(session_factory, clock)
    cfg = settings()

    class AllowRisk:
        def evaluate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return RiskDecision(
                allowed=True,
                estimated_amount=Decimal("955"),
                commission=Decimal("2.865"),
                cash_after=Decimal("100000"),
                projected_issuer_percent=Decimal("5"),
                projected_class_percent=Decimal("15"),
                triggered_rules=(),
                blocking_rules=(),
            )

    orders = OrderService(client, cfg, clock=clock, risk_engine=AllowRisk())
    with session_factory() as session:
        account = session.scalar(select(Account))
        bond = session.scalar(select(Instrument).where(Instrument.ticker == "SU26248RMFS3"))
        goal = session.scalar(select(Goal))
        assert account is not None
        assert bond is not None
        assert goal is not None
        recommendation = Recommendation(
            account_id=account.id,
            goal_id=goal.id,
            instrument_id=bond.id,
            action=RecommendationAction.BUY,
            status=RecommendationStatus.NEW,
            order_type=OrderType.LIMIT,
            instrument_uid=bond.instrument_uid,
            figi=bond.figi,
            ticker=bond.ticker,
            currency="RUB",
            lots=1,
            lot_size=1,
            quote_price=Decimal("95.5"),
            price_per_lot=Decimal("955"),
            estimated_amount=Decimal("955"),
            commission_estimate=Decimal("2.865"),
            reason="test",
            calculated_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=30),
            mode=TradingMode.CONFIRM,
            fingerprint="f" * 64,
            input_hash="i" * 64,
        )
        session.add(recommendation)
        session.commit()
        order = orders.approve(session, recommendation.id, user="tester")
        session.commit()
        assert order.limit_price == Decimal("955")
        assert Decimal(order.request_payload["approved_price"]) == Decimal("955")
