"""Persistent operator controls that override environment defaults."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AppSetting

EMERGENCY_STOP_KEY = "emergency_stop"


def emergency_stop_enabled(session: Session, *, default: bool) -> bool:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == EMERGENCY_STOP_KEY))
    if setting is None:
        return default
    value = setting.value.get("enabled")
    return value if isinstance(value, bool) else default


def set_emergency_stop(session: Session, *, enabled: bool) -> AppSetting:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == EMERGENCY_STOP_KEY))
    if setting is None:
        setting = AppSetting(
            key=EMERGENCY_STOP_KEY,
            value={"enabled": enabled},
        )
        session.add(setting)
    else:
        setting.value = {"enabled": enabled}
    session.flush()
    return setting
