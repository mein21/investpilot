from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.screener.domain import Bar
from app.screener.features import compute_features, median, period_return
from helpers import bar_series, screener_config, trading_days


def test_median_is_deterministic_for_even_and_odd_samples() -> None:
    assert median([Decimal("1"), Decimal("5"), Decimal("3")]) == Decimal("3")
    assert median([Decimal("1"), Decimal("2"), Decimal("9"), Decimal("5")]) == Decimal("3.5")
    assert median([]) is None


def test_turnover_multiplies_lots_by_lot_size() -> None:
    bar = Bar(
        trade_date=date(2025, 1, 1),
        open=Decimal("200"),
        high=Decimal("201"),
        low=Decimal("199"),
        close=Decimal("200"),
        volume_lots=10,
        lot=100,
    )
    assert bar.turnover == Decimal("200000")


def test_feature_calculation_has_no_lookahead() -> None:
    days = trading_days(320)
    closes = [Decimal(100) + Decimal(index) / Decimal(10) for index in range(320)]
    benchmark = bar_series(
        days, [Decimal(100) + Decimal(index) / Decimal(20) for index in range(320)]
    )
    stock = bar_series(days, closes)
    config = screener_config()

    as_of = days[299]
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}
    full = compute_features(stock, benchmark, calendar, index, as_of, config)
    truncated = compute_features(
        [bar for bar in stock if bar.trade_date <= as_of],
        [bar for bar in benchmark if bar.trade_date <= as_of],
        calendar,
        index,
        as_of,
        config,
    )

    assert full is not None
    assert truncated is not None
    assert full == truncated
    assert full.close == closes[299]
    assert full.ret_1m == period_return(
        {bar.trade_date: bar for bar in stock},
        calendar,
        index,
        as_of,
        21,
    )


def test_compute_features_returns_none_without_benchmark_or_signal_bar() -> None:
    days = trading_days(80)
    stock = bar_series(days, [100 + index for index in range(80)])
    calendar = [bar.trade_date for bar in stock]
    index = {day: position for position, day in enumerate(calendar)}
    assert compute_features(stock, [], calendar, index, days[-1], screener_config()) is None

    benchmark = bar_series(days, [100 + index for index in range(80)])
    assert (
        compute_features(stock, benchmark, calendar, index, days[-2], screener_config()) is not None
    )
    assert (
        compute_features(stock[:-1], benchmark, calendar, index, days[-1], screener_config())
        is None
    )


def test_up_down_volume_ratio_is_capped() -> None:
    days = trading_days(80)
    benchmark = bar_series(days, [100 + index for index in range(80)])
    stock = bar_series(days, [100 + index for index in range(80)])
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}
    result = compute_features(stock, benchmark, calendar, index, days[-1], screener_config())
    assert result is not None
    assert result.up_down_volume_ratio_60 == Decimal("10")


def test_abnormal_move_is_reported_in_sigma_units() -> None:
    days = trading_days(80)
    benchmark = bar_series(days, [100 + index / 10 for index in range(80)])
    closes = [Decimal(100)] * 75 + [
        Decimal(105),
        Decimal(110),
        Decimal(115),
        Decimal(120),
        Decimal(125),
    ]
    stock = bar_series(days, closes)
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}
    result = compute_features(stock, benchmark, calendar, index, days[-1], screener_config())
    assert result is not None
    assert result.abnormal_5d is not None
    assert abs(result.abnormal_5d) < Decimal("25")


def test_period_return_can_skip_recent_sessions() -> None:
    days = trading_days(320)
    closes = [Decimal(100) + Decimal(index) for index in range(320)]
    bars = bar_series(days, closes)
    calendar = days
    index = {day: position for position, day in enumerate(calendar)}
    by_date = {bar.trade_date: bar for bar in bars}

    value = period_return(by_date, calendar, index, days[-1], 126, end_offset=5)

    assert value == closes[-6] / closes[-132] - Decimal("1")


def test_breakout_volume_ratio_compares_latest_turnover_to_prior_base() -> None:
    days = trading_days(80)
    benchmark = bar_series(days, [100 + index / 10 for index in range(80)])
    closes = [Decimal(100)] * 79 + [Decimal(110)]
    stock = bar_series(days, closes)
    stock[-1] = replace(stock[-1], volume_lots=3000, close=Decimal("110"))
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    result = compute_features(stock, benchmark, calendar, index, days[-1], screener_config())

    assert result is not None
    assert result.breakout_volume_ratio_20 == Decimal("3.3")


def test_max_turnover_share_detects_single_spike() -> None:
    days = trading_days(80)
    benchmark = bar_series(days, [100 + index / 10 for index in range(80)])
    stock = bar_series(days, [100] * 80)
    stock[-1] = replace(stock[-1], volume_lots=100_000)
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    result = compute_features(stock, benchmark, calendar, index, days[-1], screener_config())

    assert result is not None
    assert result.max_turnover_share_60 is not None
    assert result.max_turnover_share_60 > Decimal("0.5")


def test_large_gap_frequency_counts_overnight_gaps() -> None:
    days = trading_days(80)
    benchmark = bar_series(days, [100 + index / 10 for index in range(80)])
    stock = [
        Bar(
            trade_date=day,
            open=Decimal("110"),
            high=Decimal("111"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume_lots=1000,
            lot=1,
        )
        for day in days
    ]
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    result = compute_features(stock, benchmark, calendar, index, days[-1], screener_config())

    assert result is not None
    assert result.large_gap_freq_60 == Decimal("1")
