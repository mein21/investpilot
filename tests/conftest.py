from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.session import build_engine, build_session_factory, init_db
from sqlalchemy.orm import Session, sessionmaker

FIXED_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not read the developer's .env or ambient T-Invest settings."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in (
        "TINVEST_CLIENT",
        "TINVEST_TOKEN",
        "TINVEST_SANDBOX",
        "TINVEST_PROXY",
        "TINVEST_CA_BUNDLE",
        "TRADING_MODE",
        "DATABASE_URL",
        "AUTH_ENABLED",
        "SEED_DEMO_DATA",
        "SCHEDULER_ENABLED",
        "RECOMMENDATIONS_SCREENER_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECOMMENDATIONS_SCREENER_ENABLED", "false")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FIXED_NOW)


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = build_engine("sqlite:///:memory:", memory=True)
    init_db(engine)
    return build_session_factory(engine)
