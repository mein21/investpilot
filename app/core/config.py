"""Application settings loaded from environment/.env with strict safety validations."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import ExecutionMode, RiskProfile, TradingMode

SQLITE_PREFIX = "sqlite:///"
SANDBOX_BASE_URL = "https://sandbox-invest-public-api.tinkoff.ru/rest"
PROD_BASE_URL = "https://invest-public-api.tinkoff.ru/rest"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "InvestPilot Local"
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    base_currency: str = "RUB"

    auth_enabled: bool = True
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    admin_password_hash: str | None = None
    session_secret: SecretStr | None = None
    session_ttl_hours: int = Field(default=12, ge=1, le=168)
    session_cookie_name: str = "investpilot_session"
    session_cookie_secure: bool = False
    account_cookie_name: str = "investpilot_account"
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=1, le=86400)

    database_url: str = "sqlite:///./data/investpilot.db"
    mock_state_path: Path | None = None
    screener_db_path: Path = Path("./data/screener.db")
    recommendations_screener_enabled: bool = True
    screener_rating_max_age_hours: int = Field(default=72, ge=1, le=720)

    tinvest_client: Literal["mock", "rest"] = "mock"
    tinvest_token: SecretStr | None = None
    tinvest_sandbox: bool = True
    tinvest_base_url: str = ""
    tinvest_app_name: str = "InvestPilotLocal"
    tinvest_proxy: str = ""
    tinvest_ca_bundle: str = ""
    tinvest_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    tinvest_max_retries: int = Field(default=2, ge=0, le=5)
    tinvest_operations_days: int = Field(default=365, ge=1, le=3650)
    universe_kinds: str = "shares,etfs,currencies"

    trading_mode: TradingMode = TradingMode.RECOMMEND
    real_trading_enabled: bool = False
    max_order_amount: Decimal = Decimal("50000")
    max_daily_order_amount: Decimal = Decimal("100000")
    max_daily_order_count: int = Field(default=10, ge=0)
    emergency_stop: bool = False
    quote_max_age_seconds: int = Field(default=300, ge=1)
    max_price_deviation_percent: Decimal = Decimal("5")
    commission_rate: Decimal = Decimal("0.003")
    min_trade_amount: Decimal = Decimal("500")
    min_expected_correction_percent: Decimal = Decimal("0.20")
    rebalance_threshold_percent: Decimal = Decimal("1.0")
    max_issuer_percent: Decimal = Decimal("25")
    max_recommendations: int = Field(default=25, ge=1, le=500)
    recommendation_ttl_minutes: int = Field(default=30, ge=1, le=1440)

    growth_analysis_enabled: bool = True
    growth_asset_classes: str = "STOCK,ETF"
    growth_history_days: int = Field(default=365, ge=120, le=730)
    growth_cache_hours: int = Field(default=12, ge=1, le=168)
    growth_metric_max_age_hours: int = Field(default=72, ge=1, le=720)
    growth_scan_limit: int = Field(default=100, ge=1, le=500)
    growth_scan_interval_minutes: int = Field(default=30, ge=5, le=1440)

    universe_auto_load_enabled: bool = True
    universe_auto_load_interval_hours: int = Field(default=24, ge=1, le=720)
    recommendations_auto_enabled: bool = True
    recommendations_auto_interval_minutes: int = Field(default=30, ge=5, le=1440)

    default_min_cash_reserve: Decimal = Decimal("50000")
    default_risk_profile: RiskProfile = RiskProfile.BALANCED
    goal_funding_window_days: int = Field(default=30, ge=1, le=365)

    scheduler_enabled: bool = True
    sync_interval_minutes: int = Field(default=5, ge=1, le=1440)
    market_data_interval_minutes: int = Field(default=60, ge=1, le=1440)
    snapshot_interval_minutes: int = Field(default=60, ge=5)
    backup_interval_hours: int = Field(default=24, ge=1)
    backup_dir: Path = Path("./backups")
    backup_keep: int = Field(default=7, ge=1, le=365)

    seed_demo_data: bool = False

    @field_validator(
        "tinvest_token",
        "admin_password",
        "session_secret",
        mode="before",
    )
    @classmethod
    def _empty_secret_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not (value.startswith("sqlite://") or value.startswith("postgresql")):
            raise ValueError("DATABASE_URL must be sqlite:// or postgresql+psycopg://")
        return value

    @field_validator("commission_rate")
    @classmethod
    def _validate_commission(cls, value: Decimal) -> Decimal:
        if value < 0 or value > Decimal("0.1"):
            raise ValueError("COMMISSION_RATE must be between 0 and 0.1")
        return value

    @field_validator(
        "max_price_deviation_percent",
        "min_expected_correction_percent",
        "rebalance_threshold_percent",
        "max_issuer_percent",
    )
    @classmethod
    def _validate_percent(cls, value: Decimal) -> Decimal:
        if value < 0 or value > Decimal("100"):
            raise ValueError("percentage must be between 0 and 100")
        return value

    @field_validator(
        "max_order_amount",
        "max_daily_order_amount",
        "min_trade_amount",
        "default_min_cash_reserve",
    )
    @classmethod
    def _validate_non_negative_money(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("money value must be non-negative")
        return value

    @field_validator("base_currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("BASE_CURRENCY must be a 3-letter ISO code")
        return normalized

    @model_validator(mode="after")
    def _validate_safety(self) -> Settings:
        if self.trading_mode is TradingMode.AUTO:
            raise ValueError("AUTO mode is a forbidden feature flag and cannot be enabled")
        if self.auth_enabled:
            if self.session_secret is None or len(self.session_secret.get_secret_value()) < 32:
                raise ValueError(
                    "SESSION_SECRET must contain at least 32 characters when AUTH_ENABLED=true"
                )
            if self.admin_password is None and not self.admin_password_hash:
                raise ValueError(
                    "ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required when AUTH_ENABLED=true"
                )
            if self.admin_password is not None and len(self.admin_password.get_secret_value()) < 10:
                raise ValueError("ADMIN_PASSWORD must contain at least 10 characters")
        return self

    @property
    def resolved_tinvest_base_url(self) -> str:
        if self.tinvest_base_url.strip():
            return self.tinvest_base_url.strip().rstrip("/")
        return SANDBOX_BASE_URL if self.tinvest_sandbox else PROD_BASE_URL

    @property
    def execution_mode(self) -> ExecutionMode:
        if self.trading_mode is TradingMode.PAPER:
            return ExecutionMode.PAPER
        if self.tinvest_sandbox:
            return ExecutionMode.SANDBOX
        return ExecutionMode.REAL

    @property
    def real_orders_armed(self) -> bool:
        return (
            self.real_trading_enabled
            and not self.tinvest_sandbox
            and self.trading_mode is TradingMode.CONFIRM
        )

    @property
    def tinvest_token_value(self) -> str | None:
        if self.tinvest_token is None:
            return None
        value = self.tinvest_token.get_secret_value().strip()
        return value or None

    @property
    def session_secret_value(self) -> str:
        if self.session_secret is None:
            return ""
        return self.session_secret.get_secret_value()

    @property
    def sqlite_path(self) -> Path | None:
        if not self.database_url.startswith(SQLITE_PREFIX):
            return None
        raw = self.database_url[len(SQLITE_PREFIX) :]
        if raw in ("", ":memory:") or raw.startswith("file:"):
            return None
        if raw.startswith("/"):
            return Path(raw)
        return Path(raw)

    @property
    def resolved_mock_state_path(self) -> Path | None:
        if self.mock_state_path is not None:
            return self.mock_state_path
        sqlite_path = self.sqlite_path
        if sqlite_path is None:
            return None
        return sqlite_path.with_name(f"{sqlite_path.stem}.mock-state.json")

    def ensure_directories(self) -> None:
        path = self.sqlite_path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def masking_secrets(self) -> list[str]:
        values: list[str] = []
        for secret in (self.tinvest_token, self.admin_password, self.session_secret):
            if secret is not None:
                raw = secret.get_secret_value()
                if raw:
                    values.append(raw)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
