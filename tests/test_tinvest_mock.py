from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.clock import FrozenClock
from app.core.enums import OrderSide, OrderType
from app.integrations.tinvest.mock import MockOrderBehavior, MockTInvestClient
from app.integrations.tinvest.types import BrokerOrderStatus, OrderRequest


def request(*, lots: int = 1, side: OrderSide = OrderSide.BUY) -> OrderRequest:
    return OrderRequest(
        account_id="sandbox-demo-account",
        instrument_id="mock-uid-sber",
        side=side,
        lots=lots,
        order_type=OrderType.LIMIT,
        client_order_id="00000000-0000-0000-0000-000000000001",
        limit_price=Decimal("300"),
    )


def test_mock_snapshot_is_deterministic(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)

    accounts = client.get_accounts()
    portfolio = client.get_portfolio("sandbox-demo-account")
    positions = client.get_positions("sandbox-demo-account")
    operations = client.get_operations(
        "sandbox-demo-account",
        clock.now() - timedelta(days=365),
        clock.now(),
    )

    assert len(accounts) == 1
    assert accounts[0].is_sandbox is True
    assert portfolio.total_amount_portfolio == Decimal("1085000.00")
    assert {item.ticker for item in portfolio.positions} == {
        "SBER",
        "GAZP",
        "SU26248RMFS3",
        "TMOS",
        "USD000UTSTOM",
    }
    assert all(item.quantity > 0 for item in portfolio.positions)
    assert {item.currency for item in positions.cash} == {"RUB", "USD"}
    assert len(operations) == 8
    assert all(item.occurred_at.tzinfo is not None for item in operations)
    assert sum(1 for item in operations if item.payment > 0) == 2


def test_mock_full_fill_is_idempotent(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)
    cash_before = client.cash("RUB")
    quantity_before = client.quantity("mock-uid-sber")

    first = client.post_order(request())
    second = client.post_order(request())

    assert first.status is BrokerOrderStatus.FILL
    assert first.broker_order_id == second.broker_order_id
    assert client.cash("RUB") == cash_before - Decimal("3009.00")
    assert client.quantity("mock-uid-sber") == quantity_before + Decimal("10")
    assert first.executions[0].lots == 1
    assert first.executions[0].commission == Decimal("9.00")


def test_mock_partial_fill_and_rejection(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)
    client.set_order_behavior(MockOrderBehavior.PARTIAL)

    partial = client.post_order(request(lots=2))

    assert partial.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert partial.lots_requested == 2
    assert partial.lots_executed == 1
    assert partial.executions[0].is_partial is True

    huge = OrderRequest(
        account_id="sandbox-demo-account",
        instrument_id="mock-uid-sber",
        side=OrderSide.BUY,
        lots=100_000,
        order_type=OrderType.LIMIT,
        client_order_id="00000000-0000-0000-0000-000000000002",
        limit_price=Decimal("300"),
    )
    rejected = client.post_order(huge)
    assert rejected.status is BrokerOrderStatus.REJECTED
    assert rejected.lots_executed == 0


def test_mock_rejects_unavailable_and_stale_quotes(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)
    client.set_trading("mock-uid-sber", buy_available=False)
    rejected = client.post_order(request())
    assert rejected.status is BrokerOrderStatus.REJECTED

    client.set_price_age(600)
    price = client.get_last_prices(["mock-uid-sber"])[0]
    assert price.at == clock.now() - timedelta(seconds=600)
    assert price.at.tzinfo == UTC


def test_mock_state_survives_restart(clock: FrozenClock, tmp_path: Path) -> None:
    state_path = tmp_path / "mock-state.json"
    client = MockTInvestClient(clock=clock, state_path=state_path)
    order = client.post_order(request())
    account_id = client.open_sandbox_account("Постоянный счёт")
    quantity = client.quantity("mock-uid-sber")
    cash = client.cash("RUB")
    assert state_path.exists()

    restored = MockTInvestClient(clock=clock, state_path=state_path)

    assert restored.quantity("mock-uid-sber") == quantity
    assert restored.cash("RUB") == cash
    state = restored.get_order_state("sandbox-demo-account", order.broker_order_id)
    assert state.status is BrokerOrderStatus.FILL
    assert account_id in {item.tinvest_account_id for item in restored.get_accounts()}
    assert "Постоянный счёт" in {item.name for item in restored.get_accounts()}


def test_mock_sandbox_lifecycle(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)
    account_id = client.open_sandbox_account("Тестовый счёт")
    balance = client.sandbox_pay_in(account_id, "RUB", Decimal("1234.50"))

    assert balance == Decimal("151234.50")
    assert account_id in {account.tinvest_account_id for account in client.get_accounts()}
    client.close_sandbox_account(account_id)
    assert account_id not in {account.tinvest_account_id for account in client.get_accounts()}


def test_mock_requires_timezone_aware_datetime() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 10, 27, 12, 0))


def test_mock_candles_are_deterministic(clock: FrozenClock) -> None:
    client = MockTInvestClient(clock=clock)
    candles = client.get_candles(
        "mock-uid-sber",
        clock.now() - timedelta(days=30),
        clock.now(),
        "day",
    )
    assert 30 <= len(candles) <= 32
    assert all(candle.close > 0 for candle in candles)
    assert all(candle.high >= candle.close >= candle.low for candle in candles)
    again = client.get_candles(
        "mock-uid-sber",
        clock.now() - timedelta(days=30),
        clock.now(),
        "day",
    )
    assert [candle.close for candle in candles] == [candle.close for candle in again]
