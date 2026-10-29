"""The only interface business code may use to talk to T-Invest."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.integrations.tinvest.types import (
    AccountInfo,
    CandleInfo,
    CashInfo,
    InstrumentInfo,
    OperationInfo,
    OrderRequest,
    OrderResult,
    PortfolioInfo,
    PositionsInfo,
    PriceInfo,
    TradingStatusInfo,
)

IdType = Literal["uid", "figi", "ticker"]
CandleInterval = Literal["1min", "5min", "15min", "hour", "day", "week", "month"]
InstrumentKind = Literal["shares", "bonds", "etfs", "currencies", "futures"]


class TInvestClient(ABC):
    @property
    @abstractmethod
    def is_sandbox(self) -> bool: ...

    @property
    def is_mock(self) -> bool:
        return False

    @abstractmethod
    def get_accounts(self) -> list[AccountInfo]: ...

    @abstractmethod
    def get_portfolio(self, account_id: str) -> PortfolioInfo: ...

    @abstractmethod
    def get_positions(self, account_id: str) -> PositionsInfo: ...

    @abstractmethod
    def get_operations(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[OperationInfo]: ...

    @abstractmethod
    def get_instrument(
        self,
        instrument_id: str,
        id_type: IdType = "uid",
        class_code: str | None = None,
    ) -> InstrumentInfo: ...

    @abstractmethod
    def get_candles(
        self,
        instrument_id: str,
        date_from: datetime,
        date_to: datetime,
        interval: CandleInterval = "day",
    ) -> list[CandleInfo]: ...

    @abstractmethod
    def list_instruments(self, kind: InstrumentKind = "shares") -> list[InstrumentInfo]: ...

    @abstractmethod
    def get_last_prices(self, instrument_ids: list[str]) -> list[PriceInfo]: ...

    @abstractmethod
    def get_trading_statuses(self, instrument_ids: list[str]) -> list[TradingStatusInfo]: ...

    @abstractmethod
    def post_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def get_order_state(
        self,
        account_id: str,
        broker_order_id: str,
        lot_size: int = 1,
    ) -> OrderResult: ...

    @abstractmethod
    def get_orders(self, account_id: str) -> list[OrderResult]: ...

    @abstractmethod
    def cancel_order(self, account_id: str, broker_order_id: str) -> None: ...

    @abstractmethod
    def open_sandbox_account(self, name: str) -> str: ...

    @abstractmethod
    def sandbox_pay_in(self, account_id: str, currency: str, amount: Decimal) -> Decimal: ...

    @abstractmethod
    def close_sandbox_account(self, account_id: str) -> None: ...

    def close(self) -> None:
        return None

    def __enter__(self) -> TInvestClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def merge_cash(*groups: tuple[CashInfo, ...]) -> tuple[CashInfo, ...]:
    """Merge duplicate currency entries without losing Decimal precision."""

    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for group in groups:
        for item in group:
            amount, blocked = totals.get(item.currency, (Decimal("0"), Decimal("0")))
            totals[item.currency] = (amount + item.amount, blocked + item.blocked)
    return tuple(
        CashInfo(currency=currency, amount=amount, blocked=blocked)
        for currency, (amount, blocked) in sorted(totals.items())
    )
