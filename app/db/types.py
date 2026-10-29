"""Portable exact-value SQL types.

SQLite has no exact DECIMAL storage; SQLAlchemy's Numeric would round-trip via
float on this backend. Money is therefore stored as canonical decimal text,
which is exact on both SQLite and PostgreSQL. Sorting and aggregation are done
in Python with Decimal, never in the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class DecimalType(TypeDecorator[Decimal]):
    impl = String(48)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError("DecimalType accepts only Decimal values")
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be stored")
        return format(value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"invalid stored decimal: {value!r}") from exc
        if not result.is_finite():
            raise ValueError("non-finite Decimal in database")
        return result

    @property
    def python_type(self) -> type[Decimal]:
        return Decimal


class UTCDateTime(TypeDecorator[datetime]):
    """Stores ISO-8601 UTC text so SQLite cannot drop tzinfo."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UTCDateTime accepts only datetime values")
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be stored; use timezone-aware UTC")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @property
    def python_type(self) -> type[datetime]:
        return datetime
