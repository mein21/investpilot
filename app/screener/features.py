"""Deterministic feature engineering on daily bars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise

from app.screener.config import ScreenerConfig
from app.screener.domain import Bar, Dividend, FeatureSet
from app.screener.quality import adjust_prices_for_dividends

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
TRADING_DAYS = Decimal("252")
SQRT_252 = TRADING_DAYS.sqrt()


def mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / TWO


def stdev(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    avg = mean(values)
    if avg is None:
        return None
    variance = sum(((value - avg) ** TWO for value in values), ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def sma(values: Sequence[Decimal], window: int, end: int | None = None) -> Decimal | None:
    if window <= 0:
        return None
    stop = len(values) if end is None else end + 1
    start = stop - window
    if start < 0 or stop > len(values):
        return None
    return sum(values[start:stop], ZERO) / Decimal(window)


def max_value(values: Sequence[Decimal]) -> Decimal | None:
    return max(values) if values else None


def min_value(values: Sequence[Decimal]) -> Decimal | None:
    return min(values) if values else None


def pct_change(values: Sequence[Decimal], periods: int) -> Decimal | None:
    if periods <= 0 or len(values) <= periods:
        return None
    start = values[-periods - 1]
    if start <= 0:
        return None
    return values[-1] / start - ONE


def simple_returns(values: Sequence[Decimal]) -> list[Decimal]:
    return [current / previous - ONE for previous, current in pairwise(values) if previous > 0]


def max_drawdown(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    peak = values[0]
    worst = ZERO
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = value / peak - ONE
            worst = min(worst, drawdown)
    return worst


def close_on_or_before(
    bars_by_date: dict[date, Bar],
    target: date,
) -> Decimal | None:
    candidates = [day for day in bars_by_date if day <= target]
    if not candidates:
        return None
    return bars_by_date[max(candidates)].close


def period_return(
    bars_by_date: dict[date, Bar],
    calendar: Sequence[date],
    calendar_index: dict[date, int],
    as_of: date,
    sessions: int,
    *,
    end_offset: int = 0,
) -> Decimal | None:
    index = calendar_index.get(as_of)
    if index is None or index - sessions - end_offset < 0:
        return None
    start_date = calendar[index - sessions - end_offset]
    end_date = calendar[index - end_offset]
    start = close_on_or_before(bars_by_date, start_date)
    end = close_on_or_before(bars_by_date, end_date)
    if start is None or end is None or start <= 0:
        return None
    return end / start - ONE


def _turnovers(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.turnover for bar in bars if bar.traded]


def _rolling_consistency(closes: Sequence[Decimal], window: int, lookback: int) -> Decimal | None:
    if len(closes) < window + lookback:
        return None
    above = 0
    total = 0
    for offset in range(lookback, 0, -1):
        end = len(closes) - offset
        average = sma(closes, window, end=end)
        if average is None:
            continue
        total += 1
        if closes[end] > average:
            above += 1
    if total == 0:
        return None
    return Decimal(above) / Decimal(total)


def _downside_vol(returns: Sequence[Decimal]) -> Decimal | None:
    if not returns:
        return None
    squares = [min(value, ZERO) ** TWO for value in returns]
    return (sum(squares, ZERO) / Decimal(len(squares))).sqrt() * SQRT_252


def _annualized_vol(returns: Sequence[Decimal]) -> Decimal | None:
    deviation = stdev(returns)
    return None if deviation is None else deviation * SQRT_252


def _beta(
    stock_returns: dict[date, Decimal], benchmark_returns: dict[date, Decimal]
) -> Decimal | None:
    common = sorted(set(stock_returns) & set(benchmark_returns))[-126:]
    if len(common) < 40:
        return None
    stock = [stock_returns[day] for day in common]
    bench = [benchmark_returns[day] for day in common]
    stock_mean = mean(stock)
    bench_mean = mean(bench)
    if stock_mean is None or bench_mean is None:
        return None
    covariance = sum(
        ((s - stock_mean) * (b - bench_mean) for s, b in zip(stock, bench, strict=True)),
        ZERO,
    ) / Decimal(len(common) - 1)
    variance = sum(((b - bench_mean) ** TWO for b in bench), ZERO) / Decimal(len(common) - 1)
    if variance <= 0:
        return None
    return covariance / variance


def _returns_by_date(bars: Sequence[Bar]) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for previous, current in pairwise(bars):
        if previous.close > 0:
            result[current.trade_date] = current.close / previous.close - ONE
    return result


def compute_features(
    bars: Sequence[Bar],
    benchmark_bars: Sequence[Bar],
    calendar: Sequence[date],
    calendar_index: dict[date, int],
    as_of: date,
    config: ScreenerConfig,
    *,
    dividends: Sequence[Dividend] = (),
) -> FeatureSet | None:
    raw_stock = [bar for bar in bars if bar.trade_date <= as_of]
    benchmark = [bar for bar in benchmark_bars if bar.trade_date <= as_of]
    if not raw_stock or not benchmark:
        return None
    if raw_stock[-1].trade_date != as_of:
        return None
    stock = adjust_prices_for_dividends(raw_stock, dividends, calendar, as_of)
    stock_by_date = {bar.trade_date: bar for bar in stock}
    benchmark_by_date = {bar.trade_date: bar for bar in benchmark}
    closes = [bar.close for bar in stock]
    latest = stock[-1]
    raw_latest = raw_stock[-1]

    ret_1m = period_return(stock_by_date, calendar, calendar_index, as_of, 21)
    ret_3m = period_return(stock_by_date, calendar, calendar_index, as_of, 63)
    ret_6m = period_return(stock_by_date, calendar, calendar_index, as_of, 126)
    ret_6m_ex_5d = period_return(
        stock_by_date,
        calendar,
        calendar_index,
        as_of,
        126,
        end_offset=5,
    )
    ret_12m = period_return(stock_by_date, calendar, calendar_index, as_of, 252)
    bench_1m = period_return(benchmark_by_date, calendar, calendar_index, as_of, 21)
    bench_3m = period_return(benchmark_by_date, calendar, calendar_index, as_of, 63)
    bench_6m = period_return(benchmark_by_date, calendar, calendar_index, as_of, 126)
    bench_6m_ex_5d = period_return(
        benchmark_by_date,
        calendar,
        calendar_index,
        as_of,
        126,
        end_offset=5,
    )
    bench_12m = period_return(benchmark_by_date, calendar, calendar_index, as_of, 252)

    def excess(stock_value: Decimal | None, bench_value: Decimal | None) -> Decimal | None:
        if stock_value is None or bench_value is None:
            return None
        return stock_value - bench_value

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    sma50_prev = sma(closes, 50, end=len(closes) - 21) if len(closes) >= 71 else None
    sma200_prev = sma(closes, 200, end=len(closes) - 21) if len(closes) >= 221 else None
    sma50_slope = (
        sma50 / sma50_prev - ONE if sma50 is not None and sma50_prev and sma50_prev > 0 else None
    )
    sma200_slope = (
        sma200 / sma200_prev - ONE
        if sma200 is not None and sma200_prev and sma200_prev > 0
        else None
    )

    turnovers = _turnovers(raw_stock)
    turnover_5 = mean(turnovers[-5:])
    turnover_20 = median(turnovers[-20:])
    turnover_60 = median(turnovers[-60:])
    turnover_ratio_5_60 = (
        turnover_5 / turnover_60
        if turnover_5 is not None and turnover_60 and turnover_60 > 0
        else None
    )
    turnover_ratio_20_60 = (
        turnover_20 / turnover_60
        if turnover_20 is not None and turnover_60 and turnover_60 > 0
        else None
    )
    turnover_deviation = stdev(turnovers[-60:])
    turnover_cv = (
        turnover_deviation / turnover_60
        if turnover_deviation is not None and turnover_60 and turnover_60 > 0
        else None
    )
    recent_turnovers = turnovers[-60:]
    turnover_total = sum(recent_turnovers, ZERO)
    max_turnover_share = (
        max(recent_turnovers) / turnover_total if recent_turnovers and turnover_total > 0 else None
    )

    up_volume = ZERO
    down_volume = ZERO
    for previous, current in zip(raw_stock[-61:], raw_stock[-60:], strict=False):
        if current.close > previous.close:
            up_volume += current.turnover
        elif current.close < previous.close:
            down_volume += current.turnover
    up_down_ratio = (
        min(up_volume / down_volume, Decimal("10"))
        if down_volume > 0
        else (Decimal("10") if up_volume > 0 else None)
    )

    highs_20 = [bar.high for bar in stock[-20:]]
    highs_60 = [bar.high for bar in stock[-60:]]
    highs_63 = [bar.high for bar in stock[-63:]]
    highs_126 = [bar.high for bar in stock[-126:]]
    lows_20 = [bar.low for bar in stock[-20:]]
    lows_60 = [bar.low for bar in stock[-60:]]
    high_20 = max_value(highs_20)
    high_60 = max_value(highs_60)
    high_63 = max_value(highs_63)
    high_126 = max_value(highs_126)
    low_20 = min_value(lows_20)
    low_60 = min_value(lows_60)
    range_20 = (
        (high_20 - low_20) / latest.close
        if high_20 is not None and low_20 is not None and latest.close > 0
        else None
    )
    range_60 = (
        (high_60 - low_60) / latest.close
        if high_60 is not None and low_60 is not None and latest.close > 0
        else None
    )
    squeeze = None
    if range_20 is not None and range_60 is not None and range_60 > 0:
        squeeze = ONE - min(range_20 / range_60, ONE)
    prior_high_20 = max_value([bar.high for bar in stock[-21:-1]])
    prior_high_60 = max_value([bar.high for bar in stock[-61:-1]])
    prior_high_63 = max_value([bar.high for bar in stock[-64:-1]])
    prior_high_126 = max_value([bar.high for bar in stock[-127:-1]])
    breakout_20 = (
        latest.close / prior_high_20 - ONE
        if prior_high_20 is not None and prior_high_20 > 0
        else None
    )
    breakout_60 = (
        latest.close / prior_high_60 - ONE
        if prior_high_60 is not None and prior_high_60 > 0
        else None
    )
    breakout_63 = (
        latest.close / prior_high_63 - ONE
        if prior_high_63 is not None and prior_high_63 > 0
        else None
    )
    breakout_126 = (
        latest.close / prior_high_126 - ONE
        if prior_high_126 is not None and prior_high_126 > 0
        else None
    )
    prior_turnovers = [bar.turnover for bar in raw_stock[-21:-1] if bar.traded]
    prior_turnover_median = median(prior_turnovers)
    latest_turnover = raw_latest.turnover if raw_latest.traded else None
    breakout_volume_ratio = (
        min(latest_turnover / prior_turnover_median, Decimal("10"))
        if latest_turnover is not None
        and prior_turnover_median is not None
        and prior_turnover_median > 0
        else None
    )
    dist_high_60 = latest.close / high_60 - ONE if high_60 is not None and high_60 > 0 else None
    dist_high_63 = latest.close / high_63 - ONE if high_63 is not None and high_63 > 0 else None
    dist_high_126 = latest.close / high_126 - ONE if high_126 is not None and high_126 > 0 else None
    high_252 = max_value([bar.high for bar in stock[-252:]]) if len(stock) >= 252 else None
    dist_high_252 = latest.close / high_252 - ONE if high_252 is not None and high_252 > 0 else None

    returns_20 = simple_returns(closes[-21:])
    returns_60 = simple_returns(closes[-61:])
    volatility_20 = _annualized_vol(returns_20)
    volatility_60 = _annualized_vol(returns_60)
    downside_vol = _downside_vol(returns_60)
    max_dd_126 = max_drawdown(closes[-127:])
    gap_window = list(pairwise(stock[-61:]))
    gaps = [
        current.open / previous.close - ONE
        for previous, current in gap_window
        if previous.close > 0
    ]
    large_gaps = sum(1 for gap in gaps if abs(gap) >= config.large_gap_threshold)
    large_gap_freq = Decimal(large_gaps) / Decimal(len(gaps)) if gaps else None

    true_ranges: list[Decimal] = []
    for previous, current in pairwise(stock[-15:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr = mean(true_ranges)
    atr_pct = atr / latest.close if atr is not None and latest.close > 0 else None

    stock_returns = _returns_by_date(stock)
    bench_returns = _returns_by_date(benchmark)
    beta = _beta(stock_returns, bench_returns)
    ret_5 = pct_change(closes, 5)
    abnormal_5d = (
        ret_5 * SQRT_252 / (volatility_60 * Decimal("5").sqrt())
        if ret_5 is not None and volatility_60 is not None and volatility_60 > 0
        else None
    )

    return FeatureSet(
        as_of=as_of,
        close=raw_latest.close,
        ret_1m=ret_1m,
        ret_3m=ret_3m,
        ret_6m=ret_6m,
        ret_6m_ex_5d=ret_6m_ex_5d,
        ret_12m=ret_12m,
        excess_1m=excess(ret_1m, bench_1m),
        excess_3m=excess(ret_3m, bench_3m),
        excess_6m=excess(ret_6m, bench_6m),
        excess_6m_ex_5d=excess(ret_6m_ex_5d, bench_6m_ex_5d),
        excess_12m=excess(ret_12m, bench_12m),
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        above_sma20=(latest.close > sma20) if sma20 is not None else None,
        above_sma50=(latest.close > sma50) if sma50 is not None else None,
        above_sma200=(latest.close > sma200) if sma200 is not None else None,
        sma50_slope_20=sma50_slope,
        sma200_slope_20=sma200_slope,
        trend_consistency_60=_rolling_consistency(closes, 50, 60),
        drawdown_252=max_drawdown(closes[-252:]),
        median_turnover_20=turnover_20,
        median_turnover_60=turnover_60,
        turnover_ratio_5_60=turnover_ratio_5_60,
        turnover_ratio_20_60=turnover_ratio_20_60,
        up_down_volume_ratio_60=up_down_ratio,
        turnover_cv_60=turnover_cv,
        max_turnover_share_60=max_turnover_share,
        range_20=range_20,
        range_60=range_60,
        squeeze_20_60=squeeze,
        dist_high_60=dist_high_60,
        dist_high_63=dist_high_63,
        dist_high_126=dist_high_126,
        dist_high_252=dist_high_252,
        breakout_20=breakout_20,
        breakout_60=breakout_60,
        breakout_63=breakout_63,
        breakout_126=breakout_126,
        breakout_volume_ratio_20=breakout_volume_ratio,
        volatility_20=volatility_20,
        volatility_60=volatility_60,
        downside_vol_60=downside_vol,
        max_drawdown_126=max_dd_126,
        atr_14_pct=atr_pct,
        large_gap_freq_60=large_gap_freq,
        beta_126=beta,
        abnormal_5d=abnormal_5d,
    )
