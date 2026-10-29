"""Decimal-only money helpers.

No production calculation may convert money to float. All public helpers accept
Decimal and return Decimal, except the explicit T-Invest Quotation conversion
helpers that exchange integer units/nano.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
NANO = Decimal("1000000000")

MONEY_QUANT = Decimal("0.01")
PRICE_QUANT = Decimal("0.000000001")
QTY_QUANT = Decimal("0.000000001")
PERCENT_QUANT = Decimal("0.0001")


class MoneyError(ValueError):
    """Raised when a monetary value cannot be represented safely."""


def to_decimal(value: Any) -> Decimal:
    """Convert an incoming scalar to Decimal without binary-float drift.

    Floats are converted through their shortest decimal string representation.
    ``None`` and empty strings are rejected to avoid silently treating missing
    market data as zero.
    """

    if value is None:
        raise MoneyError("value is required")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise MoneyError("boolean is not a monetary value")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyError("empty value")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"invalid decimal value: {value!r}") from exc
    else:
        raise MoneyError(f"unsupported monetary type: {type(value).__name__}")
    if not result.is_finite():
        raise MoneyError("non-finite decimal value")
    return result


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def quantize_qty(value: Decimal) -> Decimal:
    return value.quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def quotation_to_decimal(units: int | str, nano: int | str) -> Decimal:
    """Convert official T-Invest Quotation/MoneyValue units+nano to Decimal."""

    return to_decimal(units) + to_decimal(nano) / NANO


def decimal_to_quotation(value: Decimal) -> tuple[int, int]:
    """Convert Decimal to official units+nano, truncating beyond nano safely."""

    normalized = quantize_price(to_decimal(value))
    units = int(normalized)
    nano = int((normalized - Decimal(units)) * NANO)
    return units, nano


def safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        return ZERO
    return numerator / denominator


def percent_of(value: Decimal, percent: Decimal) -> Decimal:
    return value * percent / HUNDRED


def percent_share(part: Decimal, total: Decimal) -> Decimal:
    if total == ZERO:
        return ZERO
    return part / total * HUNDRED


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise MoneyError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def clamp(value: Decimal, low: Decimal = ZERO, high: Decimal = HUNDRED) -> Decimal:
    return max(low, min(high, value))


def lots_floor(budget: Decimal, price_per_lot: Decimal) -> int:
    """Maximum whole lots affordable by budget; 0 when price is not positive."""

    if budget <= ZERO or price_per_lot <= ZERO:
        return 0
    return int((budget / price_per_lot).to_integral_value(rounding=ROUND_DOWN))


def lots_ceil(value_needed: Decimal, price_per_lot: Decimal) -> int:
    if value_needed <= ZERO or price_per_lot <= ZERO:
        return 0
    return int((value_needed / price_per_lot).to_integral_value(rounding="ROUND_CEILING"))


def is_positive(value: Decimal) -> bool:
    return value > ZERO


def abs_decimal(value: Decimal) -> Decimal:
    return value.copy_abs()
