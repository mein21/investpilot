"""Walk-forward validation for the deterministic screener."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import product
from typing import Any, cast

from app.core.config import Settings
from app.screener.config import ScreenerConfig
from app.screener.diagnostics import (
    FEATURE_SPECS,
    block_bootstrap_mean_ci,
    block_bootstrap_rate_ci,
    bootstrap_mean_ci,
    correlation_rows,
    information_coefficients,
    spearman,
)
from app.screener.domain import (
    BacktestAlternative,
    BacktestCohort,
    BacktestHoldout,
    BacktestPortfolio,
    BacktestResult,
    BacktestSummary,
    BacktestTrade,
    BacktestVerdict,
    Bar,
    Candidate,
    Dividend,
    FeatureSet,
    ShareRecord,
)
from app.screener.features import compute_features, mean, median, stdev
from app.screener.market import compute_regime
from app.screener.portfolio import simulate_portfolio
from app.screener.quality import clean_bars, dividend_ex_dates, evaluate_quality
from app.screener.rating import score_features
from app.screener.screen import (
    _load_benchmark,
    _load_instruments,
    _refresh_bars,
    _refresh_dividends,
    build_screener_client,
    select_primary_listings,
)
from app.screener.store import ScreenerStore

LOGGER = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class _HistRecord:
    share: ShareRecord
    features: FeatureSet
    bars: list[Any]
    quality: Any


@dataclass(frozen=True, slots=True)
class _TradeInput:
    candidate: Candidate
    signal_date: date
    entry_date: date
    exit_date: date
    entry_open: Decimal
    exit_close: Decimal
    max_high: Decimal
    min_low: Decimal
    dividends: Decimal
    benchmark_return: Decimal
    days: int


@dataclass(frozen=True, slots=True)
class _PreparedCohort:
    signal_date: date
    entry_date: date
    exit_date: date
    benchmark_return: Decimal
    eligible: list[_TradeInput]
    regime: str = "unknown"


def _prepare_cohort(
    signal_index: int,
    calendar: list[date],
    calendar_index: dict[date, int],
    benchmark_bars: list[Any],
    primary: list[ShareRecord],
    bars_by_uid: dict[str, list[Any]],
    dividends_by_uid: dict[str, list[Dividend]],
    config: ScreenerConfig,
) -> _PreparedCohort | None:
    signal_date = calendar[signal_index]
    entry_index = signal_index + 1
    exit_index = entry_index + config.horizon_sessions - 1
    if exit_index >= len(calendar):
        return None
    entry_date = calendar[entry_index]
    exit_date = calendar[exit_index]
    benchmark_return = benchmark_bars[exit_index].close / benchmark_bars[entry_index].open - ONE
    benchmark_by_date = {bar.trade_date: bar for bar in benchmark_bars}
    entry_limit_index = min(entry_index + config.max_entry_delay_sessions, len(calendar) - 1)
    entry_limit_date = calendar[entry_limit_index]
    records: list[_HistRecord] = []
    raw: list[
        tuple[
            ShareRecord,
            list[Any],
            Decimal,
            Decimal,
            Decimal,
            Decimal,
            Decimal,
            date,
            date,
            int,
            Decimal,
        ]
    ] = []
    for share in primary:
        bars = bars_by_uid.get(share.uid, [])
        if not bars:
            continue
        quality, cleaned = evaluate_quality(
            bars,
            calendar,
            signal_date,
            config,
            dividends=dividends_by_uid.get(share.uid, []),
        )
        if quality.critical:
            continue
        if quality.history_sessions < config.min_history_sessions:
            continue
        if quality.activity_ratio < config.min_activity_ratio:
            continue
        features = compute_features(
            cleaned,
            benchmark_bars,
            calendar,
            calendar_index,
            signal_date,
            config,
            dividends=dividends_by_uid.get(share.uid, []),
        )
        if features is None:
            continue
        turnover = features.median_turnover_60
        if turnover is None or turnover < config.min_median_turnover:
            continue
        if turnover * config.participation_cap < config.assumed_position_rub:
            continue
        if (
            features.max_turnover_share_60 is not None
            and features.max_turnover_share_60 > config.max_single_turnover_share
        ):
            continue
        entry_window, _ = clean_bars(bar for bar in bars if bar.trade_date <= entry_limit_date)
        entry_bar = next(
            (bar for bar in entry_window if bar.trade_date >= entry_date),
            None,
        )
        if entry_bar is None or entry_bar.open <= 0:
            continue
        actual_entry_index = calendar_index.get(entry_bar.trade_date)
        if actual_entry_index is None:
            continue
        target_exit_index = actual_entry_index + config.horizon_sessions - 1
        if target_exit_index >= len(calendar):
            continue
        exit_limit_index = min(
            target_exit_index + config.max_exit_delay_sessions,
            len(calendar) - 1,
        )
        exit_limit_date = calendar[exit_limit_index]
        target_exit_date = calendar[target_exit_index]
        future, _ = clean_bars(bar for bar in bars if bar.trade_date <= exit_limit_date)
        exit_bar = next(
            (bar for bar in future if bar.trade_date >= target_exit_date),
            None,
        )
        if exit_bar is None or exit_bar.close <= 0:
            continue
        actual_exit_index = calendar_index.get(exit_bar.trade_date)
        if actual_exit_index is None or actual_exit_index <= actual_entry_index:
            continue
        holding = [
            bar for bar in future if entry_bar.trade_date <= bar.trade_date <= exit_bar.trade_date
        ]
        if not holding:
            continue
        benchmark_entry = benchmark_by_date.get(entry_bar.trade_date)
        benchmark_exit = benchmark_by_date.get(exit_bar.trade_date)
        if benchmark_entry is None or benchmark_exit is None:
            continue
        item_benchmark_return = benchmark_exit.close / benchmark_entry.open - ONE
        max_high = max(bar.high for bar in holding)
        min_low = min(bar.low for bar in holding)
        ex_dates = dividend_ex_dates(dividends_by_uid.get(share.uid, []), calendar)
        dividends = sum(
            (
                amount
                for day, amount in ex_dates.items()
                if entry_bar.trade_date < day <= exit_bar.trade_date
            ),
            ZERO,
        )
        raw.append(
            (
                share,
                cleaned,
                entry_bar.open,
                exit_bar.close,
                max_high,
                min_low,
                dividends,
                entry_bar.trade_date,
                exit_bar.trade_date,
                actual_exit_index - actual_entry_index + 1,
                item_benchmark_return,
            )
        )
        records.append(_HistRecord(share=share, features=features, bars=cleaned, quality=quality))

    if not records:
        return _PreparedCohort(
            signal_date=signal_date,
            entry_date=entry_date,
            exit_date=exit_date,
            benchmark_return=benchmark_return,
            eligible=[],
        )

    sector_groups: dict[str, list[_HistRecord]] = defaultdict(list)
    for record in records:
        sector_groups[record.share.sector or "other"].append(record)
    medians: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for sector, items in sector_groups.items():
        if len(items) < config.sector_min_peers:
            continue
        ret3 = median([item.features.ret_3m for item in items if item.features.ret_3m is not None])
        ret6 = median([item.features.ret_6m for item in items if item.features.ret_6m is not None])
        medians[sector] = (ret3, ret6)
    contextual: list[_HistRecord] = []
    for record in records:
        median3, median6 = medians.get(record.share.sector or "other", (None, None))
        features = record.features
        sector3 = (
            features.ret_3m - median3
            if features.ret_3m is not None and median3 is not None
            else None
        )
        sector6 = (
            features.ret_6m - median6
            if features.ret_6m is not None and median6 is not None
            else None
        )
        contextual.append(
            replace(
                record,
                features=replace(
                    features,
                    sector_excess_3m=sector3,
                    sector_excess_6m=sector6,
                ),
            )
        )
    all_features = [record.features for record in contextual]
    scored: dict[str, Candidate] = {}
    for record in contextual:
        breakdown = score_features(record.features, all_features, config)
        if breakdown.score is None:
            continue
        scored[record.share.uid] = Candidate(
            share=record.share,
            bar=record.bars[-1],
            features=record.features,
            quality=record.quality,
            blocks=breakdown.blocks,
            score=breakdown.score,
            penalties=breakdown.penalties,
            reasons=breakdown.reasons,
            warnings=breakdown.warnings,
        )
    eligible: list[_TradeInput] = []
    for (
        share,
        _,
        entry_open,
        exit_close,
        max_high,
        min_low,
        dividends,
        actual_entry_date,
        actual_exit_date,
        days,
        item_benchmark_return,
    ) in raw:
        candidate = scored.get(share.uid)
        if candidate is None:
            continue
        eligible.append(
            _TradeInput(
                candidate=candidate,
                signal_date=signal_date,
                entry_date=actual_entry_date,
                exit_date=actual_exit_date,
                entry_open=entry_open,
                exit_close=exit_close,
                max_high=max_high,
                min_low=min_low,
                dividends=dividends,
                benchmark_return=item_benchmark_return,
                days=days,
            )
        )
    return _PreparedCohort(
        signal_date=signal_date,
        entry_date=entry_date,
        exit_date=exit_date,
        benchmark_return=benchmark_return,
        eligible=eligible,
    )


def _apply_caps(
    pool: Sequence[_TradeInput],
    effective_max: int,
    config: ScreenerConfig,
) -> list[_TradeInput]:
    """Apply the same sector and issuer caps to any ranked candidate pool."""
    if effective_max <= 0:
        return []
    sector_limit = max(1, int(Decimal(effective_max) * config.max_sector_share))
    sector_counts: dict[str, int] = {}
    issuer_counts: dict[str, int] = {}
    selected: list[_TradeInput] = []
    for item in pool:
        if len(selected) >= effective_max:
            break
        sector = item.candidate.share.sector or "other"
        if sector_counts.get(sector, 0) >= sector_limit:
            continue
        issuer = item.candidate.share.issuer_key
        if (
            config.max_issuer_candidates > 0
            and issuer_counts.get(issuer, 0) >= config.max_issuer_candidates
        ):
            continue
        selected.append(item)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
    return selected


def _select(
    cohort: _PreparedCohort,
    min_score: Decimal,
    max_candidates: int,
    config: ScreenerConfig,
    *,
    apply_market_filter: bool = True,
) -> list[_TradeInput]:
    effective_min = min_score
    effective_max = max_candidates
    if apply_market_filter and cohort.regime == "risk_off":
        effective_min = max(effective_min, config.risk_off_min_score)
        effective_max = min(effective_max, config.risk_off_max_candidates)
    pool = [item for item in cohort.eligible if item.candidate.score >= effective_min]
    pool.sort(
        key=lambda item: (
            item.candidate.score,
            item.candidate.features.median_turnover_60 or ZERO,
            item.candidate.ticker,
        ),
        reverse=True,
    )
    return _apply_caps(pool, effective_max, config)


def _simulate(item: _TradeInput, config: ScreenerConfig) -> BacktestTrade:
    entry = item.entry_open * (ONE + config.slippage_rate)
    exit_price = item.exit_close * (ONE - config.slippage_rate)
    gross = (item.exit_close + item.dividends) / item.entry_open - ONE
    net = (exit_price + item.dividends) / entry - ONE
    net = (ONE + net) * (ONE - config.commission_rate) ** 2 - ONE
    target_hit = item.max_high >= item.entry_open * (ONE + config.target_return)
    excess = net - item.benchmark_return
    return BacktestTrade(
        cohort=item.signal_date,
        signal_date=item.signal_date,
        entry_date=item.entry_date,
        exit_date=item.exit_date,
        uid=item.candidate.share.uid,
        ticker=item.candidate.ticker,
        score=item.candidate.score,
        entry_price=item.entry_open,
        exit_price=item.exit_close,
        gross_return=gross,
        net_return=net,
        benchmark_return=item.benchmark_return,
        excess_return=excess,
        dividends=item.dividends,
        target_hit=target_hit,
        excess_hit=excess >= config.excess_target,
        max_adverse=item.min_low / item.entry_open - ONE if item.entry_open > 0 else ZERO,
        days_held=item.days,
    )


def _metrics(trades: list[BacktestTrade]) -> dict[str, Decimal | None]:
    if not trades:
        return {
            "mean_net": None,
            "median_net": None,
            "mean_gross": None,
            "median_gross": None,
            "mean_bench": None,
            "mean_excess": None,
            "median_excess": None,
            "cost_drag": None,
            "mean_max_adverse": None,
            "median_max_adverse": None,
            "worst_max_adverse": None,
            "target": None,
            "excess_hit": None,
            "beat": None,
        }
    net = [trade.net_return for trade in trades]
    gross = [trade.gross_return for trade in trades]
    excess = [trade.excess_return for trade in trades]
    bench = [trade.benchmark_return for trade in trades]
    adverse = [trade.max_adverse for trade in trades]
    mean_net = mean(net)
    mean_gross = mean(gross)
    return {
        "mean_net": mean_net,
        "median_net": median(net),
        "mean_gross": mean_gross,
        "median_gross": median(gross),
        "mean_bench": mean(bench),
        "mean_excess": mean(excess),
        "median_excess": median(excess),
        "cost_drag": (
            mean_gross - mean_net if mean_gross is not None and mean_net is not None else None
        ),
        "mean_max_adverse": mean(adverse),
        "median_max_adverse": median(adverse),
        "worst_max_adverse": min(adverse) if adverse else None,
        "target": Decimal(sum(1 for trade in trades if trade.target_hit)) / Decimal(len(trades)),
        "excess_hit": Decimal(sum(1 for trade in trades if trade.excess_hit))
        / Decimal(len(trades)),
        "beat": Decimal(sum(1 for trade in trades if trade.net_return > trade.benchmark_return))
        / Decimal(len(trades)),
    }


def _quantile(values: list[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return ZERO
    index = int((Decimal(len(ordered) - 1) * q).to_integral_value())
    return ordered[max(0, min(index, len(ordered) - 1))]


def _bootstrap_ci(
    cohorts: list[BacktestCohort],
    iterations: int,
    seed: int,
) -> tuple[Decimal | None, Decimal | None]:
    values = [cohort.mean_excess for cohort in cohorts]
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        average = mean(sample)
        if average is not None:
            means.append(average)
    if not means:
        return None, None
    return _quantile(means, Decimal("0.05")), _quantile(means, Decimal("0.95"))


def _cohort_rows(
    prepared: list[_PreparedCohort],
    selected: dict[date, list[BacktestTrade]],
) -> list[BacktestCohort]:
    rows: list[BacktestCohort] = []
    for cohort in prepared:
        trades = selected.get(cohort.signal_date, [])
        if not trades:
            continue
        metrics = _metrics(trades)
        rows.append(
            BacktestCohort(
                signal_date=cohort.signal_date,
                entry_date=cohort.entry_date,
                exit_date=cohort.exit_date,
                candidates=len(cohort.eligible),
                trades=len(trades),
                mean_net_return=metrics["mean_net"] or ZERO,
                mean_benchmark_return=metrics["mean_bench"] or ZERO,
                mean_excess=metrics["mean_excess"] or ZERO,
                target_hit_rate=metrics["target"] or ZERO,
                excess_hit_rate=metrics["excess_hit"] or ZERO,
                equal_weight_return=metrics["mean_net"] or ZERO,
            )
        )
    return rows


def _wilson_interval(
    successes: int,
    total: int,
) -> tuple[Decimal | None, Decimal | None]:
    if total <= 0:
        return None, None
    z = Decimal("1.959963984540054")
    n = Decimal(total)
    p = Decimal(successes) / n
    denominator = ONE + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * (p * (ONE - p) / n + z * z / (4 * n * n)).sqrt() / denominator
    low = max(ZERO, center - margin)
    high = min(ONE, center + margin)
    if successes <= 0:
        low = ZERO
    if successes >= total:
        high = ONE
    return low, high


def _summary(
    cohorts: list[BacktestCohort],
    trades: list[BacktestTrade],
    config: ScreenerConfig,
) -> BacktestSummary:
    metrics = _metrics(trades)
    ci_low, ci_high = _bootstrap_ci(
        cohorts,
        config.bootstrap_iterations,
        config.bootstrap_seed,
    )
    insufficient = (
        len(cohorts) < config.min_backtest_cohorts or len(trades) < config.min_backtest_trades
    )
    warning = None
    if len(cohorts) < config.min_backtest_cohorts:
        warning = (
            f"Недостаточно данных: независимых когорт {len(cohorts)} < "
            f"{config.min_backtest_cohorts}. Частоты и интервалы не интерпретируются."
        )
    elif len(trades) < config.min_backtest_trades:
        warning = (
            f"Недостаточно данных: сделок {len(trades)} < {config.min_backtest_trades}. "
            "Частоты и интервалы не интерпретируются."
        )
    target_ci_low, target_ci_high = _wilson_interval(
        sum(1 for trade in trades if trade.target_hit),
        len(trades),
    )
    beat_ci_low, beat_ci_high = _wilson_interval(
        sum(1 for trade in trades if trade.excess_return > ZERO),
        len(trades),
    )
    grouped: dict[date, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.signal_date].append(trade)
    target_groups = [
        [trade.target_hit for trade in grouped.get(cohort.signal_date, [])] for cohort in cohorts
    ]
    beat_groups = [
        [trade.excess_return > ZERO for trade in grouped.get(cohort.signal_date, [])]
        for cohort in cohorts
    ]
    target_block_low, target_block_high = block_bootstrap_rate_ci(
        target_groups,
        iterations=config.bootstrap_iterations,
        seed=config.bootstrap_seed,
    )
    beat_block_low, beat_block_high = block_bootstrap_rate_ci(
        beat_groups,
        iterations=config.bootstrap_iterations,
        seed=config.bootstrap_seed + 1,
    )
    return BacktestSummary(
        cohorts=len(cohorts),
        trades=len(trades),
        mean_net_return=metrics["mean_net"],
        median_net_return=metrics["median_net"],
        mean_gross_return=metrics["mean_gross"],
        mean_benchmark_return=metrics["mean_bench"],
        mean_excess=metrics["mean_excess"],
        median_excess=metrics["median_excess"],
        cost_drag=metrics["cost_drag"],
        mean_max_adverse=metrics["mean_max_adverse"],
        median_max_adverse=metrics["median_max_adverse"],
        worst_max_adverse=metrics["worst_max_adverse"],
        excess_ci_low=ci_low,
        excess_ci_high=ci_high,
        target_hit_rate=metrics["target"],
        target_ci_low=target_ci_low,
        target_ci_high=target_ci_high,
        target_block_ci_low=target_block_low,
        target_block_ci_high=target_block_high,
        excess_hit_rate=metrics["excess_hit"],
        beat_benchmark_rate=metrics["beat"],
        beat_ci_low=beat_ci_low,
        beat_ci_high=beat_ci_high,
        beat_block_ci_low=beat_block_low,
        beat_block_ci_high=beat_block_high,
        worst_cohort_excess=min((row.mean_excess for row in cohorts), default=None),
        best_cohort_excess=max((row.mean_excess for row in cohorts), default=None),
        insufficient=insufficient,
        sample_warning=warning,
    )


def _alternative(
    name: str,
    trades: list[BacktestTrade],
    note: str = "",
    portfolio: BacktestPortfolio | None = None,
) -> BacktestAlternative:
    metrics = _metrics(trades)
    return BacktestAlternative(
        name=name,
        mean_net_return=metrics["mean_net"],
        mean_excess=metrics["mean_excess"],
        target_hit_rate=metrics["target"],
        excess_hit_rate=metrics["excess_hit"],
        trades=len(trades),
        note=note,
        portfolio_total_return=portfolio.total_return if portfolio is not None else None,
        portfolio_max_drawdown=portfolio.max_drawdown if portfolio is not None else None,
        portfolio_cagr=portfolio.cagr if portfolio is not None else None,
    )


def _group_trades(trades: list[BacktestTrade]) -> dict[date, list[BacktestTrade]]:
    grouped: dict[date, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.signal_date].append(trade)
    return dict(grouped)


def _fixed_run(
    cohorts: list[_PreparedCohort],
    min_score: Decimal,
    max_candidates: int,
    config: ScreenerConfig,
    *,
    apply_market_filter: bool = True,
) -> list[BacktestTrade]:
    result: list[BacktestTrade] = []
    for cohort in cohorts:
        selected = _select(
            cohort,
            min_score,
            max_candidates,
            config,
            apply_market_filter=apply_market_filter,
        )
        result.extend(_simulate(item, config) for item in selected)
    return result


def _parameter_grid(config: ScreenerConfig) -> list[tuple[Decimal, int]]:
    scores = list(config.target_sensitivity)
    counts = list(config.candidates_sensitivity)
    if config.min_score not in scores:
        scores.append(config.min_score)
    if config.max_candidates not in counts:
        counts.append(config.max_candidates)
    return sorted(set(product(scores, counts)), key=lambda item: (item[0], item[1]))


def _objective(mean_excess: Decimal, dispersion: Decimal) -> Decimal:
    return mean_excess - dispersion * Decimal("0.10")


def _cohort_label_end(cohort: _PreparedCohort) -> date:
    """Latest date on which the cohort's outcome could become known."""
    return max((item.exit_date for item in cohort.eligible), default=cohort.exit_date)


