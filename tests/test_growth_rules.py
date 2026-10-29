from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.integrations.tinvest.types import CandleInfo
from app.rules.growth import (
    MIN_HISTORY_CANDLES,
    analyze_candles,
    neutral_growth,
    score_growth,
)

START = datetime(2025, 1, 1, tzinfo=UTC)


def series(values: list[Decimal], *, volume: int = 1000) -> list[CandleInfo]:
    candles: list[CandleInfo] = []
    previous = values[0]
    for index, close in enumerate(values):
        candles.append(
            CandleInfo(
                open=previous,
                high=max(previous, close),
                low=min(previous, close),
                close=close,
                volume=volume,
                at=START + timedelta(days=index),
                is_complete=True,
            )
        )
        previous = close
    return candles


def compounded(start: Decimal, factor: Decimal, count: int) -> list[Decimal]:
    values = [start]
    for _ in range(count - 1):
        values.append(values[-1] * factor)
    return values


def test_uptrend_scores_above_sideways_and_downtrend() -> None:
    up = analyze_candles(series(compounded(Decimal("100"), Decimal("1.002"), 300)))
    down = analyze_candles(series(compounded(Decimal("100"), Decimal("0.998"), 300)))
    assert up is not None
    assert down is not None

    up_score = score_growth(up)
    down_score = score_growth(down)

    assert up.candle_count == 300
    assert up.last_close > Decimal("100")
    assert up_score.momentum > Decimal("90")
    assert up_score.trend > Decimal("90")
    assert up_score.composite > Decimal("90")
    assert down_score.momentum < Decimal("15")
    assert down_score.trend < Decimal("10")
    assert down_score.composite < up_score.composite


def test_metrics_are_deterministic_and_decimal_only() -> None:
    candles = series(compounded(Decimal("100"), Decimal("1.001"), 140))
    first = analyze_candles(candles)
    second = analyze_candles(candles)

    assert first == second
    assert first is not None
    for value in (
        first.last_close,
        first.volatility_annual_percent,
        first.max_drawdown_6m_percent,
        first.avg_turnover,
    ):
        assert isinstance(value, Decimal)
    assert score_growth(first) == score_growth(second)


def test_short_history_returns_none_and_neutral_score() -> None:
    candles = series(compounded(Decimal("100"), Decimal("1.001"), MIN_HISTORY_CANDLES - 1))

    assert analyze_candles(candles) is None
    neutral = score_growth(None)
    assert neutral == neutral_growth()
    assert neutral.momentum == Decimal("50")
    assert neutral.composite == Decimal("50")


def test_incomplete_and_duplicate_candles_are_ignored() -> None:
    candles = series(compounded(Decimal("100"), Decimal("1.001"), 130))
    candles.append(
        CandleInfo(
            open=Decimal("9999"),
            high=Decimal("9999"),
            low=Decimal("9999"),
            close=Decimal("9999"),
            volume=1,
            at=START + timedelta(days=130),
            is_complete=False,
        )
    )
    duplicate = CandleInfo(
        open=candles[10].open,
        high=candles[10].high,
        low=candles[10].low,
        close=candles[10].close * Decimal("1.001"),
        volume=1,
        at=candles[10].at,
        is_complete=True,
    )
    metrics = analyze_candles([*candles, duplicate])

    assert metrics is not None
    assert metrics.last_close < Decimal("9999")
    assert metrics.candle_count == 130


def test_flat_series_has_neutral_momentum_and_no_drawdown() -> None:
    metrics = analyze_candles(series([Decimal("100")] * 120))

    assert metrics is not None
    score = score_growth(metrics)
    assert score.momentum == Decimal("50")
    assert score.stability == Decimal("100")
    assert score.composite < Decimal("50")


def test_drawdown_lowers_stability() -> None:
    values = compounded(Decimal("100"), Decimal("1.002"), 200)
    for _ in range(40):
        values.append(values[-1] * Decimal("0.97"))
    metrics = analyze_candles(series(values))

    assert metrics is not None
    assert metrics.max_drawdown_6m_percent > Decimal("20")
    assert score_growth(metrics).stability < Decimal("60")
