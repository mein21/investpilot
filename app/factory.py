"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import WebSession
from app.api.routes import router
from app.bootstrap import build_tinvest_client, ensure_admin, ensure_demo_goal
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.enums import Severity
from app.core.logging import configure_logging
from app.core.security import SlidingWindowRateLimiter, load_session
from app.db.session import build_engine, build_session_factory, init_db, session_scope
from app.integrations.tinvest.client import TInvestClient
from app.models.account import Account
from app.models.user import User
from app.services.audit import record_audit
from app.services.scheduler import SchedulerService
from app.services.snapshots import capture_all_snapshots
from app.services.sync import SyncService
from app.web import presentation

LOGGER = logging.getLogger(__name__)
TEMPLATE_DIR = "app/web/templates"
STATIC_DIR = "app/web/static"
PUBLIC_PATHS = {"/login", "/health", "/favicon.ico"}


def _configure_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    env = templates.env
    env.filters.update(
        {
            "money": presentation.money,
            "quantity": presentation.quantity,
            "percent": presentation.percent,
            "ratio_percent": presentation.ratio_percent,
            "number": presentation.number,
            "date_ru": presentation.date_ru,
            "dt_ru": presentation.dt_ru,
            "short_dt": presentation.short_dt,
            "asset_label": presentation.asset_label,
            "action_label": presentation.action_label,
            "action_tone": presentation.action_tone,
            "recommendation_status_label": presentation.recommendation_status_label,
            "order_status_label": presentation.order_status_label,
            "status_tone": presentation.status_tone,
            "side_label": presentation.side_label,
            "risk_label": presentation.risk_label,
            "severity_label": presentation.severity_label,
            "trading_mode_label": presentation.trading_mode_label,
            "regime_label": presentation.regime_label,
            "screener_mode_label": presentation.screener_mode_label,
            "screener_fundamental_category_label": (
                presentation.screener_fundamental_category_label
            ),
            "trading_status_label": presentation.trading_status_label,
            "sparkline_points": presentation.sparkline_points,
            "sparkline_levels": presentation.sparkline_levels,
            "sparkline_volume_points": presentation.sparkline_volume_points,
            "score_band_for": presentation.score_band_for,
        }
    )
    env.globals.update(
        {
            "asset_labels": presentation.ASSET_LABELS,
            "risk_profiles": presentation.RISK_LABELS,
            "currency_symbols": presentation.CURRENCY_SYMBOLS,
            "screener_block_labels": presentation.SCREENER_BLOCK_LABELS,
        }
    )
    return templates


def _load_web_session(request: Request, settings: Settings) -> WebSession | None:
    if not settings.auth_enabled:
        return WebSession(user_id=0, username="local", csrf="local", expires_at=0)
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    payload = load_session(raw, settings.session_secret_value)
    if payload is None:
        return None
    user_id = payload.get("uid")
    username = payload.get("username")
    csrf = payload.get("csrf")
    expires_at = payload.get("exp")
    if (
        not isinstance(user_id, int)
        or not isinstance(username, str)
        or not isinstance(csrf, str)
        or not isinstance(expires_at, int)
    ):
        return None
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.username != username:
            return None
    return WebSession(
        user_id=user_id,
        username=username,
        csrf=csrf,
        expires_at=expires_at,
    )


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    engine: Engine | None = None,
    client: TInvestClient | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    configure_logging(
        "DEBUG" if settings.debug else "INFO", extra_secrets=settings.masking_secrets()
    )

    owns_engine = engine is None
    if engine is None:
        engine = build_engine(
            settings.database_url,
            echo=settings.debug,
            memory=settings.database_url == "sqlite://",
        )
    if session_factory is None:
        session_factory = build_session_factory(engine)
        init_db(engine)
    if client is None:
        client = build_tinvest_client(settings)
    app_clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with session_scope(session_factory) as session:
            ensure_admin(session, settings)
        _startup_data(app, settings, session_factory, client, app_clock)
        scheduler = SchedulerService(settings, session_factory, client, clock=app_clock)
        tasks = scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop(tasks)
            client.close()
            if owns_engine:
                engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.client = client
    app.state.clock = app_clock
    app.state.login_limiter = SlidingWindowRateLimiter(
        settings.login_max_attempts,
        settings.login_window_seconds,
    )
    app.state.templates = _configure_templates()

    static_app = StaticFiles(directory=STATIC_DIR)
    app.mount("/static", static_app, name="static")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path == "/static" or path.startswith("/static/") or path in PUBLIC_PATHS:
            return await call_next(request)
        web_session = _load_web_session(request, settings)
        if web_session is None:
            target = f"/login?next={quote(path)}"
            if request.headers.get("HX-Request") == "true":
                return Response(status_code=204, headers={"HX-Redirect": target})
            return RedirectResponse(target, status_code=303)
        request.state.web_session = web_session
        response = await call_next(request)
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> Response:
        if exc.status_code in {401, 403, 404, 405} and request.url.path != "/health":
            templates: Jinja2Templates = request.app.state.templates
            return templates.TemplateResponse(
                request=request,
                name="error.html",
                context={
                    "request": request,
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                    "settings": settings,
                    "web_session": getattr(request.state, "web_session", None),
                    "csrf_token": getattr(getattr(request.state, "web_session", None), "csrf", ""),
                    "flash": "",
                    "flash_level": "info",
                    "active_path": request.url.path,
                    "trading_mode_label": settings.trading_mode.label_ru,
                    "accounts": getattr(request.state, "accounts", []),
                    "active_account": getattr(request.state, "active_account", None),
                },
                status_code=exc.status_code,
            )
        return await _json_http_exception(request, exc)

    return app


async def _json_http_exception(
    request: Request,
    exc: StarletteHTTPException,
) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def _startup_data(
    app: FastAPI,
    settings: Settings,
    session_factory: sessionmaker[Session],
    client: TInvestClient,
    clock: Clock,
) -> None:
    if not settings.seed_demo_data:
        return
    with session_factory() as session:
        account_count = session.scalar(select(func.count(Account.id))) or 0
    if settings.tinvest_client == "mock" and account_count == 0:
        try:
            report = SyncService(session_factory, client, settings, clock=clock).run()
        except Exception:
            LOGGER.exception("startup sync failed")
        else:
            captured = capture_all_snapshots(
                session_factory,
                settings,
                source="startup",
                clock=clock,
            )
            with session_scope(session_factory) as session:
                record_audit(
                    session,
                    settings,
                    event_type="sync.startup",
                    actor="system",
                    message=f"Стартовая синхронизация: {report.status.value}",
                    severity=(Severity.INFO if report.status.value == "OK" else Severity.WARNING),
                    response_payload={
                        "accounts": report.accounts_synced,
                        "instruments": report.instruments_synced,
                        "operations": report.operations_synced,
                        "errors": list(report.errors),
                        "snapshots": captured,
                    },
                )
    with session_scope(session_factory) as session:
        ensure_demo_goal(session, settings)
