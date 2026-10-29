"""Portfolio valuation, allocation, concentration and snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import AssetClass
from app.core.money import quantize_money, quantize_percent
from app.core.time import ensure_utc, is_stale
from app.models.account import Account
from app.models.position import CashBalance, FxRate, Position
from app.models.snapshot import PortfolioSnapshot
from app.services.valuation import FxTable, position_value
from app.services.valuation_errors import MissingFxRateError

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def _as_decimal(value: object) -> Decimal:
    assert isinstance(value, Decimal)
    return value


@dataclass(frozen=True)
class PositionView:
    position_id: int
    instrument_id: int
    instrument_uid: str
    figi: str | None
    ticker: str
    name: str
    asset_class: AssetClass
    currency: str
    quantity: Decimal
    lot: int
    nominal: Decimal | None
    average_price: Decimal
    current_price: Decimal
    current_price_at: datetime | None
    current_price_per_lot: Decimal
    current_price_is_percent: bool
    current_nkd: Decimal
    value_base: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    realized_pnl_incomplete: bool
    commission_paid: Decimal
    dividends_received: Decimal
    coupons_received: Decimal
    weight_percent: Decimal
    is_stale: bool


@dataclass(frozen=True)
class CashView:
    currency: str
    amount: Decimal
    blocked: Decimal
    free_amount: Decimal
    value_base: Decimal
    free_value_base: Decimal
    weight_percent: Decimal


@dataclass(frozen=True)
class AllocationRow:
    asset_class: AssetClass
    value_base: Decimal
    weight_percent: Decimal


@dataclass(frozen=True)
class IssuerConcentration:
    issuer_key: str
    value_base: Decimal
    weight_percent: Decimal


@dataclass(frozen=True)
class PortfolioView:
    account_id: int
    tinvest_account_id: str
    base_currency: str
    as_of: datetime
    total_value: Decimal
    invested_value: Decimal
    cash_value: Decimal
    free_cash_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    realized_pnl_incomplete: bool
    commission_paid: Decimal
    dividends_received: Decimal
    coupons_received: Decimal
    positions: tuple[PositionView, ...]
    cash: tuple[CashView, ...]
    allocation: tuple[AllocationRow, ...]
    issuer_concentration: tuple[IssuerConcentration, ...]
    warnings: tuple[str, ...] = ()


class PortfolioService:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()

    def build(self, session: Session, account_id: int) -> PortfolioView:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"account {account_id} not found")
        now = ensure_utc(self._clock.now())
        base = self._settings.base_currency
        fx = self._load_fx(session, base)
        warnings: list[str] = []

        positions = list(
            session.scalars(
                select(Position)
                .options(selectinload(Position.instrument))
                .where(Position.account_id == account.id)
                .order_by(Position.id)
            )
        )
        cash_rows = list(
            session.scalars(
                select(CashBalance)
                .where(CashBalance.account_id == account.id)
                .order_by(CashBalance.currency)
            )
        )

        raw_positions: list[dict[str, object]] = []
        invested = ZERO
        unrealized = ZERO
        realized = ZERO
        realized_incomplete = False
        commissions = ZERO
        dividends = ZERO
        coupons = ZERO
        class_values: dict[AssetClass, Decimal] = {}
        issuer_values: dict[str, Decimal] = {}
        base_cash_amount = ZERO
        base_cash_value = ZERO

        for position in positions:
            if position.quantity <= ZERO:
                continue
            instrument = position.instrument
            price = position.current_price
            if price <= ZERO and instrument.last_price is not None:
                price = instrument.last_price
            if (
                instrument.asset_class is AssetClass.CURRENCY
                and instrument.currency.upper() == base
            ):
                base_cash_amount += position.quantity
                base_cash_value += position_value(
                    instrument.asset_class,
                    price if price > ZERO else Decimal("1"),
                    position.quantity,
                    nominal=instrument.nominal,
                )
                continue
            price_at = position.current_price_at or instrument.last_price_at
            stale = is_stale(
                price_at,
                now,
                self._settings.quote_max_age_seconds,
            )
            if price <= ZERO:
                warnings.append(f"{instrument.ticker}: нет текущей цены")
            if stale:
                warnings.append(f"{instrument.ticker}: котировка устарела или не имеет даты")

            value = position_value(
                instrument.asset_class,
                price,
                position.quantity,
                nominal=instrument.nominal,
                nkd=position.current_nkd,
            )
            invested += value
            unrealized += position.unrealized_pnl
            realized += position.realized_pnl
            realized_incomplete = realized_incomplete or position.realized_pnl_incomplete
            commissions += position.commission_paid
            dividends += position.dividends_received
            coupons += position.coupons_received
            class_values[instrument.asset_class] = (
                class_values.get(instrument.asset_class, ZERO) + value
            )
            issuer = instrument.issuer_key or instrument.ticker.upper()
            issuer_values[issuer] = issuer_values.get(issuer, ZERO) + value
            raw_positions.append(
                {
                    "position": position,
                    "price": price,
                    "price_at": price_at,
                    "value": value,
                    "stale": stale,
                }
            )

        raw_cash: list[dict[str, object]] = []
        cash_value = ZERO
        free_cash_value = ZERO
        for balance in cash_rows:
            available = balance.amount - balance.blocked
            if available < ZERO:
                available = ZERO
            try:
                value = fx.convert(balance.amount, balance.currency)
                free_value = fx.convert(available, balance.currency)
            except MissingFxRateError:
                warnings.append(
                    f"{balance.currency}: нет курса к {base}; денежные средства исключены"
                )
                value = ZERO
                free_value = ZERO
            cash_value += value
            free_cash_value += free_value
            class_values[AssetClass.CASH] = class_values.get(AssetClass.CASH, ZERO) + value
            raw_cash.append(
                {
                    "currency": balance.currency.upper(),
                    "amount": balance.amount,
                    "blocked": balance.blocked,
                    "available": available,
                    "value": value,
                    "free_value": free_value,
                }
            )

        if base_cash_value > ZERO:
            existing_base = next(
                (row for row in raw_cash if row["currency"] == base),
                None,
            )
            if existing_base is None:
                cash_value += base_cash_value
                free_cash_value += base_cash_value
                class_values[AssetClass.CASH] = (
                    class_values.get(AssetClass.CASH, ZERO) + base_cash_value
                )
                raw_cash.append(
                    {
                        "currency": base,
                        "amount": base_cash_amount,
                        "blocked": ZERO,
                        "available": base_cash_amount,
                        "value": base_cash_value,
                        "free_value": base_cash_value,
                    }
                )
            elif base_cash_amount > _as_decimal(existing_base["amount"]):
                delta_value = base_cash_value - _as_decimal(existing_base["value"])
                cash_value += delta_value
                free_cash_value += delta_value
                class_values[AssetClass.CASH] = (
                    class_values.get(AssetClass.CASH, ZERO) + delta_value
                )
                existing_base["amount"] = base_cash_amount
                existing_base["available"] = base_cash_amount
                existing_base["value"] = base_cash_value
                existing_base["free_value"] = base_cash_value

        total = invested + cash_value
        if total <= ZERO:
            warnings.append("стоимость портфеля равна нулю")

        position_views = tuple(self._position_view(item, total, base) for item in raw_positions)
        cash_views = tuple(self._cash_view(item, total, base) for item in raw_cash)
        allocation = tuple(
            AllocationRow(
                asset_class=asset_class,
                value_base=quantize_money(class_values.get(asset_class, ZERO)),
                weight_percent=quantize_percent(
                    self._weight(class_values.get(asset_class, ZERO), total)
                ),
            )
            for asset_class in AssetClass
            if class_values.get(asset_class, ZERO) > ZERO
        )
        concentration = tuple(
            IssuerConcentration(
                issuer_key=issuer,
                value_base=quantize_money(value),
                weight_percent=quantize_percent(self._weight(value, total)),
            )
            for issuer, value in sorted(
                issuer_values.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            if value > ZERO
        )

        return PortfolioView(
            account_id=account.id,
            tinvest_account_id=account.tinvest_account_id,
            base_currency=base,
            as_of=now,
            total_value=quantize_money(total),
            invested_value=quantize_money(invested),
            cash_value=quantize_money(cash_value),
            free_cash_value=quantize_money(free_cash_value),
            unrealized_pnl=quantize_money(unrealized),
            realized_pnl=quantize_money(realized),
            realized_pnl_incomplete=realized_incomplete,
            commission_paid=quantize_money(commissions),
            dividends_received=quantize_money(dividends),
            coupons_received=quantize_money(coupons),
            positions=position_views,
            cash=cash_views,
            allocation=allocation,
            issuer_concentration=concentration,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def capture_snapshot(
        self,
        session: Session,
        account_id: int,
        *,
        source: str = "manual",
        view: PortfolioView | None = None,
    ) -> PortfolioSnapshot:
        portfolio = view or self.build(session, account_id)
        snapshot = PortfolioSnapshot(
            account_id=portfolio.account_id,
            captured_at=portfolio.as_of,
            base_currency=portfolio.base_currency,
            total_value=portfolio.total_value,
            invested_value=portfolio.invested_value,
            cash_value=portfolio.cash_value,
            unrealized_pnl=portfolio.unrealized_pnl,
            realized_pnl=portfolio.realized_pnl,
            daily_pnl=None,
            allocation_json={
                row.asset_class.value: {
                    "value": str(row.value_base),
                    "weight": str(row.weight_percent),
                }
                for row in portfolio.allocation
            },
            weights_json={row.ticker: str(row.weight_percent) for row in portfolio.positions},
            concentration_json={
                row.issuer_key: str(row.weight_percent) for row in portfolio.issuer_concentration
            },
            warnings=list(portfolio.warnings),
            payload={
                "positions": [
                    {
                        "ticker": row.ticker,
                        "quantity": str(row.quantity),
                        "price": str(row.current_price),
                        "value": str(row.value_base),
                    }
                    for row in portfolio.positions
                ],
                "cash": [
                    {
                        "currency": row.currency,
                        "amount": str(row.amount),
                        "value": str(row.value_base),
                    }
                    for row in portfolio.cash
                ],
            },
            source=source,
        )
        session.add(snapshot)
        session.flush()
        return snapshot

    def _load_fx(self, session: Session, base: str) -> FxTable:
        rows = list(
            session.scalars(
                select(FxRate).where(
                    FxRate.base_currency == base,
                )
            )
        )
        rows += list(
            session.scalars(
                select(FxRate).where(
                    FxRate.quote_currency == base,
                )
            )
        )
        return FxTable.from_rows(
            base,
            [(row.base_currency, row.quote_currency, row.rate) for row in rows],
        )

    def _position_view(
        self,
        item: dict[str, object],
        total: Decimal,
        base: str,
    ) -> PositionView:
        position = item["position"]
        assert isinstance(position, Position)
        instrument = position.instrument
        value = item["value"]
        assert isinstance(value, Decimal)
        price = item["price"]
        assert isinstance(price, Decimal)
        price_at = item["price_at"]
        stale = item["stale"]
        assert isinstance(stale, bool)
        price_is_percent = instrument.asset_class is AssetClass.BOND
        price_per_lot = (
            position_value(
                instrument.asset_class,
                price,
                ONE,
                nominal=instrument.nominal,
                nkd=ZERO,
            )
            * max(1, instrument.lot)
            if price_is_percent
            else ZERO
        )
        return PositionView(
            position_id=position.id,
            instrument_id=instrument.id,
            instrument_uid=instrument.instrument_uid,
            figi=instrument.figi,
            ticker=instrument.ticker,
            name=instrument.name,
            asset_class=instrument.asset_class,
            currency=instrument.currency,
            quantity=position.quantity,
            lot=instrument.lot,
            nominal=instrument.nominal,
            average_price=position.average_price,
            current_price=price,
            current_price_at=price_at if isinstance(price_at, datetime) else None,
            current_price_per_lot=price_per_lot,
            current_price_is_percent=price_is_percent,
            current_nkd=position.current_nkd,
            value_base=quantize_money(value),
            unrealized_pnl=quantize_money(position.unrealized_pnl),
            realized_pnl=quantize_money(position.realized_pnl),
            realized_pnl_incomplete=position.realized_pnl_incomplete,
            commission_paid=quantize_money(position.commission_paid),
            dividends_received=quantize_money(position.dividends_received),
            coupons_received=quantize_money(position.coupons_received),
            weight_percent=quantize_percent(self._weight(value, total)),
            is_stale=stale,
        )

    def _cash_view(
        self,
        item: dict[str, object],
        total: Decimal,
        base: str,
    ) -> CashView:
        currency = item["currency"]
        amount = item["amount"]
        blocked = item["blocked"]
        value = item["value"]
        free_value = item["free_value"]
        available = item["available"]
        assert isinstance(currency, str)
        assert isinstance(amount, Decimal)
        assert isinstance(blocked, Decimal)
        assert isinstance(value, Decimal)
        assert isinstance(free_value, Decimal)
        assert isinstance(available, Decimal)
        return CashView(
            currency=currency,
            amount=amount,
            blocked=blocked,
            free_amount=available,
            value_base=quantize_money(value),
            free_value_base=quantize_money(free_value),
            weight_percent=quantize_percent(self._weight(value, total)),
        )

    @staticmethod
    def _weight(value: Decimal, total: Decimal) -> Decimal:
        if total <= ZERO:
            return ZERO
        return value / total * HUNDRED
