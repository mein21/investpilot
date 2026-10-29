"""Cross-sectional rating: A-E blocks, penalties and explanations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.screener.config import ScreenerConfig
from app.screener.domain import BlockScores, FeatureSet, Penalty

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    blocks: BlockScores
    score: Decimal | None
    penalties: tuple[Penalty, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    sensitivity: Decimal | None


def _percentile(
    value: Decimal | None,
    universe: Sequence[Decimal | None],
    *,
    higher_is_better: bool = True,
) -> Decimal | None:
    if value is None:
        return None
    clean = [item for item in universe if item is not None]
    if not clean:
        return None
    if len(clean) == 1:
        rank = Decimal("50")
    else:
        less = sum(1 for item in clean if item < value)
        equal = sum(1 for item in clean if item == value)
        rank = (Decimal(less) + Decimal(equal) / Decimal("2")) / Decimal(len(clean)) * HUNDRED
    return rank if higher_is_better else HUNDRED - rank


def _weighted(pairs: Sequence[tuple[Decimal | None, Decimal]]) -> Decimal | None:
    available = [(value, weight) for value, weight in pairs if value is not None]
    total_weight = sum((weight for _, weight in available), ZERO)
    if total_weight <= 0:
        return None
    return sum((value * weight for value, weight in available), ZERO) / total_weight


def _binary(value: bool | None) -> Decimal | None:
    if value is None:
        return None
    return HUNDRED if value else ZERO


def _values(
    features: Sequence[FeatureSet], getter: Callable[[FeatureSet], Decimal | None]
) -> list[Decimal | None]:
    return [getter(item) for item in features]


def _block_a(features: FeatureSet, all_features: Sequence[FeatureSet]) -> Decimal | None:
    market = _weighted(
        [
            (
                _percentile(features.excess_3m, _values(all_features, lambda item: item.excess_3m)),
                Decimal("0.35"),
            ),
            (
                _percentile(
                    features.excess_6m_ex_5d,
                    _values(all_features, lambda item: item.excess_6m_ex_5d),
                ),
                Decimal("0.30"),
            ),
            (
                _percentile(
                    features.excess_12m, _values(all_features, lambda item: item.excess_12m)
                ),
                Decimal("0.20"),
            ),
            (
                _percentile(
                    _acceleration(features),
                    _values(all_features, _acceleration),
                ),
                Decimal("0.15"),
            ),
        ]
    )
    if features.sector_excess_3m is None and features.sector_excess_6m is None:
        return market
    sector = _weighted(
        [
            (
                _percentile(
                    features.sector_excess_3m,
                    _values(all_features, lambda item: item.sector_excess_3m),
                ),
                Decimal("0.50"),
            ),
            (
                _percentile(
                    features.sector_excess_6m,
                    _values(all_features, lambda item: item.sector_excess_6m),
                ),
                Decimal("0.50"),
            ),
        ]
    )
    if market is None:
        return sector
    if sector is None:
        return market
    return market * Decimal("0.85") + sector * Decimal("0.15")


def _acceleration(features: FeatureSet) -> Decimal | None:
    if features.excess_3m is None or features.excess_6m is None:
        return None
    return features.excess_3m - features.excess_6m / Decimal("2")


def _block_b(features: FeatureSet, all_features: Sequence[FeatureSet]) -> Decimal | None:
    return _weighted(
        [
            (
                _percentile(
                    features.sma200_slope_20,
                    _values(all_features, lambda item: item.sma200_slope_20),
                ),
                Decimal("0.30"),
            ),
            (
                _percentile(
                    features.trend_consistency_60,
                    _values(all_features, lambda item: item.trend_consistency_60),
                ),
                Decimal("0.25"),
            ),
            (
                _percentile(
                    features.drawdown_252,
                    _values(all_features, lambda item: item.drawdown_252),
                ),
                Decimal("0.15"),
            ),
            (_binary(features.above_sma200), Decimal("0.10")),
            (_binary(features.above_sma50), Decimal("0.10")),
            (_binary(features.above_sma20), Decimal("0.10")),
        ]
    )


def _block_c(features: FeatureSet, all_features: Sequence[FeatureSet]) -> Decimal | None:
    return _weighted(
        [
            (
                _percentile(
                    features.turnover_ratio_5_60,
                    _values(all_features, lambda item: item.turnover_ratio_5_60),
                ),
                Decimal("0.30"),
            ),
            (
                _percentile(
                    features.turnover_ratio_20_60,
                    _values(all_features, lambda item: item.turnover_ratio_20_60),
                ),
                Decimal("0.20"),
            ),
            (
                _percentile(
                    features.up_down_volume_ratio_60,
                    _values(all_features, lambda item: item.up_down_volume_ratio_60),
                ),
                Decimal("0.30"),
            ),
            (
                _percentile(
                    features.turnover_cv_60,
                    _values(all_features, lambda item: item.turnover_cv_60),
                    higher_is_better=False,
                ),
                Decimal("0.20"),
            ),
        ]
    )


def _breakout_strength(features: FeatureSet) -> Decimal | None:
    values = [value for value in (features.breakout_63, features.breakout_126) if value is not None]
    return max(values) if values else None


def _confirmed_breakout(
    features: FeatureSet,
    config: ScreenerConfig,
) -> bool | None:
    strength = _breakout_strength(features)
    if strength is None or features.breakout_volume_ratio_20 is None:
        return None
    return strength > ZERO and (
        features.breakout_volume_ratio_20 >= config.breakout_volume_threshold
    )


def _block_d(
    features: FeatureSet,
    all_features: Sequence[FeatureSet],
    config: ScreenerConfig,
) -> Decimal | None:
    return _weighted(
        [
            (
                _percentile(
                    features.squeeze_20_60,
                    _values(all_features, lambda item: item.squeeze_20_60),
                ),
                Decimal("0.25"),
            ),
            (
                _percentile(
                    features.dist_high_63,
                    _values(all_features, lambda item: item.dist_high_63),
                ),
                Decimal("0.15"),
            ),
            (
                _percentile(
                    features.dist_high_126,
                    _values(all_features, lambda item: item.dist_high_126),
                ),
                Decimal("0.15"),
            ),
            (
                _percentile(
                    _breakout_strength(features),
                    [_breakout_strength(item) for item in all_features],
                ),
                Decimal("0.30"),
            ),
            (
                _percentile(
                    _binary(_confirmed_breakout(features, config)),
                    [_binary(_confirmed_breakout(item, config)) for item in all_features],
                ),
                Decimal("0.15"),
            ),
        ]
    )


def _block_e(features: FeatureSet, all_features: Sequence[FeatureSet]) -> Decimal | None:
    beta_distance = -abs(features.beta_126 - ONE) if features.beta_126 is not None else None
    beta_universe = [
        -abs(item.beta_126 - ONE) if item.beta_126 is not None else None for item in all_features
    ]
    return _weighted(
        [
            (
                _percentile(
                    features.volatility_60,
                    _values(all_features, lambda item: item.volatility_60),
                    higher_is_better=False,
                ),
                Decimal("0.25"),
            ),
            (
                _percentile(
                    features.max_drawdown_126,
                    _values(all_features, lambda item: item.max_drawdown_126),
                ),
                Decimal("0.25"),
            ),
            (
                _percentile(
                    features.atr_14_pct,
                    _values(all_features, lambda item: item.atr_14_pct),
                    higher_is_better=False,
                ),
                Decimal("0.15"),
            ),
            (
                _percentile(
                    features.large_gap_freq_60,
                    _values(all_features, lambda item: item.large_gap_freq_60),
                    higher_is_better=False,
                ),
                Decimal("0.15"),
            ),
            (
                _percentile(
                    features.downside_vol_60,
                    _values(all_features, lambda item: item.downside_vol_60),
                    higher_is_better=False,
                ),
                Decimal("0.10"),
            ),
            (_percentile(beta_distance, beta_universe), Decimal("0.10")),
        ]
    )


def _penalties(
    features: FeatureSet,
    sensitivity: Decimal | None,
    config: ScreenerConfig,
) -> tuple[tuple[Penalty, ...], Decimal]:
    result: list[Penalty] = []
    if features.abnormal_5d is not None:
        move = abs(features.abnormal_5d)
        if move > Decimal("2.5"):
            points = min(
                config.penalty_abnormal_5d,
                config.penalty_abnormal_5d * (move - Decimal("2.5")) / Decimal("2.5"),
            )
            result.append(
                Penalty(
                    "abnormal_5d",
                    points,
                    f"Аномальный 5-дневный ход: {features.abnormal_5d:.2f} σ.",
                )
            )
    if features.close > 0 and features.sma50 is not None and features.sma50 > 0:
        distance = features.close / features.sma50 - ONE
        if distance > Decimal("0.20"):
            points = min(
                config.penalty_distance_sma50,
                config.penalty_distance_sma50 * (distance - Decimal("0.20")) / Decimal("0.30"),
            )
            result.append(
                Penalty(
                    "distance_sma50",
                    points,
                    f"Цена выше SMA50 на {distance * 100:.1f}% — риск отката.",
                )
            )
    extension = features.breakout_63
    extension_window = 63
    if extension is None:
        extension = features.breakout_126
        extension_window = 126
    if extension is None:
        extension = features.breakout_20
        extension_window = 20
    if extension is not None and extension > Decimal("0.20"):
        points = min(
            config.penalty_distance_breakout,
            config.penalty_distance_breakout * (extension - Decimal("0.20")) / Decimal("0.20"),
        )
        result.append(
            Penalty(
                "distance_breakout",
                points,
                f"Цена выше {extension_window}-сессионного максимума "
                f"на {extension * 100:.1f}% — пробой растянут.",
            )
        )
    if features.turnover_cv_60 is not None and features.turnover_cv_60 > Decimal("1.5"):
        points = min(
            config.penalty_volume_instability,
            config.penalty_volume_instability
            * (features.turnover_cv_60 - Decimal("1.5"))
            / Decimal("1.5"),
        )
        result.append(
            Penalty(
                "volume_instability",
                points,
                f"Нестабильный оборот: CV={features.turnover_cv_60:.2f}.",
            )
        )
    if sensitivity is not None and sensitivity > config.sensitivity_threshold:
        points = min(
            config.penalty_sensitivity,
            config.penalty_sensitivity
            * (sensitivity - config.sensitivity_threshold)
            / config.sensitivity_threshold,
        )
        result.append(
            Penalty(
                "sensitivity",
                points,
                f"Рейтинг чувствителен к весам: ±{sensitivity:.1f} п.",
            )
        )
    return tuple(result), sum((item.points for item in result), ZERO)


def _reasons(features: FeatureSet, blocks: BlockScores) -> tuple[str, ...]:
    reasons: list[str] = []
    if features.excess_6m is not None:
        reasons.append(f"6 мес. к бенчмарку: {features.excess_6m * 100:+.1f} п.п.")
    if features.above_sma200:
        reasons.append("Цена выше SMA200, долгосрочный тренд восходящий.")
    elif features.above_sma50:
        reasons.append("Цена выше SMA50, среднесрочный тренд восстановился.")
    if features.up_down_volume_ratio_60 is not None and features.up_down_volume_ratio_60 > ONE:
        reasons.append(
            f"Объём на росте выше объёма на падении в {features.up_down_volume_ratio_60:.2f} раза."
        )
    breakout = features.breakout_63
    breakout_window = 63
    if breakout is None or breakout <= 0:
        breakout = features.breakout_126
        breakout_window = 126
    if breakout is not None and breakout > 0:
        if (
            features.breakout_volume_ratio_20 is not None
            and features.breakout_volume_ratio_20 >= Decimal("1.5")
        ):
            reasons.append(
                f"Пробой {breakout_window}-сессионного максимума подтверждён оборотом "
                f"×{features.breakout_volume_ratio_20:.2f} к базе."
            )
        else:
            reasons.append(f"Закрытие выше максимума предыдущих {breakout_window} сессий.")
    if features.squeeze_20_60 is not None and features.squeeze_20_60 > Decimal("0.40"):
        reasons.append(
            f"Сжатие 20-дневного диапазона к 60-дневному: {features.squeeze_20_60 * 100:.0f}%."
        )
    if features.volatility_60 is not None and features.volatility_60 < Decimal("0.35"):
        reasons.append(f"Умеренная волатильность: {features.volatility_60 * 100:.1f}% годовых.")
    if features.sector_excess_3m is not None:
        reasons.append(f"3 мес. к сектору: {features.sector_excess_3m * 100:+.1f} п.п.")
    scores = {
        "Относительная сила": blocks.relative_strength,
        "Качество тренда": blocks.trend_quality,
        "Объём": blocks.volume_confirmation,
        "Пробой": blocks.consolidation_breakout,
        "Риск": blocks.risk_moderation,
    }
    top = sorted(
        ((name, value) for name, value in scores.items() if value is not None),
        key=lambda item: item[1] or ZERO,
        reverse=True,
    )[:2]
    for name, value in top:
        reasons.append(f"Блок «{name}»: {value:.0f}/100.")
    return tuple(dict.fromkeys(reasons))[:6]


def _warnings(
    features: FeatureSet,
    penalties: Sequence[Penalty],
    config: ScreenerConfig,
) -> tuple[str, ...]:
    warnings = [item.reason for item in penalties]
    if features.volatility_60 is not None and features.volatility_60 > Decimal("0.55"):
        warnings.append(f"Высокая волатильность: {features.volatility_60 * 100:.1f}% годовых.")
    if features.beta_126 is not None and features.beta_126 > Decimal("1.4"):
        warnings.append(f"Высокая бета к бенчмарку: {features.beta_126:.2f}.")
    if features.turnover_ratio_5_60 is not None and features.turnover_ratio_5_60 > Decimal("3"):
        warnings.append(
            f"Оборот за 5 сессий в {features.turnover_ratio_5_60:.1f} раза выше 60-сессионной базы."
        )
    return tuple(dict.fromkeys(warnings))


def _weighted_score(
    blocks: BlockScores,
    weights: dict[str, Decimal],
) -> Decimal | None:
    pairs = [
        (getattr(blocks, name), weight)
        for name, weight in weights.items()
        if getattr(blocks, name) is not None and weight > 0
    ]
    total_weight = sum((weight for _, weight in pairs), ZERO)
    if total_weight <= 0:
        return None
    return sum((value * weight for value, weight in pairs), ZERO) / total_weight


def _score_sensitivity(blocks: BlockScores, config: ScreenerConfig) -> Decimal | None:
    base = _weighted_score(blocks, config.weights)
    if base is None:
        return None
    values: list[Decimal] = []
    for name, weight in config.weights.items():
        for factor in (Decimal("0.9"), Decimal("1.1")):
            changed = dict(config.weights)
            changed[name] = weight * factor
            value = _weighted_score(blocks, changed)
            if value is not None:
                values.append(abs(value - base))
    return max(values) if values else None


def score_features(
    features: FeatureSet,
    all_features: Sequence[FeatureSet],
    config: ScreenerConfig,
) -> ScoreBreakdown:
    a = _block_a(features, all_features)
    b = _block_b(features, all_features)
    c = _block_c(features, all_features)
    d = _block_d(features, all_features, config)
    e = _block_e(features, all_features)
    blocks = BlockScores(
        relative_strength=a,
        trend_quality=b,
        volume_confirmation=c,
        consolidation_breakout=d,
        risk_moderation=e,
    )
    sensitivity = _score_sensitivity(blocks, config)
    penalties, penalty_points = _penalties(features, sensitivity, config)
    raw = _weighted_score(blocks, config.weights)
    score = None if raw is None else max(ZERO, min(HUNDRED, raw - penalty_points))
    missing = [
        label
        for label, value in (
            ("A. Относительная сила", a),
            ("B. Качество тренда", b),
            ("C. Объём", c),
            ("D. Пробой", d),
            ("E. Риск", e),
        )
        if value is None
    ]
    warnings = list(_warnings(features, penalties, config))
    if missing:
        warnings.append("Нет данных для блоков: " + ", ".join(missing) + ".")
    return ScoreBreakdown(
        blocks=blocks,
        score=score,
        penalties=penalties,
        reasons=_reasons(features, blocks),
        warnings=tuple(dict.fromkeys(warnings)),
        sensitivity=sensitivity,
    )
