"""Screener orchestration: load, validate, rank and explain candidates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.core.config import PROD_BASE_URL, Settings
from app.screener.config import ScreenerConfig
from app.screener.domain import (
    Bar,
    Candidate,
    DataQuality,
    Dividend,
    FeatureSet,
    FilterStats,
    FundamentalSnapshot,
    ScreenerResult,
    ShareRecord,
)
from app.screener.features import compute_features, median
from app.screener.market import compute_regime
from app.screener.quality import evaluate_quality
from app.screener.rating import ScoreBreakdown, score_features
from app.screener.store import ScreenerStore
from app.screener.tinvest import ScreenerTInvestClient

LOGGER = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class _Record:
    share: ShareRecord
    bar: Bar | None
    features: FeatureSet | None
    quality: DataQuality
    spread_pct: Decimal | None = None
    spread_fetched_at: datetime | None = None
    history: tuple[Bar, ...] = ()


def _is_moex_rub(share: ShareRecord) -> bool:
    """Return True only for the Russian MOEX equity board.

    ``real_exchange`` is intentionally not enough: T-Invest reports foreign
    OTC receipts such as AAPL-RM with ``REAL_EXCHANGE_MOEX`` and RUB too.
    The main-board class or an explicit MOEX trading mode is required.
    """

    exchange = share.exchange.strip().lower()
    return (
        share.real_exchange.strip().upper() == "REAL_EXCHANGE_MOEX"
        and share.currency.upper() == "RUB"
        and share.is_common_or_preferred
        and not share.otc
        and (share.class_code == "TQBR" or exchange.startswith("moex"))
    )


def _primary_rank(share: ShareRecord) -> tuple[int, int, int, int, int, int]:
    first = -share.first_candle_date.toordinal() if share.first_candle_date else -(10**9)
    return (
        int(share.is_active),
        int(share.is_main_board),
        int(share.liquidity_flag),
        int(share.buy_available),
        first,
        int(share.api_trade_available),
    )


def select_primary_listings(shares: list[ShareRecord]) -> list[ShareRecord]:
    """Collapse multiple exchange listings of the same asset to one row."""

    groups: dict[str, list[ShareRecord]] = {}
    for share in shares:
        if not _is_moex_rub(share):
            continue
        key = share.asset_uid or share.uid
        groups.setdefault(key, []).append(share)
    result = [max(group, key=_primary_rank) for group in groups.values()]
    return sorted(result, key=lambda item: (item.ticker, item.class_code))


def _resolve_verify(settings: Settings) -> str | bool:
    raw = settings.tinvest_ca_bundle.strip()
    if not raw:
        return True
    path = Path(raw)
    if not path.is_absolute() and not path.exists():
        candidate = Path(__file__).resolve().parents[2] / path
        if candidate.exists():
            path = candidate
    return str(path) if path.exists() else True


def build_screener_client(settings: Settings, config: ScreenerConfig) -> ScreenerTInvestClient:
    token = settings.tinvest_token_value
    if not token:
        raise RuntimeError("TINVEST_TOKEN is required for screener data")
    base = settings.resolved_tinvest_base_url
    if settings.tinvest_sandbox:
        base = PROD_BASE_URL
    return ScreenerTInvestClient(
        token,
        base_url=base,
        verify=_resolve_verify(settings),
        proxy=settings.tinvest_proxy.strip() or None,
        timeout_seconds=settings.tinvest_timeout_seconds or config.request_timeout,
        max_retries=config.max_retries,
        marketdata_min_interval=config.marketdata_min_interval,
        instruments_min_interval=config.instruments_min_interval,
    )


def _find_benchmark(indices: list[dict[str, str]], symbol: str) -> dict[str, str] | None:
    for item in indices:
        if item.get("ticker", "").upper() == symbol.upper():
            return item
    return None


def _load_benchmark(
    store: ScreenerStore,
    client: ScreenerTInvestClient | None,
    config: ScreenerConfig,
    *,
    refresh: bool,
    warnings: list[str],
) -> tuple[str, list[Bar]]:
    symbol = config.benchmark
    bars = store.load_benchmark(symbol)
    if refresh and client is not None:
        indices = client.list_indices()
        item = _find_benchmark(indices, symbol)
        if item is None:
            fallback = _find_benchmark(indices, config.fallback_benchmark)
            if fallback is not None:
                warnings.append(
                    f"{symbol} не найден в Indicatives; используется {config.fallback_benchmark}."
                )
                symbol = config.fallback_benchmark
                item = fallback
        if item is not None:
            fetched = client.daily_candles(
                item["uid"],
                date(2018, 1, 1),
                datetime.now(UTC).date(),
            )
            completed = [bar for bar in fetched if bar.is_complete]
            store.upsert_benchmark(symbol, completed)
            bars = store.load_benchmark(symbol)
            store.log_fetch("InstrumentsService/Indicatives", rows=1)
        else:
            warnings.append(f"Бенчмарк {symbol} недоступен в Indicatives.")
    if not bars:
        fallback_bars = store.load_benchmark(config.fallback_benchmark)
        if fallback_bars:
            warnings.append(
                f"Нет сохранённых данных {symbol}; используется {config.fallback_benchmark}."
            )
            symbol = config.fallback_benchmark
            bars = fallback_bars
    if not bars:
        raise RuntimeError("Нет данных бенчмарка; выполните screen с доступом к T-Invest.")
    completed = [bar for bar in bars if bar.is_complete]
    if len(completed) < config.min_history_sessions:
        warnings.append(f"У бенчмарка {symbol} только {len(completed)} завершённых сессий.")
    return symbol, completed


def _load_instruments(
    store: ScreenerStore,
    client: ScreenerTInvestClient | None,
    config: ScreenerConfig,
    *,
    refresh: bool,
    warnings: list[str],
) -> list[ShareRecord]:
    if refresh and client is not None:
        try:
            all_shares = client.list_shares("INSTRUMENT_STATUS_ALL")
            base_shares = client.list_shares("INSTRUMENT_STATUS_BASE")
            store.upsert_shares(all_shares)
            store.upsert_shares(base_shares)
            store.log_fetch("InstrumentsService/Shares", rows=len(all_shares))
            warnings.append(
                "Вселенная построена по текущему списку Shares; полностью "
                "устранить survivorship bias нельзя."
            )
        except Exception as exc:
            warnings.append(f"Не удалось обновить список акций: {exc}. Используется кэш.")
            store.log_fetch("InstrumentsService/Shares", status="error", message=str(exc))
    shares = store.load_shares()
    if not shares:
        raise RuntimeError("Нет списка акций; нужен хотя бы один успешный refresh.")
    return shares


def _refresh_bars(
    store: ScreenerStore,
    client: ScreenerTInvestClient | None,
    shares: list[ShareRecord],
    benchmark_as_of: date,
    config: ScreenerConfig,
    *,
    refresh: bool,
    warnings: list[str],
    include_inactive: bool = False,
) -> None:
    if not refresh or client is None:
        return
    today = datetime.now(UTC).date()
    start_floor = today - timedelta(days=config.history_calendar_days)
    targets = (
        shares
        if include_inactive
        else ([share for share in shares if share.is_active] if config.require_active else shares)
    )
    if config.api_limit is not None:
        targets = targets[: config.api_limit]
        warnings.append(f"Ограничение api_limit: загружено не более {config.api_limit} бумаг.")
    failures = 0
    for index, share in enumerate(targets, start=1):
        last = store.last_bar_date(share.uid)
        first = store.first_bar_date(share.uid)
        expected_start = max(start_floor, share.first_candle_date or start_floor)
        needs_backfill = first is None or first > expected_start + timedelta(days=45)
        if last is not None and last >= benchmark_as_of and not needs_backfill:
            continue
        if needs_backfill:
            start = expected_start
        else:
            start = max(start_floor, last - timedelta(days=10)) if last else expected_start
        try:
            bars = client.daily_candles(share.uid, start, today, lot=share.lot)
            completed = [bar for bar in bars if bar.is_complete]
            store.upsert_bars(share.uid, completed)
            store.log_fetch("MarketDataService/GetCandles", uid=share.uid, rows=len(completed))
        except Exception as exc:
            failures += 1
            store.log_fetch(
                "MarketDataService/GetCandles",
                uid=share.uid,
                status="error",
                message=str(exc),
            )
            if failures <= 5:
                warnings.append(f"Свечи {share.ticker}: {exc}")
        if index % 50 == 0:
            LOGGER.info("screener bars: %s/%s", index, len(targets))
    if failures:
        warnings.append(f"Не удалось загрузить свечи для {failures} бумаг.")


def _refresh_dividends(
    store: ScreenerStore,
    client: ScreenerTInvestClient | None,
    shares: list[ShareRecord],
    config: ScreenerConfig,
    *,
    refresh: bool,
    warnings: list[str],
    include_inactive: bool = False,
) -> None:
    if not refresh or client is None:
        return
    failures = 0
    start = date(2018, 1, 1)
    today = datetime.now(UTC).date()
    targets = (
        shares
        if include_inactive
        else ([share for share in shares if share.is_active] if config.require_active else shares)
    )
    if not config.refresh_dividends:
        done = store.successful_uids("InstrumentsService/GetDividends")
        targets = [share for share in targets if share.uid not in done]
    if not targets:
        return
    for share in targets:
        try:
            items = client.dividends(share.uid, start, today)
            store.upsert_dividends(items)
            store.log_fetch("InstrumentsService/GetDividends", uid=share.uid, rows=len(items))
        except Exception as exc:
            failures += 1
            store.log_fetch(
                "InstrumentsService/GetDividends",
                uid=share.uid,
                status="error",
                message=str(exc),
            )
    if failures:
        warnings.append(f"Дивиденды не загружены для {failures} бумаг.")


def _refresh_fundamentals(
    store: ScreenerStore,
    client: ScreenerTInvestClient | None,
    shares: list[ShareRecord],
    config: ScreenerConfig,
    *,
    refresh: bool,
    warnings: list[str],
) -> None:
    if config.mode != "technical_fundamental" or not refresh or client is None:
        return
    if not config.refresh_fundamentals and store.load_fundamentals():
        return
    targets = [share for share in shares if share.is_active] if config.require_active else shares
    assets = [share.asset_uid for share in targets if share.asset_uid]
    try:
        snapshots = client.fundamentals(assets)
        store.upsert_fundamentals(snapshots)
        store.log_fetch("InstrumentsService/GetAssetFundamentals", rows=len(snapshots))
    except Exception as exc:
        warnings.append(f"Фундаментал не загружен: {exc}")
        store.log_fetch("InstrumentsService/GetAssetFundamentals", status="error", message=str(exc))


def _quality_and_features(
    shares: list[ShareRecord],
    bars_by_uid: dict[str, list[Bar]],
    dividends_by_uid: dict[str, list[Dividend]],
    benchmark_bars: list[Bar],
    calendar: list[date],
    calendar_index: dict[date, int],
    as_of: date,
    config: ScreenerConfig,
) -> list[_Record]:
    records: list[_Record] = []
    for share in shares:
        bars = bars_by_uid.get(share.uid, [])
        quality, cleaned = evaluate_quality(
            bars,
            calendar,
            as_of,
            config,
            dividends=dividends_by_uid.get(share.uid, []),
        )
        features = None
        bar = cleaned[-1] if cleaned else None
        if bar is not None and not quality.critical:
            features = compute_features(
                cleaned,
                benchmark_bars,
                calendar,
                calendar_index,
                as_of,
                config,
                dividends=dividends_by_uid.get(share.uid, []),
            )
        records.append(
            _Record(
                share=share,
                bar=bar,
                features=features,
                quality=quality,
                history=tuple(cleaned[-120:]),
            )
        )
    return records


def _attach_sector_context(records: list[_Record], config: ScreenerConfig) -> list[_Record]:
    groups: dict[str, list[_Record]] = {}
    for record in records:
        if record.features is None:
            continue
        groups.setdefault(record.share.sector or "other", []).append(record)
    medians: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for sector, items in groups.items():
        if len(items) < config.sector_min_peers:
            continue
        ret3 = median(
            [
                item.features.ret_3m
                for item in items
                if item.features and item.features.ret_3m is not None
            ]
        )
        ret6 = median(
            [
                item.features.ret_6m
                for item in items
                if item.features and item.features.ret_6m is not None
            ]
        )
        medians[sector] = (ret3, ret6)
    result: list[_Record] = []
    for record in records:
        features = record.features
        if features is None:
            result.append(record)
            continue
        sector = record.share.sector or "other"
        median3, median6 = medians.get(sector, (None, None))
        sector3 = (
            features.ret_3m - median3
            if features.ret_3m is not None and median3 is not None
            else None
        )
        sector6 = (
            features.ret_6m - median6
            if features.ret_6m is not None and median6 is not None
            else None
        )
        result.append(
            replace(
                record,
                features=replace(
                    features,
                    sector_excess_3m=sector3,
                    sector_excess_6m=sector6,
                ),
            )
        )
    return result


FUNDAMENTAL_CATEGORY_LABELS = {
    "financial": "Финансовый сектор",
    "real_estate": "Недвижимость",
    "capital_intensive": "Капиталоёмкий сектор",
    "general": "Общие правила",
}


def _fundamental_category(sector: str) -> str:
    text = (sector or "").lower()
    if any(
        marker in text for marker in ("финанс", "банк", "страхов", "financial", "bank", "insurance")
    ):
        return "financial"
    if any(marker in text for marker in ("недвиж", "девелоп", "real estate", "reit")):
        return "real_estate"
    if any(
        marker in text
        for marker in (
            "нефт",
            "газ",
            "энерг",
            "металл",
            "mining",
            "хим",
            "транспорт",
            "телеком",
            "электро",
            "energy",
            "utilities",
            "materials",
            "industrials",
            "telecom",
            "chemical",
            "utility",
            "industrial",
            "машиностро",
        )
    ):
        return "capital_intensive"
    return "general"


PEER_FUNDAMENTAL_METRICS: tuple[str, ...] = (
    "roe",
    "roa",
    "roic",
    "net_margin_mrq",
    "total_debt_to_ebitda_mrq",
    "ev_to_ebitda_mrq",
    "price_to_book_ttm",
)


def _peer_fundamental_medians(
    records: list[_Record],
    fundamentals: dict[str, FundamentalSnapshot],
    min_peers: int,
) -> dict[str, dict[str, Decimal]]:
    groups: dict[str, dict[str, list[Decimal]]] = {}
    for record in records:
        snapshot = fundamentals.get(record.share.asset_uid)
        if snapshot is None:
            continue
        category = _fundamental_category(record.share.sector)
        bucket = groups.setdefault(category, {})
        for metric in PEER_FUNDAMENTAL_METRICS:
            value = snapshot.value(metric)
            if value is not None:
                bucket.setdefault(metric, []).append(value)
    output: dict[str, dict[str, Decimal]] = {}
    for category, metrics in groups.items():
        medians = {}
        for metric, values in metrics.items():
            if len(values) >= min_peers:
                value = median(values)
                if value is not None:
                    medians[metric] = value
        if medians:
            output[category] = medians
    return output


def _peer_threshold(
    peers: dict[str, Decimal],
    metric: str,
    absolute: Decimal,
    *,
    higher_is_better: bool,
) -> Decimal:
    peer = peers.get(metric)
    if peer is None:
        return absolute
    return max(absolute, peer) if higher_is_better else min(absolute, peer)


def _fundamental_assessment(
    snapshot: FundamentalSnapshot | None,
    sector: str,
    peer_medians: dict[str, dict[str, Decimal]] | None = None,
) -> tuple[Decimal | None, tuple[str, ...], int, str]:
    category = _fundamental_category(sector)
    peers = (peer_medians or {}).get(category, {})
    if snapshot is None:
        return None, ("Фундаментал отсутствует.",), 0, category
    values = snapshot.values
    criteria: list[tuple[str, bool | None, str]]
    if category == "financial":
        criteria = [
            (
                "ROE",
                _gt(
                    values.get("roe"),
                    _peer_threshold(peers, "roe", Decimal("0.10"), higher_is_better=True),
                ),
                "ROE",
            ),
            (
                "ROA",
                _gt(
                    values.get("roa"),
                    _peer_threshold(peers, "roa", Decimal("0.01"), higher_is_better=True),
                ),
                "ROA",
            ),
            (
                "чистая процентная маржа",
                _gt(values.get("net_interest_margin_mrq"), ZERO),
                "чистая процентная маржа",
            ),
            ("P/E", _between(values.get("pe_ratio_ttm"), ZERO, Decimal("20")), "P/E"),
            (
                "P/B",
                _between(
                    values.get("price_to_book_ttm"),
                    ZERO,
                    _peer_threshold(
                        peers,
                        "price_to_book_ttm",
                        Decimal("5"),
                        higher_is_better=False,
                    ),
                ),
                "P/B",
            ),
        ]
    elif category == "real_estate":
        criteria = [
            (
                "ROE",
                _gt(
                    values.get("roe"),
                    _peer_threshold(peers, "roe", Decimal("0.08"), higher_is_better=True),
                ),
                "ROE",
            ),
            (
                "P/B",
                _between(
                    values.get("price_to_book_ttm"),
                    ZERO,
                    _peer_threshold(
                        peers,
                        "price_to_book_ttm",
                        Decimal("3"),
                        higher_is_better=False,
                    ),
                ),
                "P/B",
            ),
            (
                "долг/EBITDA",
                _lt(
                    values.get("total_debt_to_ebitda_mrq"),
                    _peer_threshold(
                        peers,
                        "total_debt_to_ebitda_mrq",
                        Decimal("5"),
                        higher_is_better=False,
                    ),
                ),
                "долг/EBITDA",
            ),
            (
                "текущая ликвидность",
                _gt(values.get("current_ratio_mrq"), ONE),
                "текущая ликвидность",
            ),
            (
                "рост выручки",
                _gt(values.get("one_year_annual_revenue_growth_rate"), ZERO),
                "рост выручки",
            ),
        ]
    elif category == "capital_intensive":
        criteria = [
            (
                "EV/EBITDA",
                _between(
                    values.get("ev_to_ebitda_mrq"),
                    ZERO,
                    _peer_threshold(
                        peers,
                        "ev_to_ebitda_mrq",
                        Decimal("8"),
                        higher_is_better=False,
                    ),
                ),
                "EV/EBITDA",
            ),
            (
                "долг/EBITDA",
                _lt(
                    values.get("total_debt_to_ebitda_mrq"),
                    _peer_threshold(
                        peers,
                        "total_debt_to_ebitda_mrq",
                        Decimal("3"),
                        higher_is_better=False,
                    ),
                ),
                "долг/EBITDA",
            ),
            (
                "покрытие фиксированных платежей",
                _gt(values.get("fixed_charge_coverage_ratio_fy"), Decimal("2")),
                "покрытие фиксированных платежей",
            ),
            (
                "ROIC",
                _gt(
                    values.get("roic"),
                    _peer_threshold(peers, "roic", Decimal("0.08"), higher_is_better=True),
                ),
                "ROIC",
            ),
            (
                "FCF к цене",
                _gt(values.get("free_cash_flow_to_price"), ZERO),
                "FCF к цене",
            ),
            (
                "рост выручки",
                _gt(values.get("one_year_annual_revenue_growth_rate"), ZERO),
                "рост выручки",
            ),
        ]
    else:
        criteria = [
            (
                "ROE",
                _gt(
                    values.get("roe"),
                    _peer_threshold(peers, "roe", Decimal("0.08"), higher_is_better=True),
                ),
                "ROE",
            ),
            (
                "маржа",
                _gt(
                    values.get("net_margin_mrq"),
                    _peer_threshold(
                        peers,
                        "net_margin_mrq",
                        Decimal("0.03"),
                        higher_is_better=True,
                    ),
                ),
                "маржа",
            ),
            (
                "долг/EBITDA",
                _lt(
                    values.get("total_debt_to_ebitda_mrq"),
                    _peer_threshold(
                        peers,
                        "total_debt_to_ebitda_mrq",
                        Decimal("3"),
                        higher_is_better=False,
                    ),
                ),
                "долг/EBITDA",
            ),
            (
                "рост выручки",
                _gt(values.get("one_year_annual_revenue_growth_rate"), ZERO),
                "рост выручки",
            ),
            ("P/E", _between(values.get("pe_ratio_ttm"), ZERO, Decimal("25")), "P/E"),
            (
                "P/B",
                _between(
                    values.get("price_to_book_ttm"),
                    ZERO,
                    _peer_threshold(
                        peers,
                        "price_to_book_ttm",
                        Decimal("8"),
                        higher_is_better=False,
                    ),
                ),
                "P/B",
            ),
        ]
    available = [(name, passed, label) for name, passed, label in criteria if passed is not None]
    if len(available) < 2:
        return (
            None,
            ("Недостаточно фундаментальных показателей.",),
            len(available),
            category,
        )
    passed = sum(1 for _, value, _ in available if value)
    score = Decimal(passed) / Decimal(len(available)) * Decimal("100")
    notes: list[str] = []
    for _name, value, label in available:
        marker = "+" if value else "−"
        notes.append(f"{marker} {label}")
    if snapshot.currency != "RUB":
        notes.append(f"Валюта отчётности: {snapshot.currency}")
    if peers:
        notes.append(f"Учтена медиана сектора: {len(peers)} показателей")
    return score, tuple(notes), len(available), category


def _gt(value: Decimal | None, threshold: Decimal) -> bool | None:
    return None if value is None else value > threshold


def _lt(value: Decimal | None, threshold: Decimal) -> bool | None:
    return None if value is None else value < threshold


def _between(value: Decimal | None, low: Decimal, high: Decimal) -> bool | None:
    return None if value is None else low < value <= high


def _invalidation_conditions(record: _Record) -> tuple[str, ...]:
    features = record.features
    if features is None:
        return ()
    conditions: list[str] = []
    if features.sma50 is not None:
        conditions.append(f"Закрытие ниже SMA50 ({features.sma50:.2f})")
    if record.history:
        recent = record.history[-20:]
        low = min((bar.low for bar in recent), default=None)
        if low is not None:
            conditions.append(f"Закрытие ниже минимума 20 сессий ({low:.2f})")
    if features.close > ZERO:
        conditions.append(
            f"Падение более 10% от текущей цены ({features.close * Decimal('0.90'):.2f})"
        )
    return tuple(conditions[:3])


def _score_record(
    record: _Record,
    all_features: list[FeatureSet],
    config: ScreenerConfig,
    fundamentals: dict[str, FundamentalSnapshot],
    peer_medians: dict[str, dict[str, Decimal]] | None = None,
) -> tuple[Candidate | None, ScoreBreakdown | None, tuple[str, ...]]:
    assert record.features is not None and record.bar is not None
    breakdown = score_features(record.features, all_features, config)
    warnings: list[str] = list(breakdown.warnings)
    fundamental_score = None
    fundamental_notes: tuple[str, ...] = ()
    fundamental_coverage = 0
    fundamental_category = ""
    if config.mode == "technical_fundamental":
        (
            fundamental_score,
            fundamental_notes,
            fundamental_coverage,
            fundamental_category,
        ) = _fundamental_assessment(
            fundamentals.get(record.share.asset_uid),
            record.share.sector,
            peer_medians,
        )
        warnings.extend(fundamental_notes)
        if fundamental_score is None or fundamental_coverage < config.min_fundamental_coverage:
            warnings.append(
                f"Фундаментал: доступно {fundamental_coverage} критериев, "
                f"нужно ≥ {config.min_fundamental_coverage}; кандидат исключён."
            )
            return None, breakdown, tuple(warnings)
        if fundamental_score < Decimal("35"):
            return None, breakdown, tuple(warnings)
    warnings.extend(f"Качество данных: {flag}." for flag in record.quality.flags)
    if breakdown.score is None:
        warnings.append("Недостаточно данных для расчёта рейтинга.")
        return None, breakdown, tuple(dict.fromkeys(warnings))
    candidate = Candidate(
        share=record.share,
        bar=record.bar,
        features=record.features,
        quality=record.quality,
        blocks=breakdown.blocks,
        score=breakdown.score,
        penalties=breakdown.penalties,
        reasons=breakdown.reasons,
        warnings=tuple(dict.fromkeys(warnings)),
        fundamental=fundamentals.get(record.share.asset_uid),
        spread_pct=record.spread_pct,
        spread_fetched_at=record.spread_fetched_at,
        fundamental_score=fundamental_score,
        fundamental_notes=fundamental_notes,
        fundamental_coverage=fundamental_coverage,
        fundamental_category=fundamental_category,
        invalidation=_invalidation_conditions(record),
        history=record.history,
        data_fetched_at=(
            fundamentals[record.share.asset_uid].fetched_at
            if record.share.asset_uid in fundamentals
            else None
        ),
    )
    return candidate, breakdown, tuple(dict.fromkeys(warnings))


def _sort_key(candidate: Candidate) -> tuple[Decimal, Decimal, str]:
    turnover = candidate.features.median_turnover_60 or ZERO
    return (candidate.score, turnover, candidate.ticker)


def _select_with_sector_cap(
    pool: list[Candidate],
    max_candidates: int,
    max_sector_share: Decimal,
    max_issuer_candidates: int = 1,
) -> list[Candidate]:
    ordered = sorted(pool, key=_sort_key, reverse=True)
    limit = max(1, int(Decimal(max_candidates) * max_sector_share))
    sector_counts: dict[str, int] = {}
    issuer_counts: dict[str, int] = {}
    selected: list[Candidate] = []
    for candidate in ordered:
        if len(selected) >= max_candidates:
            break
        sector = candidate.share.sector or "other"
        if sector_counts.get(sector, 0) >= limit:
            continue
        issuer = candidate.share.issuer_key
        if max_issuer_candidates > 0 and issuer_counts.get(issuer, 0) >= max_issuer_candidates:
            continue
        selected.append(candidate)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
    return selected


def _refresh_trading_statuses(
    pool: list[_Record],
    client: ScreenerTInvestClient | None,
    warnings: list[str],
) -> list[_Record]:
    if client is None or not pool:
        return pool
    try:
        statuses = client.get_trading_statuses([record.share.uid for record in pool])
    except Exception as exc:
        warnings.append(f"Торговые статусы: {exc}")
        return pool
    by_uid = {status.instrument_uid: status for status in statuses}
    refreshed: list[_Record] = []
    for record in pool:
        status = by_uid.get(record.share.uid)
        if status is None:
            refreshed.append(record)
            continue
        refreshed.append(
            replace(
                record,
                share=replace(
                    record.share,
                    trading_status=status.trading_status,
                    api_trade_available=status.api_trade_available,
                ),
            )
        )
    return refreshed


def _quote_records(
    pool: list[_Record],
    client: ScreenerTInvestClient | None,
    config: ScreenerConfig,
    warnings: list[str],
) -> tuple[list[_Record], bool]:
    if client is None or not config.fetch_orderbook:
        return pool, False
    quoted: list[_Record] = []
    valid = 0
    for record in sorted(
        pool,
        key=lambda item: (
            item.features.median_turnover_60 or ZERO if item.features is not None else ZERO,
            item.share.ticker,
        ),
        reverse=True,
    ):
        try:
            quote = client.orderbook(record.share.uid, depth=10)
        except Exception as exc:
            warnings.append(f"Стакан {record.share.ticker}: {exc}")
            quoted.append(record)
            continue
        if quote.is_valid:
            valid += 1
            quoted.append(
                replace(
                    record,
                    spread_pct=quote.spread_pct,
                    spread_fetched_at=quote.fetched_at,
                )
            )
        else:
            quoted.append(record)
    if valid == 0:
        warnings.append(
            "Стакан пуст или недоступен (например, рынок закрыт); фильтр спреда не применён."
        )
        return quoted, False
    bad = {
        item.share.uid
        for item in quoted
        if item.spread_pct is not None and item.spread_pct > config.max_spread_pct
    }
    if bad:
        warnings.append(f"Исключено по спреду > {config.max_spread_pct}%: {len(bad)}.")
        quoted = [item for item in quoted if item.share.uid not in bad]
    unquoted = sum(1 for item in quoted if item.spread_pct is None)
    if unquoted:
        warnings.append(
            f"Стакан недоступен для {unquoted} бумаг; они оставлены без проверки спреда."
        )
    return quoted, True


def _result_warnings(
    config: ScreenerConfig,
    benchmark: str,
    benchmark_bars: list[Bar],
    records: list[_Record],
    selected: list[Candidate],
    orderbook_valid: bool,
    refresh: bool,
) -> list[str]:
    warnings: list[str] = []
    if benchmark != config.benchmark:
        warnings.append(f"Использован резервный бенчмарк {benchmark}.")
    if len(benchmark_bars) < 320:
        warnings.append(
            f"У {benchmark} только {len(benchmark_bars)} сессий; длинная история ограничена."
        )
    warnings.append(
        "Рейтинг — это правило ранжирования, а не вероятность роста. "
        "Прошлые движения не гарантируют будущий результат."
    )
    warnings.append(
        "Survivorship bias: историческая проверка использует доступный сейчас список бумаг."
    )
    if config.mode == "technical_fundamental":
        warnings.append(
            "Фундаментал T-Invest — текущий TTM-снимок, не point-in-time; "
            "в историческом бэктесте он не используется."
        )
    if not selected:
        warnings.append("Подходящих кандидатов по текущим правилам нет.")
    if not orderbook_valid and config.fetch_orderbook:
        warnings.append("Текущий спред не проверен из-за отсутствия качественного стакана.")
    if not refresh:
        warnings.append("Использован кэш без обновления рыночных данных.")
    eligible = [record for record in records if record.share.is_active]
    if any(record.quality.flags for record in eligible):
        flagged = sum(1 for record in eligible if record.quality.flags)
        warnings.append(f"У {flagged} активных бумаг есть замечания к качеству данных.")
    return warnings


class ScreenerService:
    def __init__(
        self,
        store: ScreenerStore,
        config: ScreenerConfig,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.settings = settings

    def run(
        self,
        *,
        refresh: bool = True,
        offline: bool = False,
        as_of: date | None = None,
    ) -> ScreenerResult:
        warnings: list[str] = []
        client: ScreenerTInvestClient | None = None
        if refresh and not offline:
            if self.settings is None:
                raise RuntimeError("Settings are required for a live refresh")
            client = build_screener_client(self.settings, self.config)
        try:
            shares = _load_instruments(
                self.store,
                client,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            benchmark, benchmark_bars = _load_benchmark(
                self.store,
                client,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            if not benchmark_bars:
                raise RuntimeError("Нет завершённых данных бенчмарка.")
            benchmark_as_of = benchmark_bars[-1].trade_date
            if as_of is not None:
                benchmark_as_of = as_of
            benchmark_bars = [bar for bar in benchmark_bars if bar.trade_date <= benchmark_as_of]
            if not benchmark_bars or benchmark_bars[-1].trade_date != benchmark_as_of:
                raise RuntimeError(f"{benchmark_as_of} нет в календаре бенчмарка {benchmark}.")
            primary = select_primary_listings(shares)
            _refresh_bars(
                self.store,
                client,
                primary,
                benchmark_as_of,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            _refresh_dividends(
                self.store,
                client,
                primary,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            _refresh_fundamentals(
                self.store,
                client,
                primary,
                self.config,
                refresh=refresh and not offline,
                warnings=warnings,
            )
            bars_by_uid = self.store.load_all_bars()
            dividends_by_uid = self.store.load_dividends()
            fundamentals = self.store.load_fundamentals()

            calendar = [bar.trade_date for bar in benchmark_bars]
            calendar_index = {day: index for index, day in enumerate(calendar)}
            records = _quality_and_features(
                primary,
                bars_by_uid,
                dividends_by_uid,
                benchmark_bars,
                calendar,
                calendar_index,
                benchmark_as_of,
                self.config,
            )
            base = [record for record in records if _is_moex_rub(record.share)]
            active = (
                [record for record in base if record.share.is_active]
                if self.config.require_active
                else base
            )
            buyable = [
                record
                for record in active
                if not self.config.require_buy_available or record.share.buy_available
            ]
            if not self.config.include_qualified:
                buyable = [record for record in buyable if not record.share.for_qual_investor]
            history = [
                record
                for record in buyable
                if record.quality.history_sessions >= self.config.min_history_sessions
            ]
            activity = [
                record
                for record in history
                if record.quality.activity_ratio >= self.config.min_activity_ratio
            ]
            liquidity = [
                record
                for record in activity
                if record.features is not None
                and record.features.median_turnover_60 is not None
                and record.features.median_turnover_60 >= self.config.min_median_turnover
                and record.features.median_turnover_60 * self.config.participation_cap
                >= self.config.assumed_position_rub
            ]
            spike_free = [
                record
                for record in liquidity
                if record.features is not None
                and record.features.max_turnover_share_60 is not None
                and record.features.max_turnover_share_60 <= self.config.max_single_turnover_share
            ]
            if len(spike_free) < len(liquidity):
                warnings.append(
                    f"Исключено по единичному всплеску оборота: {len(liquidity) - len(spike_free)}."
                )
            liquidity = spike_free
            quality_ok = [record for record in liquidity if not record.quality.critical]
            quality_count = len(quality_ok)
            quality_ok = _refresh_trading_statuses(
                quality_ok,
                client if not offline else None,
                warnings,
            )
            quality_ok, orderbook_valid = _quote_records(
                quality_ok,
                client if not offline else None,
                self.config,
                warnings,
            )
            spread_ok = len(quality_ok)
            regime = compute_regime(
                benchmark,
                benchmark_bars,
                benchmark_as_of,
                [record.features for record in activity if record.features is not None],
            )
            quality_ok = _attach_sector_context(quality_ok, self.config)
            all_features = [record.features for record in quality_ok if record.features is not None]
            peer_medians = _peer_fundamental_medians(
                quality_ok,
                fundamentals,
                self.config.sector_min_peers,
            )
            fundamental_ok = 0
            scoring_records = quality_ok
            if self.config.mode == "technical_fundamental":
                for record in quality_ok:
                    _, _, coverage, _ = _fundamental_assessment(
                        fundamentals.get(record.share.asset_uid),
                        record.share.sector,
                        peer_medians,
                    )
                    if coverage >= self.config.min_fundamental_coverage:
                        fundamental_ok += 1
                coverage_ratio = (
                    Decimal(fundamental_ok) / Decimal(len(quality_ok)) if quality_ok else ZERO
                )
                if coverage_ratio < self.config.min_fundamental_universe_coverage:
                    scoring_records = []
                    warnings.append(
                        "Недостаточное покрытие фундаментала: "
                        f"{fundamental_ok} из {len(quality_ok)} бумаг "
                        f"({coverage_ratio * 100:.0f}%, нужно ≥ "
                        f"{self.config.min_fundamental_universe_coverage * 100:.0f}%). "
                        "Режим technical+fundamental не сформирован."
                    )
            all_features = [
                record.features for record in scoring_records if record.features is not None
            ]
            scored: list[Candidate] = []
            for record in scoring_records:
                candidate, _, _ = _score_record(
                    record,
                    all_features,
                    self.config,
                    fundamentals,
                    peer_medians,
                )
                if candidate is not None:
                    scored.append(candidate)
            effective_min = self.config.min_score
            effective_max = self.config.max_candidates
            if regime.label == "risk_off":
                effective_min = max(effective_min, self.config.risk_off_min_score)
                effective_max = min(effective_max, self.config.risk_off_max_candidates)
                warnings.append(
                    "Режим рынка risk-off: порог рейтинга повышен, число кандидатов ограничено."
                )
            score_ok = [candidate for candidate in scored if candidate.score >= effective_min]
            selected = _select_with_sector_cap(
                score_ok,
                effective_max,
                self.config.max_sector_share,
                self.config.max_issuer_candidates,
            )
            sector_ranks: dict[str, int] = {}
            ranked: list[Candidate] = []
            for candidate in selected:
                sector = candidate.share.sector or "other"
                sector_ranks[sector] = sector_ranks.get(sector, 0) + 1
                ranked.append(replace(candidate, sector_rank=sector_ranks[sector]))
            selected = ranked
            if self.config.max_issuer_candidates == 1:
                issuer_counts: dict[str, int] = {}
                for candidate in score_ok:
                    issuer = candidate.share.issuer_key
                    issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
                duplicate_issuers = sum(count - 1 for count in issuer_counts.values())
                if duplicate_issuers:
                    warnings.append(
                        "Лимит эмитента: не более одной бумаги на эмитента; "
                        f"исключено дублей по эмитенту: {duplicate_issuers}."
                    )
            selected_ids = {item.share.uid for item in selected}
            near = [
                item
                for item in sorted(score_ok, key=_sort_key, reverse=True)
                if item.share.uid not in selected_ids
                and item.score >= self.config.near_candidate_score
            ][:10]
            warnings.extend(
                _result_warnings(
                    self.config,
                    benchmark,
                    benchmark_bars,
                    records,
                    selected,
                    orderbook_valid,
                    refresh and not offline,
                )
            )
            errors = self.store.fetch_errors()
            if errors:
                warnings.append(f"В журнале загрузки {len(errors)} ошибок; см. fetch_log.")
            stats = FilterStats(
                shares_total=len(shares),
                moex_rub_common_preferred=sum(1 for share in shares if _is_moex_rub(share)),
                active=len(active),
                buy_available=len(buyable),
                history_ok=len(history),
                activity_ok=len(activity),
                liquidity_ok=len(liquidity),
                quality_ok=quality_count,
                spread_ok=spread_ok,
                spread_checked=orderbook_valid,
                fundamental_ok=fundamental_ok,
                score_ok=len(score_ok),
            )
            result = ScreenerResult(
                as_of=benchmark_as_of,
                mode=self.config.mode,
                benchmark=benchmark,
                universe=tuple(primary),
                candidates=tuple(selected),
                near_candidates=tuple(near),
                filter_stats=stats,
                regime=regime,
                warnings=tuple(dict.fromkeys(warnings)),
                generated_at=datetime.now(UTC),
                config=self.config.to_dict(),
            )
            from app.screener.report import result_to_dict

            self.store.save_run("screen", result_to_dict(result), as_of=benchmark_as_of)
            return result
        finally:
            if client is not None:
                client.close()
