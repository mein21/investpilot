from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ExecutionMode, OrderSide, OrderStatus, OrderType
from app.core.time import utc_now
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.instrument import Instrument
    from app.models.recommendation import Recommendation


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), index=True, nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    client_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    side: Mapped[OrderSide] = mapped_column(enum_type(OrderSide, length=8), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(
        enum_type(OrderType, length=16), default=OrderType.LIMIT, nullable=False
    )
    status: Mapped[OrderStatus] = mapped_column(
        enum_type(OrderStatus, length=24),
        default=OrderStatus.DRAFT,
        index=True,
        nullable=False,
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        enum_type(ExecutionMode, length=16), nullable=False
    )
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    lots: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    limit_price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    estimated_amount: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    estimated_amount_base: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    commission_estimate: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    commission_estimate_base: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)

    lots_executed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_amount: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    executed_commission: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    average_executed_price: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )

    broker_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    response_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now)

    account: Mapped[Account] = relationship(back_populates="orders")
    instrument: Mapped[Instrument] = relationship()
    recommendation: Mapped[Recommendation | None] = relationship(back_populates="orders")
    executions: Mapped[list[OrderExecution]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderExecution(Base):
    __tablename__ = "order_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    lots: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    commission: Mapped[Decimal] = mapped_column(DecimalType(), default=Decimal("0"), nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    order: Mapped[Order] = relationship(back_populates="executions")
