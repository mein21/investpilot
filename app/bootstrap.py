"""Application bootstrap: client, admin user and demo data."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import AssetClass, RiskProfile
from app.core.security import hash_password
from app.integrations.tinvest.client import TInvestClient
from app.integrations.tinvest.locked import locked_client
from app.integrations.tinvest.mock import MockTInvestClient
from app.integrations.tinvest.rest import TInvestRestClient
from app.models.account import Account
from app.models.goal import Goal
from app.models.user import User
from app.services.goals import AllocationInput, GoalInput, GoalService


class BootstrapError(RuntimeError):
    pass


def build_tinvest_client(settings: Settings) -> TInvestClient:
    if settings.tinvest_client == "mock":
        return locked_client(
            MockTInvestClient(
                sandbox=settings.tinvest_sandbox,
                state_path=settings.resolved_mock_state_path,
            )
        )
    token = settings.tinvest_token_value
    if not token:
        raise BootstrapError("TINVEST_TOKEN is required when TINVEST_CLIENT=rest")
    return locked_client(
        TInvestRestClient(
            token,
            base_url=settings.resolved_tinvest_base_url,
            app_name=settings.tinvest_app_name,
            sandbox=settings.tinvest_sandbox,
            timeout_seconds=settings.tinvest_timeout_seconds,
            max_retries=settings.tinvest_max_retries,
            currency=settings.base_currency,
            proxy=settings.tinvest_proxy.strip() or None,
            verify=settings.tinvest_ca_bundle.strip() or True,
        )
    )


def ensure_admin(session: Session, settings: Settings) -> User | None:
    if not settings.auth_enabled:
        return None
    user = session.scalar(select(User).where(User.username == settings.admin_username))
    if user is not None:
        return user
    if settings.admin_password_hash:
        password_hash = settings.admin_password_hash
    else:
        assert settings.admin_password is not None
        password_hash = hash_password(settings.admin_password.get_secret_value())
    user = User(
        username=settings.admin_username,
        password_hash=password_hash,
        is_active=True,
        is_admin=True,
    )
    session.add(user)
    session.flush()
    return user


def ensure_demo_goal(session: Session, settings: Settings) -> Goal | None:
    if not settings.seed_demo_data:
        return None
    account = session.scalar(select(Account).order_by(Account.id))
    if account is None:
        return None
    goal = session.scalar(select(Goal).where(Goal.account_id == account.id))
    if goal is not None:
        return goal
    return GoalService().create_goal(
        session,
        GoalInput(
            name="Финансовая независимость",
            description="Демонстрационная цель локального приложения.",
            target_amount=Decimal("2000000"),
            currency=settings.base_currency,
            target_date=date.today() + timedelta(days=365),
            regular_contribution=Decimal("30000"),
            contribution_frequency="MONTHLY",
            min_cash_reserve=settings.default_min_cash_reserve,
            max_drawdown_percent=Decimal("20"),
            risk_profile=settings.default_risk_profile or RiskProfile.BALANCED,
        ),
        [
            AllocationInput(AssetClass.STOCK, Decimal("40"), Decimal("50"), Decimal("60")),
            AllocationInput(AssetClass.BOND, Decimal("10"), Decimal("20"), Decimal("25")),
            AllocationInput(AssetClass.ETF, Decimal("0"), Decimal("5"), Decimal("10")),
            AllocationInput(AssetClass.CURRENCY, Decimal("5"), Decimal("10"), Decimal("15")),
            AllocationInput(AssetClass.CASH, Decimal("10"), Decimal("15"), Decimal("20")),
        ],
        account_id=account.id,
    )
