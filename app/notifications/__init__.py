from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Severity
from app.core.time import ensure_utc, utc_now
from app.models.notification import Notification


def create_notification(
    session: Session,
    *,
    title: str,
    message: str,
    kind: str = "system",
    severity: Severity = Severity.INFO,
    account_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    at: datetime | None = None,
) -> Notification:
    notification = Notification(
        account_id=account_id,
        kind=kind,
        severity=severity.value,
        title=title[:200],
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
        is_read=False,
        created_at=ensure_utc(at or utc_now()),
    )
    session.add(notification)
    session.flush()
    return notification


def recent_notifications(
    session: Session,
    *,
    account_id: int | None = None,
    limit: int = 100,
) -> list[Notification]:
    query = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc())
    if account_id is not None:
        query = query.where(Notification.account_id == account_id)
    return list(session.scalars(query.limit(max(1, min(limit, 500)))))


def unread_count(session: Session, *, account_id: int | None = None) -> int:
    query = select(Notification).where(Notification.is_read.is_(False))
    if account_id is not None:
        query = query.where(Notification.account_id == account_id)
    return len(list(session.scalars(query)))


def mark_read(session: Session, notification_id: int) -> bool:
    notification = session.get(Notification, notification_id)
    if notification is None:
        return False
    notification.is_read = True
    session.flush()
    return True


def mark_all_read(session: Session, *, account_id: int | None = None) -> int:
    query = select(Notification).where(Notification.is_read.is_(False))
    if account_id is not None:
        query = query.where(Notification.account_id == account_id)
    notifications = list(session.scalars(query))
    for notification in notifications:
        notification.is_read = True
    session.flush()
    return len(notifications)
