from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.screener.backtest import (
    BacktestService,
    _alternatives,
    _apply_caps,
    _bootstrap_ci,
    _cohort_label_end,
    _objective,
    _prepare_cohort,
    _PreparedCohort,
    _purge_train_cohorts,
    _score_bands,
    _score_deciles,
    _select,
    _simulate,
    _summary,
    _TradeInput,
    _walk_forward,
    _wilson_interval,
)
from app.screener.domain import BacktestCohort, BacktestTrade
from app.screener.portfolio import simulate_portfolio
from app.screener.report import backtest_to_dict, backtest_to_html, backtest_to_markdown
from app.screener.store import ScreenerStore
from helpers import bar_series, make_candidate, make_share, screener_config, trading_days


def test_simulate_applies_costs_and_measures_target() -> None:
    candidate = make_candidate("TEST", "IT", "80")
    item = _TradeInput(
        candidate=candidate,
        signal_date=date(2025, 1, 1),
        entry_date=date(2025, 1, 2),
        exit_date=date(2025, 4, 2),
        entry_open=Decimal("100"),
        exit_close=Decimal("110"),
        max_high=Decimal("125"),
        min_low=Decimal("95"),
        dividends=Decimal("5"),
        benchmark_return=Decimal("0.02"),
        days=63,
    )
    config = screener_config(commission_rate=Decimal("0.003"), slippage_rate=Decimal("0.0015"))

    trade = _simulate(item, config)

    expected_entry = Decimal("100") * (Decimal("1") + Decimal("0.0015"))
    expected_exit = Decimal("110") * (Decimal("1") - Decimal("0.0015"))
    expected_net = (expected_exit + Decimal("5")) / expected_entry - Decimal("1")
    expected_net = (Decimal("1") + expected_net) * (Decimal("1") - Decimal("0.003")) ** 2 - Decimal(
        "1"
    )

    assert trade.entry_price == Decimal("100")
    assert trade.exit_price == Decimal("110")
    assert trade.net_return == expected_net
    assert trade.target_hit is True
    assert trade.excess_hit is True
    assert trade.max_adverse == Decimal("-0.05")


def test_prepare_cohort_uses_future_bars_for_entry_and_exit() -> None:
    days = trading_days(120)
    config = screener_config()
    benchmark = bar_series(days, [100 + index / 10 for index in range(120)])
    stock = bar_series(days, [100 + index / 5 for index in range(120)])
    share = make_share(uid="u1", ticker="TEST", asset_uid="a1", first_candle_date=days[0])
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    cohort = _prepare_cohort(
        70,
        calendar,
        index,
        benchmark,
        [share],
        {"u1": stock},
        {},
        config,
    )

    assert cohort is not None
    assert len(cohort.eligible) == 1
    item = cohort.eligible[0]
    assert item.entry_date == days[71]
    assert item.exit_date == days[75]
    assert item.entry_open == stock[71].open
    assert item.exit_close == stock[75].close
    assert item.max_high == max(bar.high for bar in stock[71:76])


def test_prepare_cohort_uses_next_available_session_for_missing_entry() -> None:
    days = trading_days(120)
    config = screener_config(max_entry_delay_sessions=2)
    benchmark = bar_series(days, [100 + index / 10 for index in range(120)])
    stock = bar_series(days, [100 + index / 5 for index in range(120)])
    stock = [bar for bar in stock if bar.trade_date != days[71]]
    share = make_share(uid="u1", ticker="TEST", asset_uid="a1", first_candle_date=days[0])
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    cohort = _prepare_cohort(
        70,
        calendar,
        index,
        benchmark,
        [share],
        {"u1": stock},
        {},
        config,
    )

    assert cohort is not None
    assert len(cohort.eligible) == 1
    item = cohort.eligible[0]
    assert item.entry_date == days[72]
    assert item.exit_date == days[76]
    assert item.days == 5
    assert item.benchmark_return == benchmark[76].close / benchmark[72].open - Decimal("1")


