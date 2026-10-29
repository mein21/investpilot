"""Portfolio snapshot capture helpers shared by web, CLI and scheduler."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.db.session import session_scope
from app.models.account import Account
from app.services.portfolio import PortfolioService


def capture_all_snapshots(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    source: str,
    clock: Clock | None = None,
) -> int:
    service = PortfolioService(settings, clock=clock or SystemClock())
    captured = 0
    with session_scope(session_factory) as session:
        account_ids = list(session.scalars(select(Account.id).order_by(Account.id)))
        for account_id in account_ids:
            service.capture_snapshot(
                session,
                account_id,
                source=source,
                view=service.build(session, account_id),
            )
            captured += 1
    return captured
