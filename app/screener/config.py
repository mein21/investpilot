"""Screener configuration with explicit, reproducible defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

Mode = Literal["technical", "technical_fundamental"]

D095 = Decimal("0.95")
D30M = Decimal("30000000")
D07 = Decimal("0.7")
D60 = Decimal("60")
D52 = Decimal("52")
D035 = Decimal("0.35")
D70 = Decimal("70")
D030 = Decimal("0.30")
D020 = Decimal("0.20")
D010 = Decimal("0.10")
D12 = Decimal("12")
D10 = Decimal("10")
D8 = Decimal("8")
D0003 = Decimal("0.003")
D00015 = Decimal("0.0015")
D005 = Decimal("0.05")
D1M = Decimal("1000000")
D55 = Decimal("55")
D65 = Decimal("65")


@dataclass(frozen=True, slots=True)
class ScreenerConfig:
    db_path: Path = Path("./data/screener.db")
    history_calendar_days: int = 2150
    min_history_sessions: int = 252
    activity_window: int = 60
    min_activity_ratio: Decimal = D095
    turnover_window: int = 60
    min_median_turnover: Decimal = D30M
    max_single_turnover_share: Decimal = Decimal("0.50")
    max_spread_pct: Decimal = D07
    require_active: bool = True
    require_buy_available: bool = True
    include_qualified: bool = False
    include_otc: bool = False
    benchmark: str = "MCFTR"
    fallback_benchmark: str = "IMOEX"
    mode: Mode = "technical"
    min_score: Decimal = D60
    max_candidates: int = 10
    max_issuer_candidates: int = 1
    near_candidate_score: Decimal = D52
    max_sector_share: Decimal = D035
    risk_off_min_score: Decimal = D60
    risk_off_max_candidates: int = 5
    min_fundamental_coverage: int = 2
    min_fundamental_universe_coverage: Decimal = Decimal("0.50")

    weight_relative_strength: Decimal = D030
    weight_trend_quality: Decimal = D020
    weight_volume_confirmation: Decimal = D020
    weight_consolidation_breakout: Decimal = D020
    weight_risk_moderation: Decimal = D010

    penalty_abnormal_5d: Decimal = D12
    penalty_distance_sma50: Decimal = D10
    penalty_distance_breakout: Decimal = D8
    penalty_volume_instability: Decimal = D8
    sensitivity_threshold: Decimal = Decimal("5")
    penalty_sensitivity: Decimal = D8
    breakout_volume_threshold: Decimal = Decimal("1.5")

    horizon_sessions: int = 63
    max_entry_delay_sessions: int = 5
    max_exit_delay_sessions: int = 5
    target_return: Decimal = D020
    excess_target: Decimal = D010
    commission_rate: Decimal = D0003
    slippage_rate: Decimal = D00015
    participation_cap: Decimal = D005
    max_position_weight: Decimal = D020
    assumed_position_rub: Decimal = D1M

    backtest_train_cohorts: int = 6
    backtest_test_cohorts: int = 1
    backtest_step_sessions: int = 63
    backtest_max_cohorts: int = 40
    backtest_holdout_cohorts: int = 4
    bootstrap_iterations: int = 2000
    bootstrap_seed: int = 20261028
    min_backtest_cohorts: int = 8
    min_backtest_trades: int = 30
    min_train_trades: int = 5
    random_draws: int = 100

    refresh_marketdata: bool = True
    refresh_instruments: bool = True
    refresh_dividends: bool = False
    refresh_fundamentals: bool = False
    fetch_orderbook: bool = True
    marketdata_min_interval: float = 0.12
    instruments_min_interval: float = 0.32
    request_timeout: float = 30.0
    max_retries: int = 2
    api_limit: int | None = None

    sector_min_peers: int = 5
    large_gap_threshold: Decimal = Decimal("0.05")
    suspicious_gap_threshold: Decimal = D035
    stale_sessions_allowed: int = 1
    zero_volume_tolerance: Decimal = D005

    target_sensitivity: tuple[Decimal, ...] = (D55, D60, D65)
    candidates_sensitivity: tuple[int, ...] = (5, 10, 15)
    score_band_edges: tuple[Decimal, ...] = (Decimal("80"), D70, D60, Decimal("50"))
    min_band_trades: int = 10
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["db_path"] = str(self.db_path)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, tuple):
                data[key] = [str(item) if isinstance(item, Decimal) else item for item in value]
        return data

    @property
    def weights(self) -> dict[str, Decimal]:
        return {
            "relative_strength": self.weight_relative_strength,
            "trend_quality": self.weight_trend_quality,
            "volume_confirmation": self.weight_volume_confirmation,
            "consolidation_breakout": self.weight_consolidation_breakout,
            "risk_moderation": self.weight_risk_moderation,
        }
