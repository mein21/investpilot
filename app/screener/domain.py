"""Domain records for the deterministic stock screener."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class ShareRecord:
    uid: str
    ticker: str
    class_code: str
    name: str
    lot: int
    figi: str | None = None
    isin: str | None = None
    currency: str = "RUB"
    exchange: str = ""
    real_exchange: str = ""
    sector: str = ""
    share_type: str = ""
    asset_uid: str = ""
    first_candle_date: date | None = None
    trading_status: str = ""
    buy_available: bool = False
    api_trade_available: bool = False
    liquidity_flag: bool = False
    for_qual_investor: bool = False
    is_active: bool = True
    otc: bool = False
    weekend: bool = False

    @property
    def is_common_or_preferred(self) -> bool:
        return self.share_type in {"SHARE_TYPE_COMMON", "SHARE_TYPE_PREFERRED"}

    @property
    def is_main_board(self) -> bool:
        return self.class_code == "TQBR"

    @property
    def issuer_key(self) -> str:
        normalized = self.name.casefold().replace("ё", "е")
        tokens = re.findall(r"[0-9a-zа-я]+", normalized)
        ignored = {
            "акции",
            "акция",
            "ао",
            "ап",
            "зао",
            "оао",
            "пао",
            "привилегированные",
            "привилегированная",
            "обыкновенные",
            "обыкновенная",
            "публичное",
            "акционерное",
            "общество",
            "имени",
            "им",
        }
        key = "".join(token for token in tokens if token not in ignored)
        return key or self.asset_uid or self.uid


@dataclass(frozen=True, slots=True)
class Bar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_lots: int
    lot: int = 1
    is_complete: bool = True
    source: str = "exchange"
    fetched_at: datetime | None = None

    @property
    def turnover(self) -> Decimal:
        return self.close * Decimal(self.volume_lots) * Decimal(self.lot)

    @property
    def traded(self) -> bool:
        return self.volume_lots > 0 and self.close > 0


@dataclass(frozen=True, slots=True)
class Dividend:
    uid: str
    amount: Decimal
    currency: str = "RUB"
    record_date: date | None = None
    last_buy_date: date | None = None
    payment_date: date | None = None
    declared_date: date | None = None
    dividend_type: str = ""
    regularity: str = ""


@dataclass(frozen=True, slots=True)
class OrderBookQuote:
    uid: str
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None
    fetched_at: datetime
    depth: int = 10

    @property
    def is_valid(self) -> bool:
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask >= self.bid
            and self.spread_pct is not None
        )


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    asset_uid: str
    fetched_at: datetime
    as_of: date
    currency: str
    values: dict[str, Decimal | None]
    raw: dict[str, Any] = field(default_factory=dict)

    def value(self, key: str) -> Decimal | None:
        value = self.values.get(key)
        return value if value is not None and value.is_finite() else None


@dataclass(frozen=True, slots=True)
class DataQuality:
    history_sessions: int
    activity_ratio: Decimal
    missing_ratio: Decimal
    zero_volume_ratio: Decimal
    last_bar_date: date | None
    stale_sessions: int
    suspicious_gaps: tuple[date, ...] = ()
    flags: tuple[str, ...] = ()
    critical: bool = False

    @property
    def label(self) -> str:
        if self.critical:
            return "непригодны"
        if self.flags:
            return "с замечаниями"
        return "чистые"


@dataclass(frozen=True, slots=True)
class FeatureSet:
    as_of: date
    close: Decimal
    ret_1m: Decimal | None
    ret_3m: Decimal | None
    ret_6m: Decimal | None
    ret_6m_ex_5d: Decimal | None
    ret_12m: Decimal | None
    excess_1m: Decimal | None
    excess_3m: Decimal | None
    excess_6m: Decimal | None
    excess_6m_ex_5d: Decimal | None
    excess_12m: Decimal | None
    sma20: Decimal | None
    sma50: Decimal | None
    sma200: Decimal | None
    above_sma20: bool | None
    above_sma50: bool | None
    above_sma200: bool | None
    sma50_slope_20: Decimal | None
    sma200_slope_20: Decimal | None
    trend_consistency_60: Decimal | None
    drawdown_252: Decimal | None
    median_turnover_20: Decimal | None
    median_turnover_60: Decimal | None
    turnover_ratio_5_60: Decimal | None
    turnover_ratio_20_60: Decimal | None
    up_down_volume_ratio_60: Decimal | None
    turnover_cv_60: Decimal | None
    max_turnover_share_60: Decimal | None
    range_20: Decimal | None
    range_60: Decimal | None
    squeeze_20_60: Decimal | None
    dist_high_60: Decimal | None
    dist_high_63: Decimal | None
    dist_high_126: Decimal | None
    dist_high_252: Decimal | None
    breakout_20: Decimal | None
    breakout_60: Decimal | None
    breakout_63: Decimal | None
    breakout_126: Decimal | None
    breakout_volume_ratio_20: Decimal | None
    volatility_20: Decimal | None
    volatility_60: Decimal | None
    downside_vol_60: Decimal | None
    max_drawdown_126: Decimal | None
    atr_14_pct: Decimal | None
    large_gap_freq_60: Decimal | None
    beta_126: Decimal | None
    abnormal_5d: Decimal | None
    sector_excess_3m: Decimal | None = None
    sector_excess_6m: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BlockScores:
    relative_strength: Decimal | None
    trend_quality: Decimal | None
    volume_confirmation: Decimal | None
    consolidation_breakout: Decimal | None
    risk_moderation: Decimal | None


@dataclass(frozen=True, slots=True)
class Penalty:
    code: str
    points: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class Candidate:
    share: ShareRecord
    bar: Bar
    features: FeatureSet
    quality: DataQuality
    blocks: BlockScores
    score: Decimal
    penalties: tuple[Penalty, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    fundamental: FundamentalSnapshot | None = None
    spread_pct: Decimal | None = None
    spread_fetched_at: datetime | None = None
    sector_rank: int | None = None
    fundamental_score: Decimal | None = None
    fundamental_notes: tuple[str, ...] = ()
    fundamental_coverage: int = 0
    fundamental_category: str = ""
    invalidation: tuple[str, ...] = ()
    history: tuple[Bar, ...] = ()
    data_fetched_at: datetime | None = None

    @property
    def ticker(self) -> str:
        return self.share.ticker

    @property
    def name(self) -> str:
        return self.share.name


@dataclass(frozen=True, slots=True)
class FilterStats:
    shares_total: int = 0
    moex_rub_common_preferred: int = 0
    active: int = 0
    buy_available: int = 0
    history_ok: int = 0
    activity_ok: int = 0
    liquidity_ok: int = 0
    quality_ok: int = 0
    spread_ok: int = 0
    spread_checked: bool = False
    fundamental_ok: int = 0
    score_ok: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "shares_total": self.shares_total,
            "moex_rub_common_preferred": self.moex_rub_common_preferred,
            "active": self.active,
            "buy_available": self.buy_available,
            "history_ok": self.history_ok,
            "activity_ok": self.activity_ok,
            "liquidity_ok": self.liquidity_ok,
            "quality_ok": self.quality_ok,
            "spread_ok": self.spread_ok,
            "spread_checked": self.spread_checked,
            "fundamental_ok": self.fundamental_ok,
            "score_ok": self.score_ok,
        }


SCREENER_BLOCK_LABELS: dict[str, str] = {
    "relative_strength": "относительная сила",
    "trend_quality": "качество тренда",
    "volume_confirmation": "объёмы",
    "consolidation_breakout": "пробой",
    "risk_moderation": "риск",
}


@dataclass(frozen=True, slots=True)
class ScreenerRating:
    uid: str
    ticker: str
    score: Decimal
    rank: int
    as_of: date
    mode: str
    generated_at: datetime
    quality_label: str = ""
    critical: bool = False
    blocks: dict[str, Decimal | None] = field(default_factory=dict)
    penalties: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        parts = [
            f"скринер {self.score:.1f}/100",
            f"место {self.rank}",
            f"режим {self.mode or 'unknown'}",
            f"на {self.as_of.isoformat()}",
        ]
        block_text = "; ".join(
            f"{SCREENER_BLOCK_LABELS.get(code, code)} {value:.0f}"
            for code, value in self.blocks.items()
            if value is not None
        )
        if block_text:
            parts.append(block_text)
        if self.penalties:
            parts.append("штрафы: " + "; ".join(self.penalties))
        if self.quality_label and self.quality_label != "чистые":
            parts.append(f"данные: {self.quality_label}")
        if self.critical:
            parts.append("критичные замечания к данным")
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class MarketRegime:
    as_of: date
    benchmark: str
    label: str
    close: Decimal
    sma200: Decimal | None
    slope_20: Decimal | None
    volatility_60: Decimal | None
    drawdown_252: Decimal | None
    breadth_50: Decimal | None
    breadth_200: Decimal | None
    explanation: str


@dataclass(frozen=True, slots=True)
class ScreenerResult:
    as_of: date
    mode: str
    benchmark: str
    universe: tuple[ShareRecord, ...]
    candidates: tuple[Candidate, ...]
    near_candidates: tuple[Candidate, ...]
    filter_stats: FilterStats
    regime: MarketRegime
    warnings: tuple[str, ...]
    generated_at: datetime
    config: dict[str, Any] = field(default_factory=dict)
    historical: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BacktestPortfolioPoint:
    trade_date: date
    equity: Decimal
    benchmark: Decimal | None
    exposure: Decimal


@dataclass(frozen=True, slots=True)
class BacktestPortfolioYear:
    year: int
    return_value: Decimal | None
    benchmark_return: Decimal | None
    max_drawdown: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestPortfolio:
    initial_capital: Decimal
    final_equity: Decimal
    total_return: Decimal | None
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    cagr: Decimal | None
    volatility: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal | None
    turnover: Decimal | None
    exposure: Decimal | None
    periods: int
    trades: int
    points: tuple[BacktestPortfolioPoint, ...]
    yearly: tuple[BacktestPortfolioYear, ...]
    commission_paid: Decimal = Decimal("0")
    slippage_paid: Decimal = Decimal("0")
    cost_drag: Decimal | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestHoldout:
    start: date
    end: date
    selected_min_score: Decimal
    selected_candidates: int
    summary: BacktestSummary
    cohorts: tuple[BacktestCohort, ...]
    trades: tuple[BacktestTrade, ...]
    portfolio: BacktestPortfolio


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    cohort: date
    signal_date: date
    entry_date: date
    exit_date: date
    uid: str
    ticker: str
    score: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_return: Decimal
    net_return: Decimal
    benchmark_return: Decimal
    excess_return: Decimal
    dividends: Decimal
    target_hit: bool
    excess_hit: bool
    max_adverse: Decimal
    days_held: int


@dataclass(frozen=True, slots=True)
class BacktestCohort:
    signal_date: date
    entry_date: date
    exit_date: date
    candidates: int
    trades: int
    mean_net_return: Decimal
    mean_benchmark_return: Decimal
    mean_excess: Decimal
    target_hit_rate: Decimal
    excess_hit_rate: Decimal
    equal_weight_return: Decimal


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    cohorts: int
    trades: int
    mean_net_return: Decimal | None
    median_net_return: Decimal | None
    mean_gross_return: Decimal | None
    mean_benchmark_return: Decimal | None
    mean_excess: Decimal | None
    median_excess: Decimal | None
    cost_drag: Decimal | None
    mean_max_adverse: Decimal | None
    median_max_adverse: Decimal | None
    worst_max_adverse: Decimal | None
    excess_ci_low: Decimal | None
    excess_ci_high: Decimal | None
    target_hit_rate: Decimal | None
    target_ci_low: Decimal | None
    target_ci_high: Decimal | None
    target_block_ci_low: Decimal | None
    target_block_ci_high: Decimal | None
    excess_hit_rate: Decimal | None
    beat_benchmark_rate: Decimal | None
    beat_ci_low: Decimal | None
    beat_ci_high: Decimal | None
    beat_block_ci_low: Decimal | None
    beat_block_ci_high: Decimal | None
    worst_cohort_excess: Decimal | None
    best_cohort_excess: Decimal | None
    insufficient: bool = False
    sample_warning: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestAlternative:
    name: str
    mean_net_return: Decimal | None
    mean_excess: Decimal | None
    target_hit_rate: Decimal | None
    excess_hit_rate: Decimal | None
    trades: int
    note: str = ""
    portfolio_total_return: Decimal | None = None
    portfolio_max_drawdown: Decimal | None = None
    portfolio_cagr: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestVerdict:
    status: str
    headline: str
    details: tuple[str, ...]
    best_simple_alternative: str | None = None
    best_simple_excess: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    start: date
    end: date
    benchmark: str
    summary: BacktestSummary
    cohorts: tuple[BacktestCohort, ...]
    trades: tuple[BacktestTrade, ...]
    alternatives: tuple[BacktestAlternative, ...]
    sensitivity: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    generated_at: datetime
    verdict: BacktestVerdict | None = None
    config: dict[str, Any] = field(default_factory=dict)
    portfolio: BacktestPortfolio | None = None
    holdout: BacktestHoldout | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
