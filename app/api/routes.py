"""Server-rendered pages and state-changing actions."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session, selectinload

from app.api.deps import (
    WebSession,
    action_redirect,
    current_session,
    get_db,
    get_settings,
    load_account_context,
    require_csrf,
)
from app.core.config import Settings
from app.core.enums import AssetClass, RecommendationStatus, RiskProfile, Severity, TradingMode
from app.core.security import new_session, sign_session, verify_password
from app.core.time import utc_now
from app.db.session import session_scope
from app.integrations.tinvest.exceptions import TInvestError
from app.models.account import Account, SyncRun
from app.models.audit import AuditLog, RiskEvent
from app.models.goal import Goal
from app.models.instrument import Instrument
from app.models.notification import Notification
from app.models.order import Order
from app.models.recommendation import Recommendation
from app.models.snapshot import PortfolioSnapshot
from app.models.user import User
from app.notifications import (
    create_notification,
    mark_all_read,
    mark_read,
    recent_notifications,
    unread_count,
)
from app.risk.engine import FULL_ACCESS
from app.schemas import HealthResponse
from app.screener.config import ScreenerConfig
from app.screener.screen import ScreenerService
from app.screener.store import ScreenerStore
from app.services.analysis import analyze_history, run_scenario
from app.services.audit import record_audit
from app.services.backtest import BacktestService
from app.services.goals import (
    AllocationInput,
    GoalInput,
    GoalProgress,
    GoalService,
    GoalValidationError,
)
from app.services.growth import GrowthService, growth_coverage, top_growth
from app.services.orders import OrderBlockedError, OrderError, OrderService
from app.services.portfolio import PortfolioService, PortfolioView
from app.services.recommendations import RecommendationService
from app.services.risk_control import emergency_stop_enabled, set_emergency_stop
from app.services.snapshots import capture_all_snapshots
from app.services.sync import SyncService

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
ZERO = Decimal("0")
ALLOCATION_CLASSES = (
    AssetClass.STOCK,
    AssetClass.BOND,
    AssetClass.ETF,
    AssetClass.CURRENCY,
    AssetClass.CASH,
)
DEFAULT_ALLOCATIONS = {
    AssetClass.STOCK: ("40", "50", "60"),
    AssetClass.BOND: ("10", "20", "25"),
    AssetClass.ETF: ("0", "5", "10"),
    AssetClass.CURRENCY: ("5", "10", "15"),
    AssetClass.CASH: ("10", "15", "20"),
}


def _context(request: Request, **values: Any) -> dict[str, Any]:
    settings = get_settings(request)
    session: WebSession | None = getattr(request.state, "web_session", None)
    flash = request.query_params.get("flash", "")
    level = request.query_params.get("level", "success")
    if level not in {"success", "warning", "error", "info"}:
        level = "info"
    context: dict[str, Any] = {
        "settings": settings,
        "web_session": session,
        "csrf_token": session.csrf if session else "",
        "flash": flash[:500],
        "flash_level": level,
        "active_path": request.url.path,
        "trading_mode_label": settings.trading_mode.label_ru,
        "accounts": getattr(request.state, "accounts", []),
        "active_account": getattr(request.state, "active_account", None),
    }
    context.update(values)
    return context


def _render(request: Request, name: str, **values: Any) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=_context(request, **values),
    )


def _active_account(request: Request, session: Session) -> Account | None:
    active = getattr(request.state, "active_account", None)
    if active is not None and object_session(active) is session:
        return active
    return load_account_context(request, session)


def _require_active_account(request: Request, session: Session, account_id: int) -> Account:
    active = _active_account(request, session)
    if active is None:
        raise HTTPException(status_code=409, detail="Сначала синхронизируйте счёт.")
    if active.id != account_id:
        raise HTTPException(
            status_code=409,
            detail="Объект принадлежит другому счёту. Переключите счёт и повторите.",
        )
    return active


def _first_goal(session: Session, account_id: int, goal_id: int | None = None) -> Goal | None:
    if goal_id is not None:
        goal = session.get(Goal, goal_id)
        if goal is not None and goal.account_id == account_id:
            return goal
    return session.scalar(
        select(Goal)
        .where(Goal.account_id == account_id, Goal.is_active.is_(True))
        .order_by(Goal.id)
    )


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _decision_payload(decision: Any) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "triggered_rules": [
            {"code": check.code, "title": check.title, "detail": check.detail}
            for check in decision.triggered_rules
        ],
        "blocking_rules": [
            {"code": check.code, "title": check.title, "detail": check.detail}
            for check in decision.blocking_rules
        ],
        "warnings": list(decision.warnings),
    }


def _decision_summary(decision: Any) -> str:
    return (
        "; ".join(f"{check.title}: {check.detail}" for check in decision.blocking_rules)
        or "заблокировано"
    )


def _record_blocked_order(
    session: Session,
    settings: Settings,
    *,
    account_id: int,
    recommendation_id: int,
    error: OrderBlockedError,
) -> None:
    failed = list(error.decision.blocking_rules)
    session.add(
        RiskEvent(
            account_id=account_id,
            code="ORDER_BLOCKED",
            severity=Severity.WARNING,
            message="; ".join(f"{check.title}: {check.detail}" for check in failed)
            or "Заявка заблокирована риск-проверкой",
            details={
                "recommendation_id": recommendation_id,
                "checks": [
                    {"code": check.code, "title": check.title, "detail": check.detail}
                    for check in failed
                ],
            },
        )
    )
    session.flush()
    record_audit(
        session,
        settings,
        event_type="order.blocked",
        entity_type="recommendation",
        entity_id=recommendation_id,
        actor="system",
        severity=Severity.WARNING,
        message=_decision_summary(error.decision),
        response_payload=_decision_payload(error.decision),
    )


@router.get("/health", response_class=JSONResponse)
def health(request: Request) -> JSONResponse:
    settings = get_settings(request)
    payload = HealthResponse(
        app=settings.app_name,
        client=settings.tinvest_client,
        sandbox=settings.tinvest_sandbox,
        trading_mode=settings.trading_mode,
    )
    return JSONResponse(payload.model_dump(mode="json"))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    settings = get_settings(request)
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    if getattr(request.state, "web_session", None) is not None:
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", next_path=_safe_next(next), error="")


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
) -> Response:
    settings = get_settings(request)
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    target = _safe_next(next)
    limiter = request.app.state.login_limiter
    limiter_key = _source_ip(request) or "local"
    retry_after = limiter.retry_after(limiter_key)
    if retry_after:
        blocked_response = _render(
            request,
            "login.html",
            next_path=target,
            error=f"Слишком много попыток входа. Повторите через {retry_after} сек.",
        )
        blocked_response.status_code = 429
        blocked_response.headers["Retry-After"] = str(retry_after)
        return blocked_response
    with session_scope(request.app.state.session_factory) as session:
        user = session.scalar(select(User).where(User.username == username.strip()))
        valid = (
            user is not None and user.is_active and verify_password(password, user.password_hash)
        )
        if not valid:
            record_audit(
                session,
                settings,
                event_type="auth.login_failed",
                actor=username.strip()[:64] or "unknown",
                message="Неудачная попытка локального входа",
                severity=Severity.WARNING,
                source_ip=_source_ip(request),
            )
            session.commit()
            limiter.record_failure(limiter_key)
            return _render(
                request,
                "login.html",
                next_path=target,
                error="Неверный логин или пароль.",
            )
        assert user is not None
        limiter.reset(limiter_key)
        user.last_login_at = utc_now()
        record_audit(
            session,
            settings,
            event_type="auth.login",
            actor=user.username,
            entity_type="user",
            entity_id=user.id,
            message="Локальный вход",
            source_ip=_source_ip(request),
        )
        session.commit()
        session_data = new_session(user.id, user.username, settings.session_ttl_hours)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        sign_session(session_data.to_payload(), settings.session_secret_value),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    session = current_session(request)
    with session_scope(request.app.state.session_factory) as db:
        record_audit(
            db,
            settings,
            event_type="auth.logout",
            actor=session.username,
            message="Выход из локального приложения",
            source_ip=_source_ip(request),
        )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
    )
    response.delete_cookie(
        settings.account_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return response


@router.post("/accounts/select")
def select_account(
    request: Request,
    account_id: int = Form(...),
    next: str = Form("/"),
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    with session_scope(ctx.session_factory) as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        settings.account_cookie_name,
        str(account_id),
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    portfolio: PortfolioView | None = None
    goal: Goal | None = None
    progress: GoalProgress | None = None
    recommendations: list[Recommendation] = []
    orders: list[Order] = []
    latest_sync: SyncRun | None = None
    warnings: list[str] = []
    if account is not None:
        portfolio = PortfolioService(settings).build(session, account.id)
        goal = _first_goal(session, account.id)
        progress = (
            GoalService().progress(goal, portfolio.total_value, now=portfolio.as_of)
            if goal is not None
            else None
        )
        recommendations = list(
            session.scalars(
                select(Recommendation)
                .options(selectinload(Recommendation.instrument))
                .where(
                    Recommendation.account_id == account.id,
                    Recommendation.status == "NEW",
                )
                .order_by(Recommendation.calculated_at.desc())
                .limit(5)
            )
        )
        orders = list(
            session.scalars(
                select(Order)
                .options(
                    selectinload(Order.instrument),
                    selectinload(Order.recommendation),
                )
                .where(Order.account_id == account.id)
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        )
        latest_sync = session.scalar(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1))
        snapshots = list(
            session.scalars(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.account_id == account.id)
                .order_by(PortfolioSnapshot.captured_at.desc())
                .limit(90)
            )
        )
        warnings.extend(portfolio.warnings)
        if goal is not None and goal.currency != settings.base_currency:
            warnings.append(
                f"Валюта цели {goal.currency} не совпадает с базовой {settings.base_currency}; "
                "прогресс показан без конвертации."
            )
    chart_rows = (
        [
            {"label": row.asset_class.label_ru, "value": str(row.weight_percent)}
            for row in portfolio.allocation
        ]
        if portfolio is not None
        else []
    )
    equity_rows = (
        [
            {
                "label": row.captured_at.strftime("%d.%m %H:%M"),
                "value": str(row.total_value),
            }
            for row in reversed(snapshots)
        ]
        if account is not None
        else []
    )
    return _render(
        request,
        "dashboard.html",
        page_title="Обзор",
        account=account,
        portfolio=portfolio,
        goal=goal,
        progress=progress,
        recommendations=recommendations,
        orders=orders,
        latest_sync=latest_sync,
        warnings=warnings,
        chart_rows=chart_rows,
        equity_rows=equity_rows,
        can_recommend=settings.trading_mode is not TradingMode.OBSERVE,
    )


@router.post("/sync")
def sync_now(request: Request, csrf_token: str = Form("")) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    try:
        report = SyncService(
            ctx.session_factory,
            ctx.client,
            settings,
            clock=ctx.clock,
        ).run()
    except Exception as exc:
        with session_scope(ctx.session_factory) as db:
            record_audit(
                db,
                settings,
                event_type="sync.failed",
                actor=current_session(request).username,
                message=str(exc)[:300],
                severity=Severity.ERROR,
                source_ip=_source_ip(request),
            )
            account = _active_account(request, db)
            create_notification(
                db,
                account_id=account.id if account is not None else None,
                kind="sync",
                severity=Severity.ERROR,
                title="Синхронизация не удалась",
                message=str(exc)[:500],
                at=ctx.clock.now(),
            )
        return action_redirect(request, "/", f"Синхронизация не удалась: {exc}", "error")
    captured = capture_all_snapshots(ctx.session_factory, settings, source="sync")
    with session_scope(ctx.session_factory) as db:
        from app.bootstrap import ensure_demo_goal

        ensure_demo_goal(db, settings)
        record_audit(
            db,
            settings,
            event_type="sync.completed",
            actor=current_session(request).username,
            entity_type="sync_run",
            entity_id=report.run_id,
            message=f"Статус {report.status.value}",
            severity=Severity.INFO if report.status.value == "OK" else Severity.WARNING,
            response_payload={
                "status": report.status.value,
                "accounts": report.accounts_synced,
                "instruments": report.instruments_synced,
                "operations": report.operations_synced,
                "warnings": list(report.warnings),
                "errors": list(report.errors),
                "snapshots": captured,
            },
            source_ip=_source_ip(request),
        )
    level = "success" if report.status.value == "OK" else "warning"
    message = (
        f"Синхронизация завершена: счетов {report.accounts_synced}, "
        f"инструментов {report.instruments_synced}, операций {report.operations_synced}."
    )
    if report.errors:
        message += f" Ошибки: {report.errors[0]}"
    return action_redirect(request, "/", message, level)


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    portfolio: PortfolioView | None = None
    excluded_ids: set[int] = set()
    if account is not None:
        portfolio = PortfolioService(settings).build(session, account.id)
        excluded_ids = set(
            session.scalars(select(Instrument.id).where(Instrument.is_excluded.is_(True)))
        )
    return _render(
        request,
        "positions.html",
        page_title="Позиции",
        account=account,
        portfolio=portfolio,
        excluded_ids=excluded_ids,
        can_recommend=settings.trading_mode is not TradingMode.OBSERVE,
    )


@router.post("/instruments/{instrument_id}/exclude")
def toggle_instrument_exclusion(
    request: Request,
    instrument_id: int,
    excluded: str = Form("1"),
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    if settings.trading_mode is TradingMode.OBSERVE:
        return action_redirect(
            request,
            "/positions",
            "Режим OBSERVE не изменяет настройки инструментов.",
            "warning",
        )
    ctx = request.app.state
    with session_scope(ctx.session_factory) as db:
        instrument = db.get(Instrument, instrument_id)
        if instrument is None:
            return action_redirect(request, "/positions", "Инструмент не найден.", "error")
        is_excluded = excluded == "1"
        instrument.is_excluded = is_excluded
        if is_excluded:
            instrument.is_buy_candidate = False
        record_audit(
            db,
            settings,
            event_type="instrument.exclusion_changed",
            actor=current_session(request).username,
            entity_type="instrument",
            entity_id=instrument.id,
            message=(
                f"{instrument.ticker}: {'исключён' if is_excluded else 'возвращён'} из рекомендаций"
            ),
            request_payload={"is_excluded": is_excluded},
            source_ip=_source_ip(request),
        )
    return action_redirect(
        request,
        "/positions",
        "Инструмент исключён из покупок и продаж." if is_excluded else "Инструмент возвращён.",
        "info",
    )


@router.get("/recommendations", response_class=HTMLResponse)
def recommendations(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    goal = _first_goal(session, account.id) if account else None
    items: list[Recommendation] = []
    if account is not None:
        items = list(
            session.scalars(
                select(Recommendation)
                .options(selectinload(Recommendation.instrument))
                .where(Recommendation.account_id == account.id)
                .order_by(Recommendation.calculated_at.desc(), Recommendation.id.desc())
                .limit(100)
            )
        )
    fresh = [item for item in items if item.status.value == "NEW"]
    history = [item for item in items if item.status.value != "NEW"]
    portfolio = (
        PortfolioService(settings).build(session, account.id) if account is not None else None
    )
    now = utc_now()
    growth_enabled = settings.growth_analysis_enabled
    growth_rows = top_growth(session, settings, now=now, limit=8) if growth_enabled else []
    coverage = growth_coverage(session, settings, now=now) if growth_enabled else None
    return _render(
        request,
        "recommendations.html",
        page_title="Рекомендации",
        account=account,
        goal=goal,
        portfolio=portfolio,
        fresh=fresh,
        history=history,
        can_trade=settings.trading_mode.can_create_orders,
        can_recommend=settings.trading_mode is not TradingMode.OBSERVE,
        growth_enabled=growth_enabled,
        growth_rows=growth_rows,
        growth_coverage=coverage,
        now=now,
    )


@router.post("/instruments/universe")
def load_instrument_universe(
    request: Request,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    if settings.trading_mode is TradingMode.OBSERVE:
        return action_redirect(
            request,
            "/recommendations",
            "Режим OBSERVE не изменяет universe инструментов.",
            "warning",
        )
    ctx = request.app.state
    service = SyncService(ctx.session_factory, ctx.client, settings, clock=ctx.clock)
    try:
        count, warnings = service.load_universe()
    except TInvestError as exc:
        return action_redirect(request, "/recommendations", f"Не удалось: {exc}", "error")
    with session_scope(ctx.session_factory) as db:
        record_audit(
            db,
            settings,
            event_type="instrument.universe_loaded",
            actor=current_session(request).username,
            message=f"Загружено инструментов: {count}",
            response_payload={"count": count, "warnings": list(warnings)},
            source_ip=_source_ip(request),
        )
    level = "warning" if warnings else "success"
    message = f"Вселенная обновлена: {count} инструментов."
    if warnings:
        message += f" {warnings[0]}"
    return action_redirect(request, "/recommendations", message, level)


@router.post("/growth/scan")
def scan_growth(request: Request, csrf_token: str = Form("")) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    if settings.trading_mode is TradingMode.OBSERVE:
        return action_redirect(
            request,
            "/recommendations",
            "Режим OBSERVE не запускает анализ роста.",
            "warning",
        )
    if not settings.growth_analysis_enabled:
        return action_redirect(
            request,
            "/recommendations",
            "Анализ роста отключён настройкой GROWTH_ANALYSIS_ENABLED.",
            "warning",
        )
    ctx = request.app.state
    report = GrowthService(
        ctx.session_factory,
        ctx.client,
        settings,
        clock=ctx.clock,
    ).scan(limit=min(settings.growth_scan_limit, 20))
    with session_scope(ctx.session_factory) as db:
        record_audit(
            db,
            settings,
            event_type="growth.scan",
            actor=current_session(request).username,
            message=(
                f"Анализ роста: {report.analyzed} из {report.scanned} "
                f"(пропущено свежих: {report.skipped_fresh})"
            ),
            severity=Severity.WARNING if report.failed or report.insufficient else Severity.INFO,
            response_payload={
                "scanned": report.scanned,
                "analyzed": report.analyzed,
                "insufficient": report.insufficient,
                "failed": report.failed,
                "warnings": list(report.warnings[:20]),
            },
            source_ip=_source_ip(request),
        )
    message = (
        f"Анализ роста: рассчитано {report.analyzed} из {report.scanned}; "
        f"без истории {report.insufficient}, ошибок {report.failed}, "
        f"пропущено свежих {report.skipped_fresh}."
    )
    if report.warnings:
        message += f" {report.warnings[0]}"
    level = "warning" if report.failed or report.insufficient else "success"
    return action_redirect(request, "/recommendations", message, level)


@router.post("/instruments/add")
def add_instrument(
    request: Request,
    ticker: str = Form(""),
    class_code: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    if settings.trading_mode is TradingMode.OBSERVE:
        return action_redirect(
            request,
            "/recommendations",
            "Режим OBSERVE не изменяет universe инструментов.",
            "warning",
        )
    ctx = request.app.state
    try:
        instrument = SyncService(
            ctx.session_factory,
            ctx.client,
            settings,
            clock=ctx.clock,
        ).add_instrument(ticker, class_code or None)
    except TInvestError as exc:
        return action_redirect(
            request,
            "/recommendations",
            f"Инструмент не найден: {exc}",
            "error",
        )
    with session_scope(ctx.session_factory) as db:
        record_audit(
            db,
            settings,
            event_type="instrument.universe_added",
            actor=current_session(request).username,
            entity_type="instrument",
            entity_id=instrument.id,
            message=f"{instrument.ticker} добавлен в universe покупок",
            request_payload={"ticker": ticker, "class_code": class_code},
            source_ip=_source_ip(request),
        )
    return action_redirect(
        request,
        "/recommendations",
        f"{instrument.ticker} добавлен в universe покупок.",
        "success",
    )


def _refresh_screener(settings: Settings, *, live: bool) -> str | None:
    if not settings.recommendations_screener_enabled:
        return None
    config = ScreenerConfig(db_path=settings.screener_db_path)
    store: ScreenerStore | None = None
    try:
        store = ScreenerStore(config.db_path)
        ScreenerService(store, config, settings).run(refresh=live, offline=not live)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return f"Скринер не обновлён: {exc}"
    finally:
        if store is not None:
            store.close()
    return None


def _generate_recommendations(
    request: Request,
    settings: Settings,
    *,
    screener_warning: str | None = None,
    refreshed: bool = False,
) -> Response:
    ctx = request.app.state
    with session_scope(ctx.session_factory) as db:
        account = _active_account(request, db)
        if account is None:
            return action_redirect(
                request,
                "/",
                "Сначала синхронизируйте счёт.",
                "warning",
            )
        goal = _first_goal(db, account.id)
        if goal is None:
            return action_redirect(
                request,
                "/goals/new",
                "Создайте цель, чтобы рассчитать рекомендации.",
                "warning",
            )
        run = RecommendationService(
            settings,
            clock=ctx.clock,
        ).generate(db, account_id=account.id, goal_id=goal.id)
        record_audit(
            db,
            settings,
            event_type="recommendations.generated",
            actor=current_session(request).username,
            entity_type="account",
            entity_id=account.id,
            message=f"Сформировано рекомендаций: {len(run.recommendations)}",
            response_payload={
                "warnings": list(run.warnings),
                "screener_warning": screener_warning,
                "actions": [
                    {
                        "action": item.action.value,
                        "ticker": item.ticker,
                        "lots": item.lots,
                    }
                    for item in run.recommendations
                ],
            },
            source_ip=_source_ip(request),
        )
    message = f"Рассчитано рекомендаций: {len(run.recommendations)}."
    if refreshed:
        message = f"Данные обновлены. {message}"
    if screener_warning:
        message = f"{screener_warning} {message}"
    if run.warnings:
        message += f" {run.warnings[0]}"
    level = "warning" if run.warnings or screener_warning else "success"
    return action_redirect(request, "/recommendations", message, level)


@router.post("/recommendations/generate")
def generate_recommendations(request: Request, csrf_token: str = Form("")) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    return _generate_recommendations(request, settings)


@router.post("/recommendations/refresh")
def refresh_recommendations(request: Request, csrf_token: str = Form("")) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    screener_warning = _refresh_screener(settings, live=True)
    return _generate_recommendations(
        request,
        settings,
        screener_warning=screener_warning,
        refreshed=True,
    )


@router.post("/recommendations/{recommendation_id}/approve")
def approve_recommendation(
    request: Request,
    recommendation_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    actor = current_session(request).username
    try:
        with session_scope(ctx.session_factory) as db:
            recommendation = db.get(Recommendation, recommendation_id)
            if recommendation is None:
                raise HTTPException(status_code=404, detail="recommendation not found")
            _require_active_account(request, db, recommendation.account_id)
            order = OrderService(ctx.client, settings, clock=ctx.clock).approve(
                db,
                recommendation_id,
                user=actor,
            )
            record_audit(
                db,
                settings,
                event_type="order.approved",
                entity_type="order",
                entity_id=order.id,
                actor=actor,
                message=f"Одобрена рекомендация {recommendation_id}",
                request_payload={"recommendation_id": recommendation_id},
                response_payload={
                    "order_id": order.id,
                    "client_order_id": order.client_order_id,
                    "lots": order.lots,
                    "price": str(order.limit_price),
                },
                source_ip=_source_ip(request),
            )
    except OrderBlockedError as exc:
        with session_scope(ctx.session_factory) as db:
            account = _active_account(request, db)
            if account is not None:
                _record_blocked_order(
                    db,
                    settings,
                    account_id=account.id,
                    recommendation_id=recommendation_id,
                    error=exc,
                )
        return action_redirect(
            request,
            "/recommendations",
            f"Заявка заблокирована: {_decision_summary(exc.decision)}",
            "error",
        )
    except OrderError as exc:
        return action_redirect(
            request,
            "/recommendations",
            f"Не удалось одобрить: {exc}",
            "error",
        )
    return action_redirect(
        request,
        "/orders",
        "Заявка создана. Проверьте параметры и отправьте её явно.",
        "success",
    )


@router.post("/recommendations/{recommendation_id}/reject")
def reject_recommendation(
    request: Request,
    recommendation_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    actor = current_session(request).username
    with session_scope(ctx.session_factory) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        if recommendation is None:
            raise HTTPException(status_code=404, detail="recommendation not found")
        _require_active_account(request, db, recommendation.account_id)
        if recommendation.status.value != "NEW":
            return action_redirect(
                request,
                "/recommendations",
                "Рекомендация уже обработана.",
                "warning",
            )
        recommendation.status = RecommendationStatus.REJECTED
        recommendation.rejected_at = utc_now()
        recommendation.decided_by = actor
        record_audit(
            db,
            settings,
            event_type="recommendation.rejected",
            entity_type="recommendation",
            entity_id=recommendation.id,
            actor=actor,
            message="Рекомендация отклонена пользователем",
            source_ip=_source_ip(request),
        )
    return action_redirect(request, "/recommendations", "Рекомендация отклонена.", "info")


@router.get("/goals", response_class=HTMLResponse)
def goals_page(
    request: Request,
    session: DbSession,
    goal_id: int | None = None,
) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    goals: list[Goal] = []
    selected: Goal | None = None
    progress: GoalProgress | None = None
    portfolio: PortfolioView | None = None
    allocation_rows: list[dict[str, Any]] = []
    if account is not None:
        goals = list(
            session.scalars(
                select(Goal)
                .where(Goal.account_id == account.id)
                .order_by(Goal.is_active.desc(), Goal.id)
            )
        )
        selected = _first_goal(session, account.id, goal_id)
        portfolio = PortfolioService(settings).build(session, account.id)
        if selected is not None:
            progress = GoalService().progress(selected, portfolio.total_value, now=portfolio.as_of)
            current_map = {row.asset_class: row for row in portfolio.allocation}
            for allocation in selected.allocations:
                current = current_map.get(allocation.asset_class)
                allocation_rows.append(
                    {
                        "allocation": allocation,
                        "current_weight": current.weight_percent if current else ZERO,
                        "current_value": current.value_base if current else ZERO,
                    }
                )
    return _render(
        request,
        "goals.html",
        page_title="Цели",
        account=account,
        goals=goals,
        goal=selected,
        progress=progress,
        portfolio=portfolio,
        allocation_rows=allocation_rows,
    )


def _goal_form_values(goal: Goal | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if goal is None:
        values = {
            "name": "Моя цель",
            "target_amount": "2000000",
            "target_date": (date.today() + timedelta(days=365)).isoformat(),
            "regular_contribution": "30000",
            "contribution_frequency": "MONTHLY",
            "min_cash_reserve": "50000",
            "max_drawdown_percent": "20",
            "risk_profile": RiskProfile.BALANCED.value,
            "description": "",
            "stop_loss_enabled": "",
            "stop_loss_percent": "",
            "take_profit_enabled": "",
            "take_profit_percent": "",
        }
        allocation_values = DEFAULT_ALLOCATIONS
    else:
        values = {
            "name": goal.name,
            "target_amount": str(goal.target_amount),
            "target_date": goal.target_date.isoformat(),
            "regular_contribution": str(goal.regular_contribution),
            "contribution_frequency": goal.contribution_frequency,
            "min_cash_reserve": str(goal.min_cash_reserve),
            "max_drawdown_percent": str(goal.max_drawdown_percent),
            "risk_profile": goal.risk_profile.value,
            "description": goal.description,
            "stop_loss_enabled": "1" if goal.stop_loss_enabled else "",
            "stop_loss_percent": (
                str(goal.stop_loss_percent) if goal.stop_loss_percent is not None else ""
            ),
            "take_profit_enabled": "1" if goal.take_profit_enabled else "",
            "take_profit_percent": (
                str(goal.take_profit_percent) if goal.take_profit_percent is not None else ""
            ),
        }
        allocation_values = {
            item.asset_class: (
                str(item.min_percent),
                str(item.target_percent),
                str(item.max_percent),
            )
            for item in goal.allocations
        }
    rows = [
        {
            "asset_class": asset_class,
            "min": allocation_values.get(asset_class, ("0", "0", "0"))[0],
            "target": allocation_values.get(asset_class, ("0", "0", "0"))[1],
            "max": allocation_values.get(asset_class, ("0", "0", "0"))[2],
        }
        for asset_class in ALLOCATION_CLASSES
    ]
    return values, rows


@router.get("/goals/new", response_class=HTMLResponse)
def goal_new_page(request: Request) -> HTMLResponse:
    values, rows = _goal_form_values(None)
    return _render(
        request,
        "goal_form.html",
        page_title="Новая цель",
        mode="new",
        goal=None,
        values=values,
        allocation_rows=rows,
        error="",
    )


@router.get("/goals/{goal_id}/edit", response_class=HTMLResponse)
def goal_edit_page(
    request: Request,
    goal_id: int,
    session: DbSession,
) -> HTMLResponse:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    if goal.account_id is not None:
        _require_active_account(request, session, goal.account_id)
    values, rows = _goal_form_values(goal)
    return _render(
        request,
        "goal_form.html",
        page_title="Редактирование цели",
        mode="edit",
        goal=goal,
        values=values,
        allocation_rows=rows,
        error="",
    )


async def _goal_from_form(
    request: Request,
) -> tuple[GoalInput, list[AllocationInput], dict[str, str], list[dict[str, Any]]]:
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token", "")))
    raw = {key: str(value) for key, value in form.items()}
    try:
        target_date = date.fromisoformat(raw.get("target_date", ""))
    except ValueError as exc:
        raise GoalValidationError("Некорректная дата цели.") from exc
    try:
        risk_profile = RiskProfile(raw.get("risk_profile", RiskProfile.BALANCED.value))
    except ValueError as exc:
        raise GoalValidationError("Некорректный риск-профиль.") from exc
    allocations: list[AllocationInput] = []
    allocation_rows: list[dict[str, Any]] = []
    for asset_class in ALLOCATION_CLASSES:
        prefix = asset_class.value.lower()
        raw_min = raw.get(f"{prefix}_min", "0")
        raw_target = raw.get(f"{prefix}_target", "0")
        raw_max = raw.get(f"{prefix}_max", "0")
        allocation_rows.append(
            {
                "asset_class": asset_class,
                "min": raw_min,
                "target": raw_target,
                "max": raw_max,
            }
        )
        try:
            allocations.append(
                AllocationInput(
                    asset_class,
                    Decimal(raw_min or "0"),
                    Decimal(raw_target or "0"),
                    Decimal(raw_max or "0"),
                )
            )
        except (InvalidOperation, ValueError) as exc:
            raise GoalValidationError(
                f"Некорректное распределение для {asset_class.value}."
            ) from exc
    try:
        data = GoalInput(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            target_amount=Decimal(raw.get("target_amount", "0") or "0"),
            currency=get_settings(request).base_currency,
            target_date=target_date,
            regular_contribution=Decimal(raw.get("regular_contribution", "0") or "0"),
            contribution_frequency=raw.get("contribution_frequency", "MONTHLY"),
            min_cash_reserve=Decimal(raw.get("min_cash_reserve", "0") or "0"),
            max_drawdown_percent=Decimal(raw.get("max_drawdown_percent", "20") or "20"),
            risk_profile=risk_profile,
            stop_loss_enabled=raw.get("stop_loss_enabled") == "1",
            stop_loss_percent=(
                Decimal(raw["stop_loss_percent"])
                if raw.get("stop_loss_percent", "").strip()
                else None
            ),
            take_profit_enabled=raw.get("take_profit_enabled") == "1",
            take_profit_percent=(
                Decimal(raw["take_profit_percent"])
                if raw.get("take_profit_percent", "").strip()
                else None
            ),
        )
    except (InvalidOperation, ValueError) as exc:
        raise GoalValidationError("Проверьте числовые поля цели.") from exc
    return data, allocations, raw, allocation_rows


@router.post("/goals", response_class=HTMLResponse)
async def create_goal(request: Request) -> Response:
    settings = get_settings(request)
    ctx = request.app.state
    raw: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    try:
        data, allocations, raw, rows = await _goal_from_form(request)
        with session_scope(ctx.session_factory) as db:
            account = _active_account(request, db)
            if account is None:
                raise GoalValidationError("Сначала синхронизируйте счёт.")
            goal = GoalService().create_goal(
                db,
                data,
                allocations,
                account_id=account.id,
                today=date.today(),
            )
            record_audit(
                db,
                settings,
                event_type="goal.created",
                entity_type="goal",
                entity_id=goal.id,
                actor=current_session(request).username,
                message=f"Создана цель {goal.name}",
                source_ip=_source_ip(request),
            )
    except GoalValidationError as exc:
        values, _ = _goal_form_values(None)
        values.update({key: value for key, value in raw.items() if key != "csrf_token"})
        return _render(
            request,
            "goal_form.html",
            page_title="Новая цель",
            mode="new",
            goal=None,
            values=values,
            allocation_rows=rows or _goal_form_values(None)[1],
            error=str(exc),
        )
    return action_redirect(request, "/goals", "Цель создана.", "success")


@router.post("/goals/{goal_id}", response_class=HTMLResponse)
async def update_goal(request: Request, goal_id: int) -> Response:
    settings = get_settings(request)
    ctx = request.app.state
    raw: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    try:
        data, allocations, raw, rows = await _goal_from_form(request)
        with session_scope(ctx.session_factory) as db:
            goal = db.get(Goal, goal_id)
            if goal is None:
                raise HTTPException(status_code=404, detail="goal not found")
            if goal.account_id is not None:
                _require_active_account(request, db, goal.account_id)
            GoalService().update_goal(
                db,
                goal,
                data,
                allocations,
                today=date.today(),
            )
            record_audit(
                db,
                settings,
                event_type="goal.updated",
                entity_type="goal",
                entity_id=goal.id,
                actor=current_session(request).username,
                message=f"Обновлена цель {goal.name}",
                source_ip=_source_ip(request),
            )
    except GoalValidationError as exc:
        with session_scope(ctx.session_factory) as db:
            goal = db.get(Goal, goal_id)
        values, _ = _goal_form_values(goal)
        values.update({key: value for key, value in raw.items() if key != "csrf_token"})
        return _render(
            request,
            "goal_form.html",
            page_title="Редактирование цели",
            mode="edit",
            goal=goal,
            values=values,
            allocation_rows=rows or _goal_form_values(goal)[1],
            error=str(exc),
        )
    return action_redirect(request, f"/goals?goal_id={goal_id}", "Цель обновлена.", "success")


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    orders: list[Order] = []
    if account is not None:
        orders = list(
            session.scalars(
                select(Order)
                .options(
                    selectinload(Order.instrument),
                    selectinload(Order.recommendation),
                    selectinload(Order.executions),
                )
                .where(Order.account_id == account.id)
                .order_by(Order.created_at.desc())
                .limit(100)
            )
        )
    return _render(
        request,
        "orders.html",
        page_title="Заявки",
        account=account,
        orders=orders,
        can_trade=settings.trading_mode.can_create_orders,
    )


def _order_service(request: Request) -> OrderService:
    ctx = request.app.state
    return OrderService(ctx.client, get_settings(request), clock=ctx.clock)


@router.post("/orders/{order_id}/send")
def send_order(
    request: Request,
    order_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    actor = current_session(request).username
    try:
        with session_scope(ctx.session_factory) as db:
            existing = db.get(Order, order_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="order not found")
            _require_active_account(request, db, existing.account_id)
            order = _order_service(request).send(db, order_id, user=actor)
            record_audit(
                db,
                settings,
                event_type="order.sent",
                entity_type="order",
                entity_id=order.id,
                actor=actor,
                message=f"Заявка отправлена: {order.status.value}",
                request_payload=order.request_payload,
                response_payload={
                    **order.response_payload,
                    "broker_order_id": order.broker_order_id,
                    "error": order.error_message,
                },
                severity=(
                    Severity.ERROR
                    if order.status.value in {"FAILED", "REJECTED"}
                    else Severity.WARNING
                    if order.status.value == "SUBMIT_UNKNOWN"
                    else Severity.INFO
                ),
                source_ip=_source_ip(request),
            )
            status = order.status.value
            warning = order.warning_message
    except OrderBlockedError as exc:
        with session_scope(ctx.session_factory) as db:
            blocked_order = db.get(Order, order_id)
            if blocked_order is not None:
                _record_blocked_order(
                    db,
                    settings,
                    account_id=blocked_order.account_id,
                    recommendation_id=blocked_order.recommendation_id or 0,
                    error=exc,
                )
        return action_redirect(
            request,
            "/orders",
            f"Заявка заблокирована: {_decision_summary(exc.decision)}",
            "error",
        )
    except OrderError as exc:
        return action_redirect(request, "/orders", f"Не удалось отправить: {exc}", "error")
    if status == "EXECUTED":
        return action_redirect(request, "/orders", "Заявка исполнена.", "success")
    if status == "SUBMIT_UNKNOWN":
        return action_redirect(
            request,
            "/orders",
            "Исход отправки неизвестен. Не отправляйте повторно — выполните сверку.",
            "warning",
        )
    if status in {"FAILED", "REJECTED"}:
        return action_redirect(
            request,
            "/orders",
            f"Брокер вернул статус {status}.",
            "error",
        )
    message = f"Заявка отправлена, статус {status}."
    if warning:
        message += f" {warning}"
    return action_redirect(request, "/orders", message, "info")


@router.post("/orders/{order_id}/reconcile")
def reconcile_order(
    request: Request,
    order_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    try:
        with session_scope(ctx.session_factory) as db:
            existing = db.get(Order, order_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="order not found")
            _require_active_account(request, db, existing.account_id)
            order = _order_service(request).reconcile(db, order_id)
            record_audit(
                db,
                settings,
                event_type="order.reconciled",
                entity_type="order",
                entity_id=order.id,
                actor=current_session(request).username,
                message=f"Сверка заявки: {order.status.value}",
                response_payload={
                    **order.response_payload,
                    "broker_order_id": order.broker_order_id,
                    "warning": order.warning_message,
                },
                source_ip=_source_ip(request),
            )
            status = order.status.value
            warning = order.warning_message
    except OrderError as exc:
        return action_redirect(request, "/orders", f"Сверка не удалась: {exc}", "error")
    if warning:
        return action_redirect(request, "/orders", warning, "warning")
    return action_redirect(request, "/orders", f"Статус заявки: {status}.", "info")


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    request: Request,
    order_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    try:
        with session_scope(ctx.session_factory) as db:
            existing = db.get(Order, order_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="order not found")
            _require_active_account(request, db, existing.account_id)
            order = _order_service(request).cancel(db, order_id)
            record_audit(
                db,
                settings,
                event_type="order.cancelled",
                entity_type="order",
                entity_id=order.id,
                actor=current_session(request).username,
                message=f"Отмена заявки: {order.status.value}",
                response_payload={
                    **order.response_payload,
                    "broker_order_id": order.broker_order_id,
                    "warning": order.warning_message,
                },
                source_ip=_source_ip(request),
            )
            status = order.status.value
            warning = order.warning_message
    except OrderError as exc:
        return action_redirect(request, "/orders", f"Отмена не удалась: {exc}", "error")
    if warning:
        return action_redirect(request, "/orders", warning, "warning")
    return action_redirect(request, "/orders", f"Заявка: {status}.", "info")


@router.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    portfolio: PortfolioView | None = None
    violations: list[str] = []
    account_can_trade = account is not None and account.access_level == FULL_ACCESS
    if account is not None:
        portfolio = PortfolioService(settings).build(session, account.id)
        violations.extend(portfolio.warnings)
        if not settings.tinvest_sandbox and settings.real_orders_armed and not account_can_trade:
            violations.append(
                f"Счёт {account.name}: {account.access_level or 'unknown'}. "
                "Реальные заявки будут заблокированы, нужен токен с торговыми правами."
            )
        for issuer in portfolio.issuer_concentration:
            if issuer.weight_percent > settings.max_issuer_percent:
                violations.append(
                    f"{issuer.issuer_key}: {issuer.weight_percent}% > "
                    f"{settings.max_issuer_percent}% лимита эмитента"
                )
    events = list(
        session.scalars(select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(50))
    )
    limits = [
        ("Максимальная заявка", f"{settings.max_order_amount} {settings.base_currency}"),
        ("Дневной оборот", f"{settings.max_daily_order_amount} {settings.base_currency}"),
        ("Заявок в день", str(settings.max_daily_order_count)),
        ("Лимит эмитента", f"{settings.max_issuer_percent}%"),
        ("Мин. сумма сделки", f"{settings.min_trade_amount} {settings.base_currency}"),
        ("Макс. отклонение цены", f"{settings.max_price_deviation_percent}%"),
        ("Свежесть котировки", f"{settings.quote_max_age_seconds} сек"),
        ("Резерв по умолчанию", f"{settings.default_min_cash_reserve} {settings.base_currency}"),
    ]
    return _render(
        request,
        "risk.html",
        page_title="Риск",
        account=account,
        portfolio=portfolio,
        violations=violations,
        events=events,
        limits=limits,
        stop_enabled=settings.emergency_stop or emergency_stop_enabled(session, default=False),
        real_orders_armed=settings.real_orders_armed,
        account_can_trade=account_can_trade,
    )


@router.post("/risk/emergency-stop")
def toggle_emergency_stop(
    request: Request,
    enabled: str = Form("0"),
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    ctx = request.app.state
    is_enabled = enabled == "1"
    with session_scope(ctx.session_factory) as db:
        set_emergency_stop(db, enabled=is_enabled)
        record_audit(
            db,
            settings,
            event_type="risk.emergency_stop",
            actor=current_session(request).username,
            message="Аварийная остановка включена" if is_enabled else "Аварийная остановка снята",
            severity=Severity.CRITICAL if is_enabled else Severity.WARNING,
            request_payload={"enabled": is_enabled},
            source_ip=_source_ip(request),
        )
        account = _active_account(request, db)
        create_notification(
            db,
            account_id=account.id if account is not None else None,
            kind="risk",
            severity=Severity.CRITICAL if is_enabled else Severity.WARNING,
            title="Аварийная остановка включена" if is_enabled else "Аварийная остановка снята",
            message=(
                "Все новые заявки блокируются до снятия аварийной остановки."
                if is_enabled
                else "Ограничение снято; заявки снова проходят риск-проверки."
            ),
            entity_type="risk",
            at=ctx.clock.now(),
        )
    return action_redirect(
        request,
        "/risk",
        "Аварийная остановка включена." if is_enabled else "Аварийная остановка снята.",
        "warning" if is_enabled else "info",
    )


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request, session: DbSession) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    portfolio: PortfolioView | None = None
    stats = None
    snapshots: list[PortfolioSnapshot] = []
    if account is not None:
        portfolio = PortfolioService(settings).build(session, account.id)
        snapshots = list(
            session.scalars(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.account_id == account.id)
                .order_by(PortfolioSnapshot.captured_at.desc())
                .limit(200)
            )
        )
        stats = analyze_history(snapshots)
    return _render(
        request,
        "analysis.html",
        page_title="Анализ",
        account=account,
        portfolio=portfolio,
        stats=stats,
        snapshots=list(reversed(snapshots))[-30:],
        scenario=None,
        form={"stock": "0", "bond": "0", "etf": "0", "currency": "0"},
        error="",
    )


@router.post("/analysis", response_class=HTMLResponse)
def run_analysis(
    request: Request,
    stock: str = Form("0"),
    bond: str = Form("0"),
    etf: str = Form("0"),
    currency: str = Form("0"),
    csrf_token: str = Form(""),
) -> HTMLResponse:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    with session_scope(request.app.state.session_factory) as session:
        current = _active_account(request, session)
        portfolio = PortfolioService(settings).build(session, current.id) if current else None
        snapshots = (
            list(
                session.scalars(
                    select(PortfolioSnapshot)
                    .where(PortfolioSnapshot.account_id == current.id)
                    .order_by(PortfolioSnapshot.captured_at.desc())
                    .limit(200)
                )
            )
            if current is not None
            else []
        )
        stats = analyze_history(snapshots)
        scenario = None
        error = ""
        form = {"stock": stock, "bond": bond, "etf": etf, "currency": currency}
        if portfolio is not None:
            try:
                scenario = run_scenario(
                    portfolio,
                    {
                        AssetClass.STOCK: Decimal(stock or "0"),
                        AssetClass.BOND: Decimal(bond or "0"),
                        AssetClass.ETF: Decimal(etf or "0"),
                        AssetClass.CURRENCY: Decimal(currency or "0"),
                    },
                )
            except (InvalidOperation, ValueError) as exc:
                error = str(exc)
        return _render(
            request,
            "analysis.html",
            page_title="Анализ",
            account=current,
            portfolio=portfolio,
            stats=stats,
            snapshots=list(reversed(snapshots))[-30:],
            scenario=scenario,
            form=form,
            error=error,
        )


@router.get("/screener", response_class=HTMLResponse)
def screener_page(request: Request) -> HTMLResponse:
    settings = get_settings(request)
    config = ScreenerConfig(db_path=settings.screener_db_path)
    store: ScreenerStore | None = None
    screen: dict[str, Any] | None = None
    backtest: dict[str, Any] | None = None
    error = ""
    cache_stats: dict[str, int] = {}
    cache_coverage: tuple[date | None, date | None, int] = (None, None, 0)
    cache_latest: datetime | None = None
    screen_as_of: date | None = None
    screen_generated: datetime | None = None
    try:
        if config.db_path.exists():
            store = ScreenerStore(config.db_path)
            screen = store.load_latest_run("screen")
            backtest = store.load_latest_run("backtest")
            if screen is not None:
                screen_as_of = date.fromisoformat(str(screen["as_of"]))
                screen_generated = datetime.fromisoformat(str(screen["generated_at"]))
            cache_stats = store.stats()
            cache_coverage = store.bar_coverage()
            cache_latest = store.latest_fetch()
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        error = str(exc)
    finally:
        if store is not None:
            store.close()
    return _render(
        request,
        "screener.html",
        page_title="Скринер",
        screen=screen,
        backtest=backtest,
        screen_as_of=screen_as_of,
        screen_generated=screen_generated,
        screener_error=error,
        cache_stats=cache_stats,
        cache_coverage=cache_coverage,
        cache_latest=cache_latest,
        screener_db=config.db_path,
    )


@router.post("/screener/refresh")
def refresh_screener(
    request: Request,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    settings = get_settings(request)
    config = ScreenerConfig(db_path=settings.screener_db_path)
    store: ScreenerStore | None = None
    try:
        store = ScreenerStore(config.db_path)
        ScreenerService(store, config).run(refresh=False, offline=True)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return action_redirect(
            request,
            "/screener",
            f"Скринер не рассчитан: {exc}",
            "error",
        )
    finally:
        if store is not None:
            store.close()
    return action_redirect(
        request,
        "/screener",
        "Скринер пересчитан по локальному кэшу. Сеть не использовалась.",
        "success",
    )


@router.get("/backtest", response_class=HTMLResponse)
def backtest_page(
    request: Request,
    session: DbSession,
    days: int = 365,
) -> HTMLResponse:
    settings = get_settings(request)
    account = _active_account(request, session)
    portfolio: PortfolioView | None = None
    result = None
    if account is not None:
        portfolio = PortfolioService(settings).build(session, account.id)
        result = BacktestService(
            request.app.state.client,
            settings,
            clock=request.app.state.clock,
        ).run(portfolio, days=days)
    chart_rows = (
        [
            {"label": point.at.strftime("%d.%m.%y"), "value": str(point.value)}
            for point in result.points
        ]
        if result is not None
        else []
    )
    return _render(
        request,
        "backtest.html",
        page_title="Backtest",
        account=account,
        portfolio=portfolio,
        result=result,
        days=days,
        chart_rows=chart_rows,
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, session: DbSession) -> HTMLResponse:
    account = _active_account(request, session)
    account_id = account.id if account is not None else None
    return _render(
        request,
        "notifications.html",
        page_title="Уведомления",
        notifications=recent_notifications(session, account_id=account_id),
        unread=unread_count(session, account_id=account_id),
    )


@router.post("/notifications/{notification_id}/read")
def notification_read(
    request: Request,
    notification_id: int,
    csrf_token: str = Form(""),
) -> Response:
    require_csrf(request, csrf_token)
    ctx = request.app.state
    with session_scope(ctx.session_factory) as db:
        notification = db.get(Notification, notification_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="notification not found")
        if notification.account_id is not None:
            _require_active_account(request, db, notification.account_id)
        mark_read(db, notification_id)
    return action_redirect(request, "/notifications", "Уведомление прочитано.", "info")


@router.post("/notifications/read-all")
def notifications_read_all(request: Request, csrf_token: str = Form("")) -> Response:
    require_csrf(request, csrf_token)
    ctx = request.app.state
    with session_scope(ctx.session_factory) as db:
        account = _active_account(request, db)
        changed = mark_all_read(db, account_id=account.id if account is not None else None)
    return action_redirect(
        request,
        "/notifications",
        f"Отмечено прочитанными: {changed}.",
        "info",
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, session: DbSession) -> HTMLResponse:
    entries = list(
        session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200))
    )
    return _render(request, "audit.html", page_title="Аудит", entries=entries)


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)