def _purge_train_cohorts(
    train: Sequence[_PreparedCohort],
    test_first_signal: date,
) -> list[_PreparedCohort]:
    """Drop training cohorts whose holding period overlaps the test window.

    A training label is only usable if it is fully known before the first test
    signal. Delayed exits are taken into account, not just the nominal horizon.
    """
    return [cohort for cohort in train if _cohort_label_end(cohort) < test_first_signal]


def _walk_forward(
    prepared: list[_PreparedCohort],
    config: ScreenerConfig,
) -> tuple[list[BacktestTrade], list[dict[str, Any]]]:
    grid = _parameter_grid(config)
    oos: list[BacktestTrade] = []
    folds: list[dict[str, Any]] = []
    train_size = max(1, config.backtest_train_cohorts)
    test_size = max(1, config.backtest_test_cohorts)
    for start in range(train_size, len(prepared), test_size):
        raw_train = prepared[start - train_size : start]
        test = prepared[start : start + test_size]
        if not test:
            break
        train = _purge_train_cohorts(raw_train, test[0].signal_date)
        best: tuple[Decimal, int] | None = None
        best_score: Decimal | None = None
        train_rows: list[dict[str, Any]] = []
        for min_score, max_candidates in grid:
            train_trades = _fixed_run(train, min_score, max_candidates, config)
            metrics = _metrics(train_trades)
            if len(train_trades) < config.min_train_trades:
                continue
            mean_excess = metrics["mean_excess"] or ZERO
            target = metrics["target"] or ZERO
            dispersion = stdev([trade.excess_return for trade in train_trades]) or ZERO
            objective = _objective(mean_excess, dispersion)
            train_rows.append(
                {
                    "fold": str(start),
                    "min_score": str(min_score),
                    "max_candidates": max_candidates,
                    "train_trades": len(train_trades),
                    "train_mean_excess": str(mean_excess),
                    "train_target_rate": str(target),
                    "objective": str(objective),
                }
            )
            if best_score is None or objective > best_score:
                best_score = objective
                best = (min_score, max_candidates)
        if best is None:
            best = (config.min_score, config.max_candidates)
        fold_trades: list[BacktestTrade] = []
        for cohort in test:
            for item in _select(cohort, best[0], best[1], config):
                fold_trades.append(_simulate(item, config))
        oos.extend(fold_trades)
        folds.append(
            {
                "kind": "fold",
                "fold": str(start),
                "train_start": str(train[0].signal_date) if train else "",
                "train_end": str(train[-1].signal_date) if train else "",
                "train_cohorts": len(train),
                "purged_train_cohorts": len(raw_train) - len(train),
                "test_start": str(test[0].signal_date),
                "test_end": str(test[-1].exit_date),
                "selected_min_score": str(best[0]),
                "selected_candidates": best[1],
                "trades": len(fold_trades),
                "test_mean_excess": str(_metrics(fold_trades)["mean_excess"] or ZERO),
            }
        )
    return oos, folds


