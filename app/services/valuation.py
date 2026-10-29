"""Exact valuation and FX helpers shared by portfolio and rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import AssetClass
from app.services.valuation_errors import MissingFxRateError

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def instrument_unit_value(
    asset_class: AssetClass,
    price: Decimal,
    *,
    nominal: Decimal | None = None,
    nkd: Decimal = ZERO,
) -> Decimal:
    """Value of one instrument unit in the portfolio's base currency.

    The portfolio API is requested in the base currency, so prices are treated
    as base-currency quotes. Bond quotes are percent-of-nominal.
    """

    if price <= ZERO:
        return ZERO
    if asset_class is AssetClass.BOND:
        bond_nominal = nominal or Decimal("1000")
        if bond_nominal <= ZERO:
            bond_nominal = Decimal("1000")
        return price / HUNDRED * bond_nominal + nkd
    if asset_class is AssetClass.CURRENCY:
        currency_nominal = nominal or ONE
        if currency_nominal <= ZERO:
            currency_nominal = ONE
        return price / currency_nominal
    return price


def instrument_order_price(
    asset_class: AssetClass,
    price: Decimal,
    *,
    nominal: Decimal | None = None,
) -> Decimal:
    """Price per instrument unit in the currency expected by the broker.

    Bond quotes arrive as percent-of-nominal, while ``OrdersService/PostOrder``
    expects a currency price for bonds as well.  Keep the conversion separate
    from valuation because order prices exclude accumulated coupon income.
    """

    if asset_class is AssetClass.BOND:
        bond_nominal = nominal or Decimal("1000")
        if bond_nominal <= ZERO:
            bond_nominal = Decimal("1000")
        return price / HUNDRED * bond_nominal
    return price


def position_value(
    asset_class: AssetClass,
    price: Decimal,
    quantity: Decimal,
    *,
    nominal: Decimal | None = None,
    nkd: Decimal = ZERO,
) -> Decimal:
    if quantity <= ZERO:
        return ZERO
    return (
        instrument_unit_value(
            asset_class,
            price,
            nominal=nominal,
            nkd=nkd,
        )
        * quantity
    )


@dataclass(frozen=True)
class FxTable:
    base_currency: str
    rates: dict[tuple[str, str], Decimal]

    @classmethod
    def from_rows(cls, base_currency: str, rows: list[tuple[str, str, Decimal]]) -> FxTable:
        base = base_currency.upper()
        rates: dict[tuple[str, str], Decimal] = {}
        for source, target, rate in rows:
            if rate <= ZERO:
                continue
            rates[(source.upper(), target.upper())] = rate
        return cls(base_currency=base, rates=rates)

    def convert(self, amount: Decimal, currency: str) -> Decimal:
        source = currency.upper()
        if source == self.base_currency:
            return amount
        direct = self.rates.get((source, self.base_currency))
        if direct is not None:
            return amount * direct
        inverse = self.rates.get((self.base_currency, source))
        if inverse is not None and inverse > ZERO:
            return amount / inverse
        raise MissingFxRateError(source, self.base_currency)
