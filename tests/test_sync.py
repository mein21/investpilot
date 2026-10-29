from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import SyncStatus
from app.integrations.tinvest.exceptions import TInvestNotFoundError, TInvestUnavailableError
from app.integrations.tinvest.mock import MockTInvestClient
from app.integrations.tinvest.types import AccountInfo, OperationInfo, PositionsInfo
from app.models.account import Account, SyncRun
from app.models.instrument import Instrument
from app.models.operation import Operation
from app.models.position import CashBalance, FxRate, Position
from app.services.sync import SyncService
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


def make_service(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    client: MockTInvestClient | None = None,
) -> SyncService:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_operations_days=365,
    )
    return SyncService(
        session_factory,
        client or MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    )


def count(session: Session, model: type[object]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_sync_creates_account_positions_and_operations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    report = make_service(session_factory, clock).run()

    assert report.status is SyncStatus.OK
    assert report.accounts_synced == 1
    assert report.instruments_synced == 5
    assert report.operations_synced == 8
    assert report.errors == ()

    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        assert account.tinvest_account_id == "sandbox-demo-account"
        assert account.last_sync_status == "OK"
        assert count(session, Position) == 5
        assert count(session, CashBalance) == 2
        assert count(session, Instrument) == 5
        assert count(session, Operation) == 8
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status is SyncStatus.OK
        assert run.finished_at is not None

        position = session.scalar(
            select(Position).join(Instrument).where(Instrument.ticker == "SBER")
        )
        assert position is not None
        assert position.quantity == Decimal("1000")
        assert position.average_price == Decimal("250")
        assert position.current_price == Decimal("300")
        assert position.unrealized_pnl == Decimal("50000")
        assert position.dividends_received == Decimal("12000")

        bond_position = session.scalar(
            select(Position).join(Instrument).where(Instrument.ticker == "SU26248RMFS3")
        )
        assert bond_position is not None
        assert bond_position.current_price == Decimal("95.5")
        assert bond_position.coupons_received == Decimal("12500")
        assert bond_position.unrealized_pnl == Decimal("1000")

        sber = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
        assert sber is not None
        assert sber.last_price == Decimal("300")
        assert sber.volatility_30d == Decimal("25")
        assert sber.buy_available is True
        assert sber.api_trade_available is True


def test_sync_is_idempotent(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = make_service(session_factory, clock)
    first = service.run()
    second = service.run()

    assert first.status is SyncStatus.OK
    assert second.status is SyncStatus.OK
    with session_factory() as session:
        assert count(session, Account) == 1
        assert count(session, Position) == 5
        assert count(session, Instrument) == 5
        assert count(session, Operation) == 8
        assert count(session, SyncRun) == 2


def test_sync_marks_removed_position_with_zero_quantity(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    service = make_service(session_factory, clock, client)
    service.run()
    client.set_quantity("mock-uid-tmos", Decimal("0"))

    report = service.run()

    assert report.status is SyncStatus.OK
    with session_factory() as session:
        position = session.scalar(
            select(Position).join(Instrument).where(Instrument.ticker == "TMOS")
        )
        assert position is not None
        assert position.quantity == Decimal("0")
        assert position.unrealized_pnl == Decimal("0")
        assert count(session, Position) == 5


def test_sync_partial_when_operations_fail(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    class OperationsDown(MockTInvestClient):
        def get_operations(self, account_id: str, date_from: object, date_to: object):  # type: ignore[override]
            raise TInvestUnavailableError("operations unavailable")

    report = make_service(
        session_factory,
        clock,
        OperationsDown(clock=clock),
    ).run()

    assert report.status is SyncStatus.PARTIAL
    assert report.operations_synced == 0
    assert any("operations" in warning for warning in report.warnings)
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        assert account.last_sync_status == "PARTIAL"
        assert count(session, Position) == 5
        assert count(session, Operation) == 0
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status is SyncStatus.PARTIAL


def test_sync_failed_run_is_persisted(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    class PortfolioDown(MockTInvestClient):
        def get_portfolio(self, account_id: str):  # type: ignore[override]
            raise TInvestUnavailableError("portfolio unavailable")

    report = make_service(
        session_factory,
        clock,
        PortfolioDown(clock=clock),
    ).run()

    assert report.status is SyncStatus.FAILED
    assert report.accounts_synced == 0
    assert report.errors
    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status is SyncStatus.FAILED
        assert run.error is not None
        assert count(session, Account) == 0


class _ExternalOperationClient(MockTInvestClient):
    """Mock client with an operation for an instrument outside the portfolio."""

    def get_operations(
        self,
        account_id: str,
        date_from: object,
        date_to: object,
    ) -> list[OperationInfo]:
        operations = super().get_operations(account_id, date_from, date_to)  # type: ignore[arg-type]
        external = OperationInfo(
            operation_id="mock-op-external",
            parent_operation_id=None,
            instrument_uid="external-uid-1",
            figi="EXTFIGI1",
            currency="RUB",
            payment=Decimal("-1000"),
            price=Decimal("100"),
            state="OPERATION_STATE_EXECUTED",
            quantity=1,
            quantity_rest=0,
            occurred_at=self.now,
            type_label="Покупка ценных бумаг",
            operation_type="OPERATION_TYPE_BUY",
            instrument_type="share",
        )
        return [external, *operations]


def test_sync_operation_for_unknown_instrument_does_not_break_flush(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = make_service(session_factory, clock, _ExternalOperationClient(clock=clock))
    report = service.run()

    assert report.status is SyncStatus.OK
    assert report.operations_synced == 9
    with session_factory() as session:
        operation = session.scalar(
            select(Operation).where(Operation.tinvest_operation_id == "mock-op-external")
        )
        assert operation is not None
        assert operation.operation_type == "OPERATION_TYPE_BUY"
        assert operation.occurred_at is not None


class _DfaAccountClient(MockTInvestClient):
    """Mock client emulating a DFA account without cash/operations endpoints."""

    def get_accounts(self) -> list[AccountInfo]:
        account = self._account
        return [replace(account, account_type="ACCOUNT_TYPE_DFA")]

    def get_positions(self, account_id: str) -> PositionsInfo:
        raise TInvestNotFoundError("Account not found")

    def get_operations(
        self,
        account_id: str,
        date_from: object,
        date_to: object,
    ) -> list[OperationInfo]:
        raise TInvestNotFoundError("Account not found")


def test_sync_dfa_account_skips_cash_and_operations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = make_service(session_factory, clock, _DfaAccountClient(clock=clock))
    report = service.run()

    assert report.status is SyncStatus.OK
    assert report.operations_synced == 0
    assert report.warnings == ()
    with session_factory() as session:
        assert count(session, Position) == 5
        assert count(session, CashBalance) == 0


def test_load_universe_marks_instruments_as_candidates(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    service = make_service(session_factory, clock)
    count, warnings = service.load_universe(["shares", "etfs", "currencies"])

    assert count == 5
    assert warnings == ()
    with session_factory() as session:
        instruments = list(session.scalars(select(Instrument)))
        assert {item.ticker for item in instruments} == {
            "SBER",
            "GAZP",
            "LKOH",
            "TMOS",
            "USD000UTSTOM",
        }
        assert all(item.is_buy_candidate for item in instruments)
        sber = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
        assert sber is not None
        assert sber.last_price == Decimal("300")


def test_refresh_market_data_updates_all_instruments_and_positions(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    service = make_service(session_factory, clock, client)
    service.run()
    client.set_price("mock-uid-sber", Decimal("333"))
    count, warnings = service.refresh_market_data()
    assert count == 5
    assert warnings == ()
    with session_factory() as session:
        sber = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
        assert sber is not None
        assert sber.last_price == Decimal("333")
        position = session.scalar(
            select(Position).join(Instrument).where(Instrument.ticker == "SBER")
        )
        assert position is not None
        assert position.current_price == Decimal("333")


def test_refresh_market_data_derives_currency_pair_from_ticker(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    service = make_service(session_factory, clock, client)
    service.run()
    with session_factory() as session:
        usd = session.scalar(select(Instrument).where(Instrument.ticker == "USD000UTSTOM"))
        assert usd is not None
        usd.currency = "rub"
        session.commit()
    client.set_price("mock-uid-usd", Decimal("80.5"))
    count, warnings = service.refresh_market_data()
    assert count == 5
    assert warnings == ()
    with session_factory() as session:
        usd_rate = session.scalar(
            select(FxRate).where(
                FxRate.base_currency == "USD",
                FxRate.quote_currency == "RUB",
            )
        )
        assert usd_rate is not None
        assert usd_rate.rate == Decimal("80.5")
        assert session.scalar(select(FxRate).where(FxRate.base_currency == "rub")) is None
