from __future__ import annotations

from decimal import Decimal

from app.screener.diagnostics import (
    block_bootstrap_rate_ci,
    correlation_rows,
    information_coefficients,
    spearman,
)
from helpers import feature


def test_spearman_is_one_for_monotonic_and_none_for_constant() -> None:
    xs = [Decimal("1"), Decimal("2"), Decimal("3")]
    ys = [Decimal("10"), Decimal("20"), Decimal("30")]
    assert spearman(xs, ys) == Decimal("1")
    assert spearman(xs, [Decimal("1"), Decimal("1"), Decimal("1")]) is None


def test_block_bootstrap_rate_is_reproducible_and_uses_cohorts() -> None:
    groups = [
        [True, True, False],
        [True, False, False],
        [False, False, False],
    ]
    first = block_bootstrap_rate_ci(groups, iterations=300, seed=7)
    second = block_bootstrap_rate_ci(groups, iterations=300, seed=7)
    low, high = first
    assert first == second
    assert low is not None and high is not None
    assert Decimal("0") <= low <= high <= Decimal("1")


def test_correlation_rows_finds_duplicate_signal() -> None:
    samples = [
        (feature(excess_3m=Decimal(index), excess_12m=Decimal(index) * 2), Decimal(index))
        for index in range(30)
    ]
    rows = correlation_rows(
        samples,
        (
            ("excess_3m", lambda item: item.excess_3m),
            ("excess_12m", lambda item: item.excess_12m),
        ),
        min_observations=10,
        threshold=Decimal("0.50"),
    )
    assert rows
    assert abs(Decimal(str(rows[0]["rho"]))) > Decimal("0.99")


def test_information_coefficient_reports_insufficient_cohorts() -> None:
    rows = information_coefficients(
        [],
        (("excess_3m", lambda item: item.excess_3m),),
        iterations=50,
    )
    assert rows[0]["feature"] == "excess_3m"
    assert rows[0]["mean_ic"] is None
    assert rows[0]["ci_low"] is None
