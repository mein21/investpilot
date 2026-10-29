from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.integrations.tinvest.exceptions import TInvestUnavailableError
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.instrument_metric import InstrumentMetric
from app.services.growth import (
    GrowthService,
    growth_coverage,
    growth_score_from_record,
    load_growth_scores,
    top_growth,
)
from app.services.sync import SyncService
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def prepare(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    **overrides: object,
) -> tuple[Settings, GrowthService, MockTInvestClient]:
    values: dict[str, object] = {
        "auth_enabled": False,
        "tinvest_client": "mock",
        "seed_demo_data": False,
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    client = MockTInvestClient(clock=clock)
    SyncService(session_factory, client, settings, clock=clock).run()
    return settings, GrowthService(session_factory, client, settings, clock=clock), client


class BrokenCandlesClient(MockTInvestClient):
    def get_candles(self, *args: object, **kwargs: object) -> list[object]:
        raise TInvestUnavailableError("candles unavailable")


def test_scan_caches_metrics_and_respects_ttl(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings, service, _client = prepare(session_factory, clock)
    now = clock.now()

    report = service.scan(now=now)

    assert report.scanned == 3
    assert report.analyzed == 3
    assert report.failed == 0
    assert report.insufficient == 0
    with session_factory() as session:
        records = list(session.scalars(select(InstrumentMetric)))
        assert len(records) == 3
        assert all(record.status == "OK" for record in records)
        assert all(record.candle_count >= 63 for record in records)
        assert all(record.computed_at == now for record in records)
        coverage = growth_coverage(session, settings, now=now)
        assert coverage.candidates == 3
        assert coverage.fresh == 3
        ranked = top_growth(session, settings, now=now, limit=2)
        assert len(ranked) == 2
        assert ranked[0].score.composite >= ranked[1].score.composite
        scores = load_growth_scores(
            session,
            settings,
            [record.instrument_id for record in records],
            now=now,
        )
        assert len(scores) == 3
        assert all(Decimal("0") <= score.composite <= Decimal("100") for score in scores.values())

    fresh = service.scan(now=now)
    assert fresh.scanned == 0
    assert fresh.skipped_fresh == 3

    forced = service.scan(limit=2, force=True, now=now)
    assert forced.scanned == 2
    assert forced.analyzed == 2

    with session_factory() as session:
        record = session.scalar(select(InstrumentMetric))
        assert record is not None
        record.computed_at = now - timedelta(hours=settings.growth_cache_hours + 1)
        session.commit()
    stale = service.scan(now=now)
    assert stale.scanned == 1


def test_scan_stores_insufficient_and_error_states(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings, _service, _client = prepare(session_factory, clock)
    broken = GrowthService(
        session_factory,
        BrokenCandlesClient(clock=clock),
        settings,
        clock=clock,
    )
    report = broken.scan(now=clock.now(), limit=2)

    assert report.failed == 2
    assert report.analyzed == 0
    assert len(report.warnings) == 2
    with session_factory() as session:
        records = list(session.scalars(select(InstrumentMetric)))
        assert len(records) == 2
        assert all(record.status == "ERROR" for record in records)
        assert all("TInvestUnavailableError" in record.note for record in records)


def test_score_rejects_stale_metrics(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings, service, _client = prepare(session_factory, clock)
    now = clock.now()
    service.scan(now=now)
    strict = settings.model_copy(update={"growth_metric_max_age_hours": 1})

    with session_factory() as session:
        record = session.scalar(select(InstrumentMetric))
        assert record is not None
        record.computed_at = now - timedelta(hours=2)
        session.flush()
        assert growth_score_from_record(record, strict, now=now) is None
        assert growth_score_from_record(record, settings, now=now) is not None


def test_scan_disabled_returns_warning(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings, _service, _client = prepare(
        session_factory,
        clock,
        growth_analysis_enabled=False,
    )
    service = GrowthService(
        session_factory,
        MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    )
    report = service.scan(now=clock.now())

    assert report.scanned == 0
    assert "отключён" in report.warnings[0]


def test_coverage_and_rankings_are_empty_when_disabled(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings, service, _client = prepare(
        session_factory,
        clock,
        growth_analysis_enabled=False,
    )
    now = clock.now()
    service.scan(now=now)
    with session_factory() as session:
        assert growth_coverage(session, settings, now=now).candidates == 0
        assert top_growth(session, settings, now=now) == []
