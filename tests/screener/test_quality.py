from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.screener.domain import Dividend
from app.screener.quality import (
    adjust_prices_for_dividends,
    clean_bars,
    dividend_ex_dates,
    evaluate_quality,
)
from helpers import bar_series, screener_config, trading_days


def test_clean_bars_drops_incomplete_invalid_and_duplicate_rows() -> None:
    days = trading_days(3)
    good = bar_series(days, [100, 101, 102])
    incomplete = replace(good[1], is_complete=False)
    invalid = replace(good[2], high=Decimal("1"), low=Decimal("2"))
    duplicate = replace(good[2], close=Decimal("999"))

    cleaned, invalid_dates = clean_bars([*good, incomplete, invalid, duplicate])

    assert [bar.trade_date for bar in cleaned] == days
    assert invalid_dates == (days[2], days[2])


def test_quality_requires_history_activity_and_fresh_bar() -> None:
    days = trading_days(80)
    config = screener_config()
    bars = bar_series(days, [100 + index for index in range(80)])

    quality, _cleaned = evaluate_quality(bars, days, days[-1], config)
    assert not quality.critical
    assert quality.history_sessions == 80
    assert quality.activity_ratio == Decimal("1")
    assert quality.stale_sessions == 0

    stale, _ = evaluate_quality(bars[:-3], days, days[-1], config)
    assert stale.critical
    assert "stale" in stale.flags

    short, _ = evaluate_quality(bars[-10:], days, days[-1], config)
    assert short.critical
    assert "insufficient_history" in short.flags


def test_quality_flags_abnormal_gap_but_ignores_dividend_ex_date() -> None:
    days = trading_days(80)
    config = screener_config()
    closes = [Decimal(100)] * 79 + [Decimal(150)]
    bars = bar_series(days, closes)

    quality, _ = evaluate_quality(bars, days, days[-1], config)
    assert quality.critical
    assert "suspicious_gap" in quality.flags
    assert quality.suspicious_gaps == (days[-1],)

    dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=days[-2],
        record_date=days[-1],
    )
    ex_dates = dividend_ex_dates([dividend], days)
    assert ex_dates == {days[-1]: Decimal("10")}
    quality, _ = evaluate_quality(bars, days, days[-1], config, dividends=[dividend])
    assert "suspicious_gap" not in quality.flags


def test_dividend_without_last_buy_date_uses_record_date() -> None:
    days = trading_days(5)
    dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=None,
        record_date=days[2],
    )

    assert dividend_ex_dates([dividend], days) == {days[2]: Decimal("10")}


def test_quality_flags_low_activity_and_zero_volume() -> None:
    days = trading_days(80)
    config = screener_config()
    bars = bar_series(days, [100 + index for index in range(80)])
    quiet = [
        bar if bar.trade_date not in set(days[-10:]) else replace(bar, volume_lots=0)
        for bar in bars
    ]

    quality, _ = evaluate_quality(quiet, days, days[-1], config)
    assert quality.critical
    assert "low_activity" in quality.flags
    assert "zero_volume" in quality.flags


def test_old_split_outside_recent_history_does_not_block_ranking() -> None:
    days = trading_days(100)
    config = screener_config()
    closes = [Decimal(100)] * 100
    closes[10] = Decimal(200)
    bars = bar_series(days, closes)

    quality, _ = evaluate_quality(bars, days, days[-1], config)

    assert not quality.critical
    assert "suspicious_gap" not in quality.flags


def test_dividend_adjustment_removes_ex_date_price_drop() -> None:
    days = trading_days(5)
    bars = bar_series(days, [100, 100, 90, 90, 90])
    dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=days[1],
        record_date=days[1],
    )

    adjusted = adjust_prices_for_dividends(bars, [dividend], days, days[-1])

    assert [bar.close for bar in adjusted] == [
        Decimal("90"),
        Decimal("90"),
        Decimal("90"),
        Decimal("90"),
        Decimal("90"),
    ]
    assert [bar.close for bar in bars] == [
        Decimal("100"),
        Decimal("100"),
        Decimal("90"),
        Decimal("90"),
        Decimal("90"),
    ]


def test_dividend_declared_after_as_of_is_not_used_for_adjustment_or_gap() -> None:
    days = trading_days(5)
    bars = bar_series(days, [100, 100, 90, 90, 90])
    dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=days[1],
        record_date=days[1],
        declared_date=days[3],
    )

    adjusted = adjust_prices_for_dividends(bars, [dividend], days, days[2])
    assert [bar.close for bar in adjusted] == [bar.close for bar in bars]

    gap_days = trading_days(80)
    gap_bars = bar_series(gap_days, [Decimal(100)] * 2 + [Decimal(150)] + [Decimal(150)] * 77)
    late_dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=gap_days[1],
        record_date=gap_days[2],
        declared_date=gap_days[3],
    )
    quality, _ = evaluate_quality(
        gap_bars,
        gap_days,
        gap_days[2],
        screener_config(),
        dividends=[late_dividend],
    )
    assert "suspicious_gap" in quality.flags


def test_dividend_adjustment_ignores_dividends_after_as_of() -> None:
    days = trading_days(5)
    bars = bar_series(days, [100, 100, 90, 90, 90])
    dividend = Dividend(
        uid="uid-test",
        amount=Decimal("10"),
        last_buy_date=days[3],
        record_date=days[3],
    )

    adjusted = adjust_prices_for_dividends(bars, [dividend], days, days[2])

    assert [bar.close for bar in adjusted] == [bar.close for bar in bars]
