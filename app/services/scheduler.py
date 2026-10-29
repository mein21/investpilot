"""Lightweight asyncio scheduler for sync, snapshots and backups."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import Severity, TradingMode
from app.db.session import session_scope
from app.integrations.tinvest.client import TInvestClient
from app.models.account import Account
from app.models.goal import Goal
from app.services.audit import record_audit
from app.services.backup import BackupError, BackupService
from app.services.growth import GrowthService
from app.services.recommendations import RecommendationService
from app.services.snapshots import capture_all_snapshots
from app.services.sync import SyncService

LOGGER = logging.getLogger(__name__)
TICK_SECONDS = 30.0


@dataclass(frozen=True)
class _AutoRecommendationResult:
    account_id: int
    goal_id: int
    count: int
    warnings: tuple[str, ...]


class SchedulerService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        client: TInvestClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._client = client
        self._clock = clock or SystemClock()
        self._last_sync: datetime | None = None
        self._last_market_data: datetime | None = None
        self._last_growth: datetime | None = None
        self._last_recommendations: datetime | None = None
        self._last_universe: datetime | None = None
        self._last_snapshot: datetime | None = None
        self._last_backup: datetime | None = None

    def start(self) -> list[asyncio.Task[None]]:
        if not self._settings.scheduler_enabled:
            return []
        return [asyncio.create_task(self._run(), name="investpilot-scheduler")]

    async def stop(self, tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await asyncio.to_thread(self.run_due)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("scheduled job failed")

    def run_due(self, *, now: datetime | None = None) -> None:
        at = now or self._clock.now()
        if self._due(self._last_sync, at, self._settings.sync_interval_minutes * 60):
            self._run_sync(at)
            self._last_sync = at
        if self._due(
            self._last_market_data,
            at,
            self._settings.market_data_interval_minutes * 60,
        ):
            self._run_market_data(at)
            self._last_market_data = at
        if self._settings.growth_analysis_enabled and self._due(
            self._last_growth,
            at,
            self._settings.growth_scan_interval_minutes * 60,
        ):
            self._run_growth(at)
            self._last_growth = at
        if self._settings.recommendations_auto_enabled and self._due(
            self._last_recommendations,
            at,
            self._settings.recommendations_auto_interval_minutes * 60,
        ):
            self._run_recommendations(at)
            self._last_recommendations = at
        if self._settings.universe_auto_load_enabled and self._due(
            self._last_universe,
            at,
            self._settings.universe_auto_load_interval_hours * 3600,
        ):
            self._run_universe(at)
            self._last_universe = at
        if self._due(
            self._last_snapshot,
            at,
            self._settings.snapshot_interval_minutes * 60,
        ):
            self._run_snapshots(at)
            self._last_snapshot = at
        if self._due(
            self._last_backup,
            at,
            self._settings.backup_interval_hours * 3600,
        ):
            self._run_backup(at)
            self._last_backup = at

    @staticmethod
    def _due(last: datetime | None, now: datetime, interval_seconds: int) -> bool:
        if last is None:
            return True
        return (now - last).total_seconds() >= interval_seconds

    def _run_sync(self, at: datetime) -> None:
        report = SyncService(
            self._session_factory,
            self._client,
            self._settings,
            clock=self._clock,
        ).run()
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.sync",
                actor="scheduler",
                entity_type="sync_run",
                entity_id=report.run_id,
                message=f"Плановая синхронизация: {report.status.value}",
                severity=Severity.INFO if report.status.value == "OK" else Severity.WARNING,
                response_payload={
                    "accounts": report.accounts_synced,
                    "instruments": report.instruments_synced,
                    "operations": report.operations_synced,
                    "errors": list(report.errors),
                },
            )
        LOGGER.info("scheduled sync completed: %s", report.status.value)

    def _run_market_data(self, at: datetime) -> None:
        count, warnings = SyncService(
            self._session_factory,
            self._client,
            self._settings,
            clock=self._clock,
        ).refresh_market_data()
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.market_data",
                actor="scheduler",
                message=f"Котировки обновлены: {count} инструментов",
                severity=Severity.WARNING if warnings else Severity.INFO,
                response_payload={
                    "instruments": count,
                    "warnings": list(warnings),
                },
            )
        LOGGER.info("scheduled market data refreshed: %s instruments", count)

    def _run_growth(self, at: datetime) -> None:
        report = GrowthService(
            self._session_factory,
            self._client,
            self._settings,
            clock=self._clock,
        ).scan(now=at)
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.growth",
                actor="scheduler",
                message=(
                    f"Анализ роста: {report.analyzed} из {report.scanned} "
                    f"(пропущено свежих: {report.skipped_fresh})"
                ),
                severity=Severity.WARNING
                if report.failed or report.insufficient
                else Severity.INFO,
                response_payload={
                    "scanned": report.scanned,
                    "analyzed": report.analyzed,
                    "insufficient": report.insufficient,
                    "failed": report.failed,
                    "warnings": list(report.warnings[:10]),
                },
            )
        LOGGER.info(
            "scheduled growth scan: analyzed=%s failed=%s",
            report.analyzed,
            report.failed,
        )

    def _run_recommendations(self, at: datetime) -> None:
        if self._settings.trading_mode is TradingMode.OBSERVE:
            return
        with self._session_factory() as session:
            rows = session.execute(
                select(Account.id, Goal.id)
                .join(Goal, Goal.account_id == Account.id)
                .where(Goal.is_active.is_(True))
                .order_by(Account.id, Goal.id)
            ).all()
        targets: list[tuple[int, int]] = []
        seen: set[int] = set()
        for account_id, goal_id in rows:
            if account_id in seen:
                continue
            seen.add(account_id)
            targets.append((account_id, goal_id))
        if not targets:
            return
        results: list[_AutoRecommendationResult] = []
        failures = 0
        for account_id, goal_id in targets:
            try:
                with session_scope(self._session_factory) as session:
                    run = RecommendationService(
                        self._settings,
                        clock=self._clock,
                    ).generate(
                        session,
                        account_id=account_id,
                        goal_id=goal_id,
                        now=at,
                    )
                results.append(
                    _AutoRecommendationResult(
                        account_id=account_id,
                        goal_id=goal_id,
                        count=len(run.recommendations),
                        warnings=tuple(run.warnings[:5]),
                    )
                )
            except Exception:
                failures += 1
                LOGGER.exception(
                    "scheduled recommendations failed for account %s",
                    account_id,
                )
        generated = sum(item.count for item in results)
        warnings = [warning for item in results for warning in item.warnings]
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.recommendations",
                actor="scheduler",
                message=f"Рекомендации обновлены: {generated} (счетов: {len(results)})",
                severity=Severity.WARNING if failures or warnings else Severity.INFO,
                response_payload={
                    "generated": generated,
                    "accounts": [
                        {
                            "account_id": item.account_id,
                            "goal_id": item.goal_id,
                            "count": item.count,
                            "warnings": list(item.warnings),
                        }
                        for item in results
                    ],
                    "failed": failures,
                    "warnings": warnings[:10],
                },
            )
        LOGGER.info(
            "scheduled recommendations generated: %s accounts=%s failed=%s",
            generated,
            len(results),
            failures,
        )

    def _run_universe(self, at: datetime) -> None:
        count, warnings = SyncService(
            self._session_factory,
            self._client,
            self._settings,
            clock=self._clock,
        ).load_universe()
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.universe",
                actor="scheduler",
                message=f"Universe обновлён: {count} инструментов",
                severity=Severity.WARNING if warnings else Severity.INFO,
                response_payload={
                    "instruments": count,
                    "warnings": list(warnings[:10]),
                },
            )
        LOGGER.info("scheduled universe refreshed: %s instruments", count)

    def _run_snapshots(self, at: datetime) -> None:
        captured = capture_all_snapshots(
            self._session_factory,
            self._settings,
            source="scheduler",
            clock=self._clock,
        )
        if captured:
            with session_scope(self._session_factory) as session:
                record_audit(
                    session,
                    self._settings,
                    event_type="scheduler.snapshot",
                    actor="scheduler",
                    message=f"Снимки портфеля: {captured}",
                    response_payload={"accounts": captured},
                )
        LOGGER.info("scheduled snapshots captured: %s", captured)

    def _run_backup(self, at: datetime) -> None:
        try:
            result = BackupService(self._settings, clock=self._clock).create(now=at)
        except BackupError as exc:
            with session_scope(self._session_factory) as session:
                record_audit(
                    session,
                    self._settings,
                    event_type="scheduler.backup_failed",
                    actor="scheduler",
                    severity=Severity.ERROR,
                    message=str(exc),
                )
            LOGGER.warning("scheduled backup failed: %s", exc)
            return
        with session_scope(self._session_factory) as session:
            record_audit(
                session,
                self._settings,
                event_type="scheduler.backup",
                actor="scheduler",
                message=f"Резервная копия: {result.path.name}",
                response_payload={
                    "path": str(result.path),
                    "size_bytes": result.size_bytes,
                    "removed": [str(path) for path in result.removed],
                },
            )
        LOGGER.info("scheduled backup completed: %s", result.path)
