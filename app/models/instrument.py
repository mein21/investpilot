from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AssetClass
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.position import Position


class Instrument(TimestampMixin, Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    figi: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    class_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    isin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    asset_class: Mapped[AssetClass] = mapped_column(
        enum_type(AssetClass, length=16), default=AssetClass.OTHER, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    lot: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    exchange: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    issuer_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country_of_risk: Mapped[str | None] = mapped_column(String(8), nullable=True)
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    nominal: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    coupon_quantity_per_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    income_yield: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    liquidity_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    volatility_30d: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)

    trading_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    buy_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sell_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_trade_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_price: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_price_source: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    is_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_buy_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)

    positions: Mapped[list[Position]] = relationship(back_populates="instrument")
