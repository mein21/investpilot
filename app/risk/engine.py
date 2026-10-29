"""Pre-trade risk checks. No AI, no probabilistic scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import OrderSide, OrderStatus, TradingMode
from app.core.time import ensure_utc, is_stale, start_of_utc_day
from app.models.account import Account
from app.models.goal import Goal
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.position import Position
from app.models.snapshot import PortfolioSnapshot
from app.services.goals import GoalService
from app.services.portfolio import PortfolioService, PortfolioView
from app.services.risk_control import emergency_stop_enabled
from app.services.valuation import instrument_unit_value

ZERO = Decimal("0")
HUNDRED = Decimal("100")
PERCENT_TOLERANCE = Decimal("0.0001")
FULL_ACCESS = "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
DAILY_STATUSES = (
    OrderStatus.SENT,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.EXECUTED,
    OrderStatus.SUBMIT_UNKNOWN,
)


@dataclass(frozen=True)
class RuleCheck:
    code: str
    title: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    estimated_amount: Decimal
    commission: Decimal
    cash_after: Decimal
    projected_issuer_percent: Decimal
    projected_class_percent: Decimal
    triggered_rules: tuple[RuleCheck, ...]
    blocking_rules: tuple[RuleCheck, ...]
    warnings: tuple[str, ...] = ()


class RiskEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        portfolio_service: PortfolioService | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._portfolio = portfolio_service or PortfolioService(settings, clock=self._clock)
        self._goals = GoalService()

    def evaluate(
        self,
        session: Session,
        *,
        account_id: int,
        instrument: Instrument,
        side: OrderSide,
        lots: int,
        price: Decimal,
        quote_at: datetime | None,
        mode: TradingMode,
        goal: Goal | None = None,
        now: datetime | None = None,
        require_order_mode: bool = True,
        allow_class_below_min: bool = False,
    ) -> RiskDecision:
        if lots <= 0:
            raise ValueError("lots must be positive")
        if price <= ZERO:
            raise ValueError("price must be positive")
        at = ensure_utc(now or self._clock.now())
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"account {account_id} not found")
        portfolio = self._portfolio.build(session, account_id)
        position = session.scalar(
            select(Position).where(
                Position.account_id == account.id,
                Position.instrument_id == instrument.id,
            )
        )
        if goal is None:
            goal = session.scalar(
                select(Goal)
                .where(Goal.account_id == account.id, Goal.is_active.is_(True))
                .order_by(Goal.id)
            )

        quantity = Decimal(lots * max(1, instrument.lot))
        nkd = position.current_nkd if position is not None else ZERO
        unit_value = instrument_unit_value(
            instrument.asset_class,
            price,
            nominal=instrument.nominal,
            nkd=nkd,
        )
        estimated = unit_value * quantity
        commission = estimated * self._settings.commission_rate
        total_cost = estimated + commission if side is OrderSide.BUY else estimated - commission
        cash_available = self._cash_available(portfolio)
        cash_after = (
            cash_available - total_cost if side is OrderSide.BUY else cash_available + total_cost
        )
        daily_count, daily_amount = self._daily_totals(session, account, at)

        allocations = self._goals.allocation_map(goal) if goal is not None else {}
        class_bounds = allocations.get(instrument.asset_class)
        issuer_key = instrument.issuer_key or instrument.ticker.upper()
        issuer_value = next(
            (
                row.value_base
                for row in portfolio.issuer_concentration
                if row.issuer_key == issuer_key
            ),
            ZERO,
        )
        class_value = next(
            (
                row.value_base
                for row in portfolio.allocation
                if row.asset_class is instrument.asset_class
            ),
            ZERO,
        )
        total = portfolio.total_value
        issuer_after = (
            issuer_value + estimated
            if side is OrderSide.BUY
            else max(ZERO, issuer_value - estimated)
        )
        class_after = (
            class_value + estimated if side is OrderSide.BUY else max(ZERO, class_value - estimated)
        )
        projected_issuer = self._percent(issuer_after, total)
        projected_class = self._percent(class_after, total)
        warnings: list[str] = []

        checks: list[RuleCheck] = []

        def check(
            code: str,
            title: str,
            passed: bool,
            detail: str,
        ) -> None:
            checks.append(RuleCheck(code=code, title=title, passed=passed, detail=detail))

        emergency_stop = self._settings.emergency_stop or emergency_stop_enabled(
            session,
            default=False,
        )
        check(
            "EMERGENCY_STOP",
            "Аварийная остановка выключена",
            not emergency_stop,
            "EMERGENCY_STOP=true" if emergency_stop else "ok",
        )
        mode_ok = mode in (TradingMode.PAPER, TradingMode.CONFIRM)
        check(
            "TRADING_MODE",
            "Режим разрешает заявки",
            mode_ok or not require_order_mode,
            (
                f"mode={mode.value}"
                if require_order_mode
                else f"mode={mode.value}; recommendation only"
            ),
        )
        safety_ok = self._settings.tinvest_sandbox or self._settings.real_orders_armed
        check(
            "REAL_TRADING_SAFETY",
            "Реальная торговля безопасно настроена",
            safety_ok or not require_order_mode,
            (
                "sandbox"
                if self._settings.tinvest_sandbox
                else (
                    "реальная торговля должна быть разрешена"
                    if require_order_mode
                    else "recommendation only; real trading remains disarmed"
                )
            ),
        )
        access_ok = (
            self._settings.tinvest_sandbox
            or not require_order_mode
            or account.access_level == FULL_ACCESS
        )
        check(
            "ACCOUNT_TRADING_ACCESS",
            "Счёт имеет торговые права",
            access_ok,
            f"access_level={account.access_level or 'unknown'}",
        )
        fresh = quote_at is not None and not is_stale(
            quote_at,
            at,
            self._settings.quote_max_age_seconds,
        )
        check(
            "QUOTE_FRESH",
            "Котировка актуальна",
            fresh,
            f"quote_at={quote_at.isoformat() if quote_at else 'missing'}",
        )
        trading_ok = (
            instrument.buy_available and instrument.api_trade_available
            if side is OrderSide.BUY
            else instrument.sell_available and instrument.api_trade_available
        )
        check(
            "TRADING_AVAILABLE",
            "Инструмент доступен для сделки",
            trading_ok,
            f"status={instrument.trading_status or 'unknown'}",
        )
        check(
            "MIN_TRADE_AMOUNT",
            "Сумма не ниже минимальной",
            estimated >= self._settings.min_trade_amount,
            f"amount={estimated} min={self._settings.min_trade_amount}",
        )
        check(
            "MAX_ORDER_AMOUNT",
            "Сумма не выше лимита заявки",
            estimated <= self._settings.max_order_amount,
            f"amount={estimated} max={self._settings.max_order_amount}",
        )
        check(
            "MAX_DAILY_ORDER_COUNT",
            "Дневной лимит количества заявок",
            daily_count + 1 <= self._settings.max_daily_order_count,
            f"count={daily_count + 1} max={self._settings.max_daily_order_count}",
        )
        check(
            "MAX_DAILY_ORDER_AMOUNT",
            "Дневной лимит суммы заявок",
            daily_amount + estimated <= self._settings.max_daily_order_amount,
            f"amount={daily_amount + estimated} max={self._settings.max_daily_order_amount}",
        )

        if side is OrderSide.BUY and goal is not None:
            peak_value = session.scalar(
                select(func.max(PortfolioSnapshot.total_value)).where(
                    PortfolioSnapshot.account_id == account.id
                )
            )
            peak = max(peak_value or ZERO, total)
            drawdown = self._percent(peak - total, peak)
            check(
                "MAX_DRAWDOWN",
                "Просадка не выше допустимой",
                drawdown <= goal.max_drawdown_percent + PERCENT_TOLERANCE,
                f"drawdown={drawdown} max={goal.max_drawdown_percent}",
            )

        if side is OrderSide.SELL:
            available_quantity = (
                max(ZERO, position.quantity - position.blocked_quantity)
                if position is not None
                else ZERO
            )
            check(
                "POSITION_AVAILABLE",
                "Достаточно инструмента для продажи",
                available_quantity >= quantity,
                f"available={available_quantity} required={quantity}",
            )
        else:
            reserve = (
                goal.min_cash_reserve
                if goal is not None
                else self._settings.default_min_cash_reserve
            )
            check(
                "CASH_RESERVE",
                "Денежный резерв сохраняется",
                cash_available >= total_cost + reserve,
                f"cash={cash_available} cost={total_cost} reserve={reserve}",
            )

        issuer_limit_ok = (
            side is OrderSide.SELL
            or projected_issuer <= self._settings.max_issuer_percent + PERCENT_TOLERANCE
        )
        check(
            "MAX_ISSUER_PERCENT",
            "Лимит концентрации эмитента",
            issuer_limit_ok,
            (
                f"after={projected_issuer} max={self._settings.max_issuer_percent}"
                if side is OrderSide.BUY
                else f"after={projected_issuer}; sale reduces concentration"
            ),
        )
        if class_bounds is not None:
            minimum, _target, maximum = class_bounds
            if side is OrderSide.BUY:
                class_ok = projected_class <= maximum + PERCENT_TOLERANCE
                detail = f"after={projected_class} max={maximum}"
            else:
                class_ok = projected_class >= minimum - PERCENT_TOLERANCE
                if not class_ok and allow_class_below_min:
                    class_ok = True
                    warnings.append(
                        "принудительный выход опускает класс ниже минимальной доли; "
                        "восстановите аллокацию следующей покупкой"
                    )
                detail = f"after={projected_class} min={minimum}"
                if allow_class_below_min:
                    detail += "; forced exit allowed"
            check(
                "CLASS_ALLOCATION_LIMIT",
                "Лимит класса активов",
                class_ok,
                detail,
            )
        else:
            warnings.append("нет активной цели с долями; лимиты классов не применяются")

        if instrument.last_price is not None and instrument.last_price > ZERO:
            deviation = abs(price - instrument.last_price) / instrument.last_price * HUNDRED
            check(
                "PRICE_DEVIATION",
                "Цена не отклоняется от рынка",
                deviation <= self._settings.max_price_deviation_percent,
                f"deviation={deviation} max={self._settings.max_price_deviation_percent}",
            )

        blocking = tuple(item for item in checks if not item.passed)
        return RiskDecision(
            allowed=not blocking,
            estimated_amount=estimated,
            commission=commission,
            cash_after=cash_after,
            projected_issuer_percent=projected_issuer,
            projected_class_percent=projected_class,
            triggered_rules=tuple(checks),
            blocking_rules=blocking,
            warnings=tuple(warnings),
        )

    def _cash_available(self, portfolio: PortfolioView) -> Decimal:
        row = next(
            (item for item in portfolio.cash if item.currency == portfolio.base_currency),
            None,
        )
        return row.free_value_base if row is not None else ZERO

    def _daily_totals(
        self,
        session: Session,
        account: Account,
        at: datetime,
    ) -> tuple[int, Decimal]:
        orders = list(
            session.scalars(
                select(Order).where(
                    Order.account_id == account.id,
                    Order.status.in_(DAILY_STATUSES),
                    Order.sent_at >= start_of_utc_day(at),
                )
            )
        )
        return len(orders), sum(
            (order.estimated_amount_base for order in orders),
            ZERO,
        )

    @staticmethod
    def _percent(value: Decimal, total: Decimal) -> Decimal:
        if total <= ZERO:
            return ZERO
        return value / total * HUNDRED