def _final_holdout(
    prepared: list[_PreparedCohort],
    bars_by_uid: dict[str, list[Bar]],
    dividends_by_uid: dict[str, list[Dividend]],
    benchmark_bars: list[Bar],
    calendar: list[date],
    config: ScreenerConfig,
) -> BacktestHoldout | None:
    count = config.backtest_holdout_cohorts
    if count <= 0 or len(prepared) <= count + 1:
        return None
    holdout = prepared[-count:]
    if not holdout:
        return None
    train = _purge_train_cohorts(prepared[:-count], holdout[0].signal_date)
    if len(train) < 2:
        return None
    best: tuple[Decimal, int] | None = None
    best_score: Decimal | None = None
    for min_score, max_candidates in _parameter_grid(config):
        train_trades = _fixed_run(train, min_score, max_candidates, config)
        if len(train_trades) < config.min_train_trades:
            continue
        metrics = _metrics(train_trades)
        mean_excess = metrics["mean_excess"] or ZERO
        dispersion = stdev([trade.excess_return for trade in train_trades]) or ZERO
        objective = _objective(mean_excess, dispersion)
        if best_score is None or objective > best_score:
            best_score = objective
            best = (min_score, max_candidates)
    if best is None:
        best = (config.min_score, config.max_candidates)
    trades = _fixed_run(holdout, best[0], best[1], config)
    cohorts = _cohort_rows(holdout, _group_trades(trades))
    summary = _summary(cohorts, trades, config)
    portfolio = simulate_portfolio(
        trades,
        bars_by_uid,
        dividends_by_uid,
        benchmark_bars,
        calendar,
        config,
    )
    return BacktestHoldout(
        start=holdout[0].signal_date,
        end=holdout[-1].signal_date,
        selected_min_score=best[0],
        selected_candidates=best[1],
        summary=summary,
        cohorts=tuple(cohorts),
        trades=tuple(trades),
        portfolio=portfolio,
    )


