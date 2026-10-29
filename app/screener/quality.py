"""Data-quality checks that decide whether a stock may be ranked."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import pairwise

from app.screener.config import ScreenerConfig
from app.screener.domain import Bar, DataQuality, Dividend

ONE = Decimal("1")
ZERO = Decimal("0")


def clean_bars(bars: Iterable[Bar]) -> tuple[list[Bar], tuple[date, ...]]:
    """Keep complete, unique, internally consistent bars in date order."""

    by_date: dict[date, Bar] = {}
    invalid: list[date] = []
    for bar in bars:
        if not bar.is_complete:
            continue
        if (
            bar.open <= 0
            or bar.high <= 0
            or bar.low <= 0
            or bar.close <= 0
            or bar.high < bar.low
            or bar.open > bar.high
            or bar.open < bar.low
            or bar.close > bar.high
            or bar.close < bar.low
            or bar.volume_lots < 0
        ):
            invalid.append(bar.trade_date)
            continue
        previous = by_date.get(bar.trade_date)
        if previous is not None and previous != bar:
            invalid.append(bar.trade_date)
        by_date[bar.trade_date] = bar
    return [by_date[key] for key in sorted(by_date)], tuple(sorted(invalid))


def dividend_known_on(item: Dividend) -> date | None:
    """Return the earliest date on which the dividend was public knowledge.

    ``declared_date`` is the primary source. When it is missing, the last buy
    date is a conservative upper bound: declaration always precedes the last
    day to buy, so the event cannot be unknown by then.
    """

    return item.declared_date or item.last_buy_date or item.record_date


def dividends_known_by(dividends: Iterable[Dividend], as_of: date) -> list[Dividend]:
    """Keep only dividends that were public on ``as_of``."""

    result: list[Dividend] = []
    for item in dividends:
        known = dividend_known_on(item)
        if known is not None and known <= as_of:
            result.append(item)
    return result


def dividend_ex_dates(
    dividends: Iterable[Dividend],
    calendar: Sequence[date],
) -> dict[date, Decimal]:
    """Map dividend cash to the first session after the last buy date.

    T-Invest reports the last day to buy and the record date. The price drop
    happens on the next session; using the record date directly would shift
    total return by one or more days. If only the record date is available,
    use it as the ex-date rather than moving the adjustment after the register
    date.
    """

    result: dict[date, Decimal] = {}
    ordered = list(calendar)
    for item in dividends:
        if item.last_buy_date is not None:
            ex_date = next((day for day in ordered if day > item.last_buy_date), None)
        elif item.record_date is not None:
            ex_date = next((day for day in ordered if day >= item.record_date), None)
        else:
            ex_date = None
        if ex_date is None:
            continue
        result[ex_date] = result.get(ex_date, ZERO) + item.amount
    return result


def adjust_prices_for_dividends(
    bars: Sequence[Bar],
    dividends: Sequence[Dividend],
    calendar: Sequence[date],
    as_of: date,
) -> list[Bar]:
    """Back-adjust OHLC for cash dividends known by ``as_of``.

    Prices before an ex-date are multiplied by ``(prev_close - dividend) / prev_close``.
    Volume is left untouched: turnover features use the raw bars separately.
    """

    if not bars or not dividends:
        return list(bars)
    known = dividends_known_by(dividends, as_of)
    ex_dates = {
        day: amount
        for day, amount in dividend_ex_dates(known, calendar).items()
        if day <= as_of and amount > 0
    }
    if not ex_dates:
        return list(bars)
    ordered = sorted(bars, key=lambda item: item.trade_date)
    factors: list[tuple[date, Decimal]] = []
    for ex_date, amount in sorted(ex_dates.items()):
        previous = [bar for bar in ordered if bar.trade_date < ex_date]
        if not previous or previous[-1].close <= 0:
            continue
        factor = (previous[-1].close - amount) / previous[-1].close
        if factor <= 0:
            continue
        factors.append((ex_date, factor))
    if not factors:
        return list(bars)
    adjusted: list[Bar] = []
    for bar in bars:
        factor = ONE
        for ex_date, value in factors:
            if bar.trade_date < ex_date:
                factor *= value
        if factor == ONE:
            adjusted.append(bar)
            continue
        adjusted.append(
            replace(
                bar,
                open=bar.open * factor,
                high=bar.high * factor,
                low=bar.low * factor,
                close=bar.close * factor,
            )
        )
    return adjusted


def _sessions_between(calendar: Sequence[date], start: date, end: date) -> list[date]:
    return [day for day in calendar if start <= day <= end]


def evaluate_quality(
    bars: Sequence[Bar],
    calendar: Sequence[date],
    as_of: date,
    config: ScreenerConfig,
    *,
    dividends: Sequence[Dividend] = (),
) -> tuple[DataQuality, list[Bar]]:
    cleaned, invalid = clean_bars(bar for bar in bars if bar.trade_date <= as_of)
    flags: list[str] = []
    critical = False

    history_sessions = len(cleaned)
    if history_sessions < config.min_history_sessions:
        flags.append("insufficient_history")
        critical = True

    if history_sessions == 0:
        return (
            DataQuality(
                history_sessions=0,
                activity_ratio=ZERO,
                missing_ratio=ONE,
                zero_volume_ratio=ONE,
                last_bar_date=None,
                stale_sessions=0,
                flags=tuple([*flags, "no_bars"]),
                critical=True,
            ),
            cleaned,
        )

    last_bar = cleaned[-1].trade_date
    window = [day for day in calendar if day <= as_of][-config.activity_window :]
    window_set = set(window)
    bars_in_window = [bar for bar in cleaned if bar.trade_date in window_set]
    traded = [bar for bar in bars_in_window if bar.traded]
    activity_ratio = Decimal(len(traded)) / Decimal(len(window)) if window else ZERO
    missing_ratio = (
        Decimal(len(window) - len(bars_in_window)) / Decimal(len(window)) if window else ONE
    )
    zero_volume_ratio = (
        Decimal(len(bars_in_window) - len(traded)) / Decimal(len(bars_in_window))
        if bars_in_window
        else ONE
    )
    stale_sessions = sum(1 for day in calendar if day > last_bar and day <= as_of)

    if activity_ratio < config.min_activity_ratio:
        flags.append("low_activity")
        critical = True
    if stale_sessions > config.stale_sessions_allowed:
        flags.append("stale")
        critical = True
    if zero_volume_ratio > config.zero_volume_tolerance:
        flags.append("zero_volume")
        critical = True
    if invalid:
        flags.append("bad_ohlc")
        critical = True

    ex_dates = dividend_ex_dates(dividends_known_by(dividends, as_of), calendar)
    suspicious: list[date] = []
    recent = cleaned[-config.min_history_sessions :]
    for previous, current in pairwise(recent):
        if previous.close <= 0:
            continue
        change = (current.close - previous.close) / previous.close
        if abs(change) <= config.suspicious_gap_threshold:
            continue
        if current.trade_date in ex_dates:
            continue
        suspicious.append(current.trade_date)
    if suspicious:
        flags.append("suspicious_gap")
        critical = True

    if missing_ratio > ZERO:
        flags.append("missing_sessions")

    return (
        DataQuality(
            history_sessions=history_sessions,
            activity_ratio=activity_ratio,
            missing_ratio=missing_ratio,
            zero_volume_ratio=zero_volume_ratio,
            last_bar_date=last_bar,
            stale_sessions=stale_sessions,
            suspicious_gaps=tuple(suspicious[-5:]),
            flags=tuple(dict.fromkeys(flags)),
            critical=critical,
        ),
        cleaned,
    )
