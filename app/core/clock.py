"""Injectable UTC clock, kept separate from business logic."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._value = value.astimezone(UTC)

    def now(self) -> datetime:
        return self._value

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> None:
        from datetime import timedelta

        self._value += timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._value = value.astimezone(UTC)
