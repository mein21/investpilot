from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.core.config import Settings
from app.core.enums import TradingMode
from app.factory import create_app
from app.screener.domain import BlockScores, FilterStats, MarketRegime, ScreenerResult
from app.screener.report import result_to_dict
from app.screener.store import ScreenerStore
from app.web.presentation import sparkline_levels
from fastapi.testclient import TestClient
from helpers import bar_series, make_candidate, make_share, trading_days


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_enabled=False,
        tinvest_client="mock",
        database_url="sqlite://",
        seed_demo_data=False,
        backup_dir=tmp_path,
        trading_mode=TradingMode.PAPER,
        scheduler_enabled=False,
        screener_db_path=tmp_path / "screener.db",
    )


def seed_screener_cache(path: Path) -> None:
    days = trading_days(320)
    share = make_share(first_candle_date=days[0])
    store = ScreenerStore(path)
    try:
        store.upsert_shares([share])
        store.upsert_benchmark(
            "MCFTR",
            bar_series(
                days,
                [100 + index / 10 for index in range(len(days))],
                volume=0,
            ),
        )
        store.upsert_bars(
            share.uid,
            bar_series(
                days,
                [100 + index / 4 for index in range(len(days))],
                volume=500_000,
            ),
        )
    finally:
        store.close()


def seed_saved_run(path: Path) -> None:
    share = make_share(first_candle_date=date(2024, 1, 1))
    score = Decimal("85")
    history_days = trading_days(30)
    base_candidate = make_candidate("TEST", "IT", "85")
    base = replace(
        base_candidate,
        share=replace(
            base_candidate.share,
            trading_status="SECURITY_TRADING_STATUS_NORMAL_TRADING",
        ),
    )
    candidate = replace(
        base,
        features=replace(
            base.features,
            sma20=Decimal("115"),
            sma50=Decimal("110"),
            sma200=Decimal("105"),
        ),
        history=tuple(bar_series(history_days, [100 + index for index in range(30)])),
        invalidation=("Закрытие ниже SMA50",),
        fundamental_score=Decimal("75"),
        fundamental_coverage=4,
        fundamental_category="general",
        data_fetched_at=datetime(2026, 10, 28, 11, tzinfo=UTC),
        blocks=BlockScores(
            relative_strength=score,
            trend_quality=score,
            volume_confirmation=score,
            consolidation_breakout=score,
            risk_moderation=score,
        ),
        reasons=("Цена выше SMA200", "Импульс выше бенчмарка"),
    )
    result = ScreenerResult(
        as_of=date(2026, 10, 28),
        mode="technical",
        benchmark="MCFTR",
        universe=(share,),
        candidates=(candidate,),
        near_candidates=(),
        filter_stats=FilterStats(
            shares_total=1,
            moex_rub_common_preferred=1,
            active=1,
            buy_available=1,
            history_ok=1,
            activity_ok=1,
            liquidity_ok=1,
            quality_ok=1,
            spread_ok=1,
            score_ok=1,
        ),
        regime=MarketRegime(
            as_of=date(2026, 10, 28),
            benchmark="MCFTR",
            label="risk_on",
            close=Decimal("100"),
            sma200=Decimal("95"),
            slope_20=Decimal("0.01"),
            volatility_60=Decimal("0.1"),
            drawdown_252=Decimal("0"),
            breadth_50=Decimal("0.6"),
            breadth_200=Decimal("0.7"),
            explanation="Бенчмарк выше SMA200.",
        ),
        warnings=(
            "Survivorship bias: историческая проверка использует доступный сейчас список бумаг.",
        ),
        generated_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
        config={
            "min_score": "60",
            "max_candidates": 20,
            "max_sector_share": "0.35",
        },
    )
    store = ScreenerStore(path)
    try:
        store.save_run("screen", result_to_dict(result), as_of=result.as_of)
        store.save_run(
            "backtest",
            {
                "kind": "backtest",
                "sensitivity": [
                    {
                        "kind": "score_band",
                        "bucket": "≥ 80",
                        "min_score": "80",
                        "max_score": "100",
                        "trades": 12,
                        "mean_excess": "0.05",
                        "target_rate": "0.25",
                        "beat_rate": "0.60",
                        "insufficient": False,
                    }
                ],
            },
            as_of=result.as_of,
        )
    finally:
        store.close()


def test_sparkline_levels_align_with_price_range() -> None:
    history = [{"close": "100"}, {"close": "110"}]

    levels = sparkline_levels(history, {"SMA20": "105", "SMA200": "90"})

    assert levels == [{"label": "SMA20", "y": "24.0", "price": "105,00"}]


def test_screener_refresh_uses_cache_and_saves_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_screener_cache(settings.screener_db_path)
    app = create_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        refreshed = client.post("/screener/refresh")
        assert refreshed.status_code == 303
        page = client.get("/screener")

    assert page.status_code == 200
    assert "Кандидатов нет" in page.text
    assert "Использован кэш без обновления рыночных данных" in page.text
    assert "Кэш скринера пуст" not in page.text


def test_screener_page_renders_saved_candidate(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_saved_run(settings.screener_db_path)
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/screener")
        script = client.get("/static/screener.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert "candidate-search" in script.text
    assert "TEST" in page.text
    assert "score / 100" in page.text
    assert "Только чтение" in page.text
    assert "Survivorship bias" in page.text
    assert "Когда идея ломается" in page.text
    assert "покрытие 4" in page.text
    assert 'fill="none"' in page.text
    assert "Таблица кандидатов" in page.text
    assert 'id="candidate-search"' in page.text
    assert 'id="candidate-sort"' in page.text
    assert 'data-candidate' in page.text
    assert 'data-score="85"' in page.text
    assert "Объём, лоты" in page.text
    assert "уровни:" in page.text
    assert "Торговый статус: Нормальная торговля" in page.text
    assert "Спред (не проверен)" in page.text
    assert '<line x1="0"' in page.text
    assert 'src="/static/screener.js"' in page.text
    assert "SMA20" in page.text
    assert "источники: свечи" in page.text
    assert "Похожие сигналы вне настройки" in page.text
    assert "Кэш скринера пуст" not in page.text


def test_screener_refresh_without_cache_reports_error(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        refreshed = client.post("/screener/refresh")
        assert refreshed.status_code == 303
        assert "level=error" in refreshed.headers["location"]
        page = client.get("/screener")

    assert page.status_code == 200
    assert "Кэш скринера пуст" in page.text
