from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.screener.domain import Bar, DataQuality, FundamentalSnapshot, OrderBookQuote
from app.screener.report import result_to_dict, result_to_html, result_to_markdown
from app.screener.screen import (
    ScreenerService,
    _fundamental_assessment,
    _fundamental_category,
    _is_moex_rub,
    _quote_records,
    _Record,
    _refresh_trading_statuses,
    _select_with_sector_cap,
    select_primary_listings,
)
from app.screener.store import ScreenerStore
from helpers import feature, make_candidate, make_share, screener_config, trading_days

ONE = Decimal("1")
ZERO = Decimal("0")


def test_primary_listing_prefers_tqbr_active_and_deduplicates_asset() -> None:
    preferred = make_share(
        uid="sber-other",
        ticker="SBER",
        class_code="TQBR",
        asset_uid="sber-asset",
        first_candle_date=date(2013, 3, 1),
    )
    older = make_share(
        uid="sber-old",
        ticker="SBER",
        class_code="TQBS",
        asset_uid="sber-asset",
        first_candle_date=date(2000, 1, 1),
        is_active=False,
    )
    foreign = make_share(
        uid="foreign",
        ticker="FOREIGN",
        asset_uid="foreign-asset",
        exchange="SPB",
        real_exchange="SPB",
        currency="USD",
    )
    otc = make_share(uid="otc", ticker="OTC", asset_uid="otc-asset", otc=True)

    selected = select_primary_listings([older, foreign, otc, preferred])

    assert [item.uid for item in selected] == ["sber-other"]


def test_moex_filter_rejects_foreign_otc_receipts_but_keeps_tqbr() -> None:
    foreign = make_share(
        ticker="AAPL-RM",
        class_code="MTQR",
        exchange="otc_ncc",
        real_exchange="REAL_EXCHANGE_MOEX",
    )
    main = make_share(
        ticker="SBER",
        class_code="TQBR",
        exchange="unknown",
        real_exchange="REAL_EXCHANGE_MOEX",
        weekend=True,
    )

    assert not _is_moex_rub(foreign)
    assert _is_moex_rub(main)


def test_sector_cap_keeps_variety() -> None:
    pool = [
        make_candidate("A1", "A", "90"),
        make_candidate("A2", "A", "80"),
        make_candidate("B1", "B", "70"),
        make_candidate("C1", "C", "60"),
    ]
    selected = _select_with_sector_cap(pool, max_candidates=4, max_sector_share=Decimal("0.35"))
    assert [item.ticker for item in selected] == ["A1", "B1", "C1"]

    single = _select_with_sector_cap(pool, max_candidates=1, max_sector_share=Decimal("0.35"))
    assert [item.ticker for item in single] == ["A1"]


def test_issuer_key_merges_common_and_preferred_shares() -> None:
    common = make_share(name="Сбербанк")
    preferred = make_share(name="Сбербанк - привилегированные акции")

    assert common.issuer_key == preferred.issuer_key == "сбербанк"


def test_issuer_cap_keeps_one_security_per_issuer() -> None:
    pool = [
        make_candidate("SBER", "financial", "90", issuer="Сбербанк"),
        make_candidate("SBERP", "financial", "85", issuer="Сбербанк"),
        make_candidate("GAZP", "energy", "80", issuer="Газпром"),
    ]

    selected = _select_with_sector_cap(
        pool,
        max_candidates=3,
        max_sector_share=Decimal("1"),
        max_issuer_candidates=1,
    )

    assert [item.ticker for item in selected] == ["SBER", "GAZP"]


def test_fundamental_assessment_uses_financial_rules() -> None:
    snapshot = FundamentalSnapshot(
        asset_uid="bank",
        fetched_at=datetime(2025, 1, 1, tzinfo=UTC),
        as_of=date(2025, 1, 1),
        currency="RUB",
        values={
            "roe": Decimal("0.20"),
            "roa": Decimal("0.02"),
            "pe_ratio_ttm": Decimal("10"),
            "price_to_book_ttm": Decimal("1"),
        },
    )
    score, notes, available, category = _fundamental_assessment(snapshot, "financial")
    assert score == Decimal("100")
    assert available == 4
    assert category == "financial"
    assert all(item.startswith("+") for item in notes)