def _alternatives(
    test_cohorts: list[_PreparedCohort],
    config: ScreenerConfig,
    selected_summary: BacktestSummary,
    *,
    bars_by_uid: Mapping[str, Sequence[Bar]] | None = None,
    dividends_by_uid: Mapping[str, Sequence[Dividend]] | None = None,
    benchmark_bars: Sequence[Bar] | None = None,
    calendar: Sequence[date] | None = None,
    selected_portfolio: BacktestPortfolio | None = None,
) -> list[BacktestAlternative]:
    output: list[BacktestAlternative] = []
    benchmark_mean = mean([cohort.benchmark_return for cohort in test_cohorts])
    output.append(
        BacktestAlternative(
            name="Бенчмарк",
            mean_net_return=benchmark_mean,
            mean_excess=ZERO if benchmark_mean is not None else None,
            target_hit_rate=None,
            excess_hit_rate=None,
            trades=0,
            note=f"Total return {config.benchmark}.",
        )
    )
    equal_weight = [_simulate(item, config) for cohort in test_cohorts for item in cohort.eligible]
    equal_weight_portfolio: BacktestPortfolio | None = None
    if (
        bars_by_uid is not None
        and dividends_by_uid is not None
        and benchmark_bars is not None
        and calendar is not None
    ):
        equal_weight_portfolio = simulate_portfolio(
            equal_weight,
            bars_by_uid,
            dividends_by_uid,
            benchmark_bars,
            calendar,
            config,
        )
    output.append(
        _alternative(
            "Равный вес вселенной",
            equal_weight,
            "Средняя доходность всех допустимых бумаг каждой когорты; "
            "равный вес на бумагу, без рейтинга.",
            equal_weight_portfolio,
        )
    )
    momentum: list[BacktestTrade] = []
    for cohort in test_cohorts:
        effective_max = config.max_candidates
        if cohort.regime == "risk_off":
            effective_max = min(effective_max, config.risk_off_max_candidates)
        pool = sorted(
            (item for item in cohort.eligible if item.candidate.features.excess_6m is not None),
            key=lambda item: item.candidate.features.excess_6m or ZERO,
            reverse=True,
        )
        chosen = _apply_caps(pool, effective_max, config)
        momentum.extend(_simulate(item, config) for item in chosen)
    output.append(
        _alternative(
            "Простой моментум 6м",
            momentum,
            f"Топ-{config.max_candidates} по превышению доходности за 6 месяцев, "
            "с теми же секторными и эмитентными лимитами.",
        )
    )
    rng = random.Random(config.bootstrap_seed)
    random_trades: list[BacktestTrade] = []
    for _ in range(config.random_draws):
        for cohort in test_cohorts:
            effective_max = config.max_candidates
            if cohort.regime == "risk_off":
                effective_max = min(effective_max, config.risk_off_max_candidates)
            if effective_max <= 0:
                continue
            pool = list(cohort.eligible)
            if not pool:
                continue
            shuffled = list(pool)
            rng.shuffle(shuffled)
            chosen = _apply_caps(shuffled, effective_max, config)
            random_trades.extend(_simulate(item, config) for item in chosen)
    output.append(
        _alternative(
            "Случайный отбор",
            random_trades,
            f"Среднее по {config.random_draws} случайным выборкам того же размера "
            "с теми же секторными и эмитентными лимитами.",
        )
    )
    fixed = _fixed_run(
        test_cohorts,
        config.min_score,
        config.max_candidates,
        config,
    )
    output.append(
        _alternative(
            "Фиксированные параметры",
            fixed,
            "Без walk-forward подбора; базовая линия.",
        )
    )
    output.append(
        _alternative(
            "Top-5 (фикс.)",
            _fixed_run(test_cohorts, config.min_score, 5, config),
            "Пять лучших сигналов на когорту, без подбора числа позиций.",
        )
    )
    output.append(
        _alternative(
            "Top-10 (фикс.)",
            _fixed_run(test_cohorts, config.min_score, 10, config),
            "Десять лучших сигналов на когорту, без подбора числа позиций.",
        )
    )
    output.append(
        _alternative(
            "С рыночным фильтром (фикс.)",
            _fixed_run(
                test_cohorts,
                config.min_score,
                config.max_candidates,
                config,
                apply_market_filter=True,
            ),
            "В risk-off повышается порог score и сокращается число кандидатов.",
        )
    )
    output.append(
        _alternative(
            "Без рыночного фильтра (фикс.)",
            _fixed_run(
                test_cohorts,
                config.min_score,
                config.max_candidates,
                config,
                apply_market_filter=False,
            ),
            "Тот же порог и размер списка во всех режимах рынка.",
        )
    )
    output.append(
        BacktestAlternative(
            name="Walk-forward",
            mean_net_return=selected_summary.mean_net_return,
            mean_excess=selected_summary.mean_excess,
            target_hit_rate=selected_summary.target_hit_rate,
            excess_hit_rate=selected_summary.excess_hit_rate,
            trades=selected_summary.trades,
            note="Параметры выбирались только на прошлых когортах.",
            portfolio_total_return=(
                selected_portfolio.total_return if selected_portfolio is not None else None
            ),
            portfolio_max_drawdown=(
                selected_portfolio.max_drawdown if selected_portfolio is not None else None
            ),
            portfolio_cagr=selected_portfolio.cagr if selected_portfolio is not None else None,
        )
    )
    return output


