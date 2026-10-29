from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from app.core.clock import FrozenClock
from app.core.enums import OrderSide, OrderType
from app.integrations.tinvest.exceptions import (
    TInvestAuthError,
    TInvestOrderOutcomeUnknown,
    TInvestValidationError,
)
from app.integrations.tinvest.rest import TInvestRestClient
from app.integrations.tinvest.types import BrokerOrderStatus, OrderRequest

ORDER_ID = "00000000-0000-0000-0000-000000000042"


def money(units: str, nano: int = 0, currency: str = "RUB") -> dict[str, Any]:
    return {"units": units, "nano": nano, "currency": currency}


def make_client(
    handler: httpx.MockTransport,
    clock: FrozenClock,
    *,
    sandbox: bool = True,
    max_retries: int = 0,
) -> TInvestRestClient:
    return TInvestRestClient(
        "test-token-not-real",
        base_url="https://api.test/rest",
        sandbox=sandbox,
        max_retries=max_retries,
        clock=clock,
        http_client=httpx.Client(transport=handler),
        sleep=lambda _seconds: None,
        retry_backoff_seconds=0,
    )


def order_request() -> OrderRequest:
    return OrderRequest(
        account_id="acc-1",
        instrument_id="uid-sber",
        side=OrderSide.BUY,
        lots=1,
        order_type=OrderType.LIMIT,
        client_order_id=ORDER_ID,
        limit_price=Decimal("300.25"),
    )


def test_rest_reads_are_validated_and_headers_are_sent(clock: FrozenClock) -> None:
    calls: list[tuple[str, dict[str, Any], httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        calls.append((request.url.path, payload, request.headers))
        if request.url.path.endswith("GetSandboxAccounts"):
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "acc-1",
                            "type": "ACCOUNT_TYPE_TINKOFF",
                            "name": "Sandbox",
                            "status": "ACCOUNT_STATUS_OPEN",
                            "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                        }
                    ]
                },
            )
        if request.url.path.endswith("GetLastPrices"):
            return httpx.Response(
                200,
                json={
                    "lastPrices": [
                        {
                            "instrumentUid": "uid-sber",
                            "figi": "BBG004730N88",
                            "ticker": "SBER",
                            "classCode": "TQBR",
                            "price": {"units": "300", "nano": 500000000},
                            "time": "2026-10-27T11:59:00Z",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = make_client(httpx.MockTransport(handler), clock)
    accounts = client.get_accounts()
    prices = client.get_last_prices(["uid-sber"])

    assert accounts[0].tinvest_account_id == "acc-1"
    assert accounts[0].is_sandbox is True
    assert prices[0].price == Decimal("300.5")
    assert prices[0].at == datetime(2026, 10, 27, 11, 59, tzinfo=UTC)
    assert calls[0][0].endswith(
        "tinkoff.public.invest.api.contract.v1.SandboxService/GetSandboxAccounts"
    )
    assert calls[1][0].endswith(
        "tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices"
    )
    assert calls[1][1] == {"instrumentId": ["uid-sber"]}
    assert calls[0][2]["authorization"] == "Bearer test-token-not-real"
    assert calls[0][2]["x-app-name"] == "InvestPilotLocal"


def test_rest_retries_safe_read_on_503(clock: FrozenClock) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"message": "temporary"})
        return httpx.Response(
            200,
            json={
                "lastPrices": [
                    {
                        "instrumentUid": "uid-sber",
                        "price": {"units": "301", "nano": 0},
                        "time": "2026-10-27T11:59:30Z",
                    }
                ]
            },
        )

    client = make_client(httpx.MockTransport(handler), clock, max_retries=1)
    prices = client.get_last_prices(["uid-sber"])

    assert attempts == 2
    assert prices[0].price == Decimal("301")


def test_rest_post_order_payload_and_fill(clock: FrozenClock) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "orderId": "broker-1",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsRequested": "1",
                "lotsExecuted": "1",
                "initialOrderPrice": money("300", 250000000),
                "executedOrderPrice": money("300", 250000000),
                "totalOrderAmount": money("300", 250000000),
                "initialCommission": money("1", 0),
                "executedCommission": money("1", 0),
                "orderRequestId": ORDER_ID,
                "currency": "RUB",
            },
        )

    client = make_client(httpx.MockTransport(handler), clock)
    result = client.post_order(order_request())

    assert result.status is BrokerOrderStatus.FILL
    assert result.broker_order_id == "broker-1"
    assert len(calls) == 1
    assert calls[0]["quantity"] == "1"
    assert calls[0]["orderId"] == ORDER_ID
    assert calls[0]["direction"] == "ORDER_DIRECTION_BUY"
    assert calls[0]["orderType"] == "ORDER_TYPE_LIMIT"
    assert calls[0]["price"] == {"units": "300", "nano": 250000000}
    assert calls[0]["confirmMarginTrade"] is False


