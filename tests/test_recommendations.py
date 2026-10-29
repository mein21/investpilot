from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.enums import (
    AssetClass,
    RecommendationAction,
    RecommendationStatus,
    TradingMode,
)
from app.integrations.tinvest.mock import MockTInvestClient
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.position import CashBalance, Position
from app.models.recommendation import Recommendation
from app.models.snapshot import PortfolioSnapshot
from app.screener.store import ScreenerStore
from app.services.goals import AllocationInput, GoalInput, GoalService
from app.services.growth import GrowthService
from app.services.recommendations import RecommendationService
from app.services.sync import SyncService
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


def prepare(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    *,
    settings: Settings | None = None,
    reserve: Decimal = Decimal("0"),
    allocations: list[AllocationInput] | None = None,
    price_age_seconds: int | None = None,
    prices: dict[str, Decimal] | None = None,
    target_amount: Decimal = Decimal("2000000"),
    target_days: int = 365,
    stop_loss_enabled: bool = False,
    stop_loss_percent: Decimal | None = None,
    take_profit_enabled: bool = False,
    take_profit_percent: Decimal | None = None,
    max_drawdown_percent: Decimal = Decimal("20"),
) -> tuple[int, int, RecommendationService]:
    settings = settings or Settings(auth_enabled=False, tinvest_client="mock")
    client = MockTInvestClient(clock=clock)
    if price_age_seconds is not None:
        client.set_price_age(price_age_seconds)
    SyncService(session_factory, client, settings, clock=clock).run()
    with session_factory() as session:
        account = session.scalar(select(Account))
        assert account is not None
        for ticker, price in (prices or {}).items():
            instrument = session.scalar(select(Instrument).where(Instrument.ticker == ticker))
            assert instrument is not None
            instrument.last_price = price
            instrument.last_price_at = clock.now()
            for position in session.scalars(
                select(Position).where(Position.instrument_id == instrument.id)
            ):
                position.current_price = price
                position.current_price_at = clock.now()
        goal = GoalService().create_goal(
            session,
            GoalInput(
                name="Test goal",
                target_amount=target_amount,
                currency="RUB",
                target_date=clock.now().date() + timedelta(days=target_days),
                min_cash_reserve=reserve,
                stop_loss_enabled=stop_loss_enabled,
                stop_loss_percent=stop_loss_percent,
                take_profit_enabled=take_profit_enabled,
                take_profit_percent=take_profit_percent,
                max_drawdown_percent=max_drawdown_percent,
            ),
            allocations
            or [
                AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("50"), Decimal("60")),
                AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("20"), Decimal("25")),
                AllocationInput(AssetClass.ETF, Decimal("0"), Decimal("5"), Decimal("10")),
                AllocationInput(AssetClass.CURRENCY, Decimal("5"), Decimal("10"), Decimal("15")),
                AllocationInput(AssetClass.CASH, Decimal("10"), Decimal("15"), Decimal("20")),
            ],
            account_id=account.id,
            today=clock.now().date(),
        )
        session.commit()
        return account.id, goal.id, RecommendationService(settings, clock=clock)