def _score_deciles(trades: list[BacktestTrade]) -> list[dict[str, Any]]:
    if not trades:
        return []
    ordered = sorted(trades, key=lambda item: (-item.score, item.ticker, item.uid))
    buckets = min(10, len(ordered))
    rows: list[dict[str, Any]] = []
    for bucket in range(buckets):
        start = bucket * len(ordered) // buckets
        end = (bucket + 1) * len(ordered) // buckets
        group = ordered[start:end]
        if not group:
            continue
        metrics = _metrics(group)
        rows.append(
            {
                "kind": "score_decile",
                "bucket": f"D{bucket + 1}",
                "trades": len(group),
                "min_score": str(min(item.score for item in group)),
                "max_score": str(max(item.score for item in group)),
                "mean_net": str(metrics["mean_net"]) if metrics["mean_net"] is not None else None,
                "median_net": str(metrics["median_net"])
                if metrics["median_net"] is not None
                else None,
                "mean_gross": str(metrics["mean_gross"])
                if metrics["mean_gross"] is not None
                else None,
                "mean_excess": str(metrics["mean_excess"])
                if metrics["mean_excess"] is not None
                else None,
                "median_excess": str(metrics["median_excess"])
                if metrics["median_excess"] is not None
                else None,
                "cost_drag": str(metrics["cost_drag"])
                if metrics["cost_drag"] is not None
                else None,
                "target_rate": str(metrics["target"]) if metrics["target"] is not None else None,
                "beat_rate": str(metrics["beat"]) if metrics["beat"] is not None else None,
            }
        )
    return rows


def _score_bands(
    trades: list[BacktestTrade],
    config: ScreenerConfig,
) -> list[dict[str, Any]]:
    edges = sorted(set(config.score_band_edges), reverse=True)
    if not edges:
        return []
    definitions: list[tuple[Decimal | None, Decimal | None, str]] = []
    upper: Decimal | None = None
    for edge in edges:
        definitions.append(
            (
                edge,
                upper,
                f"≥ {edge:.0f}" if upper is None else f"{edge:.0f}–{upper:.0f}",
            )
        )
        upper = edge
    definitions.append((None, upper, f"< {upper:.0f}" if upper is not None else "все"))
    rows: list[dict[str, Any]] = []
    for low, high, label in definitions:
        band = [
            trade
            for trade in trades
            if (low is None or trade.score >= low) and (high is None or trade.score < high)
        ]
        if not band:
            continue
        metrics = _metrics(band)
        by_cohort: dict[date, list[BacktestTrade]] = defaultdict(list)
        for trade in band:
            by_cohort[trade.signal_date].append(trade)
        groups = list(by_cohort.values())
        mean_low, mean_high = block_bootstrap_mean_ci(
            [[trade.excess_return for trade in group] for group in groups],
            iterations=config.bootstrap_iterations,
            seed=config.bootstrap_seed + 3,
        )
        target_low, target_high = block_bootstrap_rate_ci(
            [[trade.target_hit for trade in group] for group in groups],
            iterations=config.bootstrap_iterations,
            seed=config.bootstrap_seed + 4,
        )
        beat_low, beat_high = block_bootstrap_rate_ci(
            [[trade.excess_return > ZERO for trade in group] for group in groups],
            iterations=config.bootstrap_iterations,
            seed=config.bootstrap_seed + 5,
        )
        insufficient = len(band) < config.min_band_trades or len(groups) < 2
        rows.append(
            {
                "kind": "score_band",
                "bucket": label,
                "trades": len(band),
                "cohorts": len(groups),
                "min_score": str(min(item.score for item in band)),
                "max_score": str(max(item.score for item in band)),
                "mean_net": str(metrics["mean_net"]) if metrics["mean_net"] is not None else None,
                "median_net": str(metrics["median_net"])
                if metrics["median_net"] is not None
                else None,
                "mean_excess": str(metrics["mean_excess"])
                if metrics["mean_excess"] is not None
                else None,
                "mean_excess_ci_low": str(mean_low) if mean_low is not None else None,
                "mean_excess_ci_high": str(mean_high) if mean_high is not None else None,
                "target_rate": str(metrics["target"]) if metrics["target"] is not None else None,
                "target_ci_low": str(target_low) if target_low is not None else None,
                "target_ci_high": str(target_high) if target_high is not None else None,
                "beat_rate": str(metrics["beat"]) if metrics["beat"] is not None else None,
                "beat_ci_low": str(beat_low) if beat_low is not None else None,
                "beat_ci_high": str(beat_high) if beat_high is not None else None,
                "insufficient": insufficient,
                "note": "Недостаточно данных" if insufficient else "",
            }
        )
    return rows


