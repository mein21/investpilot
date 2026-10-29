from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    OrderType,
    RecommendationAction,
    RecommendationStatus,
    TradingMode,
)
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.goal import Goal
    from app.models.instrument import Instrument
    from app.models.order import Order


class Recommendation(TimestampMixin, Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True, nullable=True
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), index=True, nullable=True
    )

    action: Mapped[RecommendationAction] = mapped_column(
        enum_type(RecommendationAction, length=16), nullable=False
    )
    status: Mapped[RecommendationStatus] = mapped_column(
        enum_type(RecommendationStatus, length=16),
        default=RecommendationStatus.NEW,
        index=True,
        nullable=False,
    )
    order_type: Mapped[OrderType] = mapped_column(
        enum_type(OrderType, length=16), default=OrderType.LIMIT, nullable=False
    )

    instrument_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    figi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, default="PORTFOLIO")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    lots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    quote_price: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    price_per_lot: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    estimated_amount: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    commission_estimate: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    triggered_rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    blocking_rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    expected_weight_after: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    expected_class_weight_after: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    current_weight: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    target_weight: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )

    score: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    score_breakdown: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, nullable=False
    )

    calculated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    quote_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    data_freshness_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mode: Mapped[TradingMode] = mapped_column(
        enum_type(TradingMode, length=16), default=TradingMode.RECOMMEND, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, default="rules-v4")

    account: Mapped[Account] = relationship(back_populates="recommendations")
    goal: Mapped[Goal | None] = relationship(back_populates="recommendations")
    instrument: Mapped[Instrument | None] = relationship()
    orders: Mapped[list[Order]] = relationship(back_populates="recommendation")
