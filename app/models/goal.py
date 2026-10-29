from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AssetClass, RiskProfile
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.recommendation import Recommendation


class Goal(TimestampMixin, Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(
            "CAST(target_amount AS NUMERIC) >= 0",
            name="target_amount_non_negative",
        ),
        CheckConstraint(
            "CAST(min_cash_reserve AS NUMERIC) >= 0",
            name="min_cash_reserve_non_negative",
        ),
        CheckConstraint(
            "CAST(max_drawdown_percent AS NUMERIC) >= 0 "
            "AND CAST(max_drawdown_percent AS NUMERIC) <= 100",
            name="drawdown_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_amount: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    regular_contribution: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    contribution_frequency: Mapped[str] = mapped_column(
        String(16), nullable=False, default="MONTHLY"
    )
    min_cash_reserve: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("0"), nullable=False
    )
    max_drawdown_percent: Mapped[Decimal] = mapped_column(
        DecimalType(), default=Decimal("20"), nullable=False
    )
    risk_profile: Mapped[RiskProfile] = mapped_column(
        enum_type(RiskProfile, length=16), default=RiskProfile.BALANCED, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_loss_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    take_profit_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    take_profit_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)

    allocations: Mapped[list[GoalAllocation]] = relationship(
        back_populates="goal", cascade="all, delete-orphan", lazy="selectin"
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="goal", cascade="all, delete-orphan"
    )
    account: Mapped[Account | None] = relationship()


class GoalAllocation(Base):
    __tablename__ = "goal_allocations"
    __table_args__ = (
        UniqueConstraint("goal_id", "asset_class"),
        CheckConstraint(
            "CAST(min_percent AS NUMERIC) >= 0 "
            "AND CAST(min_percent AS NUMERIC) <= CAST(target_percent AS NUMERIC)",
            name="min_le_target",
        ),
        CheckConstraint(
            "CAST(target_percent AS NUMERIC) <= CAST(max_percent AS NUMERIC) "
            "AND CAST(max_percent AS NUMERIC) <= 100",
            name="target_le_max",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[int] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_class: Mapped[AssetClass] = mapped_column(
        enum_type(AssetClass, length=16), nullable=False
    )
    min_percent: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    target_percent: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    max_percent: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)

    goal: Mapped[Goal] = relationship(back_populates="allocations")