def _format_pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    percent = value * HUNDRED
    if percent.quantize(Decimal("0.01")) == ZERO:
        percent = ZERO
    return f"{percent:+.2f}%".replace(".", ",")


def _verdict(
    summary: BacktestSummary,
    alternatives: Sequence[BacktestAlternative],
    holdout: BacktestHoldout | None,
    config: ScreenerConfig,
) -> BacktestVerdict:
    simple_names = {
        "Бенчмарк",
        "Равный вес вселенной",
        "Простой моментум 6м",
        "Случайный отбор",
        "Фиксированные параметры",
        "Top-5 (фикс.)",
        "Top-10 (фикс.)",
    }
    simple = [
        item for item in alternatives if item.name in simple_names and item.mean_excess is not None
    ]
    best = max(simple, key=lambda item: item.mean_excess or ZERO) if simple else None
    details: list[str] = []
    if summary.insufficient:
        return BacktestVerdict(
            status="insufficient_data",
            headline="Недостаточно данных для вывода о преимуществе рейтинга.",
            details=(
                f"Когорт: {summary.cohorts}, сделок: {summary.trades}; "
                f"минимум для вывода: {config.min_backtest_cohorts} когорт и "
                f"{config.min_backtest_trades} сделок.",
            ),
            best_simple_alternative=best.name if best else None,
            best_simple_excess=best.mean_excess if best else None,
        )
    ci_confirms = summary.excess_ci_low is not None and summary.excess_ci_low > ZERO
    details.append(
        "95% ДИ среднего превышения: "
        f"{_format_pct(summary.excess_ci_low)}…{_format_pct(summary.excess_ci_high)}; "
        + ("нижняя граница выше 0." if ci_confirms else "интервал включает 0.")
    )
    beats_simple = False
    if best is not None and best.mean_excess is not None and summary.mean_excess is not None:
        beats_simple = summary.mean_excess > best.mean_excess
        details.append(
            f"Walk-forward превышение {_format_pct(summary.mean_excess)} против лучшей "
            f"простой альтернативы «{best.name}» {_format_pct(best.mean_excess)}."
        )
    holdout_confirms = False
    if holdout is None:
        details.append("Финальный holdout не сформирован.")
    else:
        holdout_confirms = (
            not holdout.summary.insufficient
            and holdout.summary.excess_ci_low is not None
            and holdout.summary.excess_ci_low > ZERO
        )
        details.append(
            f"Holdout: превышение {_format_pct(holdout.summary.mean_excess)}, "
            f"95% ДИ {_format_pct(holdout.summary.excess_ci_low)}…"
            f"{_format_pct(holdout.summary.excess_ci_high)}; "
            + (
                "преимущество подтверждено."
                if holdout_confirms
                else "преимущество не подтверждено."
            )
        )
        if holdout.summary.insufficient:
            details.append(
                f"Holdout мал: когорт — {holdout.summary.cohorts}, "
                f"сделок — {holdout.summary.trades}."
            )
    if ci_confirms and beats_simple and holdout_confirms:
        status = "confirmed"
        headline = "Преимущество рейтинга подтверждено на доступной выборке."
    else:
        status = "not_confirmed"
        headline = "Преимущество рейтинга не подтверждено."
        if not ci_confirms:
            details.append("Среднее превышение статистически неотличимо от нуля.")
        if not beats_simple and best is not None:
            details.append("Рейтинг не превосходит лучшую простую альтернативу.")
        details.append("Рейтинг нельзя трактовать как доказанную стратегию.")
    return BacktestVerdict(
        status=status,
        headline=headline,
        details=tuple(details),
        best_simple_alternative=best.name if best else None,
        best_simple_excess=best.mean_excess if best else None,
    )


BLOCK_FIELDS: dict[str, str] = {
    "relative_strength": "weight_relative_strength",
    "trend_quality": "weight_trend_quality",
    "volume_confirmation": "weight_volume_confirmation",
    "consolidation_breakout": "weight_consolidation_breakout",
    "risk_moderation": "weight_risk_moderation",
}
BLOCK_LABELS: dict[str, str] = {
    "relative_strength": "Относительная сила",
    "trend_quality": "Качество тренда",
    "volume_confirmation": "Подтверждение объёмом",
    "consolidation_breakout": "Консолидация и пробой",
    "risk_moderation": "Умеренность риска",
}


