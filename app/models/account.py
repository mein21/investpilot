from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SyncStatus
from app.core.time import utc_now
from app.db.base import Base, TimestampMixin
from app.db.types import UTCDateTime
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.operation import Operation
    from app.models.order import Order
    from app.models.position import CashBalance, Position
    from app.models.recommendation import Recommendation
    from app.models.snapshot import PortfolioSnapshot


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tinvest_account_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    account_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    access_level: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    opened_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    positions: Mapped[list[Position]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    cash_balances: Mapped[list[CashBalance]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    operations: Mapped[list[Operation]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    orders: Mapped[list[Order]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[PortfolioSnapshot]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[SyncStatus] = mapped_column(
        enum_type(SyncStatus, length=16), default=SyncStatus.RUNNING, nullable=False
    )
    accounts_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    instruments_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operations_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
