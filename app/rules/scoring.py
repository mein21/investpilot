"""Deterministic, explainable instrument scoring.

The score ranks instruments for a rule-based recommendation. It is not a price
forecast and carries no investment guarantee. Every component is bounded to a
declared range, has an explicit weight and is reproducible for the same inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import RiskProfile
from app.core.money import clamp, quantize_percent
from app.rules.growth import GrowthScore, neutral_growth
from app.screener.domain import ScreenerRating

ZERO = Decimal("0")
HUNDRED = Decimal("100")
MIN_THRESHOLD = Decimal("0.01")

VOLATILITY_LIMITS: dict[RiskProfile, Decimal] = {
    RiskProfile.CONSERVATIVE: Decimal("15"),
    RiskProfile.BALANCED: Decimal("30"),
    RiskProfile.AGGRESSIVE: Decimal("60"),
    RiskProfile.CUSTOM: Decimal("30"),
}


@dataclass(frozen=True)
class ComponentSpec:
    code: str
    title: str
    description: str
    minimum: Decimal = ZERO
    maximum: Decimal = HUNDRED


BUY_COMPONENT_SPECS: dict[str, ComponentSpec] = {
    "allocation_need": ComponentSpec(
        code="allocation_need",
        title="Потребность в аллокации",
        description="Недобор целевой доли класса; выше — сильнее потребность в покупке.",
    ),
    "screener_rating": ComponentSpec(
        code="screener_rating",
        title="Рейтинг скринера",
        description=(
            "Итоговый A–E рейтинг детерминированного скринера: относительная сила, "
            "тренд, объёмы, пробой и риск. Выше — сильнее кандидат; это не вероятность."
        ),
    ),
    "momentum": ComponentSpec(
        code="momentum",
        title="Импульс роста",
        description=(
            "Доходность за 1/3/6/12 месяцев с поправкой на волатильность; "
            "выше — устойчивее исторический рост. Не прогноз цены."
        ),
    ),
    "trend_quality": ComponentSpec(
        code="trend_quality",
        title="Качество тренда",
        description=(
            "Доля дней выше SMA50/SMA200 и дистанция до скользящих средних; "
            "выше — согласованнее восходящий тренд."
        ),
    ),
    "risk_fit": ComponentSpec(
        code="risk_fit",
        title="Соответствие риску",
        description="Соответствие волатильности инструмента риск-профилю; выше — безопаснее.",
    ),
    "stability": ComponentSpec(
        code="stability",
        title="Устойчивость",
        description="Штраф за максимальную просадку за 6 месяцев; выше — ровнее путь цены.",
    ),
    "diversification": ComponentSpec(
        code="diversification",
        title="Диверсификация",
        description="Штраф за долю эмитента; выше — лучше диверсификация.",
    ),
    "income": ComponentSpec(
        code="income",
        title="Доходность",
        description="Дивидендная или купонная доходность инструмента; выше — больше дохода.",
    ),
    "liquidity": ComponentSpec(
        code="liquidity",
        title="Ликвидность",
        description="Ликвидность по флагу брокера: высокая, неизвестная или низкая.",
    ),
}

SELL_COMPONENT_SPECS: dict[str, ComponentSpec] = {
    "allocation_need": ComponentSpec(
        code="allocation_need",
        title="Перевес класса",
        description="Превышение целевой доли класса; выше — важнее сократить позицию.",
    ),
    "risk_reduction": ComponentSpec(
        code="risk_reduction",
        title="Снижение риска",
        description="Насколько продажа снижает волатильность портфеля.",
    ),
    "concentration": ComponentSpec(
        code="concentration",
        title="Концентрация",
        description="Вклад эмитента в концентрацию; выше — важнее сократить позицию.",
    ),
    "liquidity": ComponentSpec(
        code="liquidity",
        title="Ликвидность",
        description="Ликвидность по флагу брокера: высокая, неизвестная или низкая.",
    ),
}


@dataclass(frozen=True)
class ScoreComponent:
    spec: ComponentSpec
    value: Decimal
    weight: Decimal
    contribution: Decimal
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.spec.code,
            "title": self.spec.title,
            "description": self.spec.description,
            "minimum": str(self.spec.minimum),
            "maximum": str(self.spec.maximum),
            "value": str(quantize_percent(self.value)),
            "weight": str(self.weight),
            "contribution": str(quantize_percent(self.contribution)),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScoreResult:
    score: Decimal
    components: tuple[ScoreComponent, ...]

    @property
    def breakdown(self) -> list[dict[str, object]]:
        return [item.as_dict() for item in self.components]

    def component(self, code: str) -> ScoreComponent:
        for item in self.components:
            if item.spec.code == code:
                return item
        raise KeyError(code)


def _component(
    spec: ComponentSpec,
    value: Decimal,
    weight: Decimal,
    reason: str,
) -> ScoreComponent:
    normalized = clamp(value, spec.minimum, spec.maximum)
    return ScoreComponent(
        spec=spec,
        value=normalized,
        weight=weight,
        contribution=normalized * weight,
        reason=reason,
    )


def _threshold(value: Decimal) -> Decimal:
    return value if value > ZERO else MIN_THRESHOLD


def _allocation_need(gap: Decimal, threshold: Decimal) -> Decimal:
    return min(HUNDRED, max(ZERO, gap) / _threshold(threshold) * Decimal("20"))


def _risk_limit(profile: RiskProfile) -> Decimal:
    return VOLATILITY_LIMITS.get(profile, Decimal("30"))


def _risk_fit(volatility: Decimal | None, profile: RiskProfile) -> Decimal:
    limit = _risk_limit(profile)
    if volatility is None:
        return Decimal("50")
    if limit <= ZERO:
        return ZERO
    return clamp(HUNDRED - max(ZERO, volatility - limit) * Decimal("5"))


def _risk_reduction(volatility: Decimal | None, profile: RiskProfile) -> Decimal:
    limit = _risk_limit(profile)
    if volatility is not None and limit > ZERO:
        return min(HUNDRED, volatility / limit * HUNDRED)
    return HUNDRED / 2


def _liquidity(flag: bool | None) -> Decimal:
    if flag is True:
        return HUNDRED
    if flag is False:
        return Decimal("20")
    return Decimal("50")


def _total(components: tuple[ScoreComponent, ...]) -> Decimal:
    return quantize_percent(sum((item.contribution for item in components), ZERO))


def score_buy_candidate(
    *,
    allocation_gap_percent: Decimal,
    rebalance_threshold_percent: Decimal,
    issuer_weight_percent: Decimal,
    risk_profile: RiskProfile,
    volatility_30d: Decimal | None,
    income_yield: Decimal | None,
    liquidity_flag: bool | None,
    growth: GrowthScore | None = None,
    screener: ScreenerRating | None = None,
) -> ScoreResult:
    specs = BUY_COMPONENT_SPECS
    income = clamp((income_yield or ZERO) * Decimal("8")) if income_yield is not None else ZERO
    growth_score = growth or neutral_growth()
    components: list[ScoreComponent] = [
        _component(
            specs["allocation_need"],
            _allocation_need(allocation_gap_percent, rebalance_threshold_percent),
            Decimal("0.25"),
            "недобор класса",
        )
    ]
    if screener is not None:
        components.append(
            _component(
                specs["screener_rating"],
                screener.score,
                Decimal("0.40"),
                screener.reason,
            )
        )
    else:
        components.extend(
            (
                _component(
                    specs["momentum"],
                    growth_score.momentum,
                    Decimal("0.20"),
                    growth_score.momentum_reason,
                ),
                _component(
                    specs["trend_quality"],
                    growth_score.trend,
                    Decimal("0.15"),
                    growth_score.trend_reason,
                ),
            )
        )
    components.extend(
        (
            _component(
                specs["risk_fit"],
                _risk_fit(volatility_30d, risk_profile),
                Decimal("0.15"),
                "риск-профиль",
            ),
            _component(
                specs["diversification"],
                clamp(HUNDRED - issuer_weight_percent * Decimal("2")),
                Decimal("0.10"),
                "диверсификация",
            ),
        )
    )
    if screener is None:
        components.append(
            _component(
                specs["stability"],
                growth_score.stability,
                Decimal("0.05"),
                growth_score.stability_reason,
            )
        )
    components.extend(
        (
            _component(specs["income"], income, Decimal("0.05"), "доходность"),
            _component(
                specs["liquidity"],
                _liquidity(liquidity_flag),
                Decimal("0.05"),
                "ликвидность",
            ),
        )
    )
    result = tuple(components)
    return ScoreResult(score=_total(result), components=result)


def score_sell_candidate(
    *,
    class_overweight_percent: Decimal,
    rebalance_threshold_percent: Decimal,
    issuer_weight_percent: Decimal,
    max_issuer_percent: Decimal,
    risk_profile: RiskProfile,
    volatility_30d: Decimal | None,
    liquidity_flag: bool | None,
) -> ScoreResult:
    specs = SELL_COMPONENT_SPECS
    concentration = min(
        HUNDRED,
        issuer_weight_percent / max(max_issuer_percent, MIN_THRESHOLD) * HUNDRED,
    )
    components = (
        _component(
            specs["allocation_need"],
            _allocation_need(class_overweight_percent, rebalance_threshold_percent),
            Decimal("0.40"),
            "перевес класса",
        ),
        _component(
            specs["risk_reduction"],
            _risk_reduction(volatility_30d, risk_profile),
            Decimal("0.30"),
            "снижение риска",
        ),
        _component(
            specs["concentration"],
            concentration,
            Decimal("0.20"),
            "концентрация",
        ),
        _component(
            specs["liquidity"],
            _liquidity(liquidity_flag),
            Decimal("0.10"),
            "ликвидность",
        ),
    )
    return ScoreResult(score=_total(components), components=components)
