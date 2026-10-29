"""Safe approval, submission and reconciliation of broker orders."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import (
    OrderSide,
    OrderStatus,
    RecommendationAction,
    RecommendationStatus,
    Severity,
    TradingMode,
)
from app.core.time import ensure_utc
from app.integrations.tinvest.client import TInvestClient
from app.integrations.tinvest.exceptions import (
    TInvestError,
    TInvestOrderOutcomeUnknown,
)
from app.integrations.tinvest.types import (
    BrokerOrderStatus,
    OrderExecutionInfo,
    OrderRequest,
    OrderResult,
)
from app.models.account import Account
from app.models.goal import Goal
from app.models.order import Order, OrderExecution
from app.models.recommendation import Recommendation
from app.notifications import create_notification
from app.risk.engine import RiskDecision, RiskEngine
from app.services.valuation import instrument_order_price

ZERO = Decimal("0")
BROKER_STATUS_MAP: dict[BrokerOrderStatus, OrderStatus] = {
    BrokerOrderStatus.FILL: OrderStatus.EXECUTED,
    BrokerOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    BrokerOrderStatus.NEW: OrderStatus.SENT,
    BrokerOrderStatus.REJECTED: OrderStatus.REJECTED,
    BrokerOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    BrokerOrderStatus.UNKNOWN: OrderStatus.SUBMIT_UNKNOWN,
}


class OrderError(RuntimeError):
    pass


class OrderBlockedError(OrderError):
    def __init__(self, message: str, decision: RiskDecision) -> None:
        super().__init__(message)
        self.decision = decision


class OrderService:
    def __init__(
        self,
        client: TInvestClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock or SystemClock()
        self._risk = risk_engine or RiskEngine(settings, clock=self._clock)

    def approve(
        self,
        session: Session,
        recommendation_id: int,
        *,
        user: str,
        now: datetime | None = None,
    ) -> Order:
        at = ensure_utc(now or self._clock.now())
        self._assert_order_mode()
        recommendation = session.get(Recommendation, recommendation_id)
        if recommendation is None:
            raise OrderError(f"рекомендация {recommendation_id} не найдена")
        if recommendation.status is not RecommendationStatus.NEW:
            raise OrderError(f"рекомендация уже обработана (статус={recommendation.status.value})")
        if recommendation.expires_at <= at:
            recommendation.status = RecommendationStatus.EXPIRED
            raise OrderError("срок действия рекомендации истёк")
        if recommendation.instrument_id is None or recommendation.instrument is None:
            raise OrderError("у рекомендации нет инструмента")
        if recommendation.action not in (
            RecommendationAction.BUY,
            RecommendationAction.SELL,
        ):
            raise OrderError(f"действие {recommendation.action.value} не может быть исполнено")
        if recommendation.lots <= 0:
            raise OrderError("у рекомендации нет положительного количества лотов")
        existing = session.scalar(select(Order).where(Order.recommendation_id == recommendation.id))
        if existing is not None:
            raise OrderError(f"по рекомендации {recommendation.id} уже создана заявка")
        decision = self._revalidate(session, recommendation, at)
        if not decision.allowed:
            raise OrderBlockedError(
                "рекомендация заблокирована текущими риск-проверками",
                decision,
            )

        limit_price = instrument_order_price(
            recommendation.instrument.asset_class,
            recommendation.quote_price,
            nominal=recommendation.instrument.nominal,
        )
        order = Order(
            recommendation_id=recommendation.id,
            account_id=recommendation.account_id,
            instrument_id=recommendation.instrument_id,
            client_order_id=str(uuid.uuid4()),
            side=(
                OrderSide.BUY
                if recommendation.action is RecommendationAction.BUY
                else OrderSide.SELL
            ),
            status=OrderStatus.APPROVED,
            execution_mode=self._settings.execution_mode,
            is_sandbox=self._settings.tinvest_sandbox,
            lots=recommendation.lots,
            lot_size=recommendation.lot_size,
            limit_price=limit_price,
            currency=recommendation.currency,
            base_currency=self._settings.base_currency,
            estimated_amount=recommendation.estimated_amount,
            estimated_amount_base=recommendation.estimated_amount,
            commission_estimate=recommendation.commission_estimate,
            commission_estimate_base=recommendation.commission_estimate,
            broker_status="",
            request_payload={
                "recommendation_fingerprint": recommendation.fingerprint,
                "recommendation_action": recommendation.action.value,
                "approved_price": str(limit_price),
            },
            response_payload={},
            approved_by=user,
            approved_at=at,
            last_checked_at=at,
            warning_message=("; ".join(decision.warnings) if decision.warnings else None),
        )
        recommendation.status = RecommendationStatus.APPROVED
        recommendation.approved_at = at
        recommendation.decided_by = user
        session.add(order)
        session.flush()
        return order

    def send(
        self,
        session: Session,
        order_id: int,
        *,
        user: str,
        now: datetime | None = None,
    ) -> Order:
        at = ensure_utc(now or self._clock.now())
        self._assert_order_mode()
        order = session.get(Order, order_id)
        if order is None:
            raise OrderError(f"заявка {order_id} не найдена")
        if order.status is not OrderStatus.APPROVED:
            raise OrderError(f"заявка не одобрена (статус={order.status.value})")
        recommendation = order.recommendation
        if recommendation is None:
            raise OrderError("у заявки нет связанной рекомендации")
        if recommendation.status is not RecommendationStatus.APPROVED:
            raise OrderError(
                f"связанная рекомендация не одобрена (статус={recommendation.status.value})"
            )
        self._assert_environment(order)
        decision = self._revalidate(session, recommendation, at)
        if not decision.allowed:
            raise OrderBlockedError(
                "заявка заблокирована текущими риск-проверками",
                decision,
            )
        if not self._settings.trading_mode.can_create_orders:
            raise OrderError("режим торговли не разрешает отправку заявок")

        order.status = OrderStatus.SENT
        order.sent_at = at
        order.sent_by = user
        order.last_checked_at = at
        recommendation.status = RecommendationStatus.SENT
        session.flush()

        request = OrderRequest(
            account_id=order.account.tinvest_account_id,
            instrument_id=order.instrument.instrument_uid,
            side=order.side,
            lots=order.lots,
            order_type=order.order_type,
            client_order_id=order.client_order_id,
            limit_price=order.limit_price,
        )
        try:
            result = self._client.post_order(request)
        except TInvestOrderOutcomeUnknown as exc:
            order.status = OrderStatus.SUBMIT_UNKNOWN
            order.broker_status = "OUTCOME_UNKNOWN"
            order.warning_message = str(exc)
            order.last_checked_at = at
            recommendation.status = RecommendationStatus.SENT
            self._notify_order(
                session,
                order,
                title="Исход заявки неизвестен",
                severity=Severity.WARNING,
                at=at,
            )
            session.flush()
            return order
        except TInvestError as exc:
            order.status = OrderStatus.FAILED
            order.error_message = str(exc)
            order.completed_at = at
            order.last_checked_at = at
            recommendation.status = RecommendationStatus.FAILED
            self._notify_order(
                session,
                order,
                title="Заявка не отправлена",
                severity=Severity.ERROR,
                at=at,
            )
            session.flush()
            return order

        self._apply_result(order, result, at)
        severity = (
            Severity.INFO
            if order.status is OrderStatus.EXECUTED
            else Severity.WARNING
            if order.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMIT_UNKNOWN)
            else Severity.ERROR
            if order.status in (OrderStatus.FAILED, OrderStatus.REJECTED)
            else Severity.INFO
        )
        self._notify_order(
            session,
            order,
            title=f"Заявка: {order.status.value}",
            severity=severity,
            at=at,
        )
        session.flush()
        return order

    def reconcile(
        self,
        session: Session,
        order_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        at = ensure_utc(now or self._clock.now())
        order = session.get(Order, order_id)
        if order is None:
            raise OrderError(f"заявка {order_id} не найдена")
        result: OrderResult | None = None
        try:
            if order.broker_order_id:
                result = self._client.get_order_state(
                    order.account.tinvest_account_id,
                    order.broker_order_id,
                    lot_size=order.lot_size,
                )
            elif order.status is OrderStatus.SUBMIT_UNKNOWN:
                result = self._find_unknown_order(order)
                if result is None:
                    order.warning_message = (
                        "order outcome is still unknown; broker has no matching order id"
                    )
                    order.last_checked_at = at
                    session.flush()
                    return order
            else:
                raise OrderError("у заявки нет брокерского ID для сверки")
        except TInvestError as exc:
            order.warning_message = f"сверка не удалась: {exc}"
            order.last_checked_at = at
            session.flush()
            return order
        assert result is not None
        self._apply_result(order, result, at)
        session.flush()
        return order

    def cancel(
        self,
        session: Session,
        order_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        at = ensure_utc(now or self._clock.now())
        order = session.get(Order, order_id)
        if order is None:
            raise OrderError(f"заявка {order_id} не найдена")
        if not order.broker_order_id:
            raise OrderError("нельзя отменить заявку без брокерского ID")
        if order.status not in (OrderStatus.SENT, OrderStatus.PARTIALLY_FILLED):
            raise OrderError(f"заявку нельзя отменить в статусе {order.status.value}")
        try:
            self._client.cancel_order(
                order.account.tinvest_account_id,
                order.broker_order_id,
            )
        except TInvestError as exc:
            order.warning_message = f"отмена не удалась: {exc}"
            order.last_checked_at = at
            session.flush()
            return order
        return self.reconcile(session, order.id, now=at)

    def _revalidate(
        self,
        session: Session,
        recommendation: Recommendation,
        at: datetime,
    ) -> RiskDecision:
        instrument = recommendation.instrument
        if instrument is None:
            raise OrderError("инструмент рекомендации отсутствует")
        side = (
            OrderSide.BUY if recommendation.action is RecommendationAction.BUY else OrderSide.SELL
        )
        goal: Goal | None = recommendation.goal
        return self._risk.evaluate(
            session,
            account_id=recommendation.account_id,
            instrument=instrument,
            side=side,
            lots=recommendation.lots,
            price=recommendation.quote_price,
            quote_at=instrument.last_price_at,
            mode=self._settings.trading_mode,
            goal=goal,
            now=at,
            allow_class_below_min=self._is_forced_exit(recommendation),
        )

    @staticmethod
    def _notify_order(
        session: Session,
        order: Order,
        *,
        title: str,
        severity: Severity,
        at: datetime,
    ) -> None:
        instrument = order.instrument
        ticker = instrument.ticker if instrument is not None else "—"
        create_notification(
            session,
            account_id=order.account_id,
            kind="order",
            severity=severity,
            title=title,
            message=(
                f"{ticker}: {order.side.value} {order.lots} лот(ов), "
                f"сумма {order.estimated_amount} {order.currency}, "
                f"статус {order.status.value}."
            ),
            entity_type="order",
            entity_id=str(order.id),
            at=at,
        )

    @staticmethod
    def _is_forced_exit(recommendation: Recommendation) -> bool:
        for rule in recommendation.triggered_rules or ():
            if isinstance(rule, dict) and rule.get("code") == "MANDATORY_EXIT":
                return True
        return False

    def _apply_result(self, order: Order, result: OrderResult, at: datetime) -> None:
        local_status = BROKER_STATUS_MAP.get(result.status, OrderStatus.SUBMIT_UNKNOWN)
        order.status = local_status
        order.broker_order_id = result.broker_order_id or order.broker_order_id
        order.broker_status = result.raw_status
        order.lots_executed = result.lots_executed
        order.executed_commission = result.executed_commission
        order.last_checked_at = at
        order.response_payload = {
            "status": result.status.value,
            "raw_status": result.raw_status,
            "lots_requested": result.lots_requested,
            "lots_executed": result.lots_executed,
            "executed_order_price": str(result.executed_order_price),
            "total_order_amount": str(result.total_order_amount),
            "executed_commission": str(result.executed_commission),
            "message": result.message,
        }
        if result.message:
            order.error_message = (
                result.message
                if local_status in (OrderStatus.REJECTED, OrderStatus.FAILED)
                else order.error_message
            )
        if local_status is OrderStatus.SUBMIT_UNKNOWN:
            order.warning_message = (
                "брокер вернул неизвестный статус заявки; не отправляйте повторно"
            )

        order.executions.clear()
        executions = list(result.executions)
        if not executions and result.lots_executed > 0:
            quantity = Decimal(result.lots_executed * max(1, order.lot_size))
            amount = (
                result.executed_order_price * quantity
                if result.executed_order_price > ZERO
                else result.total_order_amount
            )
            executions = [
                OrderExecutionInfo(
                    trade_id=None,
                    lots=result.lots_executed,
                    lot_size=max(1, order.lot_size),
                    price=result.executed_order_price,
                    amount=amount,
                    commission=result.executed_commission,
                    executed_at=result.order_date or at,
                    is_partial=local_status is OrderStatus.PARTIALLY_FILLED,
                )
            ]
        executed_amount = ZERO
        weighted_price = ZERO
        executed_quantity = Decimal("0")
        for execution in executions:
            quantity = Decimal(execution.lots * max(1, execution.lot_size))
            order.executions.append(
                OrderExecution(
                    trade_id=execution.trade_id,
                    lots=execution.lots,
                    lot_size=max(1, execution.lot_size),
                    quantity=int(quantity),
                    price=execution.price,
                    amount=execution.amount,
                    commission=execution.commission,
                    is_partial=execution.is_partial,
                    executed_at=execution.executed_at,
                )
            )
            executed_amount += execution.amount
            weighted_price += execution.price * quantity
            executed_quantity += quantity
        order.executed_amount = executed_amount
        order.average_executed_price = (
            weighted_price / executed_quantity if executed_quantity > ZERO else ZERO
        )

        recommendation = order.recommendation
        if recommendation is not None:
            if local_status is OrderStatus.EXECUTED:
                recommendation.status = RecommendationStatus.EXECUTED
            elif local_status in (
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
                OrderStatus.CANCELLED,
            ):
                recommendation.status = RecommendationStatus.FAILED
            elif local_status in (
                OrderStatus.SENT,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.SUBMIT_UNKNOWN,
            ):
                recommendation.status = RecommendationStatus.SENT
        if local_status in (
            OrderStatus.EXECUTED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        ):
            order.completed_at = at

    def _find_unknown_order(self, order: Order) -> OrderResult | None:
        account: Account = order.account
        for result in self._client.get_orders(account.tinvest_account_id):
            if result.client_order_id == order.client_order_id:
                return result
        return None

    def _assert_order_mode(self) -> None:
        if not self._settings.trading_mode.can_create_orders:
            raise OrderError(
                "режим торговли не разрешает создание заявок; используйте PAPER или CONFIRM"
            )
        if self._settings.trading_mode is TradingMode.PAPER and not self._client.is_mock:
            raise OrderError("режим PAPER требует детерминированный mock-клиент")

    def _assert_environment(self, order: Order) -> None:
        if order.execution_mode is not self._settings.execution_mode:
            raise OrderError(
                "среда исполнения изменилась после одобрения; требуется повторное одобрение"
            )
        if order.is_sandbox != self._settings.tinvest_sandbox:
            raise OrderError(
                "настройка sandbox изменилась после одобрения; требуется повторное одобрение"
            )
