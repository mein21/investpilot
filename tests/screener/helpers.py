from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.screener.config import ScreenerConfig
from app.screener.domain import Bar, BlockScores, Candidate, DataQuality, FeatureSet, ShareRecord


def trading_days(count: int, start: date = date(2025, 1, 1)) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def bar_series(
    days: list[date],
    closes: Sequence[Decimal | int | float],
    *,
    volume: int = 1000,
    lot: int = 1,
) -> list[Bar]:
    result: list[Bar] = []
    for day, raw in zip(days, closes, strict=True):
        close = Decimal(str(raw))
        result.append(
            Bar(
                trade_date=day,
                open=close,
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume_lots=volume,
                lot=lot,
            )
        )
    return result


def make_share(**overrides: object) -> ShareRecord:
    values: dict[str, object] = {
        "uid": "uid-test",
        "ticker": "TEST",
        "class_code": "TQBR",
        "name": "Test",
        "lot": 1,
        "currency": "RUB",
        "exchange": "MOEX",
        "real_exchange": "REAL_EXCHANGE_MOEX",
        "sector": "IT",
        "share_type": "SHARE_TYPE_COMMON",
        "asset_uid": "asset-test",
        "is_active": True,
        "buy_available": True,
    }
    values.update(overrides)
    return ShareRecord(**values)  # type: ignore[arg-type]


def screener_config(**overrides: Any) -> ScreenerConfig:
    base = replace(
        ScreenerConfig(),
        min_history_sessions=60,
        activity_window=20,
        min_median_turnover=Decimal("1"),
        assumed_position_rub=Decimal("0"),
        min_score=Decimal("0"),
        near_candidate_score=Decimal("0"),
        max_candidates=10,
        fetch_orderbook=False,
        refresh_dividends=False,
        refresh_fundamentals=False,
        backtest_train_cohorts=1,
        backtest_test_cohorts=1,
        backtest_step_sessions=10,
        backtest_max_cohorts=3,
        horizon_sessions=5,
        min_backtest_cohorts=1,
        min_backtest_trades=1,
    )
    return replace(base, **overrides)


def feature(**overrides: object) -> FeatureSet:
    values: dict[str, object] = {
        "as_of": date(2025, 1, 1),
        "close": Decimal("100"),
        "ret_1m": None,
        "ret_3m": None,
        "ret_6m": None,
        "ret_6m_ex_5d": None,
        "ret_12m": None,
        "excess_1m": None,
        "excess_3m": None,
        "excess_6m": None,
        "excess_6m_ex_5d": None,
        "excess_12m": None,
        "sma20": None,
        "sma50": None,
        "sma200": None,
        "above_sma20": None,
        "above_sma50": None,
        "above_sma200": None,
        "sma50_slope_20": None,
        "sma200_slope_20": None,
        "trend_consistency_60": None,
        "drawdown_252": None,
        "median_turnover_20": None,
        "median_turnover_60": None,
        "turnover_ratio_5_60": None,
        "turnover_ratio_20_60": None,
        "up_down_volume_ratio_60": None,
        "turnover_cv_60": None,
        "max_turnover_share_60": None,
        "range_20": None,
        "range_60": None,
        "squeeze_20_60": None,
        "dist_high_60": None,
        "dist_high_63": None,
        "dist_high_126": None,
        "dist_high_252": None,
        "breakout_20": None,
        "breakout_60": None,
        "breakout_63": None,
        "breakout_126": None,
        "breakout_volume_ratio_20": None,
        "volatility_20": None,
        "volatility_60": None,
        "downside_vol_60": None,
        "max_drawdown_126": None,
        "atr_14_pct": None,
        "large_gap_freq_60": None,
        "beta_126": None,
        "abnormal_5d": None,
    }
    values.update(overrides)
    return FeatureSet(**values)  # type: ignore[arg-type]


def make_candidate(
    ticker: str,
    sector: str,
    score: str,
    *,
    issuer: str | None = None,
) -> Candidate:
    share = make_share(
        ticker=ticker,
        uid=f"uid-{ticker}",
        asset_uid=f"asset-{ticker}",
        name=f"{issuer or ticker} Issuer",
        sector=sector,
    )
    return Candidate(
        share=share,
        bar=Bar(
            trade_date=date(2025, 1, 1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume_lots=1000,
        ),
        features=feature(median_turnover_60=Decimal("1000000")),
        quality=DataQuality(
            history_sessions=252,
            activity_ratio=Decimal("1"),
            missing_ratio=Decimal("0"),
            zero_volume_ratio=Decimal("0"),
            last_bar_date=date(2025, 1, 1),
            stale_sessions=0,
        ),
        blocks=BlockScores(
            relative_strength=score,  # type: ignore[arg-type]
            trend_quality=score,  # type: ignore[arg-type]
            volume_confirmation=score,  # type: ignore[arg-type]
            consolidation_breakout=score,  # type: ignore[arg-type]
            risk_moderation=score,  # type: ignore[arg-type]
        ),
        score=Decimal(score),
        penalties=(),
        reasons=(),
        warnings=(),
    )
