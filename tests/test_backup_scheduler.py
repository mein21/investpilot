from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.bootstrap import ensure_demo_goal
from app.core.config import Settings
from app.db.session import build_engine, build_session_factory, init_db, session_scope
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.account import Account
from app.models.audit import AuditLog
from app.models.recommendation import Recommendation
from app.models.snapshot import PortfolioSnapshot
from app.services.backup import BackupService
from app.services.scheduler import SchedulerService
from sqlalchemy import select


def file_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "auth_enabled": False,
        "tinvest_client": "mock",
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "seed_demo_data": False,
        "backup_dir": tmp_path / "backups",
        "backup_keep": 2,
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_backup_creates_copy_and_prunes(tmp_path: Path) -> None:
    settings = file_settings(tmp_path)
    engine = build_engine(settings.database_url)
    init_db(engine)
    engine.dispose()
    service = BackupService(settings)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = service.create(now=start)
    second = service.create(now=start + timedelta(seconds=1))
    third = service.create(now=start + timedelta(seconds=2))
    assert not first.path.exists()
    assert second.path.exists()
    assert third.path.exists()
    assert len(third.removed) == 1
    assert first.path.name in {path.name for path in third.removed}
    remaining = list(settings.backup_dir.glob("investpilot-*.db"))
    assert len(remaining) == 2
    with sqlite3.connect(remaining[0]) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "accounts" in tables


def test_scheduler_sync_snapshot_and_audit(tmp_path: Path) -> None:
    settings = file_settings(tmp_path)
    engine = build_engine(settings.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    client = MockTInvestClient()
    try:
        scheduler = SchedulerService(settings, factory, client)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        scheduler.run_due(now=start)
        scheduler.run_due(now=start + timedelta(minutes=59))
        scheduler.run_due(now=start + timedelta(minutes=61))
    finally:
        client.close()
        engine.dispose()
    with factory() as session:
        assert session.scalar(select(Account)) is not None
        assert session.scalar(select(PortfolioSnapshot)) is not None
        events = set(session.scalars(select(AuditLog.event_type)))
    assert {
        "scheduler.sync",
        "scheduler.market_data",
        "scheduler.snapshot",
        "scheduler.backup",
    } <= events
    with factory() as session:
        market_events = list(
            session.scalars(select(AuditLog).where(AuditLog.event_type == "scheduler.market_data"))
        )
    assert len(market_events) == 2
    assert list(settings.backup_dir.glob("investpilot-*.db"))


def test_scheduler_auto_generates_recommendations(tmp_path: Path) -> None:
    settings = file_settings(tmp_path, seed_demo_data=True)
    engine = build_engine(settings.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    client = MockTInvestClient()
    try:
        scheduler = SchedulerService(settings, factory, client)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        scheduler.run_due(now=start)
        with session_scope(factory) as session:
            ensure_demo_goal(session, settings)
        scheduler.run_due(now=start + timedelta(minutes=31))
    finally:
        client.close()
        engine.dispose()
    with factory() as session:
        recommendations = list(session.scalars(select(Recommendation)))
        events = set(session.scalars(select(AuditLog.event_type)))
    assert recommendations
    assert "scheduler.recommendations" in events
    assert "scheduler.universe" in events
