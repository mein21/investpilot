"""Benchmark-relative market regime and breadth."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from app.screener.domain import Bar, FeatureSet, MarketRegime
from app.screener.features import max_drawdown, simple_returns, sma, stdev

ZERO = Decimal("0")
ONE = Decimal("1")
SQRT_252 = Decimal("252").sqrt()


def compute_regime(
    benchmark: str,
    bars: Sequence[Bar],
    as_of: date,
    stock_features: Iterable[FeatureSet],
) -> MarketRegime:
    history = [bar for bar in bars if bar.trade_date <= as_of]
    if not history:
        return MarketRegime(
            as_of=as_of,
            benchmark=benchmark,
            label="unknown",
            close=ZERO,
            sma200=None,
            slope_20=None,
            volatility_60=None,
            drawdown_252=None,
            breadth_50=None,
            breadth_200=None,
            explanation="Нет данных бенчмарка.",
        )
    closes = [bar.close for bar in history]
    current = closes[-1]
    average = sma(closes, 200)
    previous = sma(closes, 200, end=len(closes) - 21) if len(closes) >= 221 else None
    slope = average / previous - ONE if average and previous and previous > 0 else None
    returns = simple_returns(closes[-61:])
    deviation = stdev(returns)
    volatility = deviation * SQRT_252 if deviation is not None else None
    drawdown = max_drawdown(closes[-252:])

    features = list(stock_features)
    breadth_50 = (
        Decimal(sum(1 for item in features if item.above_sma50)) / Decimal(len(features))
        if features
        else None
    )
    breadth_200 = (
        Decimal(sum(1 for item in features if item.above_sma200)) / Decimal(len(features))
        if features
        else None
    )

    if average is None or slope is None:
        label = "unknown"
        explanation = "Недостаточно истории бенчмарка для режима."
    elif current > average and slope > 0:
        label = "risk_on"
        explanation = "Бенчмарк выше SMA200, наклон положительный."
    elif current < average and slope < 0:
        label = "risk_off"
        explanation = "Бенчмарк ниже SMA200, наклон отрицательный."
    else:
        label = "neutral"
        explanation = "Сигналы бенчмарка смешанные."

    return MarketRegime(
        as_of=as_of,
        benchmark=benchmark,
        label=label,
        close=current,
        sma200=average,
        slope_20=slope,
        volatility_60=volatility,
        drawdown_252=drawdown,
        breadth_50=breadth_50,
        breadth_200=breadth_200,
        explanation=explanation,
    )
