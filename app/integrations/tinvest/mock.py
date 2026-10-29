"""Deterministic in-memory T-Invest client. No network, no randomness, no AI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.clock import Clock, SystemClock
from app.core.enums import AssetClass, OrderSide
from app.core.money import quantize_money, quantize_price
from app.integrations.tinvest.client import CandleInterval, InstrumentKind, TInvestClient
from app.integrations.tinvest.exceptions import (
    TInvestNotFoundError,
    TInvestValidationError,
)
from app.integrations.tinvest.types import (
    AccountInfo,
    BrokerOrderStatus,
    CandleInfo,
    CashInfo,
    InstrumentInfo,
    OperationInfo,
    OrderExecutionInfo,
    OrderRequest,
    OrderResult,
    PortfolioInfo,
    PortfolioPositionInfo,
    PositionsInfo,
    PriceInfo,
    TradeInfo,
    TradingStatusInfo,
)

ZERO = Decimal("0")
MOCK_COMMISSION_RATE = Decimal("0.003")
MOCK_STATE_VERSION = 1
LOGGER = logging.getLogger(__name__)


class MockOrderBehavior(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NEW = "NEW"
    REJECT = "REJECT"


@dataclass(frozen=True)
class MockInstrumentSpec:
    uid: str
    figi: str
    ticker: str
    class_code: str
    isin: str
    name: str
    instrument_type: str
    asset_class: AssetClass
    currency: str
    lot: int
    exchange: str
    issuer_key: str
    price: Decimal
    average_price: Decimal
    quantity: Decimal
    sector: str | None = None
    country_of_risk: str | None = "RU"
    maturity_date: date | None = None
    nominal: Decimal | None = None
    coupon_quantity_per_year: int | None = None
    income_yield: Decimal | None = None
    liquidity_flag: bool | None = True
    volatility_30d: Decimal | None = None
    nkd: Decimal = ZERO

    def unit_value(self, quote: Decimal) -> Decimal:
        if self.asset_class is AssetClass.BOND:
            nominal = self.nominal or Decimal("1000")
            return quote / Decimal("100") * nominal + self.nkd
        if self.asset_class is AssetClass.CURRENCY:
            nominal = self.nominal or Decimal("1")
            if nominal <= ZERO:
                nominal = Decimal("1")
            return quote / nominal
        return quote


@dataclass
class MockTradingState:
    status: str = "SECURITY_TRADING_STATUS_NORMAL_TRADING"
    buy_available: bool = True
    sell_available: bool = True
    api_trade_available: bool = True
    limit_order_available: bool = True
    market_order_available: bool = True


DEFAULT_SPECS: tuple[MockInstrumentSpec, ...] = (
    MockInstrumentSpec(
        uid="mock-uid-sber",
        figi="BBG004730N88",
        ticker="SBER",
        class_code="TQBR",
        isin="RU0009029540",
        name="Сбербанк",
        instrument_type="share",
        asset_class=AssetClass.STOCK,
        currency="RUB",
        lot=10,
        exchange="MOEX",
        issuer_key="SBER",
        price=Decimal("300"),
        average_price=Decimal("250"),
        quantity=Decimal("1000"),
        sector="Финансы",
        liquidity_flag=True,
        volatility_30d=Decimal("25"),
    ),
    MockInstrumentSpec(
        uid="mock-uid-gazp",
        figi="BBG004730RP0",
        ticker="GAZP",
        class_code="TQBR",
        isin="RU0007661625",
        name="Газпром",
        instrument_type="share",
        asset_class=AssetClass.STOCK,
        currency="RUB",
        lot=10,
        exchange="MOEX",
        issuer_key="GAZP",
        price=Decimal("150"),
        average_price=Decimal("140"),
        quantity=Decimal("2000"),
        sector="Энергетика",
        liquidity_flag=True,
        volatility_30d=Decimal("30"),
    ),
    MockInstrumentSpec(
        uid="mock-uid-lkoh",
        figi="BBG004731032",
        ticker="LKOH",
        class_code="TQBR",
        isin="RU0009024277",
        name="Лукойл",
        instrument_type="share",
        asset_class=AssetClass.STOCK,
        currency="RUB",
        lot=1,
        exchange="MOEX",
        issuer_key="LKOH",
        price=Decimal("6000"),
        average_price=Decimal("6000"),
        quantity=ZERO,
        sector="Энергетика",
        liquidity_flag=True,
        volatility_30d=Decimal("22"),
    ),
    MockInstrumentSpec(
        uid="mock-uid-ofz",
        figi="BBG00X8RZ2T2",
        ticker="SU26248RMFS3",
        class_code="TQOB",
        isin="RU000A0JX0J2",
        name="ОФЗ 26248",
        instrument_type="bond",
        asset_class=AssetClass.BOND,
        currency="RUB",
        lot=1,
        exchange="MOEX",
        issuer_key="MINFIN",
        price=Decimal("95.5"),
        average_price=Decimal("955"),
        quantity=Decimal("200"),
        sector="Государство",
        country_of_risk="RU",
        maturity_date=date(2030, 5, 15),
        nominal=Decimal("1000"),
        coupon_quantity_per_year=2,
        income_yield=Decimal("12.5"),
        liquidity_flag=True,
        volatility_30d=Decimal("6"),
        nkd=Decimal("5"),
    ),
    MockInstrumentSpec(
        uid="mock-uid-tmos",
        figi="BBG333333333",
        ticker="TMOS",
        class_code="TQTF",
        isin="RU000A101X76",
        name="Т-Технологии",
        instrument_type="etf",
        asset_class=AssetClass.ETF,
        currency="RUB",
        lot=10,
        exchange="MOEX",
        issuer_key="TMOS",
        price=Decimal("8"),
        average_price=Decimal("7.5"),
        quantity=Decimal("1000"),
        sector="Технологии",
        liquidity_flag=True,
        volatility_30d=Decimal("15"),
    ),
    MockInstrumentSpec(
        uid="mock-uid-usd",
        figi="BBG0013HGFT4",
        ticker="USD000UTSTOM",
        class_code="CETS",
        isin="",
        name="Доллар США",
        instrument_type="currency",
        asset_class=AssetClass.CURRENCY,
        currency="USD",
        lot=1000,
        exchange="MOEX",
        issuer_key="USD",
        price=Decimal("90"),
        average_price=Decimal("88"),
        quantity=Decimal("1000"),
        country_of_risk=None,
        nominal=Decimal("1"),
        liquidity_flag=None,
        volatility_30d=Decimal("10"),
    ),
)


class MockTInvestClient(TInvestClient):
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sandbox: bool = True,
        commission_rate: Decimal = MOCK_COMMISSION_RATE,
        state_path: Path | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._sandbox = sandbox
        self._commission_rate = commission_rate
        self._state_path = Path(state_path) if state_path is not None else None
        self._anchor = self._clock.now()
        self._specs = {spec.uid: spec for spec in DEFAULT_SPECS}
        self._prices = {spec.uid: spec.price for spec in DEFAULT_SPECS}
        self._price_age_seconds = 30
        self._positions = {spec.uid: spec.quantity for spec in DEFAULT_SPECS}
        self._cash: dict[str, Decimal] = {"RUB": Decimal("150000"), "USD": Decimal("500")}
        self._trading = {spec.uid: MockTradingState() for spec in DEFAULT_SPECS}
        self._behavior: MockOrderBehavior = MockOrderBehavior.FULL
        self._orders: dict[str, OrderResult] = {}
        self._broker_to_client: dict[str, str] = {}
        self._order_sequence = 0
        self._account = AccountInfo(
            tinvest_account_id="sandbox-demo-account",
            name="Демо-счёт (mock)",
            account_type="ACCOUNT_TYPE_TINKOFF",
            status="ACCOUNT_STATUS_OPEN",
            access_level="ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
            opened_date=self._anchor - timedelta(days=400),
            is_sandbox=sandbox,
        )
        self._extra_accounts: list[AccountInfo] = []
        self._load_state()

    @property
    def is_sandbox(self) -> bool:
        return self._sandbox

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    @property
    def is_mock(self) -> bool:
        return True

    @property
    def now(self) -> datetime:
        return self._clock.now()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != MOCK_STATE_VERSION:
                raise ValueError("unsupported mock state version")
            anchor = datetime.fromisoformat(str(raw["anchor"]))
            prices = {
                uid: Decimal(str(value))
                for uid, value in dict(raw.get("prices", {})).items()
                if uid in self._specs
            }
            positions = {
                uid: Decimal(str(value))
                for uid, value in dict(raw.get("positions", {})).items()
                if uid in self._specs
            }
            cash = {
                str(currency).upper(): Decimal(str(value))
                for currency, value in dict(raw.get("cash", {})).items()
            }
            trading: dict[str, MockTradingState] = {}
            for uid, item in dict(raw.get("trading", {})).items():
                if uid not in self._specs or not isinstance(item, dict):
                    continue
                trading[uid] = MockTradingState(
                    status=str(item.get("status", "SECURITY_TRADING_STATUS_NORMAL_TRADING")),
                    buy_available=bool(item.get("buy_available", True)),
                    sell_available=bool(item.get("sell_available", True)),
                    api_trade_available=bool(item.get("api_trade_available", True)),
                    limit_order_available=bool(item.get("limit_order_available", True)),
                    market_order_available=bool(item.get("market_order_available", True)),
                )
            orders: dict[str, OrderResult] = {}
            for item in list(raw.get("orders", [])):
                if not isinstance(item, dict):
                    continue
                executions = tuple(
                    OrderExecutionInfo(
                        trade_id=execution.get("trade_id"),
                        lots=int(execution["lots"]),
                        lot_size=int(execution["lot_size"]),
                        price=Decimal(str(execution["price"])),
                        amount=Decimal(str(execution["amount"])),
                        commission=Decimal(str(execution["commission"])),
                        executed_at=datetime.fromisoformat(str(execution["executed_at"])),
                        is_partial=bool(execution.get("is_partial", False)),
                    )
                    for execution in list(item.get("executions", []))
                    if isinstance(execution, dict)
                )
                order_date_raw = item.get("order_date")
                order = OrderResult(
                    broker_order_id=str(item["broker_order_id"]),
                    client_order_id=str(item["client_order_id"]),
                    status=BrokerOrderStatus(str(item["status"])),
                    raw_status=str(item.get("raw_status", "")),
                    lots_requested=int(item["lots_requested"]),
                    lots_executed=int(item["lots_executed"]),
                    initial_order_price=Decimal(str(item["initial_order_price"])),
                    executed_order_price=Decimal(str(item["executed_order_price"])),
                    total_order_amount=Decimal(str(item["total_order_amount"])),
                    initial_commission=Decimal(str(item["initial_commission"])),
                    executed_commission=Decimal(str(item["executed_commission"])),
                    currency=str(item["currency"]),
                    message=str(item.get("message", "")),
                    executions=executions,
                    order_date=(
                        datetime.fromisoformat(str(order_date_raw))
                        if order_date_raw is not None
                        else None
                    ),
                )
                orders[order.client_order_id] = order
            extra_accounts = [
                AccountInfo(
                    tinvest_account_id=str(item["tinvest_account_id"]),
                    name=str(item.get("name", "")),
                    account_type=str(item.get("account_type", "")),
                    status=str(item.get("status", "")),
                    access_level=str(item.get("access_level", "")),
                    opened_date=(
                        datetime.fromisoformat(str(item["opened_date"]))
                        if item.get("opened_date") is not None
                        else None
                    ),
                    closed_date=(
                        datetime.fromisoformat(str(item["closed_date"]))
                        if item.get("closed_date") is not None
                        else None
                    ),
                    is_sandbox=bool(item.get("is_sandbox", True)),
                )
                for item in list(raw.get("extra_accounts", []))
                if isinstance(item, dict)
            ]
            behavior = MockOrderBehavior(str(raw.get("behavior", MockOrderBehavior.FULL.value)))
            broker_to_client = {
                str(key): str(value) for key, value in dict(raw.get("broker_to_client", {})).items()
            }
            order_sequence = int(raw.get("order_sequence", 0))
            price_age_seconds = int(raw.get("price_age_seconds", 30))
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            LOGGER.warning("mock state ignored: %s", exc)
            return
        self._anchor = anchor
        self._prices.update(prices)
        self._positions.update(positions)
        self._cash.update(cash)
        self._trading.update(trading)
        self._behavior = behavior
        self._orders = orders
        self._broker_to_client = broker_to_client
        self._order_sequence = order_sequence
        self._price_age_seconds = price_age_seconds
        self._extra_accounts = extra_accounts

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        payload: dict[str, Any] = {
            "version": MOCK_STATE_VERSION,
            "anchor": self._anchor.isoformat(),
            "price_age_seconds": self._price_age_seconds,
            "prices": {uid: str(value) for uid, value in self._prices.items()},
            "positions": {uid: str(value) for uid, value in self._positions.items()},
            "cash": {currency: str(value) for currency, value in self._cash.items()},
            "trading": {
                uid: {
                    "status": state.status,
                    "buy_available": state.buy_available,
                    "sell_available": state.sell_available,
                    "api_trade_available": state.api_trade_available,
                    "limit_order_available": state.limit_order_available,
                    "market_order_available": state.market_order_available,
                }
                for uid, state in self._trading.items()
            },
            "behavior": self._behavior.value,
            "order_sequence": self._order_sequence,
            "orders": [
                {
                    "broker_order_id": order.broker_order_id,
                    "client_order_id": order.client_order_id,
                    "status": order.status.value,
                    "raw_status": order.raw_status,
                    "lots_requested": order.lots_requested,
                    "lots_executed": order.lots_executed,
                    "initial_order_price": str(order.initial_order_price),
                    "executed_order_price": str(order.executed_order_price),
                    "total_order_amount": str(order.total_order_amount),
                    "initial_commission": str(order.initial_commission),
                    "executed_commission": str(order.executed_commission),
                    "currency": order.currency,
                    "message": order.message,
                    "order_date": order.order_date.isoformat() if order.order_date else None,
                    "executions": [
                        {
                            "trade_id": execution.trade_id,
                            "lots": execution.lots,
                            "lot_size": execution.lot_size,
                            "price": str(execution.price),
                            "amount": str(execution.amount),
                            "commission": str(execution.commission),
                            "executed_at": execution.executed_at.isoformat(),
                            "is_partial": execution.is_partial,
                        }
                        for execution in order.executions
                    ],
                }
                for order in self._orders.values()
            ],
            "broker_to_client": dict(self._broker_to_client),
            "extra_accounts": [
                {
                    "tinvest_account_id": account.tinvest_account_id,
                    "name": account.name,
                    "account_type": account.account_type,
                    "status": account.status,
                    "access_level": account.access_level,
                    "opened_date": (
                        account.opened_date.isoformat() if account.opened_date else None
                    ),
                    "closed_date": (
                        account.closed_date.isoformat() if account.closed_date else None
                    ),
                    "is_sandbox": account.is_sandbox,
                }
                for account in self._extra_accounts
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_path.with_name(f".{self._state_path.name}.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(self._state_path)
        except (OSError, TypeError, ValueError) as exc:
            LOGGER.warning("mock state save failed: %s", exc)

    def _find_spec(self, identifier: str) -> MockInstrumentSpec:
        for spec in self._specs.values():
            if identifier in (spec.uid, spec.figi, spec.ticker):
                return spec
        raise TInvestNotFoundError(f"instrument {identifier} not found in mock")

    def _status(self, spec: MockInstrumentSpec) -> MockTradingState:
        return self._trading[spec.uid]

    def _instrument_info(self, spec: MockInstrumentSpec) -> InstrumentInfo:
        state = self._status(spec)
        return InstrumentInfo(
            instrument_uid=spec.uid,
            figi=spec.figi,
            ticker=spec.ticker,
            class_code=spec.class_code,
            isin=spec.isin or None,
            name=spec.name,
            instrument_type=spec.instrument_type,
            asset_class=spec.asset_class,
            currency=spec.currency,
            lot=spec.lot,
            exchange=spec.exchange,
            issuer_key=spec.issuer_key,
            sector=spec.sector,
            country_of_risk=spec.country_of_risk,
            maturity_date=spec.maturity_date,
            nominal=spec.nominal,
            coupon_quantity_per_year=spec.coupon_quantity_per_year,
            income_yield=spec.income_yield,
            liquidity_flag=spec.liquidity_flag,
            trading_status=state.status,
            buy_available=state.buy_available,
            sell_available=state.sell_available,
            api_trade_available=state.api_trade_available,
            extra={"volatility_30d": spec.volatility_30d},
        )

    # ----- read methods -------------------------------------------------

    def get_accounts(self) -> list[AccountInfo]:
        return [self._account, *self._extra_accounts]

    def _cash_infos(self) -> tuple[CashInfo, ...]:
        return tuple(
            CashInfo(currency=currency, amount=amount, blocked=ZERO)
            for currency, amount in sorted(self._cash.items())
            if amount != ZERO
        )

    def get_portfolio(self, account_id: str) -> PortfolioInfo:
        self._require_account(account_id)
        positions: list[PortfolioPositionInfo] = []
        total = ZERO
        for uid, quantity in self._positions.items():
            if quantity == ZERO:
                continue
            spec = self._specs[uid]
            quote = self._prices[uid]
            unit_value = spec.unit_value(quote)
            expected = (unit_value - spec.average_price) * quantity
            positions.append(
                PortfolioPositionInfo(
                    instrument_uid=spec.uid,
                    figi=spec.figi,
                    ticker=spec.ticker,
                    class_code=spec.class_code,
                    instrument_type=spec.instrument_type,
                    quantity=quantity,
                    blocked_lots=ZERO,
                    average_price=spec.average_price,
                    average_price_fifo=None,
                    current_price=unit_value - spec.nkd,
                    current_nkd=spec.nkd,
                    expected_yield=expected,
                    currency=spec.currency,
                    current_price_at=self.now - timedelta(seconds=self._price_age_seconds),
                )
            )
            total += unit_value * quantity
        for currency, amount in self._cash.items():
            if currency == "RUB":
                total += amount
            elif currency == "USD":
                total += amount * self._prices["mock-uid-usd"]
        return PortfolioInfo(
            account_id=account_id,
            positions=tuple(positions),
            total_amount_portfolio=quantize_money(total),
            currency="RUB",
            daily_yield=Decimal("0"),
            received_at=self.now,
        )

    def get_positions(self, account_id: str) -> PositionsInfo:
        self._require_account(account_id)
        return PositionsInfo(
            account_id=account_id,
            cash=self._cash_infos(),
            received_at=self.now,
        )

    def get_operations(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[OperationInfo]:
        self._require_account(account_id)
        operations: list[OperationInfo] = []
        sequence = 1
        for index, spec in enumerate(self._specs.values()):
            quantity = int(spec.quantity)
            if quantity <= 0:
                continue
            when = self._anchor - timedelta(days=300 - index * 10)
            operations.append(
                OperationInfo(
                    operation_id=f"mock-op-{sequence:04d}",
                    parent_operation_id=None,
                    instrument_uid=spec.uid,
                    figi=spec.figi,
                    currency=spec.currency,
                    payment=-spec.average_price * quantity,
                    price=spec.average_price,
                    state="OPERATION_STATE_EXECUTED",
                    quantity=quantity,
                    quantity_rest=0,
                    occurred_at=when,
                    type_label="Покупка ценных бумаг",
                    operation_type="OPERATION_TYPE_BUY",
                    instrument_type=spec.instrument_type,
                    trades=(
                        TradeInfo(
                            trade_id=f"mock-trade-{sequence:04d}",
                            quantity=quantity,
                            price=spec.average_price,
                            at=when,
                        ),
                    ),
                )
            )
            sequence += 1
        operations.extend(
            [
                OperationInfo(
                    operation_id="mock-op-dividend",
                    parent_operation_id=None,
                    instrument_uid="mock-uid-sber",
                    figi="BBG004730N88",
                    currency="RUB",
                    payment=Decimal("12000"),
                    price=ZERO,
                    state="OPERATION_STATE_EXECUTED",
                    quantity=0,
                    quantity_rest=0,
                    occurred_at=self._anchor - timedelta(days=45),
                    type_label="Выплата дивидендов",
                    operation_type="OPERATION_TYPE_DIVIDEND",
                    instrument_type="share",
                ),
                OperationInfo(
                    operation_id="mock-op-coupon",
                    parent_operation_id=None,
                    instrument_uid="mock-uid-ofz",
                    figi="BBG00X8RZ2T2",
                    currency="RUB",
                    payment=Decimal("12500"),
                    price=ZERO,
                    state="OPERATION_STATE_EXECUTED",
                    quantity=0,
                    quantity_rest=0,
                    occurred_at=self._anchor - timedelta(days=20),
                    type_label="Выплата купонов",
                    operation_type="OPERATION_TYPE_COUPON",
                    instrument_type="bond",
                ),
                OperationInfo(
                    operation_id="mock-op-fee",
                    parent_operation_id=None,
                    instrument_uid=None,
                    figi=None,
                    currency="RUB",
                    payment=Decimal("-150"),
                    price=ZERO,
                    state="OPERATION_STATE_EXECUTED",
                    quantity=0,
                    quantity_rest=0,
                    occurred_at=self._anchor - timedelta(days=10),
                    type_label="Комиссия за операцию",
                    operation_type="OPERATION_TYPE_BROKER_FEE",
                    instrument_type="",
                ),
            ]
        )
        return [
            operation for operation in operations if date_from <= operation.occurred_at <= date_to
        ]

    def get_instrument(
        self,
        instrument_id: str,
        id_type: str = "uid",
        class_code: str | None = None,
    ) -> InstrumentInfo:
        if id_type == "class_code":
            raise TInvestValidationError("unsupported mock id type")
        spec = self._find_spec(instrument_id)
        if id_type == "ticker" and class_code and spec.class_code != class_code:
            raise TInvestNotFoundError(f"instrument {instrument_id}/{class_code} not found")
        return self._instrument_info(spec)

    def get_candles(
        self,
        instrument_id: str,
        date_from: datetime,
        date_to: datetime,
        interval: CandleInterval = "day",
    ) -> list[CandleInfo]:
        spec = self._find_spec(instrument_id)
        step = {
            "1min": timedelta(minutes=1),
            "5min": timedelta(minutes=5),
            "15min": timedelta(minutes=15),
            "hour": timedelta(hours=1),
            "day": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
        }.get(interval, timedelta(days=1))
        seed = sum(ord(char) for char in spec.uid)
        candles: list[CandleInfo] = []
        current = date_from
        previous = spec.price
        index = 0
        while current <= date_to and len(candles) < 1000:
            factor = Decimal("1") + Decimal((index * 13 + seed) % 21 - 10) / Decimal("100")
            close = quantize_price(spec.price * factor)
            open_price = previous
            high = quantize_price(max(open_price, close) * Decimal("1.01"))
            low = quantize_price(min(open_price, close) * Decimal("0.99"))
            candles.append(
                CandleInfo(
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=1000 + (index * 17 + seed) % 5000,
                    at=current,
                    is_complete=True,
                )
            )
            previous = close
            current += step
            index += 1
        return candles

    def list_instruments(self, kind: InstrumentKind = "shares") -> list[InstrumentInfo]:
        mapping = {
            "shares": AssetClass.STOCK,
            "bonds": AssetClass.BOND,
            "etfs": AssetClass.ETF,
            "currencies": AssetClass.CURRENCY,
            "futures": AssetClass.OTHER,
        }
        asset_class = mapping.get(kind)
        if asset_class is None:
            raise TInvestValidationError("unsupported instrument kind")
        return [
            self._instrument_info(spec)
            for spec in self._specs.values()
            if spec.asset_class is asset_class
        ]

    def get_last_prices(self, instrument_ids: list[str]) -> list[PriceInfo]:
        at = self.now - timedelta(seconds=self._price_age_seconds)
        result: list[PriceInfo] = []
        for identifier in instrument_ids:
            try:
                spec = self._find_spec(identifier)
            except TInvestNotFoundError:
                continue
            result.append(
                PriceInfo(
                    instrument_uid=spec.uid,
                    figi=spec.figi,
                    ticker=spec.ticker,
                    class_code=spec.class_code,
                    price=self._prices[spec.uid],
                    at=at,
                )
            )
        return result

    def get_trading_statuses(self, instrument_ids: list[str]) -> list[TradingStatusInfo]:
        result: list[TradingStatusInfo] = []
        for identifier in instrument_ids:
            try:
                spec = self._find_spec(identifier)
            except TInvestNotFoundError:
                continue
            state = self._status(spec)
            result.append(
                TradingStatusInfo(
                    instrument_uid=spec.uid,
                    figi=spec.figi,
                    ticker=spec.ticker,
                    class_code=spec.class_code,
                    trading_status=state.status,
                    limit_order_available=state.limit_order_available,
                    market_order_available=state.market_order_available,
                    api_trade_available=state.api_trade_available,
                    bestprice_order_available=False,
                )
            )
        return result

    # ----- orders -------------------------------------------------------

    def set_order_behavior(self, behavior: MockOrderBehavior) -> None:
        self._behavior = behavior

    def post_order(self, request: OrderRequest) -> OrderResult:
        if request.client_order_id in self._orders:
            return self._orders[request.client_order_id]
        if request.lots <= 0:
            raise TInvestValidationError("lots must be positive")
        spec = self._find_spec(request.instrument_id)
        state = self._status(spec)
        quote = request.limit_price if request.limit_price is not None else self._prices[spec.uid]
        if quote is None or quote <= ZERO:
            raise TInvestValidationError("limit price is required and must be positive")
        self._order_sequence += 1
        broker_id = f"mock-order-{self._order_sequence:06d}"
        unit_value = spec.unit_value(quote)
        amount = quantize_money(unit_value * Decimal(request.lots) * Decimal(spec.lot))
        commission = quantize_money(amount * self._commission_rate)
        status = BrokerOrderStatus.NEW
        lots_executed = 0

        tradable = (
            state.api_trade_available
            and state.limit_order_available
            and (state.buy_available if request.side is OrderSide.BUY else state.sell_available)
        )
        if self._behavior is MockOrderBehavior.REJECT or not tradable:
            status = BrokerOrderStatus.REJECTED
        elif self._behavior is MockOrderBehavior.PARTIAL:
            lots_executed = max(1, request.lots // 2)
            status = BrokerOrderStatus.PARTIALLY_FILLED
        elif self._behavior is MockOrderBehavior.FULL:
            lots_executed = request.lots
            status = BrokerOrderStatus.FILL

        executions: list[OrderExecutionInfo] = []
        executed_amount = ZERO
        executed_commission = ZERO
        if lots_executed > 0:
            executed_quantity = lots_executed * spec.lot
            exec_amount = quantize_money(unit_value * Decimal(executed_quantity))
            exec_commission = quantize_money(exec_amount * self._commission_rate)
            if request.side is OrderSide.BUY:
                total_cost = exec_amount + exec_commission
                if self._cash.get(spec.currency, ZERO) < total_cost:
                    status = BrokerOrderStatus.REJECTED
                    lots_executed = 0
                else:
                    self._cash[spec.currency] = self._cash.get(spec.currency, ZERO) - total_cost
                    self._positions[spec.uid] = self._positions.get(spec.uid, ZERO) + Decimal(
                        executed_quantity
                    )
            else:
                available = self._positions.get(spec.uid, ZERO)
                if available < Decimal(executed_quantity):
                    status = BrokerOrderStatus.REJECTED
                    lots_executed = 0
                else:
                    self._positions[spec.uid] = available - Decimal(executed_quantity)
                    self._cash[spec.currency] = (
                        self._cash.get(spec.currency, ZERO) + exec_amount - exec_commission
                    )
            if lots_executed > 0:
                executed_amount = exec_amount
                executed_commission = exec_commission
                executions.append(
                    OrderExecutionInfo(
                        trade_id=f"mock-order-trade-{self._order_sequence:06d}",
                        lots=lots_executed,
                        lot_size=spec.lot,
                        price=quote,
                        amount=exec_amount,
                        commission=exec_commission,
                        executed_at=self.now,
                        is_partial=status is BrokerOrderStatus.PARTIALLY_FILLED,
                    )
                )

        result = OrderResult(
            broker_order_id=broker_id,
            client_order_id=request.client_order_id,
            status=status,
            raw_status={
                BrokerOrderStatus.FILL: "EXECUTION_REPORT_STATUS_FILL",
                BrokerOrderStatus.PARTIALLY_FILLED: "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                BrokerOrderStatus.NEW: "EXECUTION_REPORT_STATUS_NEW",
                BrokerOrderStatus.REJECTED: "EXECUTION_REPORT_STATUS_REJECTED",
                BrokerOrderStatus.CANCELLED: "EXECUTION_REPORT_STATUS_CANCELLED",
                BrokerOrderStatus.UNKNOWN: "EXECUTION_REPORT_STATUS_UNSPECIFIED",
            }[status],
            lots_requested=request.lots,
            lots_executed=lots_executed,
            initial_order_price=amount,
            executed_order_price=(
                quantize_money(executed_amount / Decimal(lots_executed * spec.lot))
                if lots_executed
                else ZERO
            ),
            total_order_amount=quantize_money(amount + commission),
            initial_commission=commission,
            executed_commission=executed_commission,
            currency=spec.currency,
            message=(
                "mock response" if status is not BrokerOrderStatus.REJECTED else "mock rejection"
            ),
            executions=tuple(executions),
            order_date=self.now,
        )
        self._orders[request.client_order_id] = result
        self._broker_to_client[broker_id] = request.client_order_id
        self._save_state()
        return result

    def _find_order(self, broker_order_id: str) -> OrderResult:
        client_id = self._broker_to_client.get(broker_order_id)
        if client_id is None and broker_order_id in self._orders:
            client_id = broker_order_id
        if client_id is None or client_id not in self._orders:
            raise TInvestNotFoundError(f"order {broker_order_id} not found in mock")
        return self._orders[client_id]

    def get_order_state(
        self,
        account_id: str,
        broker_order_id: str,
        lot_size: int = 1,
    ) -> OrderResult:
        self._require_account(account_id)
        return self._find_order(broker_order_id)

    def get_orders(self, account_id: str) -> list[OrderResult]:
        self._require_account(account_id)
        return list(self._orders.values())

    def cancel_order(self, account_id: str, broker_order_id: str) -> None:
        self._require_account(account_id)
        order = self._find_order(broker_order_id)
        if order.status in (BrokerOrderStatus.NEW, BrokerOrderStatus.PARTIALLY_FILLED):
            updated = replace(
                order,
                status=BrokerOrderStatus.CANCELLED,
                raw_status="EXECUTION_REPORT_STATUS_CANCELLED",
            )
            self._orders[updated.client_order_id] = updated
            self._save_state()

    # ----- sandbox ------------------------------------------------------

    def open_sandbox_account(self, name: str) -> str:
        account_id = f"sandbox-mock-{len(self._extra_accounts) + 2}"
        self._extra_accounts.append(
            AccountInfo(
                tinvest_account_id=account_id,
                name=name,
                account_type="ACCOUNT_TYPE_TINKOFF",
                status="ACCOUNT_STATUS_OPEN",
                access_level="ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                opened_date=self.now,
                is_sandbox=True,
            )
        )
        self._save_state()
        return account_id

    def sandbox_pay_in(self, account_id: str, currency: str, amount: Decimal) -> Decimal:
        self._require_account(account_id)
        if amount <= ZERO:
            raise TInvestValidationError("sandbox pay-in must be positive")
        self._cash[currency] = self._cash.get(currency, ZERO) + amount
        self._save_state()
        return self._cash[currency]

    def close_sandbox_account(self, account_id: str) -> None:
        self._require_account(account_id)
        if account_id == self._account.tinvest_account_id:
            raise TInvestValidationError("cannot close the primary mock account")
        self._extra_accounts = [
            account for account in self._extra_accounts if account.tinvest_account_id != account_id
        ]
        self._save_state()

    # ----- test hooks ---------------------------------------------------

    def set_price(self, instrument_uid: str, price: Decimal) -> None:
        spec = self._find_spec(instrument_uid)
        self._prices[spec.uid] = price
        self._save_state()

    def set_price_age(self, seconds: int) -> None:
        self._price_age_seconds = seconds
        self._save_state()

    def set_quantity(self, instrument_uid: str, quantity: Decimal) -> None:
        spec = self._find_spec(instrument_uid)
        if quantity < ZERO:
            raise TInvestValidationError("quantity cannot be negative")
        self._positions[spec.uid] = quantity
        self._save_state()

    def set_trading(
        self,
        instrument_uid: str,
        *,
        status: str = "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        buy_available: bool = True,
        sell_available: bool = True,
        api_trade_available: bool = True,
    ) -> None:
        spec = self._find_spec(instrument_uid)
        self._trading[spec.uid] = MockTradingState(
            status=status,
            buy_available=buy_available,
            sell_available=sell_available,
            api_trade_available=api_trade_available,
            limit_order_available=True,
            market_order_available=True,
        )
        self._save_state()

    def _require_account(self, account_id: str) -> None:
        known = {self._account.tinvest_account_id} | {
            account.tinvest_account_id for account in self._extra_accounts
        }
        if account_id not in known:
            raise TInvestNotFoundError(f"account {account_id} not found in mock")

    def quantity(self, instrument_uid: str) -> Decimal:
        return self._positions.get(instrument_uid, ZERO)

    def cash(self, currency: str = "RUB") -> Decimal:
        return self._cash.get(currency, ZERO)
