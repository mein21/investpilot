from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utc_now
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.instrument import Instrument


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "instrument_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)
    blocked_quantity: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    average_price: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    average_price_fifo: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    current_price: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    current_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    current_nkd: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    realized_pnl_incomplete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    commission_paid: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    dividends_received: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    coupons_received: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    last_synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    account: Mapped[Account] = relationship(back_populates="positions")
    instrument: Mapped[Instrument] = relationship(back_populates="positions")


class CashBalance(TimestampMixin, Base):
    __tablename__ = "cash_balances"
    __table_args__ = (UniqueConstraint("account_id", "currency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)
    blocked: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)

    account: Mapped[Account] = relationship(back_populates="cash_balances")


class FxRate(TimestampMixin, Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("base_currency", "quote_currency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    rate: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
