from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from app.core.enums import RiskProfile
from app.rules.growth import GrowthScore
from app.rules.scoring import (
    BUY_COMPONENT_SPECS,
    SELL_COMPONENT_SPECS,
    ComponentSpec,
    score_buy_candidate,
    score_sell_candidate,
)
from app.screener.domain import ScreenerRating


def buy_score(**overrides: object):
    values: dict[str, object] = {
        "allocation_gap_percent": Decimal("2"),
        "rebalance_threshold_percent": Decimal("1"),
        "issuer_weight_percent": Decimal("10"),
        "risk_profile": RiskProfile.BALANCED,
        "volatility_30d": Decimal("30"),
        "income_yield": Decimal("5"),
        "liquidity_flag": True,
    }
    values.update(overrides)
    return score_buy_candidate(**values)  # type: ignore[arg-type]


def sell_score(**overrides: object):
    values: dict[str, object] = {
        "class_overweight_percent": Decimal("5"),
        "rebalance_threshold_percent": Decimal("1"),
        "issuer_weight_percent": Decimal("20"),
        "max_issuer_percent": Decimal("25"),
        "risk_profile": RiskProfile.CONSERVATIVE,
        "volatility_30d": Decimal("15"),
        "liquidity_flag": None,
    }
    values.update(overrides)
    return score_sell_candidate(**values)  # type: ignore[arg-type]


def test_buy_score_is_reproducible_and_decimal_only() -> None:
    first = buy_score()
    second = buy_score()

    assert first == second
    assert first.score == Decimal("60")
    assert isinstance(first.score, Decimal)
    for item in first.components:
        assert isinstance(item.value, Decimal)
        assert isinstance(item.weight, Decimal)
        assert isinstance(item.contribution, Decimal)
        assert all(isinstance(value, str) for value in item.as_dict().values())


def test_buy_component_metadata_and_weights_are_explicit() -> None:
    result = buy_score()

    assert [item.spec.code for item in result.components] == [
        "allocation_need",
        "momentum",
        "trend_quality",
        "risk_fit",
        "diversification",
        "stability",
        "income",
        "liquidity",
    ]
    assert sum((item.weight for item in result.components), Decimal("0")) == Decimal("1.00")
    for item in result.components:
        assert item.spec in BUY_COMPONENT_SPECS.values()
        assert item.spec.title
        assert item.spec.description
        assert item.spec.minimum == Decimal("0")
        assert item.spec.maximum == Decimal("100")
        payload = item.as_dict()
        assert payload["minimum"] == "0"
        assert payload["maximum"] == "100"
        assert payload["description"] == item.spec.description
        assert Decimal("0") <= item.value <= Decimal("100")


def test_allocation_need_grows_with_gap_and_is_capped() -> None:
    small = buy_score(allocation_gap_percent=Decimal("0.5")).component("allocation_need")
    large = buy_score(allocation_gap_percent=Decimal("2")).component("allocation_need")
    capped = buy_score(allocation_gap_percent=Decimal("100")).component("allocation_need")

    assert small.value == Decimal("10")
    assert large.value == Decimal("40")
    assert capped.value == Decimal("100")


def test_zero_threshold_does_not_divide_by_zero() -> None:
    result = buy_score(rebalance_threshold_percent=Decimal("0"))

    assert result.component("allocation_need").value == Decimal("100")


def test_risk_fit_depends_on_profile_and_volatility() -> None:
    conservative = buy_score(
        risk_profile=RiskProfile.CONSERVATIVE,
        volatility_30d=Decimal("15"),
    ).component("risk_fit")
    balanced = buy_score(
        risk_profile=RiskProfile.BALANCED,
        volatility_30d=Decimal("35"),
    ).component("risk_fit")
    aggressive = buy_score(
        risk_profile=RiskProfile.AGGRESSIVE,
        volatility_30d=Decimal("35"),
    ).component("risk_fit")
    unknown = buy_score(volatility_30d=None).component("risk_fit")

    assert conservative.value == Decimal("100")
    assert balanced.value == Decimal("75")
    assert aggressive.value == Decimal("100")
    assert unknown.value == Decimal("50")


def test_diversification_and_income_components_are_bounded() -> None:
    concentrated = buy_score(issuer_weight_percent=Decimal("80")).component("diversification")
    diversified = buy_score(issuer_weight_percent=Decimal("0")).component("diversification")
    high_income = buy_score(income_yield=Decimal("50")).component("income")
    no_income = buy_score(income_yield=None).component("income")

    assert concentrated.value == Decimal("0")
    assert diversified.value == Decimal("100")
    assert high_income.value == Decimal("100")
    assert no_income.value == Decimal("0")