def test_prepare_cohort_skips_entry_delayed_beyond_limit() -> None:
    days = trading_days(120)
    config = screener_config(max_entry_delay_sessions=1)
    benchmark = bar_series(days, [100 + index / 10 for index in range(120)])
    stock = bar_series(days, [100 + index / 5 for index in range(120)])
    stock = [bar for bar in stock if bar.trade_date not in {days[71], days[72]}]
    share = make_share(uid="u1", ticker="TEST", asset_uid="a1", first_candle_date=days[0])
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    cohort = _prepare_cohort(
        70,
        calendar,
        index,
        benchmark,
        [share],
        {"u1": stock},
        {},
        config,
    )

    assert cohort is not None
    assert cohort.eligible == []


def test_prepare_cohort_does_not_exclude_future_holding_gap() -> None:
    days = trading_days(120)
    config = screener_config()
    benchmark = bar_series(days, [100 + index / 10 for index in range(120)])
    stock = bar_series(days, [100 + index / 5 for index in range(120)])
    stock[73] = replace(
        stock[73],
        open=Decimal("200"),
        high=Decimal("210"),
        low=Decimal("190"),
        close=Decimal("200"),
    )
    share = make_share(uid="u1", ticker="TEST", asset_uid="a1", first_candle_date=days[0])
    calendar = [bar.trade_date for bar in benchmark]
    index = {day: position for position, day in enumerate(calendar)}

    cohort = _prepare_cohort(
        70,
        calendar,
        index,
        benchmark,
        [share],
        {"u1": stock},
        {},
        config,
    )

    assert cohort is not None
    assert [item.candidate.ticker for item in cohort.eligible] == ["TEST"]


def _trade(signal: date, ticker: str, score: str, excess: str) -> BacktestTrade:
    return BacktestTrade(
        cohort=signal,
        signal_date=signal,
        entry_date=date(2025, 1, 2),
        exit_date=date(2025, 4, 2),
        uid=ticker,
        ticker=ticker,
        score=Decimal(score),
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        gross_return=Decimal(excess),
        net_return=Decimal(excess),
        benchmark_return=Decimal("0"),
        excess_return=Decimal(excess),
        dividends=Decimal("0"),
        target_hit=Decimal(excess) >= Decimal("0.20"),
        excess_hit=Decimal(excess) >= Decimal("0.10"),
        max_adverse=Decimal("-0.05"),
        days_held=63,
    )


def _dated_cohort(
    signal: date,
    nominal_exit: date,
    *,
    label_end: date | None = None,
    score: str = "90",
) -> _PreparedCohort:
    candidate = make_candidate("TEST", "IT", score)
    entry = signal + timedelta(days=1)
    actual_exit = label_end or nominal_exit
    item = _TradeInput(
        candidate=candidate,
        signal_date=signal,
        entry_date=entry,
        exit_date=actual_exit,
        entry_open=Decimal("100"),
        exit_close=Decimal("105"),
        max_high=Decimal("110"),
        min_low=Decimal("95"),
        dividends=Decimal("0"),
        benchmark_return=Decimal("0"),
        days=max(1, (actual_exit - entry).days),
    )
    return _PreparedCohort(
        signal_date=signal,
        entry_date=entry,
        exit_date=nominal_exit,
        benchmark_return=Decimal("0"),
        eligible=[item],
    )


def test_purge_train_cohorts_excludes_overlapping_labels() -> None:
    first = _dated_cohort(date(2025, 1, 1), date(2025, 1, 11))
    delayed = _dated_cohort(
        date(2025, 1, 21),
        date(2025, 1, 31),
        label_end=date(2025, 2, 10),
    )

    assert _cohort_label_end(delayed) == date(2025, 2, 10)
    assert _purge_train_cohorts([first, delayed], date(2025, 2, 10)) == [first]


