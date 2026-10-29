from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.enums import AssetClass
from app.core.money import HUNDRED, quantize_money, quantize_percent
from app.models.snapshot import PortfolioSnapshot
from app.services.portfolio import PortfolioView

ZERO = Decimal("0")
CLASS_ORDER = (
    AssetClass.STOCK,
    AssetClass.BOND,
    AssetClass.ETF,
    AssetClass.CURRENCY,
    AssetClass.CASH,
    AssetClass.OTHER,
)


@dataclass(frozen=True)
class ScenarioRow:
    asset_class: AssetClass
    shock_percent: Decimal
    current_value: Decimal
    stressed_value: Decimal
    change: Decimal
    current_weight: Decimal
    stressed_weight: Decimal


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    base_currency: str
    current_total: Decimal
    stressed_total: Decimal
    change: Decimal
    change_percent: Decimal
    rows: tuple[ScenarioRow, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class HistoryStats:
    count: int
    start_at: datetime | None
    end_at: datetime | None
    start_value: Decimal
    end_value: Decimal
    total_return_percent: Decimal
    max_drawdown_percent: Decimal
    best_step_percent: Decimal
    worst_step_percent: Decimal


def run_scenario(
    portfolio: PortfolioView,
    shocks: Mapping[AssetClass, Decimal],
    *,
    name: str = "Пользовательский сценарий",
) -> ScenarioResult:
    values = {row.asset_class: row.value_base for row in portfolio.allocation}
    warnings: list[str] = []
    for asset_class, shock in shocks.items():
        if shock < Decimal("-100") or shock > Decimal("500"):
            raise ValueError(f"shock for {asset_class.value} must be between -100 and 500")
        if asset_class not in values:
            warnings.append(f"{asset_class.label_ru}: в портфеле нет активов этого класса")
    stressed_values = {
        asset_class: value * (HUNDRED + shocks.get(asset_class, ZERO)) / HUNDRED
        for asset_class, value in values.items()
    }
    stressed_total = sum(stressed_values.values(), ZERO)
    current_total = portfolio.total_value
    change = stressed_total - current_total
    rows: list[ScenarioRow] = []
    for asset_class in CLASS_ORDER:
        if asset_class not in values:
            continue
        current = values[asset_class]
        stressed = stressed_values[asset_class]
        rows.append(
            ScenarioRow(
                asset_class=asset_class,
                shock_percent=shocks.get(asset_class, ZERO),
                current_value=quantize_money(current),
                stressed_value=quantize_money(stressed),
                change=quantize_money(stressed - current),
                current_weight=quantize_percent(
                    current / current_total * HUNDRED if current_total > ZERO else ZERO
                ),
                stressed_weight=quantize_percent(
                    stressed / stressed_total * HUNDRED if stressed_total > ZERO else ZERO
                ),
            )
        )
    return ScenarioResult(
        name=name,
        base_currency=portfolio.base_currency,
        current_total=quantize_money(current_total),
        stressed_total=quantize_money(stressed_total),
        change=quantize_money(change),
        change_percent=quantize_percent(
            change / current_total * HUNDRED if current_total > ZERO else ZERO
        ),
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


def analyze_history(snapshots: list[PortfolioSnapshot]) -> HistoryStats | None:
    ordered = sorted(snapshots, key=lambda item: (item.captured_at, item.id))
    if not ordered:
        return None
    values = [item.total_value for item in ordered]
    start = values[0]
    end = values[-1]
    total_return = (end - start) / start * HUNDRED if start > ZERO else ZERO
    peak = start
    max_drawdown = ZERO
    steps: list[Decimal] = []
    previous: Decimal | None = None
    for value in values:
        if value > peak:
            peak = value
        if peak > ZERO:
            drawdown = (peak - value) / peak * HUNDRED
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        if previous is not None and previous > ZERO:
            steps.append((value - previous) / previous * HUNDRED)
        previous = value
    return HistoryStats(
        count=len(ordered),
        start_at=ordered[0].captured_at,
        end_at=ordered[-1].captured_at,
        start_value=quantize_money(start),
        end_value=quantize_money(end),
        total_return_percent=quantize_percent(total_return),
        max_drawdown_percent=quantize_percent(max_drawdown),
        best_step_percent=quantize_percent(max(steps, default=ZERO)),
        worst_step_percent=quantize_percent(min(steps, default=ZERO)),
    )
