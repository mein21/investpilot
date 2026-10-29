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
    from app.models.instrument import Instrument


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tinvest_operation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    parent_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), index=True, nullable=True
    )
    instrument_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    figi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operation_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    payment: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_rest: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True, nullable=False)
    trades: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)

    account: Mapped[Account] = relationship(back_populates="operations")
    instrument: Mapped[Instrument | None] = relationship()
