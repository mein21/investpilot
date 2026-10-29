from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.screener.domain import Bar
from app.screener.store import ScreenerStore


def _bar(day: date) -> Bar:
    return Bar(day, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 1000)


def test_bar_date_bounds_ignore_incomplete(tmp_path: Path) -> None:
    store = ScreenerStore(tmp_path / "screener.db")
    store.upsert_bars(
        "uid-test",
        [
            _bar(date(2025, 1, 1)),
            _bar(date(2025, 1, 2)),
            replace(_bar(date(2025, 1, 3)), is_complete=False),
        ],
    )

    assert store.first_bar_date("uid-test") == date(2025, 1, 1)
    assert store.last_bar_date("uid-test") == date(2025, 1, 2)
    store.close()