def _diagnostics(
    test_cohorts: list[_PreparedCohort],
    oos_trades: list[BacktestTrade],
    config: ScreenerConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pooled: list[tuple[Any, Decimal]] = []
    cohort_samples: list[list[tuple[Any, Decimal]]] = []
    block_samples: list[list[tuple[Any, Decimal]]] = []
    for cohort in test_cohorts:
        current: list[tuple[Any, Decimal]] = []
        for item in cohort.eligible:
            excess = _simulate(item, config).excess_return
            pooled.append((item.candidate.features, excess))
            current.append((item.candidate, excess))
        cohort_samples.append(current)
        block_samples.append(current)
    rows.extend(
        correlation_rows(
            pooled,
            FEATURE_SPECS,
            min_observations=max(10, config.min_train_trades),
            threshold=Decimal("0.50"),
        )
    )
    feature_cohorts = [
        [(member[0].features, member[1]) for member in current] for current in cohort_samples
    ]
    rows.extend(
        information_coefficients(
            feature_cohorts,
            FEATURE_SPECS,
            min_observations=max(3, config.min_train_trades // 2),
            iterations=config.bootstrap_iterations,
            seed=config.bootstrap_seed,
        )
    )
    for block, label in BLOCK_LABELS.items():
        ics: list[Decimal] = []
        for current in block_samples:
            pairs = [
                (getattr(member[0].blocks, block), member[1])
                for member in current
                if getattr(member[0].blocks, block) is not None
            ]
            if len(pairs) < max(3, config.min_train_trades // 2):
                continue
            ic = spearman(
                [item[0] for item in pairs],
                [item[1] for item in pairs],
            )
            if ic is not None:
                ics.append(ic)
        average = mean(ics)
        low, high = bootstrap_mean_ci(
            ics,
            iterations=config.bootstrap_iterations,
            seed=config.bootstrap_seed + 2,
        )
        rows.append(
            {
                "kind": "block_ic",
                "block": label,
                "cohorts": len(ics),
                "mean_ic": str(average) if average is not None else None,
                "ci_low": str(low) if low is not None else None,
                "ci_high": str(high) if high is not None else None,
            }
        )
    rows.extend(_ablation_rows(test_cohorts, config))
    for regime in ("risk_on", "neutral", "risk_off", "unknown"):
        cohort_days = {cohort.signal_date for cohort in test_cohorts if cohort.regime == regime}
        trades = [trade for trade in oos_trades if trade.signal_date in cohort_days]
        metrics = _metrics(trades)
        rows.append(
            {
                "kind": "regime",
                "regime": regime,
                "cohorts": len(cohort_days),
                "trades": len(trades),
                "mean_net": str(metrics["mean_net"]) if metrics["mean_net"] is not None else None,
                "mean_excess": str(metrics["mean_excess"])
                if metrics["mean_excess"] is not None
                else None,
                "beat_rate": str(metrics["beat"]) if metrics["beat"] is not None else None,
            }
        )
    rows.append(
        {
            "kind": "market_filter",
            "risk_off_cohorts": sum(1 for cohort in test_cohorts if cohort.regime == "risk_off"),
            "total_cohorts": len(test_cohorts),
            "risk_off_min_score": str(config.risk_off_min_score),
            "risk_off_max_candidates": config.risk_off_max_candidates,
            "note": "Сравнение режимов без/с фильтром — в таблице альтернатив.",
        }
    )
    return rows


def _ablation_rows(
    test_cohorts: list[_PreparedCohort],
    config: ScreenerConfig,
) -> list[dict[str, Any]]:
    base = _fixed_run(test_cohorts, config.min_score, config.max_candidates, config)
    base_mean = _metrics(base)["mean_excess"]
    rows: list[dict[str, Any]] = []
    for block, field_name in BLOCK_FIELDS.items():
        ablated_config = replace(config, **cast(Any, {field_name: ZERO}))
        trades: list[BacktestTrade] = []
        for cohort in test_cohorts:
            all_features = [item.candidate.features for item in cohort.eligible]
            candidates: list[_TradeInput] = []
            for item in cohort.eligible:
                scored = score_features(item.candidate.features, all_features, ablated_config)
                if scored.score is None:
                    continue
                candidates.append(
                    replace(
                        item,
                        candidate=replace(
                            item.candidate,
                            score=scored.score,
                            blocks=scored.blocks,
                        ),
                    )
                )
            ablated = _PreparedCohort(
                signal_date=cohort.signal_date,
                entry_date=cohort.entry_date,
                exit_date=cohort.exit_date,
                benchmark_return=cohort.benchmark_return,
                eligible=candidates,
                regime=cohort.regime,
            )
            trades.extend(
                _simulate(item, ablated_config)
                for item in _select(
                    ablated,
                    config.min_score,
                    config.max_candidates,
                    ablated_config,
                )
            )
        metrics = _metrics(trades)
        mean_excess = metrics["mean_excess"]
        delta = (
            mean_excess - base_mean if mean_excess is not None and base_mean is not None else None
        )
        rows.append(
            {
                "kind": "ablation",
                "block": BLOCK_LABELS[block],
                "trades": len(trades),
                "mean_excess": str(mean_excess) if mean_excess is not None else None,
                "delta_vs_base": str(delta) if delta is not None else None,
                "note": "Блок исключён из score; остальные веса без изменений.",
            }
        )
    return rows


def _sensitivity(
    test_cohorts: list[_PreparedCohort],
    config: ScreenerConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for min_score, max_candidates in _parameter_grid(config):
        trades = _fixed_run(test_cohorts, min_score, max_candidates, config)
        metrics = _metrics(trades)
        rows.append(
            {
                "kind": "parameter",
                "min_score": str(min_score),
                "max_candidates": max_candidates,
                "trades": len(trades),
                "mean_excess": str(metrics["mean_excess"])
                if metrics["mean_excess"] is not None
                else None,
                "target_rate": str(metrics["target"]) if metrics["target"] is not None else None,
            }
        )
    return rows


def _filter_cohorts(
    cohorts: list[_PreparedCohort],
    *,
    min_turnover: Decimal | None = None,
    min_activity: Decimal | None = None,
    min_history: int | None = None,
) -> list[_PreparedCohort]:
    filtered: list[_PreparedCohort] = []
    for cohort in cohorts:
        eligible = [
            item
            for item in cohort.eligible
            if (
                min_turnover is None
                or (
                    item.candidate.features.median_turnover_60 is not None
                    and item.candidate.features.median_turnover_60 >= min_turnover
                )
            )
            and (min_activity is None or item.candidate.quality.activity_ratio >= min_activity)
            and (min_history is None or item.candidate.quality.history_sessions >= min_history)
        ]
        filtered.append(replace(cohort, eligible=eligible))
    return filtered


def _filter_sensitivity(
    test_cohorts: list[_PreparedCohort],
    config: ScreenerConfig,
) -> list[dict[str, Any]]:
    base_trades = _fixed_run(test_cohorts, config.min_score, config.max_candidates, config)
    base_metrics = _metrics(base_trades)
    base_mean = base_metrics["mean_excess"]
    variants: list[tuple[str, Decimal | None, int | None, Decimal | None]] = [
        ("Оборот ≥ 45 млн ₽", config.min_median_turnover * Decimal("1.5"), None, None),
        ("Оборот ≥ 60 млн ₽", config.min_median_turnover * Decimal("2"), None, None),
        ("Активность ≥ 98%", None, None, Decimal("0.98")),
        ("История ≥ 300 сессий", None, 300, None),
    ]
    rows: list[dict[str, Any]] = [
        {
            "kind": "filter",
            "filter": "Базовые фильтры",
            "value": "252 / 95% / 30 млн / 0.7%",
            "trades": len(base_trades),
            "mean_excess": str(base_mean) if base_mean is not None else None,
            "target_rate": str(base_metrics["target"])
            if base_metrics["target"] is not None
            else None,
            "delta_vs_base": None,
            "note": "Текущий конфиг.",
        }
    ]
    for label, min_turnover, min_history, min_activity in variants:
        filtered = _filter_cohorts(
            test_cohorts,
            min_turnover=min_turnover,
            min_activity=min_activity,
            min_history=min_history,
        )
        trades = _fixed_run(filtered, config.min_score, config.max_candidates, config)
        metrics = _metrics(trades)
        mean_excess = metrics["mean_excess"]
        delta = (
            mean_excess - base_mean if mean_excess is not None and base_mean is not None else None
        )
        if delta is not None and delta == ZERO:
            delta = ZERO
        rows.append(
            {
                "kind": "filter",
                "filter": label,
                "value": label.split("≥", 1)[1].strip() if "≥" in label else "",
                "trades": len(trades),
                "mean_excess": str(mean_excess) if mean_excess is not None else None,
                "target_rate": str(metrics["target"]) if metrics["target"] is not None else None,
                "delta_vs_base": str(delta) if delta is not None else None,
                "note": "Только ужесточение уже прошедших базовые фильтры.",
            }
        )
    rows.append(
        {
            "kind": "filter",
            "filter": "Спред ≤ 0.7%",
            "value": "н/д",
            "trades": None,
            "mean_excess": None,
            "target_rate": None,
            "delta_vs_base": None,
            "note": "Нет исторического стакана; в бэктесте не применяется.",
        }
    )
    return rows


class BacktestService:
    def __init__(
        self,
        store: ScreenerStore,
        config: ScreenerConfig,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.settings = settings

    def run(
        self,
        *,
        refresh: bool = True,
        offline: bool = False,
        start: date | None = None,
        end: date | None = None,
    ) -> BacktestResult:
        warnings: list[str] = []
        client = None
        if refresh and not offline:
            if self.settings is None:
                raise RuntimeError("Settings are required for a live refresh")
            client = build_screener_client(self.settings, self.config)
        try:
            shares = _load_instruments(
                self.store,
                client,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            benchmark, benchmark_bars = _load_benchmark(
                self.store,
                client,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            primary = select_primary_listings(shares)
            as_of = end or benchmark_bars[-1].trade_date
            benchmark_bars = [bar for bar in benchmark_bars if bar.trade_date <= as_of]
            _refresh_bars(
                self.store,
                client,
                primary,
                as_of,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
                include_inactive=True,
            )
            _refresh_dividends(
                self.store,
                client,
                primary,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
                include_inactive=True,
            )
            bars_by_uid = self.store.load_all_bars()
            dividends_by_uid = self.store.load_dividends()
            inactive_with_history = sum(
                1
                for share in primary
                if not share.is_active and len(bars_by_uid.get(share.uid, [])) > 0
            )
            if inactive_with_history:
                warnings.append(
                    f"В историческую вселенную включено неактивных бумаг с историей: "
                    f"{inactive_with_history}; survivorship bias уменьшен, но не устранён."
                )
            else:
                warnings.append(
                    "В кэше нет истории неактивных бумаг: survivorship bias не уменьшен. "
                    "Для его снижения нужен refresh с INSTRUMENT_STATUS_ALL."
                )
            calendar = [bar.trade_date for bar in benchmark_bars]
            calendar_index = {day: index for index, day in enumerate(calendar)}
            if len(calendar) < self.config.min_history_sessions + self.config.horizon_sessions + 2:
                raise RuntimeError("Недостаточно истории бенчмарка для бэктеста.")
            first_index = self.config.min_history_sessions
            if start is not None:
                first_index = max(
                    first_index,
                    next(
                        (index for index, day in enumerate(calendar) if day >= start), len(calendar)
                    ),
                )
            last_signal = len(calendar) - self.config.horizon_sessions - 2
            indices = list(
                range(
                    first_index,
                    min(last_signal, len(calendar) - 1) + 1,
                    max(1, self.config.backtest_step_sessions),
                )
            )[: self.config.backtest_max_cohorts]
            prepared: list[_PreparedCohort] = []
            for index in indices:
                cohort = _prepare_cohort(
                    index,
                    calendar,
                    calendar_index,
                    benchmark_bars,
                    primary,
                    bars_by_uid,
                    dividends_by_uid,
                    self.config,
                )
                if cohort is not None:
                    regime = compute_regime(
                        self.config.benchmark,
                        benchmark_bars,
                        cohort.signal_date,
                        [item.candidate.features for item in cohort.eligible],
                    )
                    prepared.append(replace(cohort, regime=regime.label))
            if len(prepared) < self.config.min_backtest_cohorts:
                warnings.append(
                    f"Подготовлено только {len(prepared)} когорт; выводы будут слабыми."
                )
            holdout_count = (
                self.config.backtest_holdout_cohorts
                if len(prepared) > self.config.backtest_holdout_cohorts + 1
                else 0
            )
            main_prepared = prepared[:-holdout_count] if holdout_count else prepared
            oos_trades, folds = _walk_forward(main_prepared, self.config)
            portfolio = simulate_portfolio(
                oos_trades,
                bars_by_uid,
                dividends_by_uid,
                benchmark_bars,
                calendar,
                self.config,
            )
            if portfolio.warning:
                warnings.append(portfolio.warning)
            holdout = _final_holdout(
                prepared,
                bars_by_uid,
                dividends_by_uid,
                benchmark_bars,
                calendar,
                self.config,
            )
            if holdout is not None:
                if holdout.summary.sample_warning:
                    warnings.append(f"Holdout: {holdout.summary.sample_warning}")
                if holdout.portfolio.warning:
                    warnings.append(f"Holdout: {holdout.portfolio.warning}")
            fold_test_dates = {
                date.fromisoformat(row["test_start"])
                for row in folds
                if row.get("kind") == "fold" and row.get("test_start")
            }
            test_cohorts = [
                cohort for cohort in main_prepared if cohort.signal_date in fold_test_dates
            ]
            oos_grouped = _group_trades(oos_trades)
            cohorts = _cohort_rows(test_cohorts, oos_grouped)
            summary = _summary(cohorts, oos_trades, self.config)
            alternatives = _alternatives(
                test_cohorts,
                self.config,
                summary,
                bars_by_uid=bars_by_uid,
                dividends_by_uid=dividends_by_uid,
                benchmark_bars=benchmark_bars,
                calendar=calendar,
                selected_portfolio=portfolio,
            )
            sensitivity = (
                folds
                + _sensitivity(test_cohorts, self.config)
                + _filter_sensitivity(test_cohorts, self.config)
                + _score_deciles(oos_trades)
                + _score_bands(oos_trades, self.config)
            )
            diagnostics = _diagnostics(test_cohorts, oos_trades, self.config)
            verdict = _verdict(summary, alternatives, holdout, self.config)
            warnings.extend(
                [
                    "Walk-forward использует только прошлые когорты для выбора параметров; "
                    "текущая когорта — out-of-sample.",
                    "Финальный holdout исключён из walk-forward и не участвовал "
                    "в выборе параметров."
                    if holdout_count
                    else "Финальный holdout не сформирован: слишком мало когорт.",
                    "Цель +20% считается по максимуму цены внутри горизонта, "
                    "доходность и превышение бенчмарка — после издержек.",
                    "Фундаментал не используется в истории: нет point-in-time данных.",
                    "Survivorship bias: список бумаг взят из текущего Shares.",
                    "Исторический спред, торговый статус и квалификационный флаг "
                    "недоступны в T-Invest: в бэктесте они не применяются, "
                    "ликвидность оценивается медианным оборотом и participation cap.",
                    "Сплиты и смена тикеров не корректируются; гэпы ≥35% вне "
                    "дивидендных дат помечаются как подозрительные и исключаются из рейтинга.",
                    "Издержки: комиссия и проскальзывание на вход и выход; "
                    "участие ограничено допущением о размере позиции.",
                ]
            )
            if summary.sample_warning:
                warnings.append(summary.sample_warning)
            if not oos_trades:
                warnings.append("Нет out-of-sample сделок; результаты не интерпретируются.")
            result = BacktestResult(
                start=cohorts[0].signal_date if cohorts else benchmark_bars[0].trade_date,
                end=cohorts[-1].signal_date if cohorts else as_of,
                benchmark=benchmark,
                summary=summary,
                cohorts=tuple(cohorts),
                trades=tuple(oos_trades),
                alternatives=tuple(alternatives),
                sensitivity=tuple(sensitivity),
                warnings=tuple(dict.fromkeys(warnings)),
                generated_at=datetime.now(UTC),
                verdict=verdict,
                config=self.config.to_dict(),
                portfolio=portfolio,
                holdout=holdout,
                diagnostics=tuple(diagnostics),
            )
            from app.screener.report import backtest_to_dict

            self.store.save_run("backtest", backtest_to_dict(result), as_of=as_of)
            return result
        finally:
            if client is not None:
                client.close()
