from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import DecimalType, UTCDateTime

if TYPE_CHECKING:
    from app.models.account import Account


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    total_value: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    invested_value: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    cash_value: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    daily_pnl: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    allocation_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    weights_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    concentration_json: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="sync")

    account: Mapped[Account] = relationship(back_populates="snapshots")