def test_generate_rebalance_recommendations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(session_factory, clock)
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        by_ticker = {item.ticker: item for item in run.recommendations}
        assert by_ticker["SBER"].action is RecommendationAction.SELL
        assert by_ticker["SBER"].lots == 10
        assert by_ticker["SBER"].estimated_amount == Decimal("30000.00")
        assert by_ticker["GAZP"].action is RecommendationAction.SELL
        assert by_ticker["GAZP"].lots == 20
        assert by_ticker["GAZP"].estimated_amount == Decimal("30000.00")
        assert by_ticker["SU26248RMFS3"].action is RecommendationAction.BUY
        assert by_ticker["SU26248RMFS3"].lots == 25
        assert by_ticker["SU26248RMFS3"].estimated_amount == Decimal("24000.00")
        assert by_ticker["TMOS"].action is RecommendationAction.BUY
        assert by_ticker["TMOS"].lots == 576
        assert by_ticker["TMOS"].estimated_amount == Decimal("46080.00")
        assert by_ticker["USD000UTSTOM"].action is RecommendationAction.HOLD
        assert by_ticker["PORTFOLIO"].action is RecommendationAction.REBALANCE
        assert by_ticker["PORTFOLIO"].lots == 0
        assert by_ticker["PORTFOLIO"].instrument_id is None
        assert "Ребалансировка" in by_ticker["PORTFOLIO"].reason
        assert {rule["code"] for rule in by_ticker["PORTFOLIO"].triggered_rules} == {
            "REBALANCE_LEG"
        }

        assert len(run.recommendations) == 6
        assert any("не хватает на один торговый лот" in item for item in run.warnings)
        assert all(item.status is RecommendationStatus.NEW for item in run.recommendations)
        assert all(item.goal_id == goal_id for item in run.recommendations)
        assert all(item.mode.value == "RECOMMEND" for item in run.recommendations)
        assert len({item.fingerprint for item in run.recommendations}) == 6
        assert len({item.input_hash for item in run.recommendations}) == 1
        assert all(len(item.fingerprint) == 64 for item in run.recommendations)
        assert all(item.expires_at > clock.now() for item in run.recommendations)
        assert by_ticker["GAZP"].score == Decimal("81.1982")
        assert by_ticker["SU26248RMFS3"].score is not None
        assert len(by_ticker["SU26248RMFS3"].score_breakdown) == 8
        assert by_ticker["GAZP"].blocking_rules == []
        assert by_ticker["SU26248RMFS3"].triggered_rules
        assert "снижение риска" in by_ticker["GAZP"].score_breakdown[1]["reason"]
        assert "концентрация эмитента" in by_ticker["GAZP"].reason
        assert (
            "класс «Акции» — 55,30% при цели 50,00% (перевес 5,30 п.п.)" in by_ticker["SBER"].reason
        )
        assert "Продажа на 30\u00a0000,00 RUB" in by_ticker["SBER"].reason
        assert (
            "класс «Фонды» — 0,74% при цели 5,00% (недобор 4,26 п.п.)" in by_ticker["TMOS"].reason
        )
        assert "Покупка на 46\u00a0080,00 RUB" in by_ticker["TMOS"].reason
        assert "класс «Валюта» занимает 8,29% при цели 10,00%" in by_ticker["USD000UTSTOM"].reason
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 6


