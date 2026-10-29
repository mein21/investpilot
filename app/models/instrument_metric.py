from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import DecimalType, UTCDateTime

if TYPE_CHECKING:
    from app.models.instrument import Instrument


class InstrumentMetric(TimestampMixin, Base):
    """Cached deterministic growth metrics computed from daily candles."""

    __tablename__ = "instrument_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")
    note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    candle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    window_end_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_close: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    return_1m_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    return_3m_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    return_6m_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    return_12m_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    volatility_annual_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    max_drawdown_6m_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    drawdown_from_peak_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    sma_50_distance_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    sma_200_distance_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    above_sma_50_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    above_sma_200_percent: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    avg_turnover: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tinvest-candles")

    instrument: Mapped[Instrument] = relationship()