def test_fundamental_assessment_uses_peer_medians() -> None:
    snapshot = FundamentalSnapshot(
        asset_uid="bank",
        fetched_at=datetime(2025, 1, 1, tzinfo=UTC),
        as_of=date(2025, 1, 1),
        currency="RUB",
        values={
            "roe": Decimal("0.20"),
            "roa": Decimal("0.02"),
            "pe_ratio_ttm": Decimal("10"),
            "price_to_book_ttm": Decimal("1"),
        },
    )
    score, _notes, available, category = _fundamental_assessment(
        snapshot,
        "financial",
        {"financial": {"roe": Decimal("0.25")}},
    )
    assert category == "financial"
    assert available == 4
    assert score is not None and score < Decimal("100")


def test_fundamental_category_maps_russian_and_english_sectors() -> None:
    assert _fundamental_category("Финансы") == "financial"
    assert _fundamental_category("financial") == "financial"
    assert _fundamental_category("Недвижимость") == "real_estate"
    assert _fundamental_category("energy") == "capital_intensive"
    assert _fundamental_category("it") == "general"


def test_screen_offline_builds_report_from_cache(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(db_path=tmp_path / "screener.db")
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [
            Bar(
                day,
                Decimal(str(100 + index / 10)),
                Decimal(str(101 + index / 10)),
                Decimal(str(99 + index / 10)),
                Decimal(str(100 + index / 10)),
                0,
            )
            for index, day in enumerate(days)
        ],
    )
    store.upsert_bars(
        share.uid,
        [
            Bar(
                day,
                Decimal(str(100 + index / 5)),
                Decimal(str(101 + index / 5)),
                Decimal(str(99 + index / 5)),
                Decimal(str(100 + index / 5)),
                1000,
            )
            for index, day in enumerate(days)
        ],
    )

    result = ScreenerService(store, config).run(refresh=False)
    payload = result_to_dict(result)
    html = result_to_html(result)
    markdown = result_to_markdown(result)

    assert result.as_of == days[-1]
    assert result.filter_stats.quality_ok == 1
    assert result.filter_stats.spread_checked is False
    assert len(result.candidates) == 1
    assert payload["candidates"][0]["ticker"] == "TEST"
    assert payload["candidates"][0]["sector_rank"] == 1
    assert payload["candidates"][0]["provenance"]["price"]["source"] == ("T-Invest daily candle")
    assert payload["provenance"]["market_data"]["source"] == "T-Invest API"
    assert "TEST" in html
    assert "Спред (не проверен)" in markdown
    assert "Спред (не проверен)" in html
    assert "TEST" in markdown
    store.close()


def test_screen_excludes_single_turnover_spike(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(db_path=tmp_path / "spike-screener.db")
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [
            Bar(
                day,
                Decimal(str(100 + index / 10)),
                Decimal(str(101 + index / 10)),
                Decimal(str(99 + index / 10)),
                Decimal(str(100 + index / 10)),
                0,
            )
            for index, day in enumerate(days)
        ],
    )
    bars = [
        Bar(
            day,
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            60_000_000 if index == len(days) - 10 else 1_000_000,
        )
        for index, day in enumerate(days)
    ]
    store.upsert_bars(share.uid, bars)

    result = ScreenerService(store, config).run(refresh=False)

    assert result.candidates == ()
    assert any("единичному всплеску оборота" in item for item in result.warnings)
    assert result.filter_stats.liquidity_ok == 0
    store.close()


