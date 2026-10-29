"""Broker-neutral domain types returned by TInvestClient implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.core.enums import AssetClass, OrderSide, OrderType


class BrokerOrderStatus(StrEnum):
    FILL = "FILL"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    NEW = "NEW"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AccountInfo:
    tinvest_account_id: str
    name: str
    account_type: str
    status: str
    access_level: str
    opened_date: datetime | None = None
    closed_date: datetime | None = None
    is_sandbox: bool = False


@dataclass(frozen=True)
class InstrumentInfo:
    instrument_uid: str
    figi: str | None
    ticker: str
    class_code: str
    isin: str | None
    name: str
    instrument_type: str
    asset_class: AssetClass
    currency: str
    lot: int
    exchange: str
    issuer_key: str
    sector: str | None = None
    country_of_risk: str | None = None
    maturity_date: date | None = None
    nominal: Decimal | None = None
    coupon_quantity_per_year: int | None = None
    income_yield: Decimal | None = None
    liquidity_flag: bool | None = None
    trading_status: str = ""
    buy_available: bool = False
    sell_available: bool = False
    api_trade_available: bool = False
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceInfo:
    instrument_uid: str
    figi: str | None
    ticker: str
    class_code: str
    price: Decimal
    at: datetime


@dataclass(frozen=True)
class TradingStatusInfo:
    instrument_uid: str
    figi: str | None
    ticker: str
    class_code: str
    trading_status: str
    limit_order_available: bool
    market_order_available: bool
    api_trade_available: bool
    bestprice_order_available: bool


@dataclass(frozen=True)
class PortfolioPositionInfo:
    instrument_uid: str
    figi: str | None
    ticker: str
    class_code: str
    instrument_type: str
    quantity: Decimal
    blocked_lots: Decimal
    average_price: Decimal
    average_price_fifo: Decimal | None
    current_price: Decimal
    current_nkd: Decimal
    expected_yield: Decimal
    currency: str
    current_price_at: datetime | None = None


@dataclass(frozen=True)
class PortfolioInfo:
    account_id: str
    positions: tuple[PortfolioPositionInfo, ...]
    total_amount_portfolio: Decimal
    currency: str
    daily_yield: Decimal | None
    received_at: datetime


@dataclass(frozen=True)
class CashInfo:
    currency: str
    amount: Decimal
    blocked: Decimal


@dataclass(frozen=True)
class PositionsInfo:
    account_id: str
    cash: tuple[CashInfo, ...]
    received_at: datetime


@dataclass(frozen=True)
class TradeInfo:
    trade_id: str | None
    quantity: int
    price: Decimal
    at: datetime


@dataclass(frozen=True)
class OperationInfo:
    operation_id: str
    parent_operation_id: str | None
    instrument_uid: str | None
    figi: str | None
    currency: str
    payment: Decimal
    price: Decimal
    state: str
    quantity: int
    quantity_rest: int
    occurred_at: datetime
    type_label: str
    operation_type: str
    instrument_type: str
    trades: tuple[TradeInfo, ...] = ()


@dataclass(frozen=True)
class CandleInfo:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    at: datetime
    is_complete: bool


@dataclass(frozen=True)
class OrderRequest:
    account_id: str
    instrument_id: str
    side: OrderSide
    lots: int
    order_type: OrderType
    client_order_id: str
    limit_price: Decimal | None = None
    time_in_force: str = "TIME_IN_FORCE_DAY"
    price_type: str = "PRICE_TYPE_CURRENCY"


@dataclass(frozen=True)
class OrderExecutionInfo:
    trade_id: str | None
    lots: int
    lot_size: int
    price: Decimal
    amount: Decimal
    commission: Decimal
    executed_at: datetime
    is_partial: bool


@dataclass(frozen=True)
class OrderResult:
    broker_order_id: str
    client_order_id: str
    status: BrokerOrderStatus
    raw_status: str
    lots_requested: int
    lots_executed: int
    initial_order_price: Decimal
    executed_order_price: Decimal
    total_order_amount: Decimal
    initial_commission: Decimal
    executed_commission: Decimal
    currency: str
    message: str = ""
    executions: tuple[OrderExecutionInfo, ...] = ()
    order_date: datetime | None = None
