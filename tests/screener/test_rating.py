from __future__ import annotations

from decimal import Decimal

from app.screener.rating import score_features
from helpers import feature, screener_config


def test_missing_data_is_not_replaced_by_neutral_score() -> None:
    empty = feature()
    breakdown = score_features(empty, [empty], screener_config())

    assert breakdown.blocks.relative_strength is None
    assert breakdown.blocks.trend_quality is None
    assert breakdown.blocks.volume_confirmation is None
    assert breakdown.blocks.consolidation_breakout is None
    assert breakdown.blocks.risk_moderation is None
    assert breakdown.score is None
    assert breakdown.sensitivity is None
    assert any("Нет данных" in warning for warning in breakdown.warnings)


def test_stronger_relative_strength_scores_higher() -> None:
    weak = feature(
        excess_3m=Decimal("-0.10"),
        excess_6m=Decimal("-0.05"),
        excess_6m_ex_5d=Decimal("-0.04"),
    )
    middle = feature(
        excess_3m=Decimal("0.05"),
        excess_6m=Decimal("0.02"),
        excess_6m_ex_5d=Decimal("0.01"),
    )
    strong = feature(
        excess_3m=Decimal("0.20"),
        excess_6m=Decimal("0.15"),
        excess_6m_ex_5d=Decimal("0.14"),
    )
    universe = [weak, middle, strong]

    weak_score = score_features(weak, universe, screener_config())
    strong_score = score_features(strong, universe, screener_config())

    assert strong_score.blocks.relative_strength is not None
    assert weak_score.blocks.relative_strength is not None
    assert strong_score.score is not None
    assert weak_score.score is not None
    assert strong_score.blocks.relative_strength > weak_score.blocks.relative_strength
    assert strong_score.score > weak_score.score


def test_penalties_reduce_score_and_are_explained() -> None:
    common = {
        "excess_3m": Decimal("0.05"),
        "excess_6m": Decimal("0.02"),
        "excess_6m_ex_5d": Decimal("0.01"),
        "volatility_60": Decimal("0.20"),
        "max_drawdown_126": Decimal("-0.10"),
        "atr_14_pct": Decimal("0.02"),
        "large_gap_freq_60": Decimal("0.01"),
        "downside_vol_60": Decimal("0.15"),
        "beta_126": Decimal("1.0"),
    }
    normal = feature(
        close=Decimal("100"),
        sma50=Decimal("100"),
        abnormal_5d=Decimal("0.5"),
        turnover_cv_60=Decimal("1"),
        **common,
    )
    stretched = feature(
        close=Decimal("140"),
        sma50=Decimal("100"),
        abnormal_5d=Decimal("5"),
        turnover_cv_60=Decimal("4"),
        **common,
    )
    universe = [normal, stretched]
    base = score_features(normal, universe, screener_config())
    penalized = score_features(stretched, universe, screener_config())

    codes = {penalty.code for penalty in penalized.penalties}
    assert {"abnormal_5d", "distance_sma50", "volume_instability"} <= codes
    assert base.score is not None
    assert penalized.score is not None
    assert penalized.score < Decimal("100")
    assert all(penalty.points > 0 for penalty in penalized.penalties)
    assert base.score >= penalized.score


def test_trend_block_does_not_double_count_sma50_slope() -> None:
    common = {
        "sma200_slope_20": Decimal("0.05"),
        "trend_consistency_60": Decimal("1"),
        "drawdown_252": Decimal("-0.10"),
        "above_sma200": True,
        "above_sma50": True,
        "above_sma20": True,
    }
    low_slope = feature(sma50_slope_20=Decimal("0.01"), **common)
    high_slope = feature(sma50_slope_20=Decimal("0.50"), **common)
    universe = [low_slope, high_slope]

    low_score = score_features(low_slope, universe, screener_config())
    high_score = score_features(high_slope, universe, screener_config())

    assert low_score.blocks.trend_quality == high_score.blocks.trend_quality


def test_sector_excess_is_used_when_available() -> None:
    leader = feature(excess_3m=Decimal("0.10"), sector_excess_3m=Decimal("0.15"))
    laggard = feature(excess_3m=Decimal("0.10"), sector_excess_3m=Decimal("-0.15"))
    universe = [leader, laggard]

    leader_score = score_features(leader, universe, screener_config())
    laggard_score = score_features(laggard, universe, screener_config())

    assert leader_score.blocks.relative_strength is not None
    assert laggard_score.blocks.relative_strength is not None
    assert leader_score.blocks.relative_strength > laggard_score.blocks.relative_strength


