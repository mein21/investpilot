"""Read-only T-Invest adapter used by the screener.

The adapter intentionally exposes only market-data and reference-data methods.
Mutating order methods inherited from the portfolio client are disabled.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.core.time import ensure_utc
from app.integrations.tinvest.dto import ApiModel, MoneyValueDTO, QuotationDTO
from app.integrations.tinvest.exceptions import TInvestValidationError
from app.integrations.tinvest.rest import TInvestRestClient
from app.screener.domain import (
    Bar,
    Dividend,
    FundamentalSnapshot,
    OrderBookQuote,
    ShareRecord,
)

MAX_DAILY_BARS = 2400
FUNDAMENTAL_CHUNK = 100


class ShareDTO(ApiModel):
    uid: str
    figi: str = ""
    ticker: str = ""
    class_code: str = ""
    isin: str = ""
    lot: int = 1
    currency: str = "RUB"
    name: str = ""
    exchange: str = ""
    real_exchange: str = ""
    sector: str = ""
    share_type: str = ""
    asset_uid: str = ""
    first1day_candle_date: datetime | None = None
    trading_status: str = ""
    buy_available_flag: bool = False
    api_trade_available_flag: bool = False
    liquidity_flag: bool = False
    for_qual_investor_flag: bool = False
    otc_flag: bool = False
    weekend_flag: bool = False

    @field_validator("lot", mode="before")
    @classmethod
    def _lot(cls, value: object) -> int:
        try:
            return max(1, int(str(value)))
        except (TypeError, ValueError):
            return 1


class SharesResponseDTO(ApiModel):
    instruments: list[ShareDTO] = Field(default_factory=list)


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
    def _volume(cls, value: object) -> int:
        try:
            return max(0, int(str(value)))
        except (TypeError, ValueError):
            return 0


class CandlesResponseDTO(ApiModel):
    candles: list[CandleDTO] = Field(default_factory=list)


class OrderDTO(ApiModel):
    price: QuotationDTO = Field(default_factory=QuotationDTO)
    quantity: int = 0


class OrderBookDTO(ApiModel):
    figi: str = ""
    instrument_uid: str = ""
    bids: list[OrderDTO] = Field(default_factory=list)
    asks: list[OrderDTO] = Field(default_factory=list)
    last_price: QuotationDTO = Field(default_factory=QuotationDTO)
    close_price: QuotationDTO = Field(default_factory=QuotationDTO)
    time: datetime | None = None


class OrderBookResponseDTO(ApiModel):
    orderbook: OrderBookDTO = Field(default_factory=OrderBookDTO)


class DividendDTO(ApiModel):
    dividend_net: MoneyValueDTO = Field(default_factory=MoneyValueDTO)
    payment_date: datetime | None = None
    declared_date: datetime | None = None
    last_buy_date: datetime | None = None
    dividend_type: str = ""
    record_date: datetime | None = None
    regularity: str = ""


class DividendsResponseDTO(ApiModel):
    dividends: list[DividendDTO] = Field(default_factory=list)


class IndicativeDTO(ApiModel):
    uid: str
    figi: str = ""
    ticker: str = ""
    class_code: str = ""
    isin: str = ""
    name: str = ""
    exchange: str = ""
    currency: str = "RUB"
    instrument_type: str = ""


class IndicativesResponseDTO(ApiModel):
    instruments: list[IndicativeDTO] = Field(default_factory=list)


class AssetFundamentalDTO(ApiModel):
    asset_uid: str = ""
    currency: str = "RUB"
    market_capitalization: float | None = None
    high_price_last_52_weeks: float | None = None
    low_price_last_52_weeks: float | None = None
    average_daily_volume_last_10_days: float | None = None
    average_daily_volume_last_4_weeks: float | None = None
    beta: float | None = None
    free_float: float | None = None
    forward_annual_dividend_yield: float | None = None
    shares_outstanding: float | None = None
    revenue_ttm: float | None = None
    ebitda_ttm: float | None = None
    net_income_ttm: float | None = None
    eps_ttm: float | None = None
    diluted_eps_ttm: float | None = None
    free_cash_flow_ttm: float | None = None
    five_year_annual_revenue_growth_rate: float | None = None
    three_year_annual_revenue_growth_rate: float | None = None
    one_year_annual_revenue_growth_rate: float | None = None
    pe_ratio_ttm: float | None = None
    price_to_sales_ttm: float | None = None
    price_to_book_ttm: float | None = None
    price_to_free_cash_flow_ttm: float | None = None
    total_enterprise_value_mrq: float | None = None
    ev_to_ebitda_mrq: float | None = None
    net_margin_mrq: float | None = None
    net_interest_margin_mrq: float | None = None
    roe: float | None = None
    roa: float | None = None
    roic: float | None = None
    total_debt_mrq: float | None = None
    total_debt_to_equity_mrq: float | None = None
    total_debt_to_ebitda_mrq: float | None = None
    free_cash_flow_to_price: float | None = None
    net_debt_to_ebitda: float | None = None
    current_ratio_mrq: float | None = None
    fixed_charge_coverage_ratio_fy: float | None = None
    dividend_yield_daily_ttm: float | None = None
    dividend_rate_ttm: float | None = None
    dividends_per_share: float | None = None
    five_years_average_dividend_yield: float | None = None
    five_year_annual_dividend_growth_rate: float | None = None
    dividend_payout_ratio_fy: float | None = None
    buy_back_ttm: float | None = None
    number_of_employees: float | None = None
    ex_dividend_date: datetime | None = None
    fiscal_period_end_date: datetime | None = None


class FundamentalsResponseDTO(ApiModel):
    fundamentals: list[AssetFundamentalDTO] = Field(default_factory=list)


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result == 0:
        return None
    return result


def _date(value: datetime | None) -> date | None:
    if value is None:
        return None
    return ensure_utc(value).date()


class ScreenerTInvestClient(TInvestRestClient):
    """Thin read-only wrapper with gentle service-specific pacing."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        verify: str | bool = True,
        proxy: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        app_name: str = "InvestPilotScreener",
        marketdata_min_interval: float = 0.12,
        instruments_min_interval: float = 0.32,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            app_name=f"{app_name}/0.1",
            sandbox=False,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            proxy=proxy,
            verify=verify,
        )
        self._marketdata_min_interval = max(0.0, marketdata_min_interval)
        self._instruments_min_interval = max(0.0, instruments_min_interval)
        self._last_call: dict[str, float] = {}

    def _rate_limit(self, service_method: str) -> None:
        if service_method.startswith("MarketDataService/"):
            interval = self._marketdata_min_interval
            bucket = "marketdata"
        elif service_method.startswith("InstrumentsService/"):
            interval = self._instruments_min_interval
            bucket = "instruments"
        else:
            interval = 0.05
            bucket = "other"
        now = time.monotonic()
        wait = interval - (now - self._last_call.get(bucket, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_call[bucket] = time.monotonic()

    def _read(self, service_method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._rate_limit(service_method)
        return super()._read(service_method, payload)

    # ----- read-only enforcement ---------------------------------------

    def post_order(self, *args: Any, **kwargs: Any) -> Any:
        raise TInvestValidationError("screener client is read-only")

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        raise TInvestValidationError("screener client is read-only")

    def open_sandbox_account(self, *args: Any, **kwargs: Any) -> Any:
        raise TInvestValidationError("screener client is read-only")

    def sandbox_pay_in(self, *args: Any, **kwargs: Any) -> Any:
        raise TInvestValidationError("screener client is read-only")

    def close_sandbox_account(self, *args: Any, **kwargs: Any) -> Any:
        raise TInvestValidationError("screener client is read-only")

    # ----- reference data ----------------------------------------------

    def list_shares(self, status: str = "INSTRUMENT_STATUS_ALL") -> list[ShareRecord]:
        dto = self._validated(
            SharesResponseDTO,
            self._read("InstrumentsService/Shares", {"instrumentStatus": status}),
        )
        active = status != "INSTRUMENT_STATUS_ALL"
        records: list[ShareRecord] = []
        for item in dto.instruments:
            records.append(
                ShareRecord(
                    uid=item.uid,
                    ticker=item.ticker,
                    class_code=item.class_code,
                    name=item.name,
                    lot=item.lot,
                    figi=item.figi or None,
                    isin=item.isin or None,
                    currency=item.currency.upper(),
                    exchange=item.exchange,
                    real_exchange=item.real_exchange,
                    sector=item.sector,
                    share_type=item.share_type,
                    asset_uid=item.asset_uid,
                    first_candle_date=_date(item.first1day_candle_date),
                    trading_status=item.trading_status,
                    buy_available=item.buy_available_flag,
                    api_trade_available=item.api_trade_available_flag,
                    liquidity_flag=item.liquidity_flag,
                    for_qual_investor=item.for_qual_investor_flag,
                    is_active=active,
                    otc=item.otc_flag,
                    weekend=item.weekend_flag,
                )
            )
        return records

    def list_indices(self) -> list[dict[str, str]]:
        dto = self._validated(
            IndicativesResponseDTO,
            self._read("InstrumentsService/Indicatives", {}),
        )
        return [
            {
                "uid": item.uid,
                "ticker": item.ticker,
                "name": item.name,
                "class_code": item.class_code,
                "currency": item.currency,
            }
            for item in dto.instruments
        ]

    def daily_candles(
        self,
        instrument_uid: str,
        date_from: date,
        date_to: date,
        *,
        lot: int = 1,
    ) -> list[Bar]:
        dto = self._validated(
            CandlesResponseDTO,
            self._read(
                "MarketDataService/GetCandles",
                {
                    "instrumentId": instrument_uid,
                    "from": _iso(date_from),
                    "to": _iso(date_to),
                    "interval": "CANDLE_INTERVAL_DAY",
                    "limit": MAX_DAILY_BARS,
                },
            ),
        )
        bars = [
            Bar(
                trade_date=ensure_utc(item.time).date(),
                open=item.open.to_decimal(),
                high=item.high.to_decimal(),
                low=item.low.to_decimal(),
                close=item.close.to_decimal(),
                volume_lots=item.volume,
                lot=max(1, lot),
                is_complete=item.is_complete,
            )
            for item in dto.candles
        ]
        return bars

    def orderbook(self, instrument_uid: str, depth: int = 10) -> OrderBookQuote:
        dto = self._validated(
            OrderBookResponseDTO,
            self._read(
                "MarketDataService/GetOrderBook",
                {
                    "instrumentId": instrument_uid,
                    "depth": depth,
                },
            ),
        )
        book = dto.orderbook
        bids = [order.price.to_decimal() for order in book.bids if order.quantity > 0]
        asks = [order.price.to_decimal() for order in book.asks if order.quantity > 0]
        bid = max(bids) if bids else None
        ask = min(asks) if asks else None
        spread: Decimal | None = None
        if bid is not None and ask is not None and ask >= bid and bid > 0:
            spread = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
        return OrderBookQuote(
            uid=instrument_uid,
            bid=bid,
            ask=ask,
            spread_pct=spread,
            fetched_at=datetime.now(UTC),
            depth=depth,
        )

    def dividends(
        self,
        instrument_uid: str,
        date_from: date,
        date_to: date,
    ) -> list[Dividend]:
        dto = self._validated(
            DividendsResponseDTO,
            self._read(
                "InstrumentsService/GetDividends",
                {
                    "instrumentId": instrument_uid,
                    "from": _iso(date_from),
                    "to": _iso(date_to),
                },
            ),
        )
        result: list[Dividend] = []
        for item in dto.dividends:
            amount = item.dividend_net.to_decimal()
            if amount <= 0:
                continue
            result.append(
                Dividend(
                    uid=instrument_uid,
                    amount=amount,
                    currency=item.dividend_net.currency.upper() or "RUB",
                    record_date=_date(item.record_date),
                    last_buy_date=_date(item.last_buy_date),
                    payment_date=_date(item.payment_date),
                    declared_date=_date(item.declared_date),
                    dividend_type=item.dividend_type,
                    regularity=item.regularity,
                )
            )
        return result

    def fundamentals(self, asset_uids: Sequence[str]) -> list[FundamentalSnapshot]:
        unique = list(dict.fromkeys(uid for uid in asset_uids if uid))
        result: list[FundamentalSnapshot] = []
        for index in range(0, len(unique), FUNDAMENTAL_CHUNK):
            chunk = unique[index : index + FUNDAMENTAL_CHUNK]
            dto = self._validated(
                FundamentalsResponseDTO,
                self._read("InstrumentsService/GetAssetFundamentals", {"assets": chunk}),
            )
            now = datetime.now(UTC)
            for item in dto.fundamentals:
                values = {
                    key: _decimal(getattr(item, key))
                    for key in (
                        "market_capitalization",
                        "high_price_last_52_weeks",
                        "low_price_last_52_weeks",
                        "average_daily_volume_last_10_days",
                        "average_daily_volume_last_4_weeks",
                        "free_float",
                        "shares_outstanding",
                        "beta",
                        "forward_annual_dividend_yield",
                        "revenue_ttm",
                        "ebitda_ttm",
                        "net_income_ttm",
                        "eps_ttm",
                        "diluted_eps_ttm",
                        "free_cash_flow_ttm",
                        "one_year_annual_revenue_growth_rate",
                        "three_year_annual_revenue_growth_rate",
                        "five_year_annual_revenue_growth_rate",
                        "pe_ratio_ttm",
                        "price_to_sales_ttm",
                        "price_to_book_ttm",
                        "price_to_free_cash_flow_ttm",
                        "ev_to_ebitda_mrq",
                        "net_margin_mrq",
                        "net_interest_margin_mrq",
                        "roe",
                        "roa",
                        "roic",
                        "total_debt_mrq",
                        "total_debt_to_equity_mrq",
                        "total_debt_to_ebitda_mrq",
                        "free_cash_flow_to_price",
                        "net_debt_to_ebitda",
                        "current_ratio_mrq",
                        "fixed_charge_coverage_ratio_fy",
                        "dividend_yield_daily_ttm",
                        "dividend_rate_ttm",
                        "dividends_per_share",
                        "five_years_average_dividend_yield",
                        "five_year_annual_dividend_growth_rate",
                        "dividend_payout_ratio_fy",
                        "buy_back_ttm",
                    )
                }
                result.append(
                    FundamentalSnapshot(
                        asset_uid=item.asset_uid,
                        fetched_at=now,
                        as_of=_date(item.fiscal_period_end_date) or now.date(),
                        currency=item.currency.upper() or "RUB",
                        values=values,
                        raw={},
                    )
                )
        return result


def _iso(value: date) -> str:
    return f"{value.isoformat()}T00:00:00Z"
