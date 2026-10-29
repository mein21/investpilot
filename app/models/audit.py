from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Severity
from app.core.time import utc_now
from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime
from app.models._types import enum_type

if TYPE_CHECKING:
    from app.models.account import Account


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    severity: Mapped[Severity] = mapped_column(
        enum_type(Severity, length=16), default=Severity.INFO, nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    request_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    response_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, index=True, nullable=False
    )


class RiskLimit(TimestampMixin, Base):
    __tablename__ = "risk_limits"
    __table_args__ = (UniqueConstraint("code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="AMOUNT")
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=True
    )
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[Severity] = mapped_column(
        enum_type(Severity, length=16), default=Severity.WARNING, nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, index=True, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    account: Mapped[Account | None] = relationship()


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"
    __table_args__ = (UniqueConstraint("key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
