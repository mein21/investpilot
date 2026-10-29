"""Deterministic rank diagnostics for the stock screener.

The module intentionally contains no ML: only Spearman rank correlations,
information coefficients by cohort and a cohort-block bootstrap for rates.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from decimal import Decimal

from app.screener.domain import FeatureSet
from app.screener.features import mean

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


FeatureGetter = Callable[[FeatureSet], Decimal | None]


def _average_ranks(values: Sequence[Decimal]) -> list[Decimal]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [ZERO] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        average = (Decimal(position + 1) + Decimal(end)) / Decimal(2)
        for offset in range(position, end):
            ranks[order[offset]] = average
        position = end
    return ranks


def spearman(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal | None:
    """Spearman rank correlation with average ranks for ties."""

    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rank_x = _average_ranks(xs)
    rank_y = _average_ranks(ys)
    mean_x = mean(rank_x)
    mean_y = mean(rank_y)
    if mean_x is None or mean_y is None:
        return None
    covariance = sum(
        ((x - mean_x) * (y - mean_y) for x, y in zip(rank_x, rank_y, strict=True)), ZERO
    )
    variance_x = sum(((x - mean_x) ** 2 for x in rank_x), ZERO)
    variance_y = sum(((y - mean_y) ** 2 for y in rank_y), ZERO)
    denominator = (variance_x * variance_y).sqrt()
    if denominator <= ZERO:
        return None
    return covariance / denominator


def quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * q).to_integral_value())
    return ordered[max(0, min(index, len(ordered) - 1))]


def bootstrap_mean_ci(
    values: Sequence[Decimal],
    *,
    iterations: int,
    seed: int,
    low: Decimal = Decimal("0.05"),
    high: Decimal = Decimal("0.95"),
) -> tuple[Decimal | None, Decimal | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    draws: list[Decimal] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        average = mean(sample)
        if average is not None:
            draws.append(average)
    if not draws:
        return None, None
    return quantile(draws, low), quantile(draws, high)


def block_bootstrap_mean_ci(
    groups: Sequence[Sequence[Decimal]],
    *,
    iterations: int,
    seed: int,
) -> tuple[Decimal | None, Decimal | None]:
    """Bootstrap a pooled mean by resampling whole cohorts (blocks)."""

    usable = [group for group in groups if group]
    if len(usable) < 2:
        return None, None
    rng = random.Random(seed)
    values: list[Decimal] = []
    for _ in range(iterations):
        total = ZERO
        count = 0
        for _ in usable:
            group = usable[rng.randrange(len(usable))]
            total += sum(group, ZERO)
            count += len(group)
        if count:
            values.append(total / Decimal(count))
    if not values:
        return None, None
    return quantile(values, Decimal("0.05")), quantile(values, Decimal("0.95"))


def block_bootstrap_rate_ci(
    groups: Sequence[Sequence[bool]],
    *,
    iterations: int,
    seed: int,
) -> tuple[Decimal | None, Decimal | None]:
    """Bootstrap a success rate by resampling whole cohorts (blocks).

    Resampling cohorts rather than individual trades preserves the dependence
    between signals observed on the same date and across overlapping horizons.
    """

    usable = [group for group in groups if group]
    if len(usable) < 2:
        return None, None
    rng = random.Random(seed)
    values: list[Decimal] = []
    for _ in range(iterations):
        successes = 0
        total = 0
        for _ in usable:
            group = usable[rng.randrange(len(usable))]
            successes += sum(1 for item in group if item)
            total += len(group)
        if total:
            values.append(Decimal(successes) / Decimal(total))
    if not values:
        return None, None
    return quantile(values, Decimal("0.05")), quantile(values, Decimal("0.95"))


def correlation_rows(
    samples: Sequence[tuple[FeatureSet, Decimal]],
    specs: Sequence[tuple[str, FeatureGetter]],
    *,
    min_observations: int = 30,
    threshold: Decimal = Decimal("0.50"),
    limit: int = 25,
) -> list[dict[str, object]]:
    """Pairwise Spearman correlations for non-constant rating features."""

    rows: list[dict[str, object]] = []
    for left_index, (left_name, left_getter) in enumerate(specs):
        for right_name, right_getter in specs[left_index + 1 :]:
            pairs = [
                (left, right)
                for features, _ in samples
                if (left := left_getter(features)) is not None
                and (right := right_getter(features)) is not None
            ]
            if len(pairs) < min_observations:
                continue
            rho = spearman(
                [item[0] for item in pairs],
                [item[1] for item in pairs],
            )
            if rho is None or abs(rho) < threshold:
                continue
            rows.append(
                {
                    "kind": "feature_correlation",
                    "left": left_name,
                    "right": right_name,
                    "rho": str(rho),
                    "observations": len(pairs),
                    "note": "Высокая корреляция: признаки могут дублировать эффект."
                    if abs(rho) >= Decimal("0.80")
                    else "",
                }
            )
    rows.sort(key=lambda item: abs(Decimal(str(item["rho"]))), reverse=True)
    return rows[:limit]


def information_coefficients(
    cohort_samples: Sequence[Sequence[tuple[FeatureSet, Decimal]]],
    specs: Sequence[tuple[str, FeatureGetter]],
    *,
    min_observations: int = 5,
    iterations: int = 2000,
    seed: int = 20261028,
) -> list[dict[str, object]]:
    """Mean Spearman IC of each feature with the future excess return.

    Each cohort contributes one IC observation; the confidence interval is a
    cohort-level bootstrap, so overlapping 63-session windows are not treated
    as independent observations.
    """

    rows: list[dict[str, object]] = []
    for name, getter in specs:
        values: list[Decimal] = []
        for samples in cohort_samples:
            pairs = [
                (feature_value, future)
                for features, future in samples
                if (feature_value := getter(features)) is not None
            ]
            if len(pairs) < min_observations:
                continue
            ic = spearman(
                [item[0] for item in pairs],
                [item[1] for item in pairs],
            )
            if ic is not None:
                values.append(ic)
        average = mean(values)
        low, high = bootstrap_mean_ci(values, iterations=iterations, seed=seed)
        rows.append(
            {
                "kind": "feature_ic",
                "feature": name,
                "cohorts": len(values),
                "mean_ic": str(average) if average is not None else None,
                "ci_low": str(low) if low is not None else None,
                "ci_high": str(high) if high is not None else None,
                "positive_share": str(
                    Decimal(sum(1 for value in values if value > ZERO)) / Decimal(len(values))
                )
                if values
                else None,
            }
        )
    rows.sort(
        key=lambda item: abs(Decimal(str(item["mean_ic"]))) if item["mean_ic"] else ZERO,
        reverse=True,
    )
    return rows


FEATURE_SPECS: tuple[tuple[str, FeatureGetter], ...] = (
    ("excess_3m", lambda item: item.excess_3m),
    ("excess_6m_ex_5d", lambda item: item.excess_6m_ex_5d),
    ("excess_12m", lambda item: item.excess_12m),
    ("sma50_slope_20", lambda item: item.sma50_slope_20),
    ("sma200_slope_20", lambda item: item.sma200_slope_20),
    ("trend_consistency_60", lambda item: item.trend_consistency_60),
    ("drawdown_252", lambda item: item.drawdown_252),
    ("turnover_ratio_5_60", lambda item: item.turnover_ratio_5_60),
    ("turnover_ratio_20_60", lambda item: item.turnover_ratio_20_60),
    ("up_down_volume_ratio_60", lambda item: item.up_down_volume_ratio_60),
    ("turnover_cv_60", lambda item: item.turnover_cv_60),
    ("max_turnover_share_60", lambda item: item.max_turnover_share_60),
    ("squeeze_20_60", lambda item: item.squeeze_20_60),
    ("breakout_20", lambda item: item.breakout_20),
    ("breakout_60", lambda item: item.breakout_60),
    ("breakout_63", lambda item: item.breakout_63),
    ("breakout_126", lambda item: item.breakout_126),
    ("breakout_volume_ratio_20", lambda item: item.breakout_volume_ratio_20),
    ("dist_high_60", lambda item: item.dist_high_60),
    ("dist_high_63", lambda item: item.dist_high_63),
    ("dist_high_126", lambda item: item.dist_high_126),
    ("volatility_60", lambda item: item.volatility_60),
    ("max_drawdown_126", lambda item: item.max_drawdown_126),
    ("atr_14_pct", lambda item: item.atr_14_pct),
    ("large_gap_freq_60", lambda item: item.large_gap_freq_60),
    ("beta_126", lambda item: item.beta_126),
)