def test_buy_score_uses_cached_growth_metrics(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(auth_enabled=False, tinvest_client="mock")
    account_id, goal_id, service = prepare(session_factory, clock, settings=settings)
    GrowthService(
        session_factory,
        MockTInvestClient(clock=clock),
        settings,
        clock=clock,
    ).scan(now=clock.now())
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        by_ticker = {item.ticker: item for item in run.recommendations}
        tmos = by_ticker["TMOS"]
        assert tmos.action is RecommendationAction.BUY
        breakdown = {item["component"]: item for item in tmos.score_breakdown}
        assert set(breakdown) == {
            "allocation_need",
            "momentum",
            "trend_quality",
            "risk_fit",
            "diversification",
            "stability",
            "income",
            "liquidity",
        }
        assert "риск-коррекция" in str(breakdown["momentum"]["reason"])
        assert "нет истории котировок" not in str(breakdown["momentum"]["reason"])


def _screener_payload(clock: FrozenClock, tickers: list[str]) -> dict[str, object]:
    scores = {"GAZP": "95", "SBER": "70"}
    return {
        "kind": "screen",
        "as_of": clock.now().date().isoformat(),
        "generated_at": clock.now().isoformat(),
        "mode": "technical",
        "candidates": [
            {
                "uid": f"mock-uid-{ticker.lower()}",
                "ticker": ticker,
                "score": scores[ticker],
                "rank": index,
                "quality": {"label": "чистые", "critical": False},
                "blocks": {"relative_strength": "90", "trend_quality": "90"},
                "penalties": [],
                "warnings": [],
            }
            for index, ticker in enumerate(tickers, start=1)
        ],
    }


def test_stock_buy_uses_fresh_screener_rating(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    tmp_path: Path,
) -> None:
    path = tmp_path / "screener.db"
    ScreenerStore(path).save_run(
        "screen",
        _screener_payload(clock, ["GAZP", "SBER"]),
        as_of=clock.now().date(),
    )
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        screener_db_path=path,
        recommendations_screener_enabled=True,
        max_issuer_percent=Decimal("100"),
        max_order_amount=Decimal("50000"),
        max_daily_order_amount=Decimal("100000"),
        default_min_cash_reserve=Decimal("0"),
    )
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=settings,
        allocations=[
            AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("70"), Decimal("80")),
            AllocationInput(AssetClass.CASH, Decimal("0"), Decimal("30"), Decimal("30")),
        ],
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        buy = next(
            item for item in run.recommendations if item.action is RecommendationAction.BUY
        )
        assert buy.ticker == "GAZP"
        breakdown = {item["component"]: item for item in buy.score_breakdown}
        assert set(breakdown) == {
            "allocation_need",
            "screener_rating",
            "risk_fit",
            "diversification",
            "income",
            "liquidity",
        }
        assert Decimal(str(breakdown["screener_rating"]["value"])) == Decimal("95")
        assert "скринер 95.0/100" in str(breakdown["screener_rating"]["reason"])
        assert any(rule["code"] == "SCREENER_RATING" for rule in buy.triggered_rules)
        assert "Скринер: 95.0/100" in buy.reason
        assert not any("вне свежего скринера" in warning for warning in run.warnings)


def test_stock_missing_from_screener_is_not_bought(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
    tmp_path: Path,
) -> None:
    path = tmp_path / "screener.db"
    ScreenerStore(path).save_run(
        "screen",
        _screener_payload(clock, ["SBER"]),
        as_of=clock.now().date(),
    )
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        screener_db_path=path,
        recommendations_screener_enabled=True,
        max_issuer_percent=Decimal("100"),
        max_order_amount=Decimal("50000"),
        max_daily_order_amount=Decimal("100000"),
        default_min_cash_reserve=Decimal("0"),
    )
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=settings,
        allocations=[
            AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("70"), Decimal("80")),
            AllocationInput(AssetClass.CASH, Decimal("0"), Decimal("30"), Decimal("30")),
        ],
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        buy = next(
            item for item in run.recommendations if item.action is RecommendationAction.BUY
        )
        assert buy.ticker == "SBER"
        assert any("1 бумаг вне свежего скринера" in warning for warning in run.warnings)


