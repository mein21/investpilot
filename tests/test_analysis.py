from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import AssetClass
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.account import Account
from app.models.snapshot import PortfolioSnapshot
from app.services.analysis import analyze_history, run_scenario
from app.services.portfolio import PortfolioService
from app.services.sync import SyncService
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def test_scenario_applies_class_shocks(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(auth_enabled=False, tinvest_client="mock")
    SyncService(
        session_factory,
        MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    ).run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        portfolio = PortfolioService(settings, clock=clock).build(session, account.id)
        result = run_scenario(portfolio, {AssetClass.STOCK: Decimal("-10")})

        assert result.current_total == Decimal("1085000.00")
        assert result.stressed_total == Decimal("1025000.00")
        assert result.change == Decimal("-60000.00")
        assert result.change_percent == Decimal("-5.53")
        stock = next(row for row in result.rows if row.asset_class is AssetClass.STOCK)
        assert stock.shock_percent == Decimal("-10")
        assert stock.stressed_value == Decimal("540000.00")
        assert stock.current_weight == Decimal("55.2995")
        assert stock.stressed_weight == Decimal("52.6829")


def test_scenario_rejects_impossible_shock(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(auth_enabled=False, tinvest_client="mock")
    SyncService(
        session_factory,
        MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    ).run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        portfolio = PortfolioService(settings, clock=clock).build(session, account.id)
        try:
            run_scenario(portfolio, {AssetClass.STOCK: Decimal("-101")})
        except ValueError as exc:
            assert "between -100 and 500" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_history_stats() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    snapshots = [
        PortfolioSnapshot(
            id=1,
            account_id=1,
            captured_at=start,
            base_currency="RUB",
            total_value=Decimal("1000"),
            invested_value=Decimal("1000"),
            cash_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            daily_pnl=None,
            allocation_json={},
            weights_json={},
            concentration_json={},
            warnings=[],
            payload={},
            source="test",
        ),
        PortfolioSnapshot(
            id=2,
            account_id=1,
            captured_at=start + timedelta(days=1),
            base_currency="RUB",
            total_value=Decimal("1200"),
            invested_value=Decimal("1200"),
            cash_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            daily_pnl=None,
            allocation_json={},
            weights_json={},
            concentration_json={},
            warnings=[],
            payload={},
            source="test",
        ),
        PortfolioSnapshot(
            id=3,
            account_id=1,
            captured_at=start + timedelta(days=2),
            base_currency="RUB",
            total_value=Decimal("900"),
            invested_value=Decimal("900"),
            cash_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            daily_pnl=None,
            allocation_json={},
            weights_json={},
            concentration_json={},
            warnings=[],
            payload={},
            source="test",
        ),
    ]
    stats = analyze_history(snapshots)
    assert stats is not None
    assert stats.count == 3
    assert stats.start_value == Decimal("1000.00")
    assert stats.end_value == Decimal("900.00")
    assert stats.total_return_percent == Decimal("-10.00")
    assert stats.max_drawdown_percent == Decimal("25.00")
    assert stats.best_step_percent == Decimal("20.00")
    assert stats.worst_step_percent == Decimal("-25.00")
