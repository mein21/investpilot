from __future__ import annotations

from decimal import Decimal

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import AssetClass
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.position import FxRate
from app.models.snapshot import PortfolioSnapshot
from app.services.portfolio import PortfolioService
from app.services.sync import SyncService
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker


def settings() -> Settings:
    return Settings(
        auth_enabled=False,
        tinvest_client="mock",
        tinvest_operations_days=365,
        quote_max_age_seconds=300,
    )


def synced_portfolio(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    client: MockTInvestClient | None = None,
) -> tuple[int, PortfolioService, MockTInvestClient]:
    client = client or MockTInvestClient(clock=clock)
    SyncService(session_factory, client, settings(), clock=clock).run()
    with session_factory() as session:
        from app.models.account import Account

        account_id = int(session.scalar(select(Account.id)) or 0)
    return account_id, PortfolioService(settings(), clock=clock), client


def test_portfolio_valuation_allocation_and_snapshot(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, service, _ = synced_portfolio(session_factory, clock)

    with session_factory() as session:
        view = service.build(session, account_id)
        snapshot = service.capture_snapshot(session, account_id)
        session.commit()

        assert view.total_value == Decimal("1085000.00")
        assert view.invested_value == Decimal("890000.00")
        assert view.cash_value == Decimal("195000.00")
        assert view.free_cash_value == Decimal("195000.00")
        assert view.unrealized_pnl == Decimal("73500.00")
        assert view.realized_pnl == Decimal("0.00")
        assert view.dividends_received == Decimal("12000.00")
        assert view.coupons_received == Decimal("12500.00")
        assert view.realized_pnl_incomplete is True
        assert len(view.positions) == 5
        assert len(view.cash) == 2
        assert view.warnings == ()

        weights = {row.asset_class: row.weight_percent for row in view.allocation}
        assert weights[AssetClass.STOCK] == Decimal("55.2995")
        assert weights[AssetClass.BOND] == Decimal("17.6959")
        assert weights[AssetClass.ETF] == Decimal("0.7373")
        assert weights[AssetClass.CURRENCY] == Decimal("8.2949")
        assert weights[AssetClass.CASH] == Decimal("17.9724")
        assert sum(weights.values()) == Decimal("100.0000")

        sber = next(row for row in view.positions if row.ticker == "SBER")
        assert sber.value_base == Decimal("300000.00")
        assert sber.weight_percent == Decimal("27.6498")
        assert sber.is_stale is False
        assert sber.current_price == Decimal("300")

        usd_cash = next(row for row in view.cash if row.currency == "USD")
        assert usd_cash.value_base == Decimal("45000.00")

        assert snapshot.total_value == Decimal("1085000.00")
        assert snapshot.allocation_json["STOCK"]["value"] == "600000.00"
        assert snapshot.weights_json["SBER"] == "27.6498"
        assert snapshot.concentration_json["SBER"] == "27.6498"
        assert count_snapshots(session) == 1


def test_portfolio_values_bond_quote_as_percent_of_nominal(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    from app.models.account import Account
    from app.models.instrument import Instrument
    from app.models.position import Position

    with session_factory() as session:
        account = Account(tinvest_account_id="acc-bond", name="Bond")
        session.add(account)
        session.flush()
        bond = Instrument(
            instrument_uid="bond-uid",
            ticker="RU000A1032P1",
            name="СФО ВТБ",
            asset_class=AssetClass.BOND,
            currency="RUB",
            lot=1,
            nominal=Decimal("7.4"),
            issuer_key="VTB",
            last_price=Decimal("99.85"),
            last_price_at=clock.now(),
        )
        session.add(bond)
        session.flush()
        session.add(
            Position(
                account_id=account.id,
                instrument_id=bond.id,
                quantity=Decimal("2"),
                average_price=Decimal("7.39"),
                current_price=Decimal("99.7297297297"),
                current_price_at=clock.now(),
                current_nkd=Decimal("0.05"),
            )
        )
        session.commit()
        account_id = account.id

    with session_factory() as session:
        view = PortfolioService(settings(), clock=clock).build(session, account_id)

    bond_row = next(row for row in view.positions if row.ticker == "RU000A1032P1")
    assert view.invested_value == Decimal("14.86")
    assert bond_row.value_base == Decimal("14.86")
    assert bond_row.current_price_is_percent is True
    assert bond_row.current_price_per_lot.quantize(Decimal("0.01")) == Decimal("7.38")


def test_portfolio_warns_and_excludes_cash_without_fx(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, service, _ = synced_portfolio(session_factory, clock)

    with session_factory() as session:
        session.execute(delete(FxRate))
        session.commit()
        view = service.build(session, account_id)

        assert view.total_value == Decimal("1040000.00")
        assert view.cash_value == Decimal("150000.00")
        assert any("USD" in warning and "курс" in warning for warning in view.warnings)


def test_portfolio_marks_stale_quotes(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    account_id, service, _ = synced_portfolio(session_factory, clock, client)
    client.set_price_age(600)
    SyncService(session_factory, client, settings(), clock=clock).run()

    with session_factory() as session:
        view = service.build(session, account_id)
        assert any("устарела" in warning for warning in view.warnings)
        assert all(row.is_stale for row in view.positions)


def test_zero_quantity_position_is_not_rendered(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    client = MockTInvestClient(clock=clock)
    account_id, service, _ = synced_portfolio(session_factory, clock, client)
    client.set_quantity("mock-uid-tmos", Decimal("0"))
    SyncService(session_factory, client, settings(), clock=clock).run()

    with session_factory() as session:
        view = service.build(session, account_id)
        assert len(view.positions) == 4
        assert all(row.ticker != "TMOS" for row in view.positions)


def count_snapshots(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(PortfolioSnapshot)) or 0)


def test_base_currency_position_is_counted_as_cash(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    from app.models.account import Account
    from app.models.instrument import Instrument
    from app.models.position import Position

    with session_factory() as session:
        account = Account(tinvest_account_id="acc-rub", name="Test")
        session.add(account)
        session.flush()
        rub = Instrument(
            instrument_uid="rub-uid",
            ticker="RUB000UTSTOM",
            name="Российский рубль",
            asset_class=AssetClass.CURRENCY,
            currency="RUB",
            lot=1,
            nominal=Decimal("1"),
            issuer_key="RUB",
        )
        session.add(rub)
        session.flush()
        session.add(
            Position(
                account_id=account.id,
                instrument_id=rub.id,
                quantity=Decimal("100"),
                current_price=Decimal("1"),
                current_price_at=clock.now(),
                average_price=Decimal("1"),
            )
        )
        session.commit()
        account_id = account.id

    with session_factory() as session:
        view = PortfolioService(settings(), clock=clock).build(session, account_id)

    assert view.invested_value == Decimal("0.00")
    assert view.cash_value == Decimal("100.00")
    assert view.total_value == Decimal("100.00")
    assert len(view.positions) == 0
    assert any(row.currency == "RUB" and row.amount == Decimal("100") for row in view.cash)
    cash_rows = {row.asset_class: row.value_base for row in view.allocation}
    assert cash_rows[AssetClass.CASH] == Decimal("100.00")


def test_base_currency_not_double_counted_with_cash_balance(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    from app.models.account import Account
    from app.models.instrument import Instrument
    from app.models.position import CashBalance, Position

    with session_factory() as session:
        account = Account(tinvest_account_id="acc-rub2", name="Test")
        session.add(account)
        session.flush()
        rub = Instrument(
            instrument_uid="rub-uid-2",
            ticker="RUB000UTSTOM",
            name="Российский рубль",
            asset_class=AssetClass.CURRENCY,
            currency="RUB",
            lot=1,
            nominal=Decimal("1"),
            issuer_key="RUB",
        )
        session.add(rub)
        session.flush()
        session.add_all(
            [
                Position(
                    account_id=account.id,
                    instrument_id=rub.id,
                    quantity=Decimal("100"),
                    current_price=Decimal("1"),
                    current_price_at=clock.now(),
                    average_price=Decimal("1"),
                ),
                CashBalance(
                    account_id=account.id,
                    currency="RUB",
                    amount=Decimal("100"),
                    blocked=Decimal("0"),
                ),
            ]
        )
        session.commit()
        account_id = account.id

    with session_factory() as session:
        view = PortfolioService(settings(), clock=clock).build(session, account_id)

    assert view.cash_value == Decimal("100.00")
    assert view.invested_value == Decimal("0.00")
    assert view.total_value == Decimal("100.00")
    assert len(view.cash) == 1
    assert view.cash[0].amount == Decimal("100")


def test_base_currency_match_is_case_insensitive(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    from app.models.account import Account
    from app.models.instrument import Instrument
    from app.models.position import CashBalance, Position

    with session_factory() as session:
        account = Account(tinvest_account_id="acc-rub3", name="Test")
        session.add(account)
        session.flush()
        rub = Instrument(
            instrument_uid="rub-uid-3",
            ticker="RUB000UTSTOM",
            name="Российский рубль",
            asset_class=AssetClass.CURRENCY,
            currency="rub",
            lot=1,
            nominal=Decimal("1"),
            issuer_key="RUB",
        )
        session.add(rub)
        session.flush()
        session.add_all(
            [
                Position(
                    account_id=account.id,
                    instrument_id=rub.id,
                    quantity=Decimal("100"),
                    current_price=Decimal("1"),
                    current_price_at=clock.now(),
                    average_price=Decimal("1"),
                ),
                CashBalance(
                    account_id=account.id,
                    currency="rub",
                    amount=Decimal("100"),
                    blocked=Decimal("0"),
                ),
            ]
        )
        session.commit()
        account_id = account.id

    with session_factory() as session:
        view = PortfolioService(settings(), clock=clock).build(session, account_id)

    assert view.cash_value == Decimal("100.00")
    assert view.invested_value == Decimal("0.00")
    assert view.total_value == Decimal("100.00")
    assert len(view.cash) == 1
    assert view.cash[0].currency == "RUB"
    assert view.cash[0].amount == Decimal("100")