def test_fundamental_mode_requires_universe_coverage(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(
        db_path=tmp_path / "screener.db",
        mode="technical_fundamental",
    )
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [
            Bar(day, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 0)
            for day in days
        ],
    )
    store.upsert_bars(
        share.uid,
        [
            Bar(
                day,
                Decimal(str(100 + index / 5)),
                Decimal(str(101 + index / 5)),
                Decimal(str(99 + index / 5)),
                Decimal(str(100 + index / 5)),
                1000,
            )
            for index, day in enumerate(days)
        ],
    )

    result = ScreenerService(store, config).run(refresh=False)

    assert result.filter_stats.quality_ok == 1
    assert result.filter_stats.fundamental_ok == 0
    assert result.candidates == ()
    assert any("Недостаточное покрытие фундаментала" in item for item in result.warnings)
    store.close()


def test_screen_excludes_critical_quality(tmp_path: Path) -> None:
    days = trading_days(120)
    config = screener_config(db_path=tmp_path / "screener.db")
    store = ScreenerStore(config.db_path)
    share = make_share(first_candle_date=days[0])
    store.upsert_shares([share])
    store.upsert_benchmark(
        "MCFTR",
        [
            Bar(day, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 0)
            for day in days
        ],
    )
    bars = [
        Bar(
            day,
            Decimal(str(100 + index / 5)),
            Decimal(str(101 + index / 5)),
            Decimal(str(99 + index / 5)),
            Decimal(str(100 + index / 5)),
            1000,
        )
        for index, day in enumerate(days)
    ]
    bars[-1] = replace(bars[-1], close=Decimal("200"), high=Decimal("200"))
    store.upsert_bars(share.uid, bars)

    result = ScreenerService(store, config).run(refresh=False)
    assert result.candidates == ()
    assert result.filter_stats.quality_ok == 0
    store.close()


class _StatusClient:
    def get_trading_statuses(self, uids: list[str]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                instrument_uid=uid,
                trading_status="SECURITY_TRADING_STATUS_NORMAL_TRADING",
                api_trade_available=True,
            )
            for uid in uids
        ]


class _QuoteClient:
    def __init__(self) -> None:
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def orderbook(self, uid: str, depth: int = 10) -> OrderBookQuote:
        if uid == "uid-WIDE":
            return OrderBookQuote(uid, Decimal("99"), Decimal("101"), Decimal("2"), self.now)
        return OrderBookQuote(uid, Decimal("100"), Decimal("100.1"), Decimal("0.1"), self.now)


def _record(ticker: str, turnover: str) -> _Record:
    share = make_share(
        uid=f"uid-{ticker}",
        ticker=ticker,
        asset_uid=f"asset-{ticker}",
        sector="A",
    )
    return _Record(
        share=share,
        bar=None,
        features=feature(median_turnover_60=Decimal(turnover)),
        quality=DataQuality(
            history_sessions=100,
            activity_ratio=ONE,
            missing_ratio=ZERO,
            zero_volume_ratio=ZERO,
            last_bar_date=date(2025, 1, 1),
            stale_sessions=0,
        ),
    )


def test_trading_status_refresh_updates_candidate_share() -> None:
    pool = [_record("TEST", "100")]
    warnings: list[str] = []

    result = _refresh_trading_statuses(pool, _StatusClient(), warnings)  # type: ignore[arg-type]

    assert result[0].share.trading_status == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
    assert result[0].share.api_trade_available is True
    assert warnings == []


def test_quote_records_drops_wide_spread_candidate() -> None:
    pool = [
        _record("WIDE", "90"),
        _record("TIGHT", "80"),
    ]
    config = screener_config(fetch_orderbook=True, max_spread_pct=Decimal("0.7"))
    warnings: list[str] = []

    result, applied = _quote_records(pool, _QuoteClient(), config, warnings)  # type: ignore[arg-type]

    assert applied
    assert [item.share.ticker for item in result] == ["TIGHT"]
    assert any("спреду" in warning for warning in warnings)
