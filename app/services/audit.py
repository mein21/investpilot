"""Audit trail helpers with automatic secret masking."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Severity
from app.core.security import sanitize
from app.models.audit import AuditLog


def record_audit(
    session: Session,
    settings: Settings,
    *,
    event_type: str,
    actor: str,
    message: str = "",
    entity_type: str = "",
    entity_id: str | int | None = None,
    severity: Severity = Severity.INFO,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    source_ip: str | None = None,
    correlation_id: str = "",
) -> AuditLog:
    secrets = settings.masking_secrets()
    entry = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        actor=actor,
        correlation_id=correlation_id,
        severity=severity,
        message=message,
        request_payload=sanitize(request_payload or {}, secrets),
        response_payload=sanitize(response_payload or {}, secrets),
        source_ip=source_ip,
    )
    session.add(entry)
    session.flush()
    return entry
