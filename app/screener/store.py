"""SQLite persistence for screener data and run artefacts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.screener.domain import (
    Bar,
    Dividend,
    FundamentalSnapshot,
    ShareRecord,
)


def _d(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class ScreenerStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    uid TEXT PRIMARY KEY,
                    figi TEXT,
                    ticker TEXT NOT NULL,
                    class_code TEXT NOT NULL,
                    isin TEXT,
                    name TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    lot INTEGER NOT NULL,
                    exchange TEXT NOT NULL DEFAULT '',
                    real_exchange TEXT NOT NULL DEFAULT '',
                    sector TEXT NOT NULL DEFAULT '',
                    share_type TEXT NOT NULL DEFAULT '',
                    asset_uid TEXT NOT NULL DEFAULT '',
                    first_candle_date TEXT,
                    trading_status TEXT NOT NULL DEFAULT '',
                    buy_available INTEGER NOT NULL DEFAULT 0,
                    api_trade_available INTEGER NOT NULL DEFAULT 0,
                    liquidity_flag INTEGER NOT NULL DEFAULT 0,
                    for_qual_investor INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    otc INTEGER NOT NULL DEFAULT 0,
                    weekend INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_instruments_ticker ON instruments(ticker);
                CREATE INDEX IF NOT EXISTS idx_instruments_asset ON instruments(asset_uid);

                CREATE TABLE IF NOT EXISTS bars (
                    uid TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume_lots INTEGER NOT NULL,
                    lot INTEGER NOT NULL,
                    is_complete INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'exchange',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (uid, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(trade_date);

                CREATE TABLE IF NOT EXISTS benchmark_bars (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    is_complete INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date)
                );

                CREATE TABLE IF NOT EXISTS dividends (
                    dividend_key TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    record_date TEXT,
                    last_buy_date TEXT,
                    payment_date TEXT,
                    declared_date TEXT,
                    dividend_type TEXT NOT NULL DEFAULT '',
                    regularity TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dividends_uid ON dividends(uid);

                CREATE TABLE IF NOT EXISTS fundamentals (
                    asset_uid TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    values_json TEXT NOT NULL,
                    PRIMARY KEY (asset_uid, as_of)
                );

                CREATE TABLE IF NOT EXISTS fetch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    uid TEXT,
                    status TEXT NOT NULL,
                    rows INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    as_of TEXT,
                    payload_json TEXT NOT NULL
                );
                """
            )

    # ----- instruments -------------------------------------------------

    def upsert_shares(self, shares: Iterable[ShareRecord]) -> int:
        now = _dt(datetime.now(UTC))
        rows = [
            (
                item.uid,
                item.figi,
                item.ticker,
                item.class_code,
                item.isin,
                item.name,
                item.currency,
                item.lot,
                item.exchange,
                item.real_exchange,
                item.sector,
                item.share_type,
                item.asset_uid,
                item.first_candle_date.isoformat() if item.first_candle_date else None,
                item.trading_status,
                int(item.buy_available),
                int(item.api_trade_available),
                int(item.liquidity_flag),
                int(item.for_qual_investor),
                int(item.is_active),
                int(item.otc),
                int(item.weekend),
                now,
            )
            for item in shares
        ]
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO instruments (
                    uid, figi, ticker, class_code, isin, name, currency, lot,
                    exchange, real_exchange, sector, share_type, asset_uid,
                    first_candle_date, trading_status, buy_available,
                    api_trade_available, liquidity_flag, for_qual_investor,
                    is_active, otc, weekend, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    figi=excluded.figi, ticker=excluded.ticker,
                    class_code=excluded.class_code, isin=excluded.isin,
                    name=excluded.name, currency=excluded.currency, lot=excluded.lot,
                    exchange=excluded.exchange, real_exchange=excluded.real_exchange,
                    sector=excluded.sector, share_type=excluded.share_type,
                    asset_uid=excluded.asset_uid,
                    first_candle_date=excluded.first_candle_date,
                    trading_status=excluded.trading_status,
                    buy_available=excluded.buy_available,
                    api_trade_available=excluded.api_trade_available,
                    liquidity_flag=excluded.liquidity_flag,
                    for_qual_investor=excluded.for_qual_investor,
                    is_active=excluded.is_active, otc=excluded.otc,
                    weekend=excluded.weekend, fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def load_shares(self, active_only: bool = False) -> list[ShareRecord]:
        query = "SELECT * FROM instruments"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY ticker, class_code"
        with self._connect() as db:
            rows = db.execute(query).fetchall()
        return [
            ShareRecord(
                uid=row["uid"],
                figi=row["figi"],
                ticker=row["ticker"],
                class_code=row["class_code"],
                isin=row["isin"],
                name=row["name"],
                currency=row["currency"],
                lot=row["lot"],
                exchange=row["exchange"],
                real_exchange=row["real_exchange"],
                sector=row["sector"],
                share_type=row["share_type"],
                asset_uid=row["asset_uid"],
                first_candle_date=(
                    date.fromisoformat(row["first_candle_date"])
                    if row["first_candle_date"]
                    else None
                ),
                trading_status=row["trading_status"],
                buy_available=bool(row["buy_available"]),
                api_trade_available=bool(row["api_trade_available"]),
                liquidity_flag=bool(row["liquidity_flag"]),
                for_qual_investor=bool(row["for_qual_investor"]),
                is_active=bool(row["is_active"]),
                otc=bool(row["otc"]),
                weekend=bool(row["weekend"]),
            )
            for row in rows
        ]

    # ----- bars --------------------------------------------------------

    def upsert_bars(self, uid: str, bars: Iterable[Bar]) -> int:
        now = _dt(datetime.now(UTC))
        rows = [
            (
                uid,
                bar.trade_date.isoformat(),
                _d(bar.open),
                _d(bar.high),
                _d(bar.low),
                _d(bar.close),
                bar.volume_lots,
                bar.lot,
                int(bar.is_complete),
                bar.source,
                now,
            )
            for bar in bars
        ]
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO bars (
                    uid, trade_date, open, high, low, close, volume_lots,
                    lot, is_complete, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume_lots=excluded.volume_lots,
                    lot=excluded.lot, is_complete=excluded.is_complete,
                    source=excluded.source, fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def load_bars(
        self,
        uid: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Bar]:
        query = "SELECT * FROM bars WHERE uid = ?"
        params: list[Any] = [uid]
        if start is not None:
            query += " AND trade_date >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND trade_date <= ?"
            params.append(end.isoformat())
        query += " ORDER BY trade_date"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [
            Bar(
                trade_date=date.fromisoformat(row["trade_date"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume_lots=row["volume_lots"],
                lot=row["lot"],
                is_complete=bool(row["is_complete"]),
                source=row["source"],
                fetched_at=_parse_dt(row["fetched_at"]),
            )
            for row in rows
        ]

    def load_all_bars(self) -> dict[str, list[Bar]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM bars ORDER BY uid, trade_date").fetchall()
        result: dict[str, list[Bar]] = {}
        for row in rows:
            result.setdefault(row["uid"], []).append(
                Bar(
                    trade_date=date.fromisoformat(row["trade_date"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume_lots=row["volume_lots"],
                    lot=row["lot"],
                    is_complete=bool(row["is_complete"]),
                    source=row["source"],
                    fetched_at=_parse_dt(row["fetched_at"]),
                )
            )
        return result

    def first_bar_date(self, uid: str) -> date | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MIN(trade_date) AS first_date FROM bars WHERE uid = ? AND is_complete = 1",
                (uid,),
            ).fetchone()
        return date.fromisoformat(row["first_date"]) if row and row["first_date"] else None

    def last_bar_date(self, uid: str) -> date | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(trade_date) AS last_date FROM bars WHERE uid = ? AND is_complete = 1",
                (uid,),
            ).fetchone()
        return date.fromisoformat(row["last_date"]) if row and row["last_date"] else None

    def upsert_benchmark(self, symbol: str, bars: Iterable[Bar]) -> int:
        now = _dt(datetime.now(UTC))
        rows = [
            (
                symbol,
                bar.trade_date.isoformat(),
                _d(bar.open),
                _d(bar.high),
                _d(bar.low),
                _d(bar.close),
                int(bar.is_complete),
                now,
            )
            for bar in bars
        ]
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO benchmark_bars (
                    symbol, trade_date, open, high, low, close, is_complete, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, is_complete=excluded.is_complete,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def load_benchmark(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Bar]:
        query = "SELECT * FROM benchmark_bars WHERE symbol = ?"
        params: list[Any] = [symbol]
        if start is not None:
            query += " AND trade_date >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND trade_date <= ?"
            params.append(end.isoformat())
        query += " ORDER BY trade_date"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [
            Bar(
                trade_date=date.fromisoformat(row["trade_date"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume_lots=0,
                lot=1,
                is_complete=bool(row["is_complete"]),
                source=f"benchmark:{symbol}",
                fetched_at=_parse_dt(row["fetched_at"]),
            )
            for row in rows
        ]

    def successful_uids(self, endpoint: str) -> set[str]:
        """Return instrument uids with at least one successful fetch."""

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT uid FROM fetch_log
                WHERE endpoint = ? AND status = 'ok' AND uid IS NOT NULL
                """,
                (endpoint,),
            ).fetchall()
        return {row["uid"] for row in rows}

    # ----- dividends and fundamentals ----------------------------------

    def upsert_dividends(self, dividends: Iterable[Dividend]) -> int:
        now = _dt(datetime.now(UTC))
        rows = []
        for item in dividends:
            key = "|".join(
                [
                    item.uid,
                    item.record_date.isoformat() if item.record_date else "",
                    item.last_buy_date.isoformat() if item.last_buy_date else "",
                    format(item.amount, "f"),
                ]
            )
            rows.append(
                (
                    key,
                    item.uid,
                    _d(item.amount),
                    item.currency,
                    item.record_date.isoformat() if item.record_date else None,
                    item.last_buy_date.isoformat() if item.last_buy_date else None,
                    item.payment_date.isoformat() if item.payment_date else None,
                    item.declared_date.isoformat() if item.declared_date else None,
                    item.dividend_type,
                    item.regularity,
                    now,
                )
            )
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO dividends (
                    dividend_key, uid, amount, currency, record_date, last_buy_date,
                    payment_date, declared_date, dividend_type, regularity, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dividend_key) DO UPDATE SET
                    amount=excluded.amount, currency=excluded.currency,
                    record_date=excluded.record_date, last_buy_date=excluded.last_buy_date,
                    payment_date=excluded.payment_date,
                    declared_date=excluded.declared_date,
                    dividend_type=excluded.dividend_type,
                    regularity=excluded.regularity, fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def load_dividends(self) -> dict[str, list[Dividend]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM dividends ORDER BY uid, record_date").fetchall()
        result: dict[str, list[Dividend]] = {}
        for row in rows:
            result.setdefault(row["uid"], []).append(
                Dividend(
                    uid=row["uid"],
                    amount=Decimal(row["amount"]),
                    currency=row["currency"],
                    record_date=(
                        date.fromisoformat(row["record_date"]) if row["record_date"] else None
                    ),
                    last_buy_date=(
                        date.fromisoformat(row["last_buy_date"]) if row["last_buy_date"] else None
                    ),
                    payment_date=(
                        date.fromisoformat(row["payment_date"]) if row["payment_date"] else None
                    ),
                    declared_date=(
                        date.fromisoformat(row["declared_date"]) if row["declared_date"] else None
                    ),
                    dividend_type=row["dividend_type"],
                    regularity=row["regularity"],
                )
            )
        return result

    def upsert_fundamentals(self, snapshots: Iterable[FundamentalSnapshot]) -> int:
        rows = [
            (
                item.asset_uid,
                item.as_of.isoformat(),
                _dt(item.fetched_at),
                item.currency,
                json.dumps(
                    {key: _d(value) for key, value in item.values.items()},
                    ensure_ascii=False,
                ),
            )
            for item in snapshots
        ]
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO fundamentals (asset_uid, as_of, fetched_at, currency, values_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asset_uid, as_of) DO UPDATE SET
                    fetched_at=excluded.fetched_at, currency=excluded.currency,
                    values_json=excluded.values_json
                """,
                rows,
            )
        return len(rows)

    def load_fundamentals(self) -> dict[str, FundamentalSnapshot]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT f.* FROM fundamentals f
                JOIN (
                    SELECT asset_uid, MAX(as_of) AS as_of
                    FROM fundamentals GROUP BY asset_uid
                ) latest ON latest.asset_uid = f.asset_uid AND latest.as_of = f.as_of
                """
            ).fetchall()
        result: dict[str, FundamentalSnapshot] = {}
        for row in rows:
            raw = json.loads(row["values_json"])
            result[row["asset_uid"]] = FundamentalSnapshot(
                asset_uid=row["asset_uid"],
                fetched_at=_parse_dt(row["fetched_at"]) or datetime.now(UTC),
                as_of=date.fromisoformat(row["as_of"]),
                currency=row["currency"],
                values={key: _dec(value) for key, value in raw.items()},
            )
        return result

    # ----- diagnostics and runs ----------------------------------------

    def log_fetch(
        self,
        endpoint: str,
        *,
        uid: str | None = None,
        status: str = "ok",
        rows: int = 0,
        message: str = "",
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO fetch_log (fetched_at, endpoint, uid, status, rows, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_dt(datetime.now(UTC)), endpoint, uid, status, rows, message[:500]),
            )

    def save_run(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        as_of: date | None = None,
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO runs (kind, created_at, as_of, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    kind,
                    _dt(datetime.now(UTC)),
                    as_of.isoformat() if as_of else None,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            rowid = cursor.lastrowid
            if rowid is None:
                raise RuntimeError("SQLite did not return a run id")
            return int(rowid)

    def load_latest_run(self, kind: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def stats(self) -> dict[str, int]:
        with self._connect() as db:
            instruments = db.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
            bars = db.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
            dividends = db.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
            benchmarks = db.execute("SELECT COUNT(*) FROM benchmark_bars").fetchone()[0]
        return {
            "instruments": instruments,
            "bars": bars,
            "dividends": dividends,
            "benchmark_bars": benchmarks,
        }

    def benchmark_symbols(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT symbol FROM benchmark_bars ORDER BY symbol"
            ).fetchall()
        return [row["symbol"] for row in rows]

    def fetch_errors(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM fetch_log WHERE status != 'ok' ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def bar_coverage(self) -> tuple[date | None, date | None, int]:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                       COUNT(DISTINCT uid) AS instruments
                FROM bars WHERE is_complete = 1
                """
            ).fetchone()
        return (
            date.fromisoformat(row["first_date"]) if row and row["first_date"] else None,
            date.fromisoformat(row["last_date"]) if row and row["last_date"] else None,
            int(row["instruments"] or 0) if row else 0,
        )

    def latest_fetch(self) -> datetime | None:
        with self._connect() as db:
            row = db.execute("SELECT MAX(fetched_at) AS latest FROM bars").fetchone()
        return _parse_dt(row["latest"]) if row and row["latest"] else None

    def close(self) -> None:
        return None


def parse_run_payload(payload: str) -> dict[str, Any]:
    return json.loads(payload)


def batch(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]
