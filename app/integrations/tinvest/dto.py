"""Pydantic v2 models for T-Invest REST JSON responses.

Field names follow the verified public contract. The REST gateway emits
lowerCamelCase; ``alias_generator=to_camel`` is used, while snake_case input is
also accepted for tests. Unknown fields are ignored so the official API can add
fields without breaking the app.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.core.money import quotation_to_decimal


def _int_from_api(value: object, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("expected an integer-compatible value") from exc


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


class QuotationDTO(ApiModel):
    units: int = 0
    nano: int = 0

    @field_validator("units", "nano", mode="before")
    @classmethod
    def _coerce_int(cls, value: object) -> int:
        return _int_from_api(value)

    def to_decimal(self) -> Decimal:
        return quotation_to_decimal(self.units, self.nano)


class MoneyValueDTO(ApiModel):
    currency: str = ""
    units: int = 0
    nano: int = 0

    @field_validator("units", "nano", mode="before")
    @classmethod
    def _coerce_int(cls, value: object) -> int:
        return _int_from_api(value)

    def to_decimal(self) -> Decimal:
        return quotation_to_decimal(self.units, self.nano)


class ErrorResponseDTO(ApiModel):
    code: int | str | None = None
    message: str = ""
    description: str | None = None

    @field_validator("message", "description", mode="before")
    @classmethod
    def _coerce_error_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value)


class AccountDTO(ApiModel):
    id: str
    type: str = ""
    name: str = ""
    status: str = ""
    opened_date: datetime | None = None
    closed_date: datetime | None = None
    access_level: str = ""


class GetAccountsResponseDTO(ApiModel):
    accounts: list[AccountDTO] = Field(default_factory=list)


class PortfolioPositionDTO(ApiModel):
    figi: str = ""
    instrument_uid: str = ""
    ticker: str = ""
    class_code: str = ""
    instrument_type: str = ""
    quantity: QuotationDTO = Field(default_factory=QuotationDTO)
    blocked_lots: QuotationDTO = Field(default_factory=QuotationDTO)
    blocked: bool = False
    average_position_price: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    average_position_price_fifo: MoneyValueDTO | None = None
    current_price: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    current_nkd: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    expected_yield: QuotationDTO = Field(default_factory=QuotationDTO)


class PortfolioResponseDTO(ApiModel):
    account_id: str = ""
    positions: list[PortfolioPositionDTO] = Field(default_factory=list)
    total_amount_portfolio: MoneyValueDTO | None = None
    daily_yield: MoneyValueDTO | None = None
    total_amount_shares: MoneyValueDTO | None = None
    total_amount_bonds: MoneyValueDTO | None = None
    total_amount_etf: MoneyValueDTO | None = None
    total_amount_currencies: MoneyValueDTO | None = None


class PositionsResponseDTO(ApiModel):
    account_id: str = ""
    money: list[MoneyValueDTO] = Field(default_factory=list)
    blocked: list[MoneyValueDTO] = Field(default_factory=list)


class OperationTradeDTO(ApiModel):
    trade_id: str = ""
    date_time: datetime | None = None
    quantity: int = 0
    price: MoneyValueDTO = Field(default_factory=MoneyValueDTO)

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce_quantity(cls, value: object) -> int:
        return _int_from_api(value)


class OperationDTO(ApiModel):
    id: str
    parent_operation_id: str = ""
    currency: str = "RUB"
    payment: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    price: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    state: str = ""
    quantity: int = 0
    quantity_rest: int = 0
    figi: str = ""
    instrument_type: str = ""
    date: datetime
    type: str = ""
    operation_type: str = ""
    instrument_uid: str = ""
    trades: list[OperationTradeDTO] = Field(default_factory=list)

    @field_validator("quantity", "quantity_rest", mode="before")
    @classmethod
    def _coerce_quantity(cls, value: object) -> int:
        return _int_from_api(value)


class GetOperationsResponseDTO(ApiModel):
    operations: list[OperationDTO] = Field(default_factory=list)


class LastPriceDTO(ApiModel):
    figi: str = ""
    instrument_uid: str = ""
    ticker: str = ""
    class_code: str = ""
    price: QuotationDTO = Field(default_factory=QuotationDTO)
    time: datetime | None = None
    last_price_type: str = ""


class GetLastPricesResponseDTO(ApiModel):
    last_prices: list[LastPriceDTO] = Field(default_factory=list)


class CandleDTO(ApiModel):
    open: QuotationDTO = Field(default_factory=QuotationDTO)
    high: QuotationDTO = Field(default_factory=QuotationDTO)
    low: QuotationDTO = Field(default_factory=QuotationDTO)
    close: QuotationDTO = Field(default_factory=QuotationDTO)
    volume: int = 0
    time: datetime
    is_complete: bool = True

    @field_validator("volume", mode="before")
    @classmethod
    def _coerce_volume(cls, value: object) -> int:
        return _int_from_api(value)


class GetCandlesResponseDTO(ApiModel):
    candles: list[CandleDTO] = Field(default_factory=list)


class TradingStatusDTO(ApiModel):
    figi: str = ""
    instrument_uid: str = ""
    ticker: str = ""
    class_code: str = ""
    trading_status: str = ""
    limit_order_available_flag: bool = False
    market_order_available_flag: bool = False
    api_trade_available_flag: bool = False
    bestprice_order_available_flag: bool = False


class GetTradingStatusesResponseDTO(ApiModel):
    trading_statuses: list[TradingStatusDTO] = Field(default_factory=list)


class InstrumentDTO(ApiModel):
    figi: str = ""
    ticker: str = ""
    class_code: str = ""
    isin: str = ""
    lot: int = 1
    currency: str = "RUB"
    name: str = ""
    exchange: str = ""
    country_of_risk: str = ""
    instrument_type: str = ""
    instrument_kind: str = ""
    trading_status: str = ""
    buy_available_flag: bool | None = None
    sell_available_flag: bool | None = None
    api_trade_available_flag: bool | None = None
    uid: str = ""
    position_uid: str = ""
    asset_uid: str = ""
    liquidity_flag: bool | None = None

    @field_validator("lot", mode="before")
    @classmethod
    def _coerce_lot(cls, value: object) -> int:
        return _int_from_api(value, default=1)


class UniverseInstrumentDTO(ApiModel):
    """Common fields of Shares/Bonds/Etfs/Currencies/Futures list items."""

    uid: str = ""
    figi: str = ""
    ticker: str = ""
    class_code: str = ""
    isin: str = ""
    lot: int = 1
    currency: str = "RUB"
    name: str = ""
    exchange: str = ""
    country_of_risk: str = ""
    sector: str | None = None
    asset_uid: str = ""
    trading_status: str = ""
    buy_available_flag: bool | None = None
    sell_available_flag: bool | None = None
    api_trade_available_flag: bool | None = None
    liquidity_flag: bool | None = None
    nominal: QuotationDTO | None = None
    coupon_quantity_per_year: int | None = None
    maturity_date: datetime | None = None

    @field_validator("lot", mode="before")
    @classmethod
    def _coerce_lot(cls, value: object) -> int:
        return _int_from_api(value, default=1)

    @field_validator("coupon_quantity_per_year", mode="before")
    @classmethod
    def _coerce_coupon_count(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        return _int_from_api(value)


class InstrumentsListResponseDTO(ApiModel):
    instruments: list[UniverseInstrumentDTO] = Field(default_factory=list)


class InstrumentResponseDTO(ApiModel):
    instrument: InstrumentDTO


class InstrumentDetailsDTO(ApiModel):
    """Union of verified fields from BondBy/ShareBy/EtfBy/CurrencyBy responses."""

    figi: str = ""
    ticker: str = ""
    class_code: str = ""
    isin: str = ""
    lot: int = 1
    currency: str = "RUB"
    name: str = ""
    exchange: str = ""
    uid: str = ""
    asset_uid: str = ""
    sector: str | None = None
    country_of_risk: str = ""
    maturity_date: datetime | None = None
    nominal: MoneyValueDTO | None = None
    coupon_quantity_per_year: int | None = None
    coupon_interest_rate: QuotationDTO | None = None
    liquidity_flag: bool | None = None
    div_yield_flag: bool | None = None
    focus_type: str | None = None
    trading_status: str = ""
    buy_available_flag: bool | None = None
    sell_available_flag: bool | None = None
    api_trade_available_flag: bool | None = None

    @field_validator("lot", mode="before")
    @classmethod
    def _coerce_lot(cls, value: object) -> int:
        return _int_from_api(value, default=1)

    @field_validator("coupon_quantity_per_year", mode="before")
    @classmethod
    def _coerce_coupon_count(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        return _int_from_api(value)


class InstrumentDetailsResponseDTO(ApiModel):
    instrument: InstrumentDetailsDTO


class OrderStageDTO(ApiModel):
    price: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    quantity: int = 0
    trade_id: str = ""
    execution_time: datetime | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce_quantity(cls, value: object) -> int:
        return _int_from_api(value)


class PostOrderResponseDTO(ApiModel):
    order_id: str = ""
    execution_report_status: str = ""
    lots_requested: int = 0
    lots_executed: int = 0
    initial_order_price: MoneyValueDTO | None = None
    executed_order_price: MoneyValueDTO | None = None
    total_order_amount: MoneyValueDTO | None = None
    initial_commission: MoneyValueDTO | None = None
    executed_commission: MoneyValueDTO | None = None
    figi: str = ""
    direction: str = ""
    initial_security_price: MoneyValueDTO | None = None
    order_type: str = ""
    message: str = ""
    instrument_uid: str = ""
    ticker: str = ""
    class_code: str = ""
    order_request_id: str = ""

    @field_validator("lots_requested", "lots_executed", mode="before")
    @classmethod
    def _coerce_lots(cls, value: object) -> int:
        return _int_from_api(value)


class OrderStateDTO(PostOrderResponseDTO):
    average_position_price: MoneyValueDTO | None = None
    currency: str = ""
    order_date: datetime | None = None
    stages: list[OrderStageDTO] = Field(default_factory=list)


class GetOrdersResponseDTO(ApiModel):
    orders: list[OrderStateDTO] = Field(default_factory=list)


class OpenSandboxAccountResponseDTO(ApiModel):
    account_id: str = ""


class SandboxPayInResponseDTO(ApiModel):
    balance: MoneyValueDTO | None = None