def test_rest_never_retries_order_submission(clock: FrozenClock) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"message": "gateway down"})

    client = make_client(httpx.MockTransport(handler), clock, max_retries=3)

    with pytest.raises(TInvestOrderOutcomeUnknown):
        client.post_order(order_request())
    assert attempts == 1


def test_rest_order_state_and_partial_execution(clock: FrozenClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderId": "broker-2",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                "lotsRequested": "2",
                "lotsExecuted": "1",
                "initialOrderPrice": money("300", 0),
                "executedOrderPrice": money("300", 0),
                "totalOrderAmount": money("600", 0),
                "initialCommission": money("2", 0),
                "executedCommission": money("1", 0),
                "orderRequestId": ORDER_ID,
                "currency": "RUB",
                "orderDate": "2026-10-27T11:00:00Z",
                "stages": [
                    {
                        "price": money("300", 0),
                        "quantity": "1",
                        "tradeId": "trade-1",
                        "executionTime": "2026-10-27T11:00:01Z",
                    }
                ],
            },
        )

    client = make_client(httpx.MockTransport(handler), clock)
    result = client.get_order_state("acc-1", "broker-2", lot_size=10)

    assert result.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert result.executions[0].lots == 1
    assert result.executions[0].amount == Decimal("3000")
    assert result.executions[0].executed_at == datetime(2026, 10, 27, 11, 0, 1, tzinfo=UTC)


def test_rest_sandbox_pay_in_validates_request(clock: FrozenClock) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"balance": {"currency": "RUB", "units": "151234", "nano": 500000000}},
        )

    client = make_client(httpx.MockTransport(handler), clock)
    balance = client.sandbox_pay_in("acc-1", "rub", Decimal("1234.50"))

    assert balance == Decimal("151234.5")
    assert calls[0]["accountId"] == "acc-1"
    assert calls[0]["amount"] == {
        "currency": "RUB",
        "units": "1234",
        "nano": 500000000,
    }


def test_rest_maps_http_errors_without_retrying_mutations(clock: FrozenClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "code": 16,
                "message": "authentication token is missing or invalid",
                "description": 40003,
            },
        )

    client = make_client(httpx.MockTransport(handler), clock)
    with pytest.raises(TInvestAuthError) as exc_info:
        client.get_accounts()

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == 16
    assert exc_info.value.description == "40003"


def test_rest_rejects_http_base_url() -> None:
    with pytest.raises(TInvestValidationError):
        TInvestRestClient("token", base_url="http://api.test/rest")


def test_rest_get_candles_maps_response(clock: FrozenClock) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "open": {"units": "300", "nano": 0},
                        "high": {"units": "310", "nano": 0},
                        "low": {"units": "295", "nano": 0},
                        "close": {"units": "305", "nano": 500000000},
                        "volume": "1234",
                        "time": "2026-10-25T00:00:00Z",
                        "isComplete": True,
                    }
                ]
            },
        )

    client = make_client(httpx.MockTransport(handler), clock)
    candles = client.get_candles(
        "uid-sber",
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 27, tzinfo=UTC),
        "day",
    )

    assert len(candles) == 1
    assert candles[0].close == Decimal("305.5")
    assert candles[0].volume == 1234
    assert calls[0] == {
        "instrumentId": "uid-sber",
        "from": "2026-10-01T00:00:00Z",
        "to": "2026-10-27T00:00:00Z",
        "interval": "CANDLE_INTERVAL_DAY",
    }


def test_rest_base_url_with_contract_package_is_not_duplicated(clock: FrozenClock) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"accounts": []})

    client = TInvestRestClient(
        "test-token-not-real",
        base_url="https://api.test/rest/tinkoff.public.invest.api.contract.v1",
        sandbox=True,
        clock=clock,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.get_accounts()
    assert seen[0].count("tinkoff.public.invest.api.contract.v1") == 1


def test_rest_list_instruments_shares(clock: FrozenClock) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(
            200,
            json={
                "instruments": [
                    {
                        "uid": "uid-sber",
                        "figi": "BBG004730N88",
                        "ticker": "SBER",
                        "classCode": "TQBR",
                        "lot": 10,
                        "currency": "rub",
                        "name": "Сбербанк",
                        "exchange": "MOEX",
                        "sector": "financial",
                        "buyAvailableFlag": True,
                        "sellAvailableFlag": True,
                        "apiTradeAvailableFlag": True,
                    }
                ]
            },
        )

    client = make_client(httpx.MockTransport(handler), clock)
    instruments = client.list_instruments("shares")

    assert len(instruments) == 1
    assert instruments[0].ticker == "SBER"
    assert instruments[0].lot == 10
    assert instruments[0].buy_available is True
    assert calls[0][0].endswith("InstrumentsService/Shares")
    assert calls[0][1] == {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}
