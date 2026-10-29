from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.enums import TradingMode
from app.factory import create_app
from app.models.account import Account
from app.models.audit import AuditLog, RiskEvent
from app.models.instrument import Instrument
from app.models.notification import Notification
from app.models.order import Order
from app.models.recommendation import Recommendation
from app.schemas import HealthResponse
from fastapi.testclient import TestClient
from sqlalchemy import select


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "auth_enabled": False,
        "tinvest_client": "mock",
        "database_url": "sqlite://",
        "seed_demo_data": True,
        "backup_dir": tmp_path,
        "trading_mode": "PAPER",
        "scheduler_enabled": False,
        "screener_db_path": tmp_path / "screener.db",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_health_endpoint_matches_validated_schema(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = HealthResponse.model_validate(response.json())
    assert payload.status == "ok"
    assert payload.client == "mock"
    assert payload.sandbox is True
    assert payload.trading_mode is TradingMode.PAPER


def test_dashboard_and_pages(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        for path in (
            "/",
            "/positions",
            "/recommendations",
            "/goals",
            "/orders",
            "/risk",
            "/analysis",
            "/screener",
            "/notifications",
            "/audit",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
        dashboard = client.get("/")
        assert "Стоимость портфеля" in dashboard.text
        positions = client.get("/positions")
        assert "Сбербанк" in positions.text
        assert "95,50%" in positions.text
        assert "за лот" in positions.text
        screener = client.get("/screener")
        assert "Кэш скринера пуст" in screener.text


def test_scenario_page(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post("/sync")
        page = client.get("/analysis")
        assert page.status_code == 200
        assert "Сценарный анализ" in page.text
        response = client.post(
            "/analysis",
            data={"stock": "-10", "bond": "5", "etf": "0", "currency": "0"},
        )
        assert response.status_code == 200
        assert "Результат сценария" in response.text
        assert "Стресс-стоимость" in response.text


def test_observe_mode_hides_generation(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, trading_mode="OBSERVE"))
    with TestClient(app) as client:
        response = client.get("/recommendations")
        assert response.status_code == 200
        assert "OBSERVE: только чтение" in response.text
        assert "Пересчитать" not in response.text
        client.post("/recommendations/generate")
        factory = app.state.session_factory
        with factory() as session:
            assert session.scalar(select(Recommendation)) is None


def test_recommendation_approve_and_send(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        client.post("/recommendations/generate")
        factory = app.state.session_factory
        with factory() as session:
            recommendation = session.scalar(
                select(Recommendation).where(
                    Recommendation.status == "NEW",
                    Recommendation.action == "SELL",
                )
            )
            assert recommendation is not None
            recommendation_id = recommendation.id
        approve = client.post(f"/recommendations/{recommendation_id}/approve")
        assert approve.status_code == 303
        with factory() as session:
            order = session.scalar(select(Order))
            assert order is not None
            assert order.status.value == "APPROVED"
            order_id = order.id
        send = client.post(f"/orders/{order_id}/send")
        assert send.status_code == 303
        with factory() as session:
            order = session.get(Order, order_id)
            assert order is not None
            assert order.status.value == "EXECUTED"
            event_types = set(session.scalars(select(AuditLog.event_type)))
        assert {"order.approved", "order.sent"} <= event_types
        with factory() as session:
            notification = session.scalar(select(Notification))
            assert notification is not None
            assert notification.kind == "order"
            assert notification.is_read is False
            notification_id = notification.id
        page = client.get("/notifications")
        assert page.status_code == 200
        assert "Заявка: EXECUTED" in page.text
        read = client.post(f"/notifications/{notification_id}/read")
        assert read.status_code == 303
        with factory() as session:
            notification = session.get(Notification, notification_id)
            assert notification is not None
            assert notification.is_read is True


def test_csrf_is_required(tmp_path: Path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            auth_enabled=True,
            seed_demo_data=False,
            admin_username="admin",
            admin_password="correct horse battery",
            session_secret="s" * 40,
        )
    )
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        login = client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "next": "/"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        response = client.post("/sync", data={"csrf_token": "wrong"})
        assert response.status_code == 403


def test_account_switch_isolates_pages(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        factory = app.state.session_factory
        with factory() as session:
            first = session.scalar(select(Account))
            assert first is not None
            second = Account(
                tinvest_account_id="second-test-account",
                name="Второй счёт",
                account_type="ACCOUNT_TYPE_TINKOFF",
                status="ACCOUNT_STATUS_OPEN",
                access_level="ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                is_sandbox=True,
            )
            session.add(second)
            session.commit()
            second_id = second.id
        page = client.get("/positions")
        assert page.status_code == 200
        assert "Второй счёт" in page.text
        selected = client.post(
            "/accounts/select",
            data={"account_id": str(second_id), "next": "/positions"},
        )
        assert selected.status_code == 303
        switched = client.get("/positions")
        assert "Второй счёт" in switched.text
        assert "Сбербанк" not in switched.text


def test_login_rate_limit_blocks_repeated_failures(tmp_path: Path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            auth_enabled=True,
            seed_demo_data=False,
            admin_username="admin",
            admin_password="correct horse battery",
            session_secret="s" * 40,
            login_max_attempts=2,
        )
    )
    with TestClient(app) as client:
        for _ in range(2):
            failed = client.post(
                "/login",
                data={"username": "admin", "password": "wrong", "next": "/"},
            )
            assert failed.status_code == 200
        blocked = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "next": "/"},
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"]


def test_emergency_stop_blocks_approval(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        client.post("/recommendations/generate")
        factory = app.state.session_factory
        with factory() as session:
            recommendation = session.scalar(
                select(Recommendation).where(Recommendation.action == "SELL")
            )
            assert recommendation is not None
            recommendation_id = recommendation.id
        stop = client.post("/risk/emergency-stop", data={"enabled": "1"})
        assert stop.status_code == 303
        blocked = client.post(f"/recommendations/{recommendation_id}/approve")
        assert blocked.status_code == 303
        assert "error" in blocked.headers["location"]
        with factory() as session:
            assert session.scalar(select(Order)) is None
            assert session.scalar(select(RiskEvent).where(RiskEvent.code == "ORDER_BLOCKED"))
            notification = session.scalar(select(Notification).where(Notification.kind == "risk"))
            assert notification is not None
            assert notification.severity == "CRITICAL"


def test_instrument_exclusion_toggle(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        client.post("/sync")
        factory = app.state.session_factory
        with factory() as session:
            instrument = session.scalar(select(Instrument).where(Instrument.ticker == "SBER"))
            assert instrument is not None
            instrument_id = instrument.id
        page = client.get("/positions")
        assert "Исключить" in page.text
        assert (
            client.post(
                f"/instruments/{instrument_id}/exclude",
                data={"excluded": "1"},
            ).status_code
            == 303
        )
        with factory() as session:
            instrument = session.get(Instrument, instrument_id)
            assert instrument is not None
            assert instrument.is_excluded is True
            assert instrument.is_buy_candidate is False
        assert "Исключён" in client.get("/positions").text
        assert (
            client.post(
                f"/instruments/{instrument_id}/exclude",
                data={"excluded": "0"},
            ).status_code
            == 303
        )
        with factory() as session:
            instrument = session.get(Instrument, instrument_id)
            assert instrument is not None
            assert instrument.is_excluded is False


def test_goal_create_and_update(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=True) as client:
        client.post("/sync")
        data = {
            "name": "Новая цель",
            "target_amount": "1500000",
            "target_date": "2027-12-31",
            "regular_contribution": "25000",
            "contribution_frequency": "MONTHLY",
            "min_cash_reserve": "50000",
            "max_drawdown_percent": "20",
            "risk_profile": "BALANCED",
            "description": "Проверка формы",
        }
        for asset_class, target in (
            ("stock", "50"),
            ("bond", "20"),
            ("etf", "5"),
            ("currency", "10"),
            ("cash", "15"),
        ):
            data[f"{asset_class}_min"] = str(max(0, int(target) - 10))
            data[f"{asset_class}_target"] = target
            data[f"{asset_class}_max"] = str(min(100, int(target) + 10))
        created = client.post("/goals", data=data)
        assert created.status_code == 200
        assert "Новая цель" in created.text
        assert "Цель создана" in created.text


def test_add_instrument_to_universe(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/instruments/add",
            data={"ticker": "LKOH", "class_code": "TQBR"},
        )
        assert response.status_code == 303
        factory = app.state.session_factory
        with factory() as session:
            instrument = session.scalar(select(Instrument).where(Instrument.ticker == "LKOH"))
            assert instrument is not None
            assert instrument.is_buy_candidate is True
            assert instrument.last_price == 6000


def test_backtest_page_uses_candle_history(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/backtest?days=90")
        assert response.status_code == 200
        assert "Backtest buy-and-hold" in response.text
        assert "Доходность" in response.text
        assert "/backtest?days=90" not in response.text


def test_risk_page_warns_when_real_account_is_read_only(tmp_path: Path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            trading_mode="CONFIRM",
            tinvest_sandbox=False,
            real_trading_enabled=True,
        )
    )
    with TestClient(app) as client:
        factory = app.state.session_factory
        with factory() as session:
            account = session.scalar(select(Account))
            assert account is not None
            account.access_level = "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
            session.commit()
        page = client.get("/risk")
    assert page.status_code == 200
    assert "Реальные заявки будут заблокированы" in page.text
    assert "READ_ONLY" in page.text


def test_recommendations_show_affordable_bond_price(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post("/sync")
        client.post("/recommendations/generate")
        response = client.get("/recommendations")
    assert response.status_code == 200
    assert "Свободно для новых покупок" in response.text
    assert "Цена за лот" in response.text
    assert "от номинала" in response.text
    assert '<div class="label">Котировка</div>' not in response.text


def test_recommendations_page_has_market_refresh_button(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        page = client.get("/recommendations")
    assert page.status_code == 200
    assert 'action="/recommendations/refresh"' in page.text
    assert "Обновить с рынка" in page.text
    assert "Пересчитать по кэшу" in page.text


def test_recommendations_refresh_refreshes_screener_and_generates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[bool] = []

    def fake_refresh(settings: Settings, *, live: bool) -> str | None:
        calls.append(live)
        return None

    monkeypatch.setattr("app.api.routes._refresh_screener", fake_refresh)
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/recommendations/refresh")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/recommendations?flash=")
        with app.state.session_factory() as session:
            assert session.scalar(select(Recommendation)) is not None
    assert calls == [True]


def test_recommendations_refresh_continues_when_screener_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.api.routes._refresh_screener",
        lambda settings, *, live: "Скринер не обновлён: boom",
    )
    app = create_app(make_settings(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/recommendations/refresh")
        assert response.status_code == 303
        assert "level=warning" in response.headers["location"]
        with app.state.session_factory() as session:
            assert session.scalar(select(Recommendation)) is not None
