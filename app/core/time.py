"""Timezone-safe datetime helpers. All persisted instants are UTC-aware."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; provide timezone information")
    return value.astimezone(UTC)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)


def to_iso_z(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def is_stale(as_of: datetime | None, now: datetime, max_age_seconds: int) -> bool:
    if as_of is None:
        return True
    return now - ensure_utc(as_of) > timedelta(seconds=max_age_seconds)


def age_seconds(as_of: datetime | None, now: datetime) -> int | None:
    if as_of is None:
        return None
    delta = now - ensure_utc(as_of)
    return max(0, int(delta.total_seconds()))


def start_of_utc_day(value: datetime) -> datetime:
    utc_value = ensure_utc(value)
    return datetime.combine(utc_value.date(), time.min, tzinfo=UTC)


def end_of_utc_day(value: datetime) -> datetime:
    return start_of_utc_day(value) + timedelta(days=1)


def target_date_as_datetime(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def days_until(value: date, now: datetime) -> int:
    return (value - now.date()).days
