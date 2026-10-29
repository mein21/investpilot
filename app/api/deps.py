"""Request dependencies, authentication and CSRF checks."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import constant_time_equals
from app.models.account import Account


@dataclass(frozen=True)
class WebSession:
    user_id: int
    username: str
    csrf: str
    expires_at: int


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def load_account_context(request: Request, session: Session) -> Account | None:
    settings = get_settings(request)
    accounts = list(session.scalars(select(Account).order_by(Account.id)))
    by_id = {account.id: account for account in accounts}
    raw = request.cookies.get(settings.account_cookie_name, "")
    selected_id: int | None = None
    try:
        selected_id = int(raw)
    except (TypeError, ValueError):
        selected_id = None
    active = by_id.get(selected_id) if selected_id is not None else None
    if active is None and accounts:
        active = accounts[0]
    request.state.accounts = accounts
    request.state.active_account = active
    request.state.active_account_id = active.id if active is not None else None
    return active


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        load_account_context(request, session)
        yield session
    finally:
        session.close()


def current_session(request: Request) -> WebSession:
    state = getattr(request.state, "web_session", None)
    if state is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return state


def require_csrf(request: Request, token: str) -> None:
    settings = get_settings(request)
    if not settings.auth_enabled:
        return
    session = current_session(request)
    if not token or not constant_time_equals(token, session.csrf):
        raise HTTPException(status_code=403, detail="CSRF token is invalid")


def flash_url(path: str, message: str, level: str = "success") -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode({'flash': message[:500], 'level': level})}"


def action_redirect(request: Request, path: str, message: str, level: str = "success") -> Response:
    url = flash_url(path, message, level)
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
