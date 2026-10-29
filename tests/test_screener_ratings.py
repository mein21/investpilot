from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.screener.store import ScreenerStore
from app.services.screener_ratings import load_screener_ratings


def _payload(clock: FrozenClock, *, generated_at: str | None = None) -> dict[str, object]:
    return {
        "kind": "screen",
        "as_of": clock.now().date().isoformat(),
        "generated_at": generated_at or clock.now().isoformat(),
        "mode": "technical",
        "candidates": [
            {
                "uid": "uid-gazp",
                "ticker": "GAZP",
                "score": "90.5",
                "rank": 1,
                "quality": {"label": "с замечаниями", "critical": False},
                "blocks": {
                    "relative_strength": "88",
                    "trend_quality": "91",
                    "volume_confirmation": "75",
                    "consolidation_breakout": None,
                    "risk_moderation": "80",
                },
                "penalties": [{"code": "VOL", "points": "-5", "reason": "высокая волатильность"}],
                "warnings": ["спред не проверен"],
            },
            {
                "uid": "uid-sber",
                "ticker": "SBER",
                "score": "70",
                "rank": 2,
                "quality": {"label": "чистые", "critical": False},
                "blocks": {},
                "penalties": [],
                "warnings": [],
            },
        ],
    }


def _settings(path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        auth_enabled=False,
        tinvest_client="mock",
        screener_db_path=path,
        recommendations_screener_enabled=enabled,
    )


def test_loads_latest_screener_run_by_uid(tmp_path: Path, clock: FrozenClock) -> None:
    path = tmp_path / "screener.db"
    ScreenerStore(path).save_run("screen", _payload(clock), as_of=clock.now().date())

    result = load_screener_ratings(_settings(path), now=clock.now())

    assert result.active is True
    assert result.as_of == clock.now().date()
    assert result.mode == "technical"
    assert len(result.ratings) == 2
    rating = result.ratings[0]
    assert rating.uid == "uid-gazp"
    assert rating.ticker == "GAZP"
    assert rating.score == Decimal("90.5")
    assert rating.rank == 1
    assert rating.blocks["relative_strength"] == Decimal("88")
    assert rating.penalties == ("высокая волатильность",)
    assert rating.warnings == ("спред не проверен",)
    assert "скринер 90.5/100" in rating.reason
    assert "относительная сила 88" in rating.reason
    assert result.get(SimpleNamespace(instrument_uid="missing", ticker="gazp")) is rating


def test_missing_screener_db_falls_back(tmp_path: Path, clock: FrozenClock) -> None:
    result = load_screener_ratings(
        _settings(tmp_path / "missing.db"),
        now=clock.now(),
    )

    assert result.active is False
    assert any("база не найдена" in warning for warning in result.warnings)


def test_stale_screener_run_falls_back(tmp_path: Path, clock: FrozenClock) -> None:
    path = tmp_path / "screener.db"
    stale = (clock.now() - timedelta(hours=73)).isoformat()
    ScreenerStore(path).save_run(
        "screen",
        _payload(clock, generated_at=stale),
        as_of=clock.now().date(),
    )

    result = load_screener_ratings(_settings(path), now=clock.now())

    assert result.active is False
    assert any("старше 72 ч" in warning for warning in result.warnings)


def test_disabled_screener_does_not_read_database(tmp_path: Path, clock: FrozenClock) -> None:
    result = load_screener_ratings(
        _settings(tmp_path / "missing.db", enabled=False),
        now=clock.now(),
    )

    assert result.active is False
    assert result.warnings == ()