def test_breakout_without_volume_confirmation_scores_lower() -> None:
    base = {
        "squeeze_20_60": Decimal("0.20"),
        "breakout_63": Decimal("0.03"),
        "breakout_126": Decimal("0.03"),
        "dist_high_63": Decimal("-0.02"),
        "dist_high_126": Decimal("-0.05"),
    }
    confirmed = feature(breakout_volume_ratio_20=Decimal("2.0"), **base)
    unconfirmed = feature(breakout_volume_ratio_20=Decimal("0.5"), **base)
    universe = [confirmed, unconfirmed]

    confirmed_score = score_features(confirmed, universe, screener_config())
    unconfirmed_score = score_features(unconfirmed, universe, screener_config())

    assert confirmed_score.blocks.consolidation_breakout is not None
    assert unconfirmed_score.blocks.consolidation_breakout is not None
    assert (
        confirmed_score.blocks.consolidation_breakout
        > unconfirmed_score.blocks.consolidation_breakout
    )


def test_breakout_block_uses_63_and_126_session_levels() -> None:
    near = feature(
        squeeze_20_60=Decimal("0.30"),
        dist_high_63=Decimal("-0.01"),
        dist_high_126=Decimal("-0.02"),
        breakout_63=Decimal("0.01"),
        breakout_126=Decimal("0.01"),
        breakout_volume_ratio_20=Decimal("2"),
    )
    far = feature(
        squeeze_20_60=Decimal("0"),
        dist_high_63=Decimal("-0.30"),
        dist_high_126=Decimal("-0.40"),
        breakout_63=Decimal("-0.30"),
        breakout_126=Decimal("-0.40"),
        breakout_volume_ratio_20=Decimal("0.5"),
    )
    near_score = score_features(near, [near, far], screener_config())
    far_score = score_features(far, [near, far], screener_config())

    assert near_score.blocks.consolidation_breakout is not None
    assert far_score.blocks.consolidation_breakout is not None
    assert near_score.blocks.consolidation_breakout > far_score.blocks.consolidation_breakout


def test_stretched_breakout_is_penalized() -> None:
    normal = feature(breakout_63=Decimal("0.03"), breakout_volume_ratio_20=Decimal("2"))
    stretched = feature(breakout_63=Decimal("0.50"), breakout_volume_ratio_20=Decimal("2"))
    breakdown = score_features(stretched, [normal, stretched], screener_config())

    codes = {penalty.code for penalty in breakdown.penalties}
    assert "distance_breakout" in codes
    assert all(penalty.points > 0 for penalty in breakdown.penalties)


def test_sensitivity_measures_weight_changes() -> None:
    low = feature(
        excess_3m=Decimal("-0.10"),
        excess_6m=Decimal("-0.05"),
        excess_6m_ex_5d=Decimal("-0.04"),
        volatility_60=Decimal("0.20"),
        max_drawdown_126=Decimal("-0.10"),
        atr_14_pct=Decimal("0.02"),
        large_gap_freq_60=Decimal("0.01"),
        downside_vol_60=Decimal("0.15"),
        beta_126=Decimal("1.0"),
    )
    high = feature(
        excess_3m=Decimal("0.20"),
        excess_6m=Decimal("0.15"),
        excess_6m_ex_5d=Decimal("0.14"),
        volatility_60=Decimal("0.20"),
        max_drawdown_126=Decimal("-0.10"),
        atr_14_pct=Decimal("0.02"),
        large_gap_freq_60=Decimal("0.01"),
        downside_vol_60=Decimal("0.15"),
        beta_126=Decimal("1.0"),
    )

    breakdown = score_features(high, [low, high], screener_config())

    assert breakdown.sensitivity is not None
    assert breakdown.sensitivity > 0

    penalized = score_features(
        high,
        [low, high],
        screener_config(
            sensitivity_threshold=Decimal("0.1"),
            penalty_sensitivity=Decimal("8"),
        ),
    )
    assert any(penalty.code == "sensitivity" for penalty in penalized.penalties)