def test_stop_loss_forces_full_exit(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=Settings(
            auth_enabled=False,
            tinvest_client="mock",
            max_order_amount=Decimal("200000"),
            max_daily_order_amount=Decimal("200000"),
        ),
        prices={"SBER": Decimal("200")},
        stop_loss_enabled=True,
        stop_loss_percent=Decimal("5"),
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        sell = next(
            item
            for item in run.recommendations
            if item.ticker == "SBER" and item.action is RecommendationAction.SELL
        )
        assert "стоп-лосс" in sell.reason
        assert any(rule["code"] == "MANDATORY_EXIT" for rule in sell.triggered_rules)
        assert sell.lots == 100
        assert sell.expected_class_weight_after < Decimal("40")
        assert any("принудительный выход опускает класс" in warning for warning in sell.risks)


def test_sell_trigger_for_unallocated_class_does_not_crash(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        prices={"SBER": Decimal("200")},
        stop_loss_enabled=True,
        stop_loss_percent=Decimal("5"),
    )
    with session_factory() as session:
        sber = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
        assert sber is not None
        sber.asset_class = AssetClass.OTHER
        session.commit()
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

    sell = next(
        item
        for item in run.recommendations
        if item.ticker == "SBER" and item.action is RecommendationAction.SELL
    )
    assert any(rule["code"] == "MANDATORY_EXIT" for rule in sell.triggered_rules)


def test_take_profit_forces_full_exit(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        prices={"SBER": Decimal("400")},
        take_profit_enabled=True,
        take_profit_percent=Decimal("20"),
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        sell = next(
            item
            for item in run.recommendations
            if item.ticker == "SBER" and item.action is RecommendationAction.SELL
        )
        assert "тейк-профит" in sell.reason
        assert any(rule["code"] == "MANDATORY_EXIT" for rule in sell.triggered_rules)


def test_goal_funding_sells_near_target_date(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        target_amount=Decimal("500000"),
        target_days=20,
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        funding = next(item for item in run.recommendations if "финансирования цели" in item.reason)
        assert funding.action is RecommendationAction.SELL
        assert funding.triggered_rules
        assert any(rule["code"] == "MANDATORY_EXIT" for rule in funding.triggered_rules)
        assert "350\u00a0000,00 RUB" in funding.reason
        assert all(item.action is not RecommendationAction.BUY for item in run.recommendations)


def test_goal_funding_skips_unreached_goal(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        target_amount=Decimal("2000000"),
        target_days=20,
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        assert not any("финансирования цели" in item.reason for item in run.recommendations)
        assert any("ниже целевой суммы" in warning for warning in run.warnings)


def test_max_drawdown_blocks_buys(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        max_drawdown_percent=Decimal("5"),
    )
    with session_factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        session.add(
            PortfolioSnapshot(
                account_id=account_id,
                captured_at=clock.now(),
                base_currency="RUB",
                total_value=Decimal("2000000"),
                invested_value=Decimal("2000000"),
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
            )
        )
        session.flush()
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        actions = {item.action for item in run.recommendations}
        assert RecommendationAction.BUY not in actions
        assert any("MAX_DRAWDOWN" in warning for warning in run.warnings)


def test_stale_quotes_produce_explanatory_holds(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        price_age_seconds=600,
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        assert run.recommendations
        assert all(item.action is RecommendationAction.HOLD for item in run.recommendations)
        assert any(
            "котировка устарела — синхронизируйте данные" in item.reason
            for item in run.recommendations
        )
        assert any("проверьте свежесть котировок" in item for item in run.warnings)


def test_regeneration_expires_previous_recommendations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(session_factory, clock)
    with session_factory() as session:
        first = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()
        second = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        first_ids = {item.id for item in first.recommendations}
        for recommendation in session.scalars(
            select(Recommendation).where(Recommendation.id.in_(first_ids))
        ):
            assert recommendation.status is RecommendationStatus.EXPIRED
        assert all(item.status is RecommendationStatus.NEW for item in second.recommendations)
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 12


def test_observe_mode_is_read_only(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        trading_mode=TradingMode.OBSERVE,
    )
    account_id, goal_id, service = prepare(session_factory, clock, settings=settings)
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        assert run.recommendations == ()
        assert run.goal is None
        assert any("OBSERVE" in warning for warning in run.warnings)
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 0


def test_no_active_goal_returns_no_recommendations(
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
        run = RecommendationService(settings, clock=clock).generate(
            session,
            account_id=account.id,
        )
        assert run.recommendations == ()
        assert any("нет активной цели" in warning for warning in run.warnings)


def test_cash_reserve_blocks_new_buys(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        reserve=Decimal("200000"),
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        actions = {item.action for item in run.recommendations}
        assert RecommendationAction.SELL in actions
        assert RecommendationAction.BUY not in actions
        assert any(
            "CASH_RESERVE" in warning or "не хватает свободных денег" in warning
            for warning in run.warnings
        )


def test_issuer_limit_blocks_buys_but_not_sells(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        max_issuer_percent=Decimal("0"),
    )
    account_id, goal_id, service = prepare(session_factory, clock, settings=settings)
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        actions = {item.action for item in run.recommendations}
        assert RecommendationAction.SELL in actions
        assert RecommendationAction.BUY not in actions
        assert any("заблокированы риск-правилами" in warning for warning in run.warnings)


def test_under_target_but_within_class_range_does_not_sell(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        max_issuer_percent=Decimal("100"),
    )
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=settings,
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        assert RecommendationAction.SELL not in {item.action for item in run.recommendations}


def test_class_above_max_percent_triggers_sell(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        max_issuer_percent=Decimal("100"),
    )
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=settings,
        allocations=[
            AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("50"), Decimal("50")),
            AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("20"), Decimal("25")),
            AllocationInput(AssetClass.ETF, Decimal("0"), Decimal("5"), Decimal("10")),
            AllocationInput(AssetClass.CURRENCY, Decimal("5"), Decimal("10"), Decimal("15")),
            AllocationInput(AssetClass.CASH, Decimal("10"), Decimal("15"), Decimal("20")),
        ],
    )
    with session_factory() as session:
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        sells = [item for item in run.recommendations if item.action is RecommendationAction.SELL]
        assert len(sells) == 1
        assert "выше max" in sells[0].reason
        assert sells[0].estimated_amount <= Decimal("50000")


def test_buy_candidate_outside_current_positions(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    allocations = [
        AllocationInput(AssetClass.STOCK, Decimal("0"), Decimal("90"), Decimal("100")),
        AllocationInput(AssetClass.BOND, Decimal("0"), Decimal("0"), Decimal("100")),
        AllocationInput(AssetClass.ETF, Decimal("0"), Decimal("0"), Decimal("100")),
        AllocationInput(AssetClass.CURRENCY, Decimal("0"), Decimal("0"), Decimal("100")),
        AllocationInput(AssetClass.CASH, Decimal("0"), Decimal("10"), Decimal("100")),
    ]
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        allocations=allocations,
    )
    with session_factory() as session:
        for instrument in session.scalars(
            select(Instrument).where(Instrument.asset_class == AssetClass.STOCK)
        ):
            instrument.buy_available = False
        session.add(
            Instrument(
                instrument_uid="test-uid-lkoh",
                figi="TESTFIGILKOH",
                ticker="LKOH",
                class_code="TQBR",
                name="Лукойл",
                instrument_type="share",
                asset_class=AssetClass.STOCK,
                currency="RUB",
                lot=1,
                exchange="MOEX",
                issuer_key="LKOH",
                buy_available=True,
                api_trade_available=True,
                is_buy_candidate=True,
                last_price=Decimal("6000"),
                last_price_at=clock.now(),
                last_price_source="test",
            )
        )
        session.commit()

        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        session.commit()

        buy = next(
            item
            for item in run.recommendations
            if item.instrument is not None and item.instrument.ticker == "LKOH"
        )
        assert buy.action is RecommendationAction.BUY
        assert buy.lots > 0


def test_blocked_cash_is_not_used_for_buy_recommendations(
    session_factory: sessionmaker[Session],
    clock: FrozenClock,
) -> None:
    settings = Settings(
        auth_enabled=False,
        tinvest_client="mock",
        default_min_cash_reserve=Decimal("0"),
        max_issuer_percent=Decimal("100"),
    )
    account_id, goal_id, service = prepare(
        session_factory,
        clock,
        settings=settings,
        reserve=Decimal("0"),
        allocations=[
            AllocationInput(AssetClass.ETF, Decimal("100"), Decimal("100"), Decimal("100")),
        ],
    )
    with session_factory() as session:
        cash = session.scalar(
            select(CashBalance).where(
                CashBalance.account_id == account_id,
                CashBalance.currency == "RUB",
            )
        )
        assert cash is not None
        cash.blocked = cash.amount - Decimal("100")
        session.commit()
        run = service.generate(session, account_id=account_id, goal_id=goal_id)
        assert RecommendationAction.BUY not in {item.action for item in run.recommendations}
        assert any("не хватает свободных денег" in warning for warning in run.warnings)
