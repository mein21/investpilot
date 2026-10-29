"""Deterministic growth analysis from daily candles.

The result ranks instruments by historical momentum, trend quality and
drawdown stability. It is not a price forecast and carries no investment
guarantee. Same candles always produce the same numbers; no AI/ML is used.

Momentum blends absolute return with a risk-adjusted ratio, so a steep but
erratic move is tempered by volatility while low-return money-market funds do
not automatically outrank high-growth instruments. Missing history yields a
neutral score instead of a fake one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from app.core.money import clamp, quantize_money, quantize_percent, quantize_price
from app.core.time import ensure_utc
from app.integrations.tinvest.types import CandleInfo

ZERO = Decimal("0")
FIFTY = Decimal("50")
HUNDRED = Decimal("100")
MIN_HISTORY_CANDLES = 63
DAILY_WINDOW = 252
SIX_MONTH_WINDOW = 126
TRADING_DAYS_PER_YEAR = Decimal("252")
MOMENTUM_WINDOWS: tuple[tuple[int, str, Decimal], ...] = (
    (21, "1m", Decimal("0.35")),
    (63, "3m", Decimal("0.35")),
    (126, "6m", Decimal("0.20")),
    (252, "12m", Decimal("0.10")),
)


@dataclass(frozen=True)
class TrendMetrics:
    candle_count: int
    window_start_at: datetime
    window_end_at: datetime
    last_close: Decimal
    return_1m_percent: Decimal | None
    return_3m_percent: Decimal | None
    return_6m_percent: Decimal | None
    return_12m_percent: Decimal | None
    volatility_annual_percent: Decimal
    max_drawdown_6m_percent: Decimal
    drawdown_from_peak_percent: Decimal
    sma_50_distance_percent: Decimal | None
    sma_200_distance_percent: Decimal | None
    above_sma_50_percent: Decimal | None
    above_sma_200_percent: Decimal | None
    avg_turnover: Decimal


@dataclass(frozen=True)
class GrowthScore:
    momentum: Decimal
    trend: Decimal
    stability: Decimal
    composite: Decimal
    momentum_reason: str
    trend_reason: str
    stability_reason: str


def neutral_growth() -> GrowthScore:
    reason = "нет свежей истории котировок — нейтральная оценка"
    return GrowthScore(
        momentum=FIFTY,
        trend=FIFTY,
        stability=FIFTY,
        composite=FIFTY,
        momentum_reason=reason,
        trend_reason=reason,
        stability_reason=reason,
    )


def _format_percent(value: Decimal, *, signed: bool = False) -> str:
    text = f"{value.quantize(Decimal('0.1')):f}".replace(".", ",")
    if signed and value > ZERO:
        text = "+" + text
    return text


def _window_return(closes: list[Decimal], window: int) -> Decimal | None:
    if len(closes) <= window:
        return None
    start = closes[-window - 1]
    if start <= ZERO:
        return None
    return quantize_percent((closes[-1] - start) / start * HUNDRED)


def _daily_returns(closes: list[Decimal]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in pairwise(closes):
        if previous > ZERO:
            returns.append((current - previous) / previous)
    return returns[-DAILY_WINDOW:]


def _annual_volatility(closes: list[Decimal]) -> Decimal:
    returns = _daily_returns(closes)
    if len(returns) < 2:
        return ZERO
    count = Decimal(len(returns))
    mean = sum(returns, ZERO) / count
    variance = sum(((value - mean) ** 2 for value in returns), ZERO) / count
    if variance <= ZERO:
        return ZERO
    stdev = variance.sqrt()
    return quantize_percent(stdev * TRADING_DAYS_PER_YEAR.sqrt() * HUNDRED)


def _max_drawdown(closes: list[Decimal]) -> Decimal:
    peak = closes[0]
    worst = ZERO
    for value in closes:
        if value > peak:
            peak = value
        if peak > ZERO:
            drawdown = (peak - value) / peak * HUNDRED
            if drawdown > worst:
                worst = drawdown
    return quantize_percent(worst)


def _drawdown_from_peak(closes: list[Decimal]) -> Decimal:
    peak = max(closes)
    if peak <= ZERO:
        return ZERO
    return quantize_percent((peak - closes[-1]) / peak * HUNDRED)


def _rolling_sma(closes: list[Decimal], window: int) -> list[tuple[int, Decimal]]:
    if len(closes) < window:
        return []
    total = sum(closes[:window], ZERO)
    rows: list[tuple[int, Decimal]] = [(window - 1, total / Decimal(window))]
    for index in range(window, len(closes)):
        total += closes[index] - closes[index - window]
        rows.append((index, total / Decimal(window)))
    return rows


def _above_share(
    closes: list[Decimal],
    rows: list[tuple[int, Decimal]],
    limit: int = SIX_MONTH_WINDOW,
) -> Decimal | None:
    tail = rows[-limit:]
    if not tail:
        return None
    above = sum(1 for index, sma in tail if closes[index] > sma)
    return quantize_percent(Decimal(above) / Decimal(len(tail)) * HUNDRED)


def _distance(closes: list[Decimal], rows: list[tuple[int, Decimal]]) -> Decimal | None:
    if not rows:
        return None
    sma = rows[-1][1]
    if sma <= ZERO:
        return None
    return quantize_percent((closes[-1] - sma) / sma * HUNDRED)


def _momentum_score(returns: dict[str, Decimal | None], volatility: Decimal) -> Decimal:
    weighted = ZERO
    weights = ZERO
    for _window, name, weight in MOMENTUM_WINDOWS:
        value = returns.get(name)
        if value is None:
            continue
        weighted += value * weight
        weights += weight
    if weights <= ZERO:
        return FIFTY
    average = weighted / weights
    absolute = clamp(FIFTY + average * Decimal("2"))
    if volatility < Decimal("0.01"):
        risk_adjusted = clamp(FIFTY + average * Decimal("4"))
    else:
        ratio = average / volatility
        risk_adjusted = clamp(FIFTY + ratio * Decimal("25"))
    return quantize_percent(clamp(risk_adjusted * Decimal("0.5") + absolute * Decimal("0.5")))


def _trend_score(
    above_50: Decimal | None,
    distance_50: Decimal | None,
    above_200: Decimal | None,
    distance_200: Decimal | None,
) -> Decimal:
    parts: list[tuple[Decimal, Decimal]] = []
    if above_50 is not None:
        parts.append((above_50, Decimal("0.45")))
    if distance_50 is not None:
        parts.append((clamp(FIFTY + distance_50 * Decimal("5")), Decimal("0.15")))
    if above_200 is not None:
        parts.append((above_200, Decimal("0.25")))
    if distance_200 is not None:
        parts.append((clamp(FIFTY + distance_200 * Decimal("2.5")), Decimal("0.15")))
    if not parts:
        return FIFTY
    total = sum((value * weight for value, weight in parts), ZERO)
    weights = sum((weight for _value, weight in parts), ZERO)
    return quantize_percent(clamp(total / weights))


def _momentum_reason(returns: dict[str, Decimal | None], volatility: Decimal) -> str:
    parts: list[str] = []
    for name, title in (("3m", "3м"), ("6m", "6м"), ("12m", "12м")):
        value = returns.get(name)
        if value is not None:
            parts.append(f"{title} {_format_percent(value, signed=True)}%")
    month = returns.get("1m")
    if month is not None:
        parts.insert(0, f"1м {_format_percent(month, signed=True)}%")
    body = ", ".join(parts) if parts else "нет данных"
    return (
        f"импульс: абсолютная доходность и риск-коррекция; {body}, "
        f"волатильность {_format_percent(volatility)}%"
    )


def _trend_reason(
    above_50: Decimal | None,
    distance_50: Decimal | None,
    above_200: Decimal | None,
    distance_200: Decimal | None,
) -> str:
    parts: list[str] = []
    if above_50 is not None:
        parts.append(f"выше SMA50 {_format_percent(above_50)}% дней")
    if distance_50 is not None:
        parts.append(f"к SMA50 {_format_percent(distance_50, signed=True)}%")
    if above_200 is not None:
        parts.append(f"выше SMA200 {_format_percent(above_200)}% дней")
    if distance_200 is not None:
        parts.append(f"к SMA200 {_format_percent(distance_200, signed=True)}%")
    return "тренд: " + (", ".join(parts) if parts else "нет данных")


def _stability_reason(max_drawdown: Decimal) -> str:
    return f"максимальная просадка за 6м {_format_percent(max_drawdown)}%"


def analyze_candles(candles: Iterable[CandleInfo]) -> TrendMetrics | None:
    """Build deterministic metrics from complete positive daily candles."""

    by_day: dict[object, CandleInfo] = {}
    for candle in sorted(candles, key=lambda item: ensure_utc(item.at)):
        if not candle.is_complete or candle.close <= ZERO:
            continue
        by_day[ensure_utc(candle.at).date()] = candle
    ordered = list(by_day.values())
    if len(ordered) < MIN_HISTORY_CANDLES:
        return None
    closes = [candle.close for candle in ordered]
    returns: dict[str, Decimal | None] = {}
    for window, name, _weight in MOMENTUM_WINDOWS:
        returns[name] = _window_return(closes, window)
    volatility = _annual_volatility(closes)
    rows_50 = _rolling_sma(closes, 50)
    rows_200 = _rolling_sma(closes, 200)
    above_50 = _above_share(closes, rows_50)
    above_200 = _above_share(closes, rows_200)
    distance_50 = _distance(closes, rows_50)
    distance_200 = _distance(closes, rows_200)
    short = closes[-SIX_MONTH_WINDOW:]
    max_drawdown = _max_drawdown(short)
    drawdown_from_peak = _drawdown_from_peak(closes)
    turnover_window = ordered[-63:]
    avg_turnover = quantize_money(
        sum((candle.close * Decimal(candle.volume) for candle in turnover_window), ZERO)
        / Decimal(len(turnover_window))
    )
    return TrendMetrics(
        candle_count=len(closes),
        window_start_at=ensure_utc(ordered[0].at),
        window_end_at=ensure_utc(ordered[-1].at),
        last_close=quantize_price(closes[-1]),
        return_1m_percent=returns["1m"],
        return_3m_percent=returns["3m"],
        return_6m_percent=returns["6m"],
        return_12m_percent=returns["12m"],
        volatility_annual_percent=volatility,
        max_drawdown_6m_percent=max_drawdown,
        drawdown_from_peak_percent=drawdown_from_peak,
        sma_50_distance_percent=distance_50,
        sma_200_distance_percent=distance_200,
        above_sma_50_percent=above_50,
        above_sma_200_percent=above_200,
        avg_turnover=avg_turnover,
    )


def score_growth(metrics: TrendMetrics | None) -> GrowthScore:
    """Map metrics to bounded 0..100 components; None means neutral."""

    if metrics is None:
        return neutral_growth()
    returns = {
        "1m": metrics.return_1m_percent,
        "3m": metrics.return_3m_percent,
        "6m": metrics.return_6m_percent,
        "12m": metrics.return_12m_percent,
    }
    momentum = _momentum_score(returns, metrics.volatility_annual_percent)
    trend = _trend_score(
        metrics.above_sma_50_percent,
        metrics.sma_50_distance_percent,
        metrics.above_sma_200_percent,
        metrics.sma_200_distance_percent,
    )
    stability = quantize_percent(clamp(HUNDRED - metrics.max_drawdown_6m_percent * Decimal("2")))
    composite = quantize_percent(
        clamp(momentum * Decimal("0.50") + trend * Decimal("0.35") + stability * Decimal("0.15"))
    )
    return GrowthScore(
        momentum=momentum,
        trend=trend,
        stability=stability,
        composite=composite,
        momentum_reason=_momentum_reason(returns, metrics.volatility_annual_percent),
        trend_reason=_trend_reason(
            metrics.above_sma_50_percent,
            metrics.sma_50_distance_percent,
            metrics.above_sma_200_percent,
            metrics.sma_200_distance_percent,
        ),
        stability_reason=_stability_reason(metrics.max_drawdown_6m_percent),
    )