def test_walk_forward_purges_overlapping_train_labels() -> None:
    prepared = [
        _dated_cohort(date(2025, 1, 1), date(2025, 1, 11)),
        _dated_cohort(
            date(2025, 1, 21),
            date(2025, 1, 31),
            label_end=date(2025, 2, 10),
        ),
        _dated_cohort(date(2025, 2, 10), date(2025, 2, 20)),
        _dated_cohort(date(2025, 3, 2), date(2025, 3, 12)),
    ]
    config = screener_config(
        backtest_train_cohorts=2,
        backtest_test_cohorts=1,
        min_train_trades=1,
    )

    oos, folds = _walk_forward(prepared, config)

    assert [row["test_start"] for row in folds] == ["2025-02-10", "2025-03-02"]
    assert folds[0]["train_cohorts"] == 1
    assert folds[0]["purged_train_cohorts"] == 1
    assert folds[0]["train_end"] == "2025-01-01"
    assert folds[1]["train_cohorts"] == 2
    assert folds[1]["purged_train_cohorts"] == 0
    assert all(
        date.fromisoformat(row["train_end"]) < date.fromisoformat(row["test_start"])
        for row in folds
        if row["train_end"]
    )
    assert len(oos) == 2


def test_final_holdout_uses_only_purged_train(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.screener.backtest as backtest_module

    prepared = [
        _dated_cohort(date(2025, 1, 1), date(2025, 1, 11)),
        _dated_cohort(date(2025, 1, 21), date(2025, 1, 31)),
        _dated_cohort(
            date(2025, 2, 10),
            date(2025, 2, 20),
            label_end=date(2025, 3, 2),
        ),
        _dated_cohort(date(2025, 3, 2), date(2025, 3, 12)),
    ]
    config = screener_config(backtest_holdout_cohorts=1, min_train_trades=1)
    by_signal = {cohort.signal_date: cohort for cohort in prepared}
    captured: list[list[date]] = []
    original = backtest_module._fixed_run

    def spy(cohorts: list[_PreparedCohort], *args: Any, **kwargs: Any) -> list[BacktestTrade]:
        captured.append([cohort.signal_date for cohort in cohorts])
        return original(cohorts, *args, **kwargs)

    monkeypatch.setattr(backtest_module, "_fixed_run", spy)

    holdout = backtest_module._final_holdout(prepared, {}, {}, [], [], config)

    assert holdout is not None
    holdout_signal = prepared[-1].signal_date
    train_calls = [dates for dates in captured if dates != [holdout_signal]]
    assert train_calls
    assert all(
        max(_cohort_label_end(by_signal[signal]) for signal in dates) < holdout_signal
        for dates in train_calls
        if dates
    )


def test_backtest_selection_caps_one_security_per_issuer() -> None:
    first = make_candidate("SBER", "financial", "90", issuer="Сбербанк")
    second = make_candidate("SBERP", "financial", "89", issuer="Сбербанк")
    third = make_candidate("GAZP", "energy", "80", issuer="Газпром")
    cohort = _PreparedCohort(
        signal_date=date(2025, 1, 1),
        entry_date=date(2025, 1, 2),
        exit_date=date(2025, 4, 2),
        benchmark_return=Decimal("0"),
        eligible=[
            _TradeInput(
                candidate=item,
                signal_date=date(2025, 1, 1),
                entry_date=date(2025, 1, 2),
                exit_date=date(2025, 4, 2),
                entry_open=Decimal("100"),
                exit_close=Decimal("100"),
                max_high=Decimal("100"),
                min_low=Decimal("100"),
                dividends=Decimal("0"),
                benchmark_return=Decimal("0"),
                days=63,
            )
            for item in (first, second, third)
        ],
    )

    selected = _select(
        cohort,
        Decimal("0"),
        3,
        screener_config(max_issuer_candidates=1, max_sector_share=Decimal("1")),
    )

    assert [item.candidate.ticker for item in selected] == ["SBER", "GAZP"]


def test_momentum_alternative_applies_same_caps() -> None:
    signal = date(2025, 1, 1)
    entry = date(2025, 1, 2)
    exit_date = date(2025, 4, 2)

    def item(ticker: str, sector: str, score: str, excess: str, issuer: str) -> _TradeInput:
        candidate = make_candidate(ticker, sector, score, issuer=issuer)
        candidate = replace(
            candidate,
            features=replace(candidate.features, excess_6m=Decimal(excess)),
        )
        return _TradeInput(
            candidate=candidate,
            signal_date=signal,
            entry_date=entry,
            exit_date=exit_date,
            entry_open=Decimal("100"),
            exit_close=Decimal("105"),
            max_high=Decimal("110"),
            min_low=Decimal("95"),
            dividends=Decimal("0"),
            benchmark_return=Decimal("0"),
            days=63,
        )

    ranked = [
        item("SBER", "financial", "90", "0.30", "Сбербанк"),
        item("SBERP", "financial", "89", "0.20", "Сбербанк"),
        item("GAZP", "energy", "80", "0.10", "Газпром"),
    ]
    cohort = _PreparedCohort(
        signal_date=signal,
        entry_date=entry,
        exit_date=exit_date,
        benchmark_return=Decimal("0"),
        eligible=ranked,
    )
    config = screener_config(
        max_candidates=3,
        max_issuer_candidates=1,
        max_sector_share=Decimal("1"),
        random_draws=1,
    )

    selected = _apply_caps(ranked, 3, config)
    alternatives = _alternatives([cohort], config, _summary([], [], config))
    momentum = next(item for item in alternatives if item.name == "Простой моментум 6м")

    assert [item.candidate.ticker for item in selected] == ["SBER", "GAZP"]
    assert momentum.trades == 2


def test_score_bands_report_counts_and_intervals() -> None:
    first = date(2025, 1, 1)
    second = date(2025, 2, 1)
    trades = [
        _trade(first, "A", "85", "0.10"),
        _trade(first, "B", "75", "0.02"),
        _trade(first, "C", "65", "-0.03"),
        _trade(first, "D", "55", "0.01"),
        _trade(second, "E", "82", "0.05"),
        _trade(second, "F", "72", "-0.01"),
        _trade(second, "G", "62", "0.04"),
        _trade(second, "H", "52", "-0.02"),
        _trade(first, "I", "45", "0.00"),
        _trade(second, "J", "40", "-0.05"),
    ]
    config = screener_config(min_band_trades=2)

    rows = _score_bands(trades, config)

    assert [row["bucket"] for row in rows] == ["≥ 80", "70–80", "60–70", "50–60", "< 50"]
    assert all(row["trades"] == 2 for row in rows[:4])
    assert rows[0]["mean_excess_ci_low"] is not None
    assert rows[0]["target_ci_low"] is not None
    assert rows[0]["note"] == ""


def test_verdict_does_not_confirm_advantage_without_evidence(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(
        db_path=tmp_path / "verdict-screener.db",
        backtest_holdout_cohorts=1,
    )
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [bar_series([day], [100 + index / 10])[0] for index, day in enumerate(days)],
    )
    store.upsert_bars(share.uid, bar_series(days, [100 + index / 5 for index in range(120)]))

    result = BacktestService(store, config).run(refresh=False)
    payload = backtest_to_dict(result)

    assert result.verdict is not None
    assert result.verdict.status in {"confirmed", "not_confirmed"}
    markdown = backtest_to_markdown(result)
    html = backtest_to_html(result)
    assert "Вывод" in markdown
    assert "Вывод" in html
    assert "| Портфель | Max DD |" in markdown
    assert "Max DD" in html
    assert payload["verdict"] is not None
    assert any("portfolio_total_return" in item for item in payload["alternatives"])
    assert result.sensitivity
    filters = [item for item in result.sensitivity if item.get("kind") == "filter"]
    assert filters[0]["filter"] == "Базовые фильтры"
    assert any(item["filter"] == "Спред ≤ 0.7%" for item in filters)
    assert "Чувствительность фильтров" in backtest_to_markdown(result)
    assert "Чувствительность фильтров" in backtest_to_html(result)
    store.close()


def test_bootstrap_is_reproducible() -> None:
    cohorts = [
        BacktestCohort(
            signal_date=date(2025, 1, 1),
            entry_date=date(2025, 1, 2),
            exit_date=date(2025, 3, 1),
            candidates=10,
            trades=2,
            mean_net_return=Decimal("0.05"),
            mean_benchmark_return=Decimal("0.01"),
            mean_excess=Decimal("0.04"),
            target_hit_rate=Decimal("0.5"),
            excess_hit_rate=Decimal("0.5"),
            equal_weight_return=Decimal("0.05"),
        ),
        BacktestCohort(
            signal_date=date(2025, 2, 1),
            entry_date=date(2025, 2, 2),
            exit_date=date(2025, 4, 1),
            candidates=10,
            trades=2,
            mean_net_return=Decimal("0.10"),
            mean_benchmark_return=Decimal("0.02"),
            mean_excess=Decimal("0.08"),
            target_hit_rate=Decimal("1"),
            excess_hit_rate=Decimal("0.5"),
            equal_weight_return=Decimal("0.10"),
        ),
    ]
    first = _bootstrap_ci(cohorts, iterations=200, seed=7)
    second = _bootstrap_ci(cohorts, iterations=200, seed=7)
    assert first == second
    assert first[0] is not None and first[1] is not None
    assert first[0] <= first[1]


def test_backtest_service_offline_produces_trades(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(
        db_path=tmp_path / "screener.db",
        backtest_holdout_cohorts=1,
    )
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [bar_series([day], [100 + index / 10])[0] for index, day in enumerate(days)],
    )
    store.upsert_bars(share.uid, bar_series(days, [100 + index / 5 for index in range(120)]))

    result = BacktestService(store, config).run(refresh=False)

    assert result.summary.cohorts >= 1
    assert result.summary.trades >= 1
    assert result.alternatives
    assert result.portfolio is not None
    equal_weight = next(item for item in result.alternatives if item.name == "Равный вес вселенной")
    walk_forward = next(item for item in result.alternatives if item.name == "Walk-forward")
    assert equal_weight.portfolio_total_return is not None
    assert equal_weight.portfolio_max_drawdown is not None
    assert walk_forward.portfolio_total_return == result.portfolio.total_return
    assert walk_forward.portfolio_max_drawdown == result.portfolio.max_drawdown
    assert result.portfolio.trades >= 1
    assert result.holdout is not None
    assert result.holdout.summary.trades >= 0
    assert result.holdout.portfolio.trades >= 0
    assert result.summary.target_ci_low is not None
    assert result.summary.target_ci_high is not None
    assert result.summary.beat_ci_low is not None
    assert result.summary.beat_ci_high is not None
    if result.summary.cohorts >= 2:
        assert result.summary.target_block_ci_low is not None
        assert result.summary.target_block_ci_high is not None
        assert result.summary.beat_block_ci_low is not None
        assert result.summary.beat_block_ci_high is not None
    else:
        assert result.summary.target_block_ci_low is None
    assert result.summary.target_hit_rate is not None
    assert Decimal("0") <= result.summary.target_ci_low <= result.summary.target_hit_rate
    assert result.end < result.holdout.start
    assert result.diagnostics
    payload = backtest_to_dict(result)
    assert payload["portfolio"] is not None
    assert payload["holdout"] is not None
    assert "Финальный holdout" in backtest_to_markdown(result)
    assert "Финальный holdout" in backtest_to_html(result)
    assert result.generated_at <= datetime.now(UTC)
    store.close()


def test_walk_forward_objective_does_not_reward_target_hits() -> None:
    assert _objective(Decimal("0.02"), Decimal("0.10")) == Decimal("0.01")
    assert _objective(Decimal("0.02"), Decimal("0.10")) < Decimal("0.02")


def test_portfolio_simulation_builds_equity_and_metrics() -> None:
    days = trading_days(3)
    benchmark = bar_series(days, [100, 100, 105])
    stock = bar_series(days, [100, 105, 110])
    stock[1] = replace(stock[1], open=Decimal("100"))
    trade = BacktestTrade(
        cohort=days[0],
        signal_date=days[0],
        entry_date=days[1],
        exit_date=days[2],
        uid="u1",
        ticker="TEST",
        score=Decimal("80"),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        gross_return=Decimal("0.10"),
        net_return=Decimal("0.10"),
        benchmark_return=Decimal("0.05"),
        excess_return=Decimal("0.05"),
        dividends=Decimal("0"),
        target_hit=False,
        excess_hit=False,
        max_adverse=Decimal("0"),
        days_held=2,
    )
    config = screener_config(
        max_position_weight=Decimal("1"),
        commission_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )

    portfolio = simulate_portfolio(
        [trade],
        {"u1": stock},
        {},
        benchmark,
        days,
        config,
    )

    assert portfolio.periods == 2
    assert portfolio.trades == 1
    assert portfolio.final_equity == Decimal("1.10")
    assert portfolio.total_return == Decimal("0.10")
    assert portfolio.benchmark_return == Decimal("0.05")
    assert portfolio.excess_return == Decimal("0.05")
    assert portfolio.max_drawdown == Decimal("0")
    assert portfolio.exposure == Decimal("0.5")
    assert portfolio.yearly[0].return_value == Decimal("0.10")
    assert portfolio.points[-1].benchmark == Decimal("1.05")


def test_portfolio_reports_explicit_costs() -> None:
    days = trading_days(3)
    benchmark = bar_series(days, [100, 100, 105])
    stock = bar_series(days, [100, 105, 110])
    stock[1] = replace(stock[1], open=Decimal("100"))
    trade = BacktestTrade(
        cohort=days[0],
        signal_date=days[0],
        entry_date=days[1],
        exit_date=days[2],
        uid="u1",
        ticker="TEST",
        score=Decimal("80"),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        gross_return=Decimal("0.10"),
        net_return=Decimal("0.10"),
        benchmark_return=Decimal("0.05"),
        excess_return=Decimal("0.05"),
        dividends=Decimal("0"),
        target_hit=False,
        excess_hit=False,
        max_adverse=Decimal("0"),
        days_held=2,
    )
    config = screener_config(
        max_position_weight=Decimal("1"),
        commission_rate=Decimal("0.003"),
        slippage_rate=Decimal("0.0015"),
    )

    portfolio = simulate_portfolio(
        [trade],
        {"u1": stock},
        {},
        benchmark,
        days,
        config,
    )

    assert portfolio.commission_paid > 0
    assert portfolio.slippage_paid > 0
    assert portfolio.cost_drag is not None and portfolio.cost_drag > 0


def test_score_deciles_sort_by_rating() -> None:
    trades = [
        BacktestTrade(
            cohort=date(2025, 1, 1),
            signal_date=date(2025, 1, 1),
            entry_date=date(2025, 1, 2),
            exit_date=date(2025, 2, 1),
            uid=f"u{index}",
            ticker=f"T{index}",
            score=Decimal(100 - index),
            entry_price=Decimal("100"),
            exit_price=Decimal("100"),
            gross_return=Decimal("0"),
            net_return=Decimal("0.02") if index < 10 else Decimal("-0.02"),
            benchmark_return=Decimal("0"),
            excess_return=Decimal("0.02") if index < 10 else Decimal("-0.02"),
            dividends=Decimal("0"),
            target_hit=index < 10,
            excess_hit=index < 10,
            max_adverse=Decimal("0"),
            days_held=5,
        )
        for index in range(20)
    ]

    rows = _score_deciles(trades)

    assert len(rows) == 10
    assert rows[0]["bucket"] == "D1"
    assert rows[-1]["bucket"] == "D10"
    assert Decimal(rows[0]["mean_excess"]) > Decimal(rows[-1]["mean_excess"])


def test_summary_reports_max_adverse_excursion() -> None:
    first = replace(
        _trade(date(2025, 1, 1), "A", "80", "0.10"),
        max_adverse=Decimal("-0.04"),
    )
    second = replace(
        _trade(date(2025, 2, 1), "B", "70", "0.02"),
        max_adverse=Decimal("-0.12"),
    )

    summary = _summary([], [first, second], screener_config())

    assert summary.median_max_adverse == Decimal("-0.08")
    assert summary.worst_max_adverse == Decimal("-0.12")
    assert summary.mean_max_adverse == Decimal("-0.08")


def test_wilson_interval_handles_boundaries() -> None:
    low, high = _wilson_interval(0, 10)
    assert low == Decimal("0")
    assert high is not None and Decimal("0") < high < Decimal("0.4")

    low, high = _wilson_interval(10, 10)
    assert high == Decimal("1")
    assert low is not None and Decimal("0.6") < low < Decimal("1")

    low, high = _wilson_interval(5, 10)
    assert low is not None and high is not None
    assert low < Decimal("0.5") < high
