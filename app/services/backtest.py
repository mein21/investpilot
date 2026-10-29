"""Deterministic buy-and-hold backtest over T-Invest candle history.

The backtest values today's positions and cash with historical daily closes.
It does not model rebalancing, dividends or FX changes, so it is a reference
estimate rather than a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.money import quantize_money, quantize_percent
from app.core.time import ensure_utc
from app.integrations.tinvest.client import TInvestClient
from app.integrations.tinvest.exceptions import TInvestError
from app.services.portfolio import PortfolioView
from app.services.valuation import instrument_unit_value

ZERO = Decimal("0")
HUNDRED = Decimal("100")
MIN_DAYS = 7
MAX_DAYS = 730


@dataclass(frozen=True)
class BacktestPoint:
    at: datetime
    value: Decimal


@dataclass(frozen=True)
class BacktestResult:
    days: int
    start_at: datetime | None
    end_at: datetime | None
    start_value: Decimal
    end_value: Decimal
    total_return_percent: Decimal
    max_drawdown_percent: Decimal
    points: tuple[BacktestPoint, ...]
    warnings: tuple[str, ...]


class BacktestService:
    def __init__(
        self,
        client: TInvestClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock or SystemClock()

    def run(self, portfolio: PortfolioView, *, days: int = 365) -> BacktestResult:
        days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
        now = ensure_utc(self._clock.now())
        start = now - timedelta(days=days)
        warnings: list[str] = []
        series: dict[int, dict[date, Decimal]] = {}
        for position in portfolio.positions:
            if position.quantity <= ZERO:
                continue
            try:
                candles = self._client.get_candles(
                    position.instrument_uid,
                    start,
                    now,
                    "day",
                )
            except TInvestError as exc:
                warnings.append(f"{position.ticker}: история недоступна ({exc})")
                continue
            closes = {
                ensure_utc(candle.at).date(): candle.close
                for candle in candles
                if candle.close > ZERO
            }
            if not closes:
                warnings.append(f"{position.ticker}: нет исторических свечей")
                continue
            series[position.instrument_id] = closes

        if not series:
            return BacktestResult(
                days=days,
                start_at=None,
                end_at=None,
                start_value=portfolio.total_value,
                end_value=portfolio.total_value,
                total_return_percent=ZERO,
                max_drawdown_percent=ZERO,
                points=(),
                warnings=tuple([*warnings, "нет исторических данных для backtest"]),
            )

        first_day = max(min(closes) for closes in series.values())
        all_days = sorted({day for closes in series.values() for day in closes if day >= first_day})
        if not all_days:
            all_days = [first_day]
        cash = portfolio.cash_value
        points: list[BacktestPoint] = []
        last_close: dict[int, Decimal] = {}
        for day in all_days:
            for position in portfolio.positions:
                instrument_closes = series.get(position.instrument_id)
                if instrument_closes and day in instrument_closes:
                    last_close[position.instrument_id] = instrument_closes[day]
            value = cash
            for position in portfolio.positions:
                close = last_close.get(position.instrument_id)
                if close is None:
                    continue
                unit = instrument_unit_value(
                    position.asset_class,
                    close,
                    nominal=position.nominal,
                )
                value += unit * position.quantity
            at = datetime.combine(day, now.timetz())
            points.append(BacktestPoint(at=at, value=quantize_money(value)))

        start_value = points[0].value
        end_value = points[-1].value
        total_return = (
            (end_value - start_value) / start_value * HUNDRED if start_value > ZERO else ZERO
        )
        peak = start_value
        max_drawdown = ZERO
        for point in points:
            if point.value > peak:
                peak = point.value
            if peak > ZERO:
                drawdown = (peak - point.value) / peak * HUNDRED
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        return BacktestResult(
            days=days,
            start_at=points[0].at,
            end_at=points[-1].at,
            start_value=start_value,
            end_value=end_value,
            total_return_percent=quantize_percent(total_return),
            max_drawdown_percent=quantize_percent(max_drawdown),
            points=tuple(points),
            warnings=tuple(warnings),
        )
