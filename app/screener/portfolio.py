"""Deterministic portfolio simulation over walk-forward trades."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.screener.config import ScreenerConfig
from app.screener.domain import (
    BacktestPortfolio,
    BacktestPortfolioPoint,
    BacktestPortfolioYear,
    BacktestTrade,
    Bar,
    Dividend,
)
from app.screener.features import mean, stdev
from app.screener.quality import dividend_ex_dates

ZERO = Decimal("0")
ONE = Decimal("1")
SQRT_252 = Decimal(252).sqrt()


@dataclass(slots=True)
class _Position:
    uid: str
    shares: Decimal
    entry_date: date
    exit_date: date
    entry_price: Decimal
    exit_price: Decimal


def _price_maps(
    bars_by_uid: Mapping[str, Sequence[Bar]],
) -> tuple[dict[str, dict[date, Decimal]], dict[str, dict[date, Decimal]]]:
    closes: dict[str, dict[date, Decimal]] = {}
    opens: dict[str, dict[date, Decimal]] = {}
    for uid, bars in bars_by_uid.items():
        closes[uid] = {bar.trade_date: bar.close for bar in bars if bar.close > ZERO}
        opens[uid] = {bar.trade_date: bar.open for bar in bars if bar.open > ZERO}
    return closes, opens


def _dividend_maps(
    dividends_by_uid: Mapping[str, Sequence[Dividend]],
    calendar: Sequence[date],
) -> dict[str, dict[date, Decimal]]:
    return {
        uid: {
            day: amount
            for day, amount in dividend_ex_dates(dividends, calendar).items()
            if amount > ZERO
        }
        for uid, dividends in dividends_by_uid.items()
    }


def _max_drawdown(points: Sequence[BacktestPortfolioPoint], initial: Decimal) -> Decimal:
    peak = initial
    result = ZERO
    for point in points:
        if point.equity > peak:
            peak = point.equity
        if peak > ZERO:
            result = max(result, (peak - point.equity) / peak)
    return result


def _year_rows(
    points: Sequence[BacktestPortfolioPoint],
) -> tuple[BacktestPortfolioYear, ...]:
    grouped: dict[int, list[BacktestPortfolioPoint]] = defaultdict(list)
    for point in points:
        grouped[point.trade_date.year].append(point)
    rows: list[BacktestPortfolioYear] = []
    previous_equity = ONE
    previous_benchmark: Decimal | None = ONE
    for year in sorted(grouped):
        year_points = grouped[year]
        last = year_points[-1]
        start_equity = previous_equity
        return_value = last.equity / start_equity - ONE if start_equity > ZERO else None
        benchmark_return = None
        if (
            last.benchmark is not None
            and previous_benchmark is not None
            and previous_benchmark > ZERO
        ):
            benchmark_return = last.benchmark / previous_benchmark - ONE
        peak = start_equity
        drawdown = ZERO
        for point in year_points:
            peak = max(peak, point.equity)
            if peak > ZERO:
                drawdown = max(drawdown, (peak - point.equity) / peak)
        rows.append(
            BacktestPortfolioYear(
                year=year,
                return_value=return_value,
                benchmark_return=benchmark_return,
                max_drawdown=drawdown,
            )
        )
        previous_equity = last.equity
        if last.benchmark is not None:
            previous_benchmark = last.benchmark
    return tuple(rows)


def _metrics(
    points: Sequence[BacktestPortfolioPoint],
) -> tuple[
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
]:
    if not points:
        return None, None, None, None, None, None
    returns: list[Decimal] = []
    previous = ONE
    for point in points:
        if previous > ZERO:
            returns.append(point.equity / previous - ONE)
        previous = point.equity
    deviation = stdev(returns)
    average = mean(returns)
    volatility = deviation * SQRT_252 if deviation is not None else None
    sharpe = None
    if average is not None and deviation is not None and deviation > ZERO:
        sharpe = average / deviation * SQRT_252
    downside = [min(value, ZERO) ** 2 for value in returns]
    downside_deviation = None
    if downside:
        downside_deviation = (sum(downside, ZERO) / Decimal(len(downside))).sqrt() * SQRT_252
    sortino = None
    if average is not None and downside_deviation is not None and downside_deviation > ZERO:
        sortino = average / downside_deviation * SQRT_252
    total = points[-1].equity / points[0].equity - ONE if points[0].equity > ZERO else None
    return total, volatility, sharpe, sortino, downside_deviation, average


def simulate_portfolio(
    trades: Sequence[BacktestTrade],
    bars_by_uid: Mapping[str, Sequence[Bar]],
    dividends_by_uid: Mapping[str, Sequence[Dividend]],
    benchmark_bars: Sequence[Bar],
    calendar: Sequence[date],
    config: ScreenerConfig,
) -> BacktestPortfolio:
    """Simulate equal-weight cohort sleeves with cash and transaction costs.

    Each cohort allocates ``min(max_position_weight, 1 / names)`` to every
    selected name. If cohorts overlap, later entries use only cash still
    available; the portfolio never uses leverage.
    """

    if not trades:
        return BacktestPortfolio(
            initial_capital=ONE,
            final_equity=ONE,
            total_return=None,
            benchmark_return=None,
            excess_return=None,
            cagr=None,
            volatility=None,
            sharpe=None,
            sortino=None,
            max_drawdown=None,
            turnover=None,
            exposure=None,
            periods=0,
            trades=0,
            points=(),
            yearly=(),
            warning="Нет сделок для портфельной симуляции.",
        )

    ordered = sorted(
        trades,
        key=lambda item: (item.entry_date, item.signal_date, item.ticker, item.uid),
    )
    first_entry = ordered[0].entry_date
    last_exit = max(item.exit_date for item in ordered)
    days = [day for day in calendar if first_entry <= day <= last_exit]
    if not days:
        days = sorted({first_entry, last_exit})

    closes, opens = _price_maps(bars_by_uid)
    dividend_map = _dividend_maps(dividends_by_uid, calendar)
    benchmark_by_date = {bar.trade_date: bar.close for bar in benchmark_bars if bar.close > ZERO}
    benchmark_open = next(
        (bar.open for bar in benchmark_bars if bar.trade_date == first_entry and bar.open > ZERO),
        None,
    )
    if benchmark_open is None:
        benchmark_open = next(
            (
                bar.close
                for bar in reversed(benchmark_bars)
                if bar.trade_date <= first_entry and bar.close > ZERO
            ),
            None,
        )

    entries: dict[date, dict[date, list[BacktestTrade]]] = defaultdict(lambda: defaultdict(list))
    for trade in ordered:
        entries[trade.entry_date][trade.signal_date].append(trade)

    cash = ONE
    traded = ZERO
    commission_paid = ZERO
    slippage_paid = ZERO
    positions: list[_Position] = []
    last_close: dict[str, Decimal] = {}
    points: list[BacktestPortfolioPoint] = []
    warnings: list[str] = []
    benchmark_last: Decimal | None = None

    for day in days:
        equity_before = cash + sum(
            (
                position.shares * last_close.get(position.uid, position.entry_price)
                for position in positions
            ),
            ZERO,
        )
        for signal_date in sorted(entries.get(day, {})):
            group = entries[day][signal_date]
            weight = min(config.max_position_weight, ONE / Decimal(len(group)))
            for trade in sorted(group, key=lambda item: (item.ticker, item.uid)):
                budget = min(
                    cash / (ONE + config.commission_rate),
                    equity_before * weight,
                )
                if budget <= ZERO:
                    continue
                entry_price = opens.get(trade.uid, {}).get(day, trade.entry_price)
                if entry_price <= ZERO:
                    continue
                effective_entry = entry_price * (ONE + config.slippage_rate)
                shares = budget / effective_entry
                entry_commission = budget * config.commission_rate
                commission_paid += entry_commission
                slippage_paid += shares * (effective_entry - entry_price)
                cash -= budget + entry_commission
                traded += budget
                positions.append(
                    _Position(
                        uid=trade.uid,
                        shares=shares,
                        entry_date=day,
                        exit_date=trade.exit_date,
                        entry_price=entry_price,
                        exit_price=trade.exit_price,
                    )
                )

        for position in positions:
            amount = dividend_map.get(position.uid, {}).get(day)
            if amount is not None and position.entry_date < day <= position.exit_date:
                cash += position.shares * amount

        for uid in {position.uid for position in positions}:
            close = closes.get(uid, {}).get(day)
            if close is not None and close > ZERO:
                last_close[uid] = close

        remaining: list[_Position] = []
        for position in positions:
            if position.exit_date > day:
                remaining.append(position)
                continue
            exit_price = closes.get(position.uid, {}).get(day, position.exit_price)
            if exit_price <= ZERO:
                exit_price = position.exit_price
            gross_proceeds = position.shares * exit_price
            proceeds = gross_proceeds * (ONE - config.slippage_rate)
            exit_commission = proceeds * config.commission_rate
            commission_paid += exit_commission
            slippage_paid += gross_proceeds - proceeds
            cash += proceeds - exit_commission
            traded += proceeds
        positions = remaining

        invested = sum(
            (
                position.shares * last_close.get(position.uid, position.entry_price)
                for position in positions
            ),
            ZERO,
        )
        equity = cash + invested
        if equity <= ZERO:
            warnings.append("Капитал портфеля исчерпан; кривая остановлена.")
            break
        if benchmark_by_date and day in benchmark_by_date:
            benchmark_last = benchmark_by_date[day]
        benchmark_value = (
            benchmark_last / benchmark_open
            if benchmark_last is not None and benchmark_open not in (None, ZERO)
            else None
        )
        points.append(
            BacktestPortfolioPoint(
                trade_date=day,
                equity=equity,
                benchmark=benchmark_value,
                exposure=invested / equity,
            )
        )

    if not points:
        return BacktestPortfolio(
            initial_capital=ONE,
            final_equity=ONE,
            total_return=None,
            benchmark_return=None,
            excess_return=None,
            cagr=None,
            volatility=None,
            sharpe=None,
            sortino=None,
            max_drawdown=None,
            turnover=None,
            exposure=None,
            periods=0,
            trades=len(ordered),
            points=(),
            yearly=(),
            warning="Не удалось построить кривую портфеля.",
        )

    final = points[-1].equity
    total_return = final - ONE
    benchmark_return = points[-1].benchmark - ONE if points[-1].benchmark is not None else None
    excess_return = total_return - benchmark_return if benchmark_return is not None else None
    periods = len(points)
    years = Decimal(periods) / Decimal(252)
    cagr = None
    if final > ZERO and years > ZERO:
        cagr = Decimal(str(float(final) ** (1.0 / float(years)) - 1.0))
    _, volatility, sharpe, sortino, _, _ = _metrics(points)
    max_drawdown = _max_drawdown(points, ONE)
    average_equity = mean([point.equity for point in points])
    turnover = None
    if average_equity is not None and average_equity > ZERO and years > ZERO:
        turnover = traded / average_equity / years
    exposure = mean([point.exposure for point in points])
    total_costs = commission_paid + slippage_paid
    cost_drag = (
        total_costs / average_equity
        if average_equity is not None and average_equity > ZERO
        else None
    )
    if config.backtest_step_sessions < config.horizon_sessions:
        warnings.append(
            "Когорты перекрываются; новые входы ограничены свободным кэшем, "
            "поэтому результат зависит от порядка входов."
        )
    if benchmark_open is None:
        warnings.append("Бенчмарк недоступен для портфельной кривой.")
    return BacktestPortfolio(
        initial_capital=ONE,
        final_equity=final,
        total_return=total_return,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        turnover=turnover,
        exposure=exposure,
        periods=periods,
        trades=len(ordered),
        points=tuple(points),
        yearly=_year_rows(points),
        commission_paid=commission_paid,
        slippage_paid=slippage_paid,
        cost_drag=cost_drag,
        warning=" ".join(warnings) if warnings else None,
    )
