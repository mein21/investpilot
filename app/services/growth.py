"""Cached deterministic growth scanner over daily T-Invest candles.

Scanning is intentionally separate from recommendations: candle history is
heavy, so each instrument is analyzed at most once per cache window and the
result is stored in ``instrument_metrics``. Recommendations read the cache and
fall back to a neutral growth score when history is missing or stale.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import AssetClass
from app.core.time import ensure_utc
from app.db.session import session_scope
from app.integrations.tinvest.client import TInvestClient
from app.integrations.tinvest.exceptions import TInvestError
from app.models.instrument import Instrument
from app.models.instrument_metric import InstrumentMetric
from app.rules.growth import (
    MIN_HISTORY_CANDLES,
    GrowthScore,
    TrendMetrics,
    analyze_candles,
    score_growth,
)

ZERO = Decimal("0")
OK_STATUS = "OK"
INSUFFICIENT_STATUS = "INSUFFICIENT"
ERROR_STATUS = "ERROR"
DEFAULT_GROWTH_CLASSES = (AssetClass.STOCK, AssetClass.ETF)
CASH_LIKE_VOL_PERCENT = Decimal("5")
CASH_LIKE_RETURN_6M_PERCENT = Decimal("10")
_SCAN_LOCK = threading.Lock()
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class GrowthScanReport:
    scanned: int = 0
    analyzed: int = 0
    insufficient: int = 0
    failed: int = 0
    skipped_fresh: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GrowthRankRow:
    instrument: Instrument
    metric: InstrumentMetric
    score: GrowthScore


@dataclass(frozen=True)
class GrowthCoverage:
    candidates: int
    fresh: int


@dataclass(frozen=True)
class _ScanTarget:
    instrument_id: int
    instrument_uid: str
    ticker: str
    computed_at: datetime | None
    liquid: bool


def growth_asset_classes(settings: Settings) -> tuple[AssetClass, ...]:
    parsed: list[AssetClass] = []
    for raw in settings.growth_asset_classes.split(","):
        code = raw.strip().upper()
        if not code:
            continue
        try:
            parsed.append(AssetClass(code))
        except ValueError:
            continue
    return tuple(parsed) or DEFAULT_GROWTH_CLASSES


def _fresh_cutoff(settings: Settings, now: datetime) -> datetime:
    return now - timedelta(hours=settings.growth_cache_hours)


def _record_is_fresh(record: InstrumentMetric, settings: Settings, now: datetime) -> bool:
    return ensure_utc(record.computed_at) >= _fresh_cutoff(settings, now)


def _is_growth_like(record: InstrumentMetric) -> bool:
    """Exclude cash-equivalent funds from the high-growth ranking.

    A low-volatility instrument with a sub-10% six-month return behaves like
    cash; letting it win a momentum screen would crowd out real growth ideas.
    """

    volatility = record.volatility_annual_percent
    return_6m = record.return_6m_percent
    if volatility is None or volatility >= CASH_LIKE_VOL_PERCENT:
        return True
    return return_6m is not None and return_6m >= CASH_LIKE_RETURN_6M_PERCENT


def _safe_note(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")[:200]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def trend_metrics_from_record(record: InstrumentMetric) -> TrendMetrics | None:
    if record.status != OK_STATUS or record.candle_count < MIN_HISTORY_CANDLES:
        return None
    if (
        record.window_start_at is None
        or record.window_end_at is None
        or record.last_close is None
        or record.volatility_annual_percent is None
        or record.max_drawdown_6m_percent is None
    ):
        return None
    return TrendMetrics(
        candle_count=record.candle_count,
        window_start_at=ensure_utc(record.window_start_at),
        window_end_at=ensure_utc(record.window_end_at),
        last_close=record.last_close,
        return_1m_percent=record.return_1m_percent,
        return_3m_percent=record.return_3m_percent,
        return_6m_percent=record.return_6m_percent,
        return_12m_percent=record.return_12m_percent,
        volatility_annual_percent=record.volatility_annual_percent,
        max_drawdown_6m_percent=record.max_drawdown_6m_percent,
        drawdown_from_peak_percent=record.drawdown_from_peak_percent or ZERO,
        sma_50_distance_percent=record.sma_50_distance_percent,
        sma_200_distance_percent=record.sma_200_distance_percent,
        above_sma_50_percent=record.above_sma_50_percent,
        above_sma_200_percent=record.above_sma_200_percent,
        avg_turnover=record.avg_turnover or ZERO,
    )


def growth_score_from_record(
    record: InstrumentMetric | None,
    settings: Settings,
    *,
    now: datetime,
) -> GrowthScore | None:
    if not settings.growth_analysis_enabled or record is None:
        return None
    max_age = timedelta(hours=settings.growth_metric_max_age_hours)
    if now - ensure_utc(record.computed_at) > max_age:
        return None
    metrics = trend_metrics_from_record(record)
    if metrics is None:
        return None
    return score_growth(metrics)


def load_growth_scores(
    session: Session,
    settings: Settings,
    instrument_ids: list[int],
    *,
    now: datetime,
) -> dict[int, GrowthScore]:
    if not settings.growth_analysis_enabled or not instrument_ids:
        return {}
    records = session.scalars(
        select(InstrumentMetric).where(InstrumentMetric.instrument_id.in_(instrument_ids))
    )
    result: dict[int, GrowthScore] = {}
    for record in records:
        if not _is_growth_like(record):
            continue
        score = growth_score_from_record(record, settings, now=now)
        if score is not None:
            result[record.instrument_id] = score
    return result


def growth_coverage(session: Session, settings: Settings, *, now: datetime) -> GrowthCoverage:
    if not settings.growth_analysis_enabled:
        return GrowthCoverage(candidates=0, fresh=0)
    classes = growth_asset_classes(settings)
    instruments = session.scalars(
        select(Instrument).where(
            Instrument.asset_class.in_(classes),
            Instrument.is_buy_candidate.is_(True),
            Instrument.is_excluded.is_(False),
            Instrument.buy_available.is_(True),
            Instrument.api_trade_available.is_(True),
        )
    )
    candidate_ids = {instrument.id for instrument in instruments}
    if not candidate_ids:
        return GrowthCoverage(candidates=0, fresh=0)
    records = session.scalars(
        select(InstrumentMetric).where(InstrumentMetric.instrument_id.in_(candidate_ids))
    )
    fresh = sum(
        1
        for record in records
        if record.status == OK_STATUS
        and record.instrument_id in candidate_ids
        and _record_is_fresh(record, settings, now)
    )
    return GrowthCoverage(candidates=len(candidate_ids), fresh=fresh)


def top_growth(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    limit: int = 8,
) -> list[GrowthRankRow]:
    if not settings.growth_analysis_enabled:
        return []
    rows = session.execute(
        select(InstrumentMetric, Instrument)
        .join(Instrument, InstrumentMetric.instrument_id == Instrument.id)
        .where(
            InstrumentMetric.status == OK_STATUS,
            Instrument.asset_class.in_(growth_asset_classes(settings)),
            Instrument.is_buy_candidate.is_(True),
            Instrument.is_excluded.is_(False),
            Instrument.buy_available.is_(True),
            Instrument.api_trade_available.is_(True),
        )
    ).all()
    ranked: list[GrowthRankRow] = []
    for metric, instrument in rows:
        if not _is_growth_like(metric):
            continue
        score = growth_score_from_record(metric, settings, now=now)
        if score is None:
            continue
        ranked.append(GrowthRankRow(instrument=instrument, metric=metric, score=score))
    ranked.sort(key=lambda item: (-item.score.composite, item.instrument.ticker))
    return ranked[: max(0, limit)]


class GrowthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: TInvestClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._settings = settings
        self._clock = clock or SystemClock()

    def scan(
        self,
        *,
        limit: int | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> GrowthScanReport:
        at = ensure_utc(now or self._clock.now())
        if not self._settings.growth_analysis_enabled:
            return GrowthScanReport(warnings=("анализ роста отключён настройкой",))
        if not _SCAN_LOCK.acquire(blocking=False):
            return GrowthScanReport(warnings=("сканирование роста уже выполняется",))
        try:
            return self._scan_locked(at, limit=limit, force=force)
        finally:
            _SCAN_LOCK.release()

    def _scan_locked(
        self,
        at: datetime,
        *,
        limit: int | None,
        force: bool,
    ) -> GrowthScanReport:
        effective_limit = max(
            1,
            min(limit if limit is not None else self._settings.growth_scan_limit, 500),
        )
        with self._session_factory() as session:
            targets, skipped_fresh = self._due_targets(session, at, force=force)
        selected = targets[:effective_limit]
        warnings: list[str] = []
        analyzed = insufficient = failed = 0
        date_from = at - timedelta(days=self._settings.growth_history_days)
        for target in selected:
            try:
                candles = self._client.get_candles(
                    target.instrument_uid,
                    date_from,
                    at,
                    "day",
                )
            except TInvestError as exc:
                failed += 1
                note = _safe_note(exc)
                warnings.append(f"{target.ticker}: история недоступна ({note})")
                self._store_failure(target, at, ERROR_STATUS, note)
                continue
            except Exception as exc:  # pragma: no cover - defensive isolation per instrument
                failed += 1
                note = _safe_note(exc)
                warnings.append(f"{target.ticker}: ошибка анализа ({note})")
                self._store_failure(target, at, ERROR_STATUS, note)
                continue
            metrics = analyze_candles(candles)
            if metrics is None:
                insufficient += 1
                warnings.append(
                    f"{target.ticker}: недостаточно истории "
                    f"({len(candles)} свечей, нужно {MIN_HISTORY_CANDLES})"
                )
                self._store_failure(
                    target,
                    at,
                    INSUFFICIENT_STATUS,
                    f"candles={len(candles)} < {MIN_HISTORY_CANDLES}",
                )
                continue
            self._store_metrics(target, metrics, at)
            analyzed += 1
        return GrowthScanReport(
            scanned=len(selected),
            analyzed=analyzed,
            insufficient=insufficient,
            failed=failed,
            skipped_fresh=skipped_fresh,
            warnings=tuple(warnings),
        )

    def _due_targets(
        self,
        session: Session,
        now: datetime,
        *,
        force: bool,
    ) -> tuple[list[_ScanTarget], int]:
        classes = growth_asset_classes(self._settings)
        rows = session.execute(
            select(Instrument, InstrumentMetric)
            .outerjoin(
                InstrumentMetric,
                InstrumentMetric.instrument_id == Instrument.id,
            )
            .where(
                Instrument.asset_class.in_(classes),
                Instrument.is_buy_candidate.is_(True),
                Instrument.is_excluded.is_(False),
                Instrument.buy_available.is_(True),
                Instrument.api_trade_available.is_(True),
            )
        ).all()
        cutoff = _fresh_cutoff(self._settings, now)
        due: list[_ScanTarget] = []
        fresh = 0
        for instrument, metric in rows:
            if not force and metric is not None and ensure_utc(metric.computed_at) >= cutoff:
                fresh += 1
                continue
            due.append(
                _ScanTarget(
                    instrument_id=instrument.id,
                    instrument_uid=instrument.instrument_uid,
                    ticker=instrument.ticker,
                    computed_at=(ensure_utc(metric.computed_at) if metric is not None else None),
                    liquid=bool(instrument.liquidity_flag),
                )
            )
        due.sort(
            key=lambda item: (
                not item.liquid,
                item.computed_at is not None,
                item.computed_at or _EPOCH,
                item.ticker,
            )
        )
        return due, fresh

    def _store_metrics(self, target: _ScanTarget, metrics: TrendMetrics, at: datetime) -> None:
        try:
            self._write_metrics(target, metrics, at)
        except IntegrityError:
            return

    def _write_metrics(self, target: _ScanTarget, metrics: TrendMetrics, at: datetime) -> None:
        with session_scope(self._session_factory) as session:
            record = self._record(session, target.instrument_id, at)
            if record is None:
                return
            record.status = OK_STATUS
            record.note = ""
            record.candle_count = metrics.candle_count
            record.window_start_at = metrics.window_start_at
            record.window_end_at = metrics.window_end_at
            record.last_close = metrics.last_close
            record.return_1m_percent = metrics.return_1m_percent
            record.return_3m_percent = metrics.return_3m_percent
            record.return_6m_percent = metrics.return_6m_percent
            record.return_12m_percent = metrics.return_12m_percent
            record.volatility_annual_percent = metrics.volatility_annual_percent
            record.max_drawdown_6m_percent = metrics.max_drawdown_6m_percent
            record.drawdown_from_peak_percent = metrics.drawdown_from_peak_percent
            record.sma_50_distance_percent = metrics.sma_50_distance_percent
            record.sma_200_distance_percent = metrics.sma_200_distance_percent
            record.above_sma_50_percent = metrics.above_sma_50_percent
            record.above_sma_200_percent = metrics.above_sma_200_percent
            record.avg_turnover = metrics.avg_turnover
            record.computed_at = at
            record.source = "tinvest-candles"

    def _store_failure(self, target: _ScanTarget, at: datetime, status: str, note: str) -> None:
        try:
            self._write_failure(target, at, status, note)
        except IntegrityError:
            return

    def _write_failure(
        self,
        target: _ScanTarget,
        at: datetime,
        status: str,
        note: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = self._record(session, target.instrument_id, at)
            if record is None:
                return
            record.status = status
            record.note = note[:300]
            record.candle_count = 0
            record.window_start_at = None
            record.window_end_at = None
            record.last_close = None
            record.return_1m_percent = None
            record.return_3m_percent = None
            record.return_6m_percent = None
            record.return_12m_percent = None
            record.volatility_annual_percent = None
            record.max_drawdown_6m_percent = None
            record.drawdown_from_peak_percent = None
            record.sma_50_distance_percent = None
            record.sma_200_distance_percent = None
            record.above_sma_50_percent = None
            record.above_sma_200_percent = None
            record.avg_turnover = None
            record.computed_at = at
            record.source = "tinvest-candles"

    def _record(
        self,
        session: Session,
        instrument_id: int,
        at: datetime,
    ) -> InstrumentMetric | None:
        if session.get(Instrument, instrument_id) is None:
            return None
        record = session.scalar(
            select(InstrumentMetric).where(InstrumentMetric.instrument_id == instrument_id)
        )
        if record is None:
            record = InstrumentMetric(instrument_id=instrument_id, computed_at=at)
            session.add(record)
        return record
