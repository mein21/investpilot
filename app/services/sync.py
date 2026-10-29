"""Account, instrument, position, cash and operation synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import AssetClass, SyncStatus
from app.core.time import ensure_utc
from app.db.session import session_scope
from app.integrations.tinvest.client import IdType, InstrumentKind, TInvestClient
from app.integrations.tinvest.exceptions import TInvestError, TInvestNotFoundError
from app.integrations.tinvest.mapping import asset_class_from_type
from app.integrations.tinvest.types import (
    AccountInfo,
    CashInfo,
    InstrumentInfo,
    OperationInfo,
    PortfolioInfo,
    PortfolioPositionInfo,
    PositionsInfo,
    PriceInfo,
    TradingStatusInfo,
)
from app.models.account import Account, SyncRun
from app.models.instrument import Instrument
from app.models.operation import Operation
from app.models.position import CashBalance, FxRate, Position
from app.services.pnl import calculate_position_pnl

ZERO = Decimal("0")
HUNDRED = Decimal("100")
DIVIDEND_TYPES = {
    "OPERATION_TYPE_DIVIDEND",
    "OPERATION_TYPE_DIVIDEND_TRANSFER",
}
COUPON_TYPES = {
    "OPERATION_TYPE_COUPON",
    "OPERATION_TYPE_COUPON_REPAYMENT",
}
DFA_ACCOUNT_TYPE = "ACCOUNT_TYPE_DFA"


@dataclass(frozen=True)
class AccountSyncResult:
    tinvest_account_id: str
    local_account_id: int
    status: str
    positions: int
    instruments: int
    operations: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SyncReport:
    run_id: int
    status: SyncStatus
    accounts_synced: int
    instruments_synced: int
    operations_synced: int
    account_results: tuple[AccountSyncResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")[:300]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _json_safe_extra(extra: dict[str, object]) -> dict[str, object]:
    allowed = {"focus_type", "div_yield_flag", "volatility_30d"}
    result: dict[str, object] = {}
    for key in allowed:
        value = extra.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, Decimal):
            result[key] = str(value)
    return result


def _maybe_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


class SyncService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: TInvestClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._settings = settings
        self._clock = clock or SystemClock()

    def run(self, *, account_ids: list[str] | None = None) -> SyncReport:
        started_at = self._clock.now()
        run_id = self._create_run(started_at)
        warnings: list[str] = []
        errors: list[str] = []
        results: list[AccountSyncResult] = []
        partial = False

        try:
            accounts = self._client.get_accounts()
        except Exception as exc:
            message = f"accounts: {_safe_error(exc)}"
            errors.append(message)
            self._finish_run(
                run_id,
                status=SyncStatus.FAILED,
                accounts_synced=0,
                instruments_synced=0,
                operations_synced=0,
                error=message,
                details={"warnings": warnings, "errors": errors},
            )
            return SyncReport(
                run_id=run_id,
                status=SyncStatus.FAILED,
                accounts_synced=0,
                instruments_synced=0,
                operations_synced=0,
                warnings=tuple(warnings),
                errors=tuple(errors),
            )

        if account_ids:
            requested = set(account_ids)
            accounts = [account for account in accounts if account.tinvest_account_id in requested]
            missing = requested - {account.tinvest_account_id for account in accounts}
            for identifier in sorted(missing):
                warnings.append(f"счёт {identifier} не вернул брокер")

        if not accounts:
            warnings.append("брокер не вернул счета")

        with session_scope(self._session_factory) as session:
            for account_info in accounts:
                try:
                    with session.begin_nested():
                        result = self._sync_account(session, account_info)
                    session.commit()
                    results.append(result)
                    warnings.extend(result.warnings)
                    if result.status != "OK":
                        partial = True
                except TInvestNotFoundError as exc:
                    session.rollback()
                    warnings.append(
                        f"счёт {account_info.tinvest_account_id} недоступен: {_safe_error(exc)}"
                    )
                except Exception as exc:
                    session.rollback()
                    message = f"account {account_info.tinvest_account_id}: {_safe_error(exc)}"
                    errors.append(message)

            if errors and not results:
                run_status = SyncStatus.FAILED
            elif errors or partial:
                run_status = SyncStatus.PARTIAL
            else:
                run_status = SyncStatus.OK

            accounts_synced = len(results)
            instruments_synced = sum(item.instruments for item in results)
            operations_synced = sum(item.operations for item in results)
            self._finish_run(
                run_id,
                status=run_status,
                accounts_synced=accounts_synced,
                instruments_synced=instruments_synced,
                operations_synced=operations_synced,
                error=errors[0] if errors else None,
                details={
                    "warnings": warnings,
                    "errors": errors,
                    "accounts": [
                        {
                            "tinvest_account_id": item.tinvest_account_id,
                            "status": item.status,
                            "positions": item.positions,
                            "instruments": item.instruments,
                            "operations": item.operations,
                        }
                        for item in results
                    ],
                },
                session=session,
            )
            session.commit()

        return SyncReport(
            run_id=run_id,
            status=run_status,
            accounts_synced=accounts_synced,
            instruments_synced=instruments_synced,
            operations_synced=operations_synced,
            account_results=tuple(results),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    def load_universe(self, kinds: list[str] | None = None) -> tuple[int, tuple[str, ...]]:
        """Fetch whole instrument classes and upsert them as buy candidates."""
        requested = kinds or [
            item.strip() for item in self._settings.universe_kinds.split(",") if item.strip()
        ]
        warnings: list[str] = []
        with session_scope(self._session_factory) as session:
            existing = {
                instrument.instrument_uid: instrument
                for instrument in session.scalars(select(Instrument))
            }
            instrument_map: dict[str, Instrument] = {}
            for kind in requested:
                try:
                    infos = self._client.list_instruments(cast(InstrumentKind, kind))
                except TInvestError as exc:
                    warnings.append(f"{kind}: {_safe_error(exc)}")
                    continue
                for info in infos:
                    instrument = existing.get(info.instrument_uid)
                    if instrument is None:
                        instrument = Instrument(
                            instrument_uid=info.instrument_uid,
                            ticker=info.ticker,
                            name=info.name,
                            asset_class=info.asset_class,
                            lot=max(1, info.lot),
                            currency=info.currency or self._settings.base_currency,
                            issuer_key=info.issuer_key or info.ticker.upper(),
                        )
                        session.add(instrument)
                        existing[info.instrument_uid] = instrument
                    self._apply_instrument_info(instrument, info)
                    if not instrument.is_excluded:
                        instrument.is_buy_candidate = True
                    instrument_map[info.instrument_uid] = instrument
                session.flush()
            self._apply_market_data(session, instrument_map, warnings)
        return len(instrument_map), tuple(warnings)

    def refresh_market_data(self) -> tuple[int, tuple[str, ...]]:
        """Refresh quotes and trading status for every known instrument."""

        warnings: list[str] = []
        with session_scope(self._session_factory) as session:
            instruments = list(session.scalars(select(Instrument)))
            instrument_map = {
                instrument.instrument_uid: instrument
                for instrument in instruments
                if instrument.instrument_uid
            }
            self._apply_market_data(session, instrument_map, warnings)
            self._update_fx_rates(session, instrument_map)
            for position in session.scalars(
                select(Position).options(selectinload(Position.instrument))
            ):
                instrument = position.instrument
                if instrument.last_price is None or instrument.last_price <= ZERO:
                    continue
                position.current_price = instrument.last_price
                position.current_price_at = instrument.last_price_at
            session.flush()
        return len(instrument_map), tuple(warnings)

    def add_instrument(self, ticker: str, class_code: str | None = None) -> Instrument:
        """Fetch one instrument by ticker and make it available for recommendations."""
        normalized = ticker.strip().upper()
        if not normalized:
            raise TInvestNotFoundError("ticker is empty")
        info = self._client.get_instrument(
            normalized,
            id_type="ticker",
            class_code=class_code.strip().upper() if class_code else None,
        )
        warnings: list[str] = []
        with session_scope(self._session_factory) as session:
            instrument = session.scalar(
                select(Instrument).where(Instrument.instrument_uid == info.instrument_uid)
            )
            if instrument is None:
                instrument = session.scalar(
                    select(Instrument).where(Instrument.ticker == info.ticker)
                )
            if instrument is None:
                instrument = Instrument(
                    instrument_uid=info.instrument_uid,
                    ticker=info.ticker,
                    name=info.name,
                    asset_class=info.asset_class,
                    lot=max(1, info.lot),
                    currency=info.currency or self._settings.base_currency,
                    issuer_key=info.issuer_key or info.ticker.upper(),
                )
                session.add(instrument)
            self._apply_instrument_info(instrument, info)
            instrument.is_excluded = False
            instrument.is_buy_candidate = True
            self._apply_market_data(
                session,
                {info.instrument_uid: instrument},
                warnings,
            )
            session.flush()
            return instrument

    def _create_run(self, started_at: datetime) -> int:
        started = ensure_utc(started_at)
        with session_scope(self._session_factory) as session:
            session.execute(
                update(SyncRun)
                .where(
                    SyncRun.status == SyncStatus.RUNNING,
                    SyncRun.started_at < started - timedelta(hours=2),
                )
                .values(
                    status=SyncStatus.FAILED,
                    finished_at=started,
                    error="stale RUNNING run closed automatically",
                )
            )
            run = SyncRun(
                started_at=started,
                status=SyncStatus.RUNNING,
                details={},
            )
            session.add(run)
            session.flush()
            return run.id

    def _finish_run(
        self,
        run_id: int,
        *,
        status: SyncStatus,
        accounts_synced: int,
        instruments_synced: int,
        operations_synced: int,
        error: str | None,
        details: dict[str, object],
        session: Session | None = None,
    ) -> None:
        if session is None:
            with session_scope(self._session_factory) as owned_session:
                self._finish_run(
                    run_id,
                    status=status,
                    accounts_synced=accounts_synced,
                    instruments_synced=instruments_synced,
                    operations_synced=operations_synced,
                    error=error,
                    details=details,
                    session=owned_session,
                )
            return
        run = session.get(SyncRun, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = self._clock.now()
        run.accounts_synced = accounts_synced
        run.instruments_synced = instruments_synced
        run.operations_synced = operations_synced
        run.error = error
        run.details = details

    def _sync_account(
        self,
        session: Session,
        account_info: AccountInfo,
    ) -> AccountSyncResult:
        now = ensure_utc(self._clock.now())
        account = self._upsert_account(session, account_info, now)
        warnings: list[str] = []
        partial = False

        is_dfa = account_info.account_type == DFA_ACCOUNT_TYPE
        portfolio = self._client.get_portfolio(account_info.tinvest_account_id)
        if is_dfa:
            positions_info = PositionsInfo(
                account_id=account_info.tinvest_account_id,
                cash=(),
                received_at=now,
            )
        else:
            try:
                positions_info = self._client.get_positions(account_info.tinvest_account_id)
            except TInvestError as exc:
                warnings.append(f"денежные остатки: {_safe_error(exc)}")
                partial = True
                positions_info = PositionsInfo(
                    account_id=account_info.tinvest_account_id,
                    cash=(),
                    received_at=now,
                )

        instrument_map = self._collect_instruments(
            session,
            portfolio,
            warnings,
        )
        self._apply_market_data(session, instrument_map, warnings)
        self._update_fx_rates(session, instrument_map)
        position_count = self._sync_positions(
            session,
            account,
            portfolio,
            instrument_map,
            now,
            warnings,
        )
        self._sync_cash(session, account, positions_info.cash)
        if warnings:
            partial = True

        operations_from = now - timedelta(days=self._settings.tinvest_operations_days)
        operations_count = 0
        if not is_dfa:
            try:
                operations = self._client.get_operations(
                    account_info.tinvest_account_id,
                    operations_from,
                    now,
                )
            except TInvestError as exc:
                warnings.append(f"операции: {_safe_error(exc)}")
                partial = True
            else:
                operations_count = self._sync_operations(
                    session,
                    account,
                    operations,
                    instrument_map,
                    warnings,
                )

        self._apply_income_totals(session, account)
        calculate_position_pnl(
            session,
            account,
            operations_from=operations_from,
        )
        account.last_sync_at = now
        account.last_sync_status = "PARTIAL" if partial else "OK"

        return AccountSyncResult(
            tinvest_account_id=account_info.tinvest_account_id,
            local_account_id=account.id,
            status=account.last_sync_status,
            positions=position_count,
            instruments=len(instrument_map),
            operations=operations_count,
            warnings=tuple(warnings),
        )

    def _upsert_account(
        self,
        session: Session,
        info: AccountInfo,
        now: datetime,
    ) -> Account:
        account = session.scalar(
            select(Account).where(Account.tinvest_account_id == info.tinvest_account_id)
        )
        if account is None:
            account = Account(tinvest_account_id=info.tinvest_account_id)
            session.add(account)
        account.name = info.name
        account.account_type = info.account_type
        account.status = info.status
        account.access_level = info.access_level
        account.is_sandbox = info.is_sandbox
        account.opened_date = info.opened_date
        account.closed_date = info.closed_date
        account.last_sync_at = now
        session.flush()
        return account

    def _collect_instruments(
        self,
        session: Session,
        portfolio: PortfolioInfo,
        warnings: list[str],
    ) -> dict[str, Instrument]:
        result: dict[str, Instrument] = {}
        for position in portfolio.positions:
            identifier = position.instrument_uid or position.figi
            if not identifier:
                warnings.append("позиция портфеля без идентификатора инструмента")
                continue
            result[identifier] = self._ensure_instrument(
                session,
                identifier,
                id_type="uid" if position.instrument_uid else "figi",
                position=position,
                warnings=warnings,
            )
        return result

    def _ensure_instrument(
        self,
        session: Session,
        identifier: str,
        *,
        id_type: IdType = "uid",
        position: PortfolioPositionInfo | None = None,
        operation: OperationInfo | None = None,
        warnings: list[str] | None = None,
    ) -> Instrument:
        instrument = session.scalar(
            select(Instrument).where(Instrument.instrument_uid == identifier)
        )
        if instrument is None and position is not None and position.figi:
            instrument = session.scalar(select(Instrument).where(Instrument.figi == position.figi))
        if instrument is None and operation is not None and operation.figi:
            instrument = session.scalar(select(Instrument).where(Instrument.figi == operation.figi))

        info: InstrumentInfo | None = None
        try:
            info = self._client.get_instrument(identifier, id_type=id_type)
        except TInvestNotFoundError:
            if warnings is not None:
                warnings.append(f"метаданные инструмента не найдены: {identifier}")
        except TInvestError as exc:
            if warnings is not None:
                warnings.append(f"метаданные инструмента: {_safe_error(exc)}")

        if instrument is None:
            instrument = Instrument(
                instrument_uid=identifier,
                ticker=identifier[:32],
                name="",
                asset_class=AssetClass.OTHER,
                lot=1,
                currency="RUB",
                issuer_key=identifier,
            )
            session.add(instrument)

        if info is not None:
            self._apply_instrument_info(instrument, info)
        elif position is not None:
            self._apply_position_hint(instrument, position)
        elif operation is not None:
            self._apply_operation_hint(instrument, operation)
        session.flush()
        return instrument

    def _apply_instrument_info(
        self,
        instrument: Instrument,
        info: InstrumentInfo,
    ) -> None:
        instrument.instrument_uid = info.instrument_uid
        instrument.figi = info.figi
        instrument.ticker = info.ticker
        instrument.class_code = info.class_code
        instrument.isin = info.isin
        instrument.name = info.name
        instrument.instrument_type = info.instrument_type or "other"
        instrument.asset_class = info.asset_class
        instrument.currency = info.currency or "RUB"
        instrument.lot = max(1, info.lot)
        instrument.exchange = info.exchange
        instrument.issuer_key = info.issuer_key or info.ticker.upper()
        instrument.sector = info.sector
        instrument.country_of_risk = info.country_of_risk
        instrument.maturity_date = info.maturity_date
        instrument.nominal = info.nominal
        instrument.coupon_quantity_per_year = info.coupon_quantity_per_year
        instrument.income_yield = info.income_yield
        instrument.liquidity_flag = info.liquidity_flag
        instrument.volatility_30d = _maybe_decimal(info.extra.get("volatility_30d"))
        instrument.trading_status = info.trading_status
        instrument.buy_available = info.buy_available
        instrument.sell_available = info.sell_available
        instrument.api_trade_available = info.api_trade_available
        instrument.extra_json = _json_safe_extra(info.extra)

    def _apply_position_hint(
        self,
        instrument: Instrument,
        position: PortfolioPositionInfo,
    ) -> None:
        instrument.figi = position.figi or instrument.figi
        instrument.ticker = position.ticker or instrument.ticker
        instrument.class_code = position.class_code or instrument.class_code
        instrument.instrument_type = position.instrument_type or instrument.instrument_type
        instrument.asset_class = asset_class_from_type(position.instrument_type)
        instrument.currency = position.currency or instrument.currency
        if not instrument.issuer_key:
            instrument.issuer_key = (position.ticker or instrument.instrument_uid).upper()

    def _apply_operation_hint(
        self,
        instrument: Instrument,
        operation: OperationInfo,
    ) -> None:
        instrument.figi = operation.figi or instrument.figi
        instrument.instrument_type = operation.instrument_type or instrument.instrument_type
        instrument.asset_class = asset_class_from_type(operation.instrument_type)
        if not instrument.issuer_key:
            instrument.issuer_key = instrument.instrument_uid.upper()

    def _apply_market_data(
        self,
        session: Session,
        instrument_map: dict[str, Instrument],
        warnings: list[str],
    ) -> None:
        identifiers = list(instrument_map)
        if not identifiers:
            return
        prices: list[PriceInfo] = []
        statuses: list[TradingStatusInfo] = []
        try:
            prices = self._client.get_last_prices(identifiers)
        except TInvestError as exc:
            warnings.append(f"последние цены: {_safe_error(exc)}")
        try:
            statuses = self._client.get_trading_statuses(identifiers)
        except TInvestError as exc:
            warnings.append(f"торговые статусы: {_safe_error(exc)}")

        price_by_uid = {price.instrument_uid: price for price in prices if price.instrument_uid}
        status_by_uid = {
            status.instrument_uid: status for status in statuses if status.instrument_uid
        }
        for identifier, instrument in instrument_map.items():
            price = price_by_uid.get(identifier)
            if price is None and instrument.figi:
                price = next(
                    (item for item in prices if item.figi and item.figi == instrument.figi),
                    None,
                )
            if price is not None:
                instrument.last_price = price.price
                instrument.last_price_at = price.at
                instrument.last_price_source = "tinvest"
            else:
                warnings.append(f"нет последней цены для {identifier}")

            status = status_by_uid.get(identifier)
            if status is None and instrument.figi:
                status = next(
                    (item for item in statuses if item.figi and item.figi == instrument.figi),
                    None,
                )
            if status is not None:
                instrument.trading_status = status.trading_status
                instrument.api_trade_available = status.api_trade_available
                if instrument.buy_available and not status.api_trade_available:
                    instrument.buy_available = False
                if instrument.sell_available and not status.api_trade_available:
                    instrument.sell_available = False
        session.flush()

    def _update_fx_rates(
        self,
        session: Session,
        instrument_map: dict[str, Instrument],
    ) -> None:
        base = self._settings.base_currency.upper()
        existing: dict[tuple[str, str], FxRate] = {}
        for row in session.scalars(select(FxRate)):
            key = (row.base_currency.upper(), row.quote_currency.upper())
            if key in existing:
                session.delete(row)
                continue
            existing[key] = row

        for instrument in instrument_map.values():
            if instrument.asset_class is not AssetClass.CURRENCY:
                continue
            if instrument.last_price is None or instrument.last_price <= ZERO:
                continue
            ticker = instrument.ticker.upper()
            code = ticker[:3]
            if len(code) != 3 or not code.isalpha() or code == base:
                continue
            rate = existing.get((code, base))
            if rate is None:
                rate = FxRate(base_currency=code, quote_currency=base)
                session.add(rate)
                existing[(code, base)] = rate
            rate.base_currency = code
            rate.quote_currency = base
            rate.rate = instrument.last_price
            rate.source = "tinvest"
        session.flush()

    @staticmethod
    def _position_price(
        instrument: Instrument,
        price: Decimal,
        warnings: list[str],
    ) -> Decimal:
        if instrument.asset_class is not AssetClass.BOND:
            return price
        nominal = instrument.nominal
        if nominal is None or nominal <= ZERO:
            warnings.append(f"{instrument.ticker}: нет номинала для пересчёта цены облигации")
            return ZERO
        return price / nominal * HUNDRED

    def _sync_positions(
        self,
        session: Session,
        account: Account,
        portfolio: PortfolioInfo,
        instrument_map: dict[str, Instrument],
        now: datetime,
        warnings: list[str],
    ) -> int:
        existing = {
            position.instrument_id: position
            for position in session.scalars(
                select(Position).where(Position.account_id == account.id)
            )
        }
        seen: set[int] = set()
        for item in portfolio.positions:
            identifier = item.instrument_uid or item.figi
            instrument = instrument_map.get(identifier or "")
            if instrument is None:
                warnings.append(f"пропущена позиция без инструмента: {identifier}")
                continue
            position = existing.get(instrument.id)
            if position is None:
                position = Position(
                    account_id=account.id,
                    instrument_id=instrument.id,
                )
                session.add(position)
            position.quantity = item.quantity
            position.blocked_quantity = item.blocked_lots
            position.average_price = item.average_price
            position.average_price_fifo = item.average_price_fifo
            position.current_price = self._position_price(
                instrument,
                item.current_price,
                warnings,
            )
            if instrument.last_price_at is not None:
                position.current_price_at = instrument.last_price_at
            position.current_nkd = item.current_nkd
            position.unrealized_pnl = item.expected_yield
            position.last_synced_at = now
            if position.realized_pnl_incomplete is False and position.id is None:
                position.realized_pnl_incomplete = True
            seen.add(instrument.id)
        for instrument_id, position in existing.items():
            if instrument_id not in seen:
                position.quantity = ZERO
                position.blocked_quantity = ZERO
                position.unrealized_pnl = ZERO
                position.last_synced_at = now
        session.flush()
        return len(seen)

    def _sync_cash(
        self,
        session: Session,
        account: Account,
        cash: tuple[CashInfo, ...],
    ) -> None:
        existing = {
            item.currency: item
            for item in session.scalars(
                select(CashBalance).where(CashBalance.account_id == account.id)
            )
        }
        seen: set[str] = set()
        for item in cash:
            balance = existing.get(item.currency)
            if balance is None:
                balance = CashBalance(
                    account_id=account.id,
                    currency=item.currency,
                )
                session.add(balance)
            balance.amount = item.amount
            balance.blocked = item.blocked
            seen.add(item.currency)
        for currency, balance in existing.items():
            if currency not in seen:
                session.delete(balance)
        session.flush()

    def _sync_operations(
        self,
        session: Session,
        account: Account,
        operations: list[OperationInfo],
        instrument_map: dict[str, Instrument],
        warnings: list[str],
    ) -> int:
        existing = {
            operation.tinvest_operation_id: operation
            for operation in session.scalars(
                select(Operation).where(Operation.account_id == account.id)
            )
        }
        for item in operations:
            operation = existing.get(item.operation_id)
            if operation is None:
                operation = session.scalar(
                    select(Operation).where(Operation.tinvest_operation_id == item.operation_id)
                )
            if operation is None:
                operation = Operation(
                    tinvest_operation_id=item.operation_id,
                    account_id=account.id,
                )
                session.add(operation)
            operation.parent_operation_id = item.parent_operation_id
            operation.account_id = account.id
            operation.instrument_uid = item.instrument_uid
            operation.figi = item.figi
            operation.operation_type = item.operation_type
            operation.type_label = item.type_label
            operation.state = item.state
            operation.currency = item.currency
            operation.payment = item.payment
            operation.price = item.price
            operation.quantity = item.quantity
            operation.quantity_rest = item.quantity_rest
            operation.occurred_at = ensure_utc(item.occurred_at)
            operation.trades = [
                {
                    "trade_id": trade.trade_id,
                    "quantity": trade.quantity,
                    "price": str(trade.price),
                    "at": ensure_utc(trade.at).isoformat(),
                }
                for trade in item.trades
            ]
            identifier = item.instrument_uid or item.figi
            instrument = instrument_map.get(identifier or "")
            if instrument is None and identifier:
                instrument = self._ensure_instrument(
                    session,
                    identifier,
                    id_type="uid" if item.instrument_uid else "figi",
                    operation=item,
                    warnings=warnings,
                )
                instrument_map[identifier] = instrument
            operation.instrument_id = instrument.id if instrument is not None else None
        session.flush()
        return len(operations)

    def _apply_income_totals(self, session: Session, account: Account) -> None:
        dividends: dict[int, Decimal] = {}
        coupons: dict[int, Decimal] = {}
        for operation in session.scalars(
            select(Operation).where(Operation.account_id == account.id)
        ):
            if operation.instrument_id is None or operation.payment <= ZERO:
                continue
            if operation.operation_type in DIVIDEND_TYPES:
                dividends[operation.instrument_id] = (
                    dividends.get(operation.instrument_id, ZERO) + operation.payment
                )
            elif operation.operation_type in COUPON_TYPES:
                coupons[operation.instrument_id] = (
                    coupons.get(operation.instrument_id, ZERO) + operation.payment
                )
        for position in session.scalars(select(Position).where(Position.account_id == account.id)):
            position.dividends_received = dividends.get(position.instrument_id, ZERO)
            position.coupons_received = coupons.get(position.instrument_id, ZERO)
        session.flush()
