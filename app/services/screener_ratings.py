"""Read the latest deterministic screener run for recommendation scoring.

The screener and the portfolio database are separate SQLite stores. This
loader only reads the latest ``screen`` payload and maps its candidates to the
main ``Instrument`` records by UID, with a ticker fallback for older data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.time import ensure_utc
from app.models.instrument import Instrument
from app.screener.domain import ScreenerRating
from app.screener.store import ScreenerStore


@dataclass(frozen=True, slots=True)
class ScreenerRatingSet:
    ratings: tuple[ScreenerRating, ...] = ()
    by_uid: Mapping[str, ScreenerRating] = field(default_factory=dict)
    by_ticker: Mapping[str, ScreenerRating] = field(default_factory=dict)
    active: bool = False
    as_of: date | None = None
    mode: str = ""
    generated_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    def get(self, instrument: Instrument) -> ScreenerRating | None:
        rating = self.by_uid.get(instrument.instrument_uid)
        if rating is not None:
            return rating
        return self.by_ticker.get(instrument.ticker.upper())


def _safe_note(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")[:200]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: Any, fallback: date) -> datetime:
    if isinstance(value, str):
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            pass
    return datetime.combine(fallback, datetime.min.time(), tzinfo=UTC)


def _parse_rank(value: Any, fallback: int) -> int:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return fallback
    return rank if rank > 0 else fallback


def _penalty_reasons(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    reasons: list[str] = []
    for item in value:
        if isinstance(item, dict):
            reason = str(item.get("reason") or item.get("code") or "").strip()
        else:
            reason = str(item).strip()
        if reason:
            reasons.append(reason)
    return tuple(dict.fromkeys(reasons))


def _candidate_warnings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        str(item).strip()
        for item in value
        if str(item).strip()
    )


def _rating_from_candidate(
    item: Mapping[str, Any],
    *,
    fallback_rank: int,
    as_of: date,
    mode: str,
    generated_at: datetime,
) -> ScreenerRating | None:
    score = _parse_decimal(item.get("score"))
    if score is None:
        return None
    uid = str(item.get("uid") or "").strip()
    ticker = str(item.get("ticker") or "").strip().upper()
    if not uid and not ticker:
        return None
    quality = item.get("quality")
    quality = quality if isinstance(quality, dict) else {}
    raw_blocks = item.get("blocks")
    blocks: dict[str, Decimal | None] = {}
    if isinstance(raw_blocks, dict):
        for code, value in raw_blocks.items():
            blocks[str(code)] = _parse_decimal(value)
    return ScreenerRating(
        uid=uid,
        ticker=ticker,
        score=score,
        rank=_parse_rank(item.get("rank"), fallback_rank),
        as_of=as_of,
        mode=mode,
        generated_at=generated_at,
        quality_label=str(quality.get("label") or "").strip(),
        critical=bool(quality.get("critical", False)),
        blocks=blocks,
        penalties=_penalty_reasons(item.get("penalties")),
        warnings=_candidate_warnings(item.get("warnings")),
    )


def _build_set(
    payload: Mapping[str, Any],
    *,
    now: datetime,
    max_age_hours: int,
) -> ScreenerRatingSet:
    as_of = _parse_date(payload.get("as_of"))
    if as_of is None:
        return ScreenerRatingSet(
            warnings=("Скринер: в последнем прогоне нет корректной даты; growth-фолбэк",)
        )
    generated_at = _parse_datetime(payload.get("generated_at"), as_of)
    max_age = timedelta(hours=max_age_hours)
    if ensure_utc(now) - generated_at > max_age:
        return ScreenerRatingSet(
            warnings=(
                "Скринер: последний прогон от "
                f"{generated_at.isoformat()} старше {max_age_hours} ч; growth-фолбэк",
            )
        )
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return ScreenerRatingSet(
            warnings=("Скринер: в последнем прогоне нет кандидатов; growth-фолбэк",)
        )
    mode = str(payload.get("mode") or "").strip()
    ratings: list[ScreenerRating] = []
    by_uid: dict[str, ScreenerRating] = {}
    by_ticker: dict[str, ScreenerRating] = {}
    for index, item in enumerate(raw_candidates, start=1):
        if not isinstance(item, dict):
            continue
        rating = _rating_from_candidate(
            item,
            fallback_rank=index,
            as_of=as_of,
            mode=mode,
            generated_at=generated_at,
        )
        if rating is None:
            continue
        if rating.uid and rating.uid in by_uid:
            continue
        if rating.ticker and rating.ticker in by_ticker:
            continue
        ratings.append(rating)
        if rating.uid:
            by_uid[rating.uid] = rating
        if rating.ticker:
            by_ticker[rating.ticker] = rating
    if not ratings:
        return ScreenerRatingSet(
            warnings=("Скринер: кандидаты без корректного рейтинга; growth-фолбэк",)
        )
    return ScreenerRatingSet(
        ratings=tuple(ratings),
        by_uid=by_uid,
        by_ticker=by_ticker,
        active=True,
        as_of=as_of,
        mode=mode,
        generated_at=generated_at,
    )


def load_screener_ratings(
    settings: Settings,
    *,
    now: datetime,
) -> ScreenerRatingSet:
    if not settings.recommendations_screener_enabled:
        return ScreenerRatingSet()
    path = Path(settings.screener_db_path)
    if not path.exists():
        return ScreenerRatingSet(
            warnings=("Скринер: база не найдена; используется growth-фолбэк",)
        )
    try:
        payload = ScreenerStore(path).load_latest_run("screen")
    except Exception as exc:
        return ScreenerRatingSet(
            warnings=(
                "Скринер: не удалось прочитать последний прогон "
                f"({_safe_note(exc)}); используется growth-фолбэк",
            )
        )
    if payload is None:
        return ScreenerRatingSet(
            warnings=("Скринер: нет ни одного прогона; используется growth-фолбэк",)
        )
    return _build_set(
        payload,
        now=now,
        max_age_hours=settings.screener_rating_max_age_hours,
    )