def test_growth_components_raise_or_lower_buy_score() -> None:
    strong = buy_score(
        growth=GrowthScore(
            momentum=Decimal("100"),
            trend=Decimal("100"),
            stability=Decimal("100"),
            composite=Decimal("100"),
            momentum_reason="strong momentum",
            trend_reason="strong trend",
            stability_reason="smooth",
        )
    )
    weak = buy_score(
        growth=GrowthScore(
            momentum=Decimal("0"),
            trend=Decimal("0"),
            stability=Decimal("0"),
            composite=Decimal("0"),
            momentum_reason="weak momentum",
            trend_reason="weak trend",
            stability_reason="deep drawdown",
        )
    )

    assert strong.score == Decimal("80")
    assert weak.score == Decimal("40")
    assert strong.component("momentum").reason == "strong momentum"
    assert weak.component("trend_quality").reason == "weak trend"
    assert buy_score().component("stability").value == Decimal("50")


def test_liquidity_component_mapping() -> None:
    assert buy_score(liquidity_flag=True).component("liquidity").value == Decimal("100")
    assert buy_score(liquidity_flag=False).component("liquidity").value == Decimal("20")
    assert buy_score(liquidity_flag=None).component("liquidity").value == Decimal("50")


def test_sell_score_components_and_exact_value() -> None:
    result = sell_score()

    assert result.score == Decimal("91")
    assert [item.spec.code for item in result.components] == [
        "allocation_need",
        "risk_reduction",
        "concentration",
        "liquidity",
    ]
    assert sum((item.weight for item in result.components), Decimal("0")) == Decimal("1.00")
    for item in result.components:
        assert item.spec in SELL_COMPONENT_SPECS.values()
        assert item.spec.description
        assert item.spec.minimum == Decimal("0")
        assert item.spec.maximum == Decimal("100")
    assert result.component("allocation_need").reason == "перевес класса"
    assert result.component("risk_reduction").reason == "снижение риска"
    assert result.component("concentration").reason == "концентрация"


def test_sell_risk_reduction_and_concentration_are_capped() -> None:
    result = sell_score(
        volatility_30d=Decimal("600"),
        issuer_weight_percent=Decimal("25"),
        max_issuer_percent=Decimal("25"),
    )

    assert result.component("risk_reduction").value == Decimal("100")
    assert result.component("concentration").value == Decimal("100")
    assert sell_score(volatility_30d=None).component("risk_reduction").value == Decimal("50")


def test_scores_stay_within_bounds_for_extreme_inputs() -> None:
    for gap in (Decimal("0"), Decimal("100")):
        for issuer in (Decimal("0"), Decimal("100")):
            for income in (None, Decimal("0"), Decimal("100")):
                result = buy_score(
                    allocation_gap_percent=gap,
                    issuer_weight_percent=issuer,
                    income_yield=income,
                )
                assert Decimal("0") <= result.score <= Decimal("100")
                assert all(
                    Decimal("0") <= item.value <= Decimal("100") for item in result.components
                )

    for gap in (Decimal("0"), Decimal("100")):
        result = sell_score(class_overweight_percent=gap)
        assert Decimal("0") <= result.score <= Decimal("100")


def test_component_spec_is_immutable_and_lookup_rejects_unknown_code() -> None:
    spec = ComponentSpec(code="x", title="X", description="desc")
    with pytest.raises(AttributeError):
        spec.code = "y"  # type: ignore[misc]
    with pytest.raises(KeyError):
        buy_score().component("unknown")


def _screener_rating(score: str = "90") -> ScreenerRating:
    return ScreenerRating(
        uid="uid-gazp",
        ticker="GAZP",
        score=Decimal(score),
        rank=1,
        as_of=date(2026, 10, 27),
        mode="technical",
        generated_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        quality_label="чистые",
        blocks={
            "relative_strength": Decimal("88"),
            "trend_quality": Decimal("91"),
            "volume_confirmation": Decimal("75"),
            "consolidation_breakout": None,
            "risk_moderation": Decimal("80"),
        },
        penalties=("высокая волатильность",),
    )


def test_screener_rating_replaces_technical_components() -> None:
    result = buy_score(screener=_screener_rating())

    assert [item.spec.code for item in result.components] == [
        "allocation_need",
        "screener_rating",
        "risk_fit",
        "diversification",
        "income",
        "liquidity",
    ]
    assert sum((item.weight for item in result.components), Decimal("0")) == Decimal("1.00")
    assert result.component("screener_rating").value == Decimal("90")
    assert result.component("screener_rating").weight == Decimal("0.40")
    assert "скринер 90.0/100" in result.component("screener_rating").reason
    assert "штрафы: высокая волатильность" in result.component("screener_rating").reason
    assert result.score == Decimal("76")


def test_screener_rating_orders_candidates_without_growth_score() -> None:
    strong = buy_score(screener=_screener_rating("95"))
    weak = buy_score(screener=_screener_rating("40"))

    assert strong.score > weak.score
    assert strong.component("screener_rating").value == Decimal("95")
    assert weak.component("screener_rating").value == Decimal("40")
