"""Deterministic recommendation generation from goal allocation gaps."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.enums import (
    AssetClass,
    OrderSide,
    OrderType,
    RecommendationAction,
    RecommendationStatus,
    TradingMode,
)
from app.core.money import lots_ceil, lots_floor, quantize_money, quantize_percent
from app.core.time import age_seconds, ensure_utc, is_stale
from app.models.account import Account
from app.models.goal import Goal
from app.models.instrument import Instrument
from app.models.position import Position
from app.models.recommendation import Recommendation
from app.risk.engine import RiskEngine
from app.rules.growth import GrowthScore
from app.rules.scoring import score_buy_candidate, score_sell_candidate
from app.screener.domain import ScreenerRating
from app.services.goals import GoalService
from app.services.growth import load_growth_scores
from app.services.portfolio import PortfolioService, PortfolioView, PositionView
from app.services.screener_ratings import ScreenerRatingSet, load_screener_ratings
from app.services.valuation import instrument_unit_value

ZERO = Decimal("0")
HUNDRED = Decimal("100")
BaseCandidate = tuple[
    Position | None,
    Instrument,
    Decimal,
    datetime | None,
    Decimal,
    Decimal,
    Decimal,
    ScreenerRating | None,
]


def _fmt_money(value: Decimal, currency: str) -> str:
    text = f"{quantize_money(value):,.2f}".replace(",", "\u00a0").replace(".", ",")
    return f"{text} {currency}"


def _fmt_percent(value: Decimal) -> str:
    return f"{quantize_percent(value):.2f}".replace(".", ",")


def _fmt_signed_money(value: Decimal, currency: str) -> str:
    if value > ZERO:
        return "+" + _fmt_money(value, currency)
    if value < ZERO:
        return "−" + _fmt_money(-value, currency)
    return _fmt_money(ZERO, currency)


@dataclass(frozen=True)
class RecommendationRun:
    account_id: int
    goal: Goal | None
    portfolio: PortfolioView
    recommendations: tuple[Recommendation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _SellTrigger:
    asset_class: AssetClass
    budget: Decimal
    trigger_text: str
    issuer_key: str | None = None
    instrument_id: int | None = None
    force_exit: bool = False
    trigger_code: str | None = None


@dataclass(frozen=True)
class _Candidate:
    position: Position | None
    instrument: Instrument
    score: Decimal
    breakdown: list[dict[str, object]]
    current_weight: Decimal
    quantity: Decimal
    price: Decimal
    price_at: datetime | None
    nkd: Decimal
    screener: ScreenerRating | None = None


class RecommendationService:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        portfolio_service: PortfolioService | None = None,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._portfolio = portfolio_service or PortfolioService(settings, clock=self._clock)
        self._risk = risk_engine or RiskEngine(
            settings,
            clock=self._clock,
            portfolio_service=self._portfolio,
        )
        self._goals = GoalService()

    def generate(
        self,
        session: Session,
        *,
        account_id: int,
        goal_id: int | None = None,
        include_hold: bool = True,
        now: datetime | None = None,
    ) -> RecommendationRun:
        at = ensure_utc(now or self._clock.now())
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"account {account_id} not found")
        portfolio = self._portfolio.build(session, account.id)
        warnings: list[str] = []

        if self._settings.trading_mode is TradingMode.OBSERVE:
            warnings.append("режим OBSERVE: доступно только чтение, рекомендации не формируются")
            return RecommendationRun(account.id, None, portfolio, (), tuple(warnings))

        self._expire_existing(session, account.id)

        goal = (
            session.get(Goal, goal_id)
            if goal_id is not None
            else session.scalar(
                select(Goal)
                .where(Goal.account_id == account.id, Goal.is_active.is_(True))
                .order_by(Goal.id)
            )
        )
        if goal is None:
            warnings.append("нет активной цели; рекомендации не формируются")
            return RecommendationRun(account.id, None, portfolio, (), tuple(warnings))
        allocations = self._goals.allocation_map(goal)
        if not allocations:
            warnings.append("у цели не заданы целевые доли")
            return RecommendationRun(account.id, goal, portfolio, (), tuple(warnings))

        positions = list(
            session.scalars(
                select(Position)
                .options(selectinload(Position.instrument))
                .where(Position.account_id == account.id, Position.quantity > ZERO)
                .order_by(Position.id)
            )
        )
        position_views = {row.instrument_id: row for row in portfolio.positions}
        percents = {row.asset_class: row.weight_percent for row in portfolio.allocation}
        issuer_weights = {
            row.issuer_key: row.weight_percent for row in portfolio.issuer_concentration
        }
        cash = self._cash_available(portfolio)
        reserve = goal.min_cash_reserve or self._settings.default_min_cash_reserve
        planned_cash = cash
        threshold = max(self._settings.rebalance_threshold_percent, Decimal("0.01"))
        total = portfolio.total_value
        screener = load_screener_ratings(self._settings, now=at)
        warnings.extend(screener.warnings)
        input_hash = self._input_hash(
            portfolio,
            goal,
            allocations,
            positions,
            threshold,
            screener=screener,
        )
        created: list[Recommendation] = []
        selected: set[int] = set()
        actioned_classes: set[AssetClass] = set()

        buys: list[tuple[AssetClass, Decimal]] = []
        for asset_class, (_minimum, target, _maximum) in allocations.items():
            if asset_class is AssetClass.CASH:
                continue
            current = percents.get(asset_class, ZERO)
            gap = target - current
            if gap > threshold:
                buys.append((asset_class, gap))

        issuer_positions: dict[str, list[Position]] = {}
        for position in positions:
            instrument = position.instrument
            issuer = instrument.issuer_key or instrument.ticker.upper()
            issuer_positions.setdefault(issuer, []).append(position)

        sell_triggers: list[_SellTrigger] = []
        for position in positions:
            instrument = position.instrument
            if not instrument.is_excluded:
                continue
            view = position_views.get(instrument.id)
            if view is None:
                continue
            sell_triggers.append(
                _SellTrigger(
                    asset_class=instrument.asset_class,
                    budget=min(self._settings.max_order_amount, view.value_base),
                    trigger_text=(
                        f"инструмент исключён пользователем; продажа до нуля, "
                        f"текущая стоимость {_fmt_money(view.value_base, portfolio.base_currency)}"
                    ),
                    instrument_id=instrument.id,
                )
            )

        for position in positions:
            instrument = position.instrument
            if instrument.is_excluded:
                continue
            view = position_views.get(instrument.id)
            if view is None:
                continue
            return_percent = self._position_return_percent(position, instrument)
            if return_percent is None:
                continue
            trigger_code: str | None = None
            trigger_text = ""
            if (
                goal.stop_loss_enabled
                and goal.stop_loss_percent is not None
                and return_percent <= -goal.stop_loss_percent
            ):
                trigger_code = "STOP_LOSS"
                trigger_text = (
                    f"стоп-лосс: доходность позиции "
                    f"{_fmt_percent(return_percent)}% ниже порога "
                    f"−{_fmt_percent(goal.stop_loss_percent)}%; продажа для ограничения убытка"
                )
            elif (
                goal.take_profit_enabled
                and goal.take_profit_percent is not None
                and return_percent >= goal.take_profit_percent
            ):
                trigger_code = "TAKE_PROFIT"
                trigger_text = (
                    f"тейк-профит: доходность позиции "
                    f"+{_fmt_percent(return_percent)}% выше порога "
                    f"+{_fmt_percent(goal.take_profit_percent)}%; продажа для фиксации прибыли"
                )
            if trigger_code is None:
                continue
            sell_triggers.append(
                _SellTrigger(
                    asset_class=instrument.asset_class,
                    budget=min(self._settings.max_order_amount, view.value_base),
                    trigger_text=trigger_text,
                    instrument_id=instrument.id,
                    force_exit=True,
                    trigger_code=trigger_code,
                )
            )

        funding_trigger = self._goal_funding_trigger(
            goal=goal,
            portfolio=portfolio,
            positions=positions,
            position_views=position_views,
            percents=percents,
            allocations=allocations,
            cash=cash,
            at=at,
            warnings=warnings,
        )
        if funding_trigger is not None:
            sell_triggers.append(funding_trigger)

        for issuer, issuer_weight in issuer_weights.items():
            if issuer_weight <= self._settings.max_issuer_percent:
                continue
            issuer_value = next(
                (
                    row.value_base
                    for row in portfolio.issuer_concentration
                    if row.issuer_key == issuer
                ),
                ZERO,
            )
            excess = issuer_value - total * self._settings.max_issuer_percent / HUNDRED
            if excess < self._settings.min_trade_amount:
                continue
            asset_class = issuer_positions[issuer][0].instrument.asset_class
            sell_triggers.append(
                _SellTrigger(
                    asset_class=asset_class,
                    budget=min(self._settings.max_order_amount, excess),
                    trigger_text=(
                        f"концентрация эмитента {issuer} {_fmt_percent(issuer_weight)}% "
                        f"выше лимита {_fmt_percent(self._settings.max_issuer_percent)}%; "
                        "продажа для снижения концентрации"
                    ),
                    issuer_key=issuer,
                )
            )

        for asset_class, (_minimum, target, maximum) in allocations.items():
            if asset_class is AssetClass.CASH:
                continue
            current = percents.get(asset_class, ZERO)
            if current <= maximum:
                continue
            excess = total * (current - target) / HUNDRED
            if excess < self._settings.min_trade_amount:
                continue
            sell_triggers.append(
                _SellTrigger(
                    asset_class=asset_class,
                    budget=min(self._settings.max_order_amount, excess),
                    trigger_text=(
                        f"доля класса {_fmt_percent(current)}% выше max "
                        f"{_fmt_percent(maximum)}%; продажа к целевой доле"
                    ),
                )
            )

        for trigger in sell_triggers:
            if trigger.budget < self._settings.min_trade_amount:
                warnings.append(f"{trigger.asset_class.value}: бюджет продажи слишком мал")
                continue
            allocation = allocations.get(trigger.asset_class)
            target = allocation[1] if allocation is not None else ZERO
            current_class = percents.get(trigger.asset_class, ZERO)
            driver = trigger.budget / total * HUNDRED if total > ZERO else ZERO
            candidates = self._sell_candidates(
                positions,
                position_views,
                issuer_weights,
                trigger.asset_class,
                driver,
                threshold,
                portfolio,
                goal,
                issuer_key=trigger.issuer_key,
                instrument_id=trigger.instrument_id,
            )
            candidates = [item for item in candidates if item.instrument.id not in selected]
            recommendation = self._build_action(
                session,
                account=account,
                goal=goal,
                portfolio=portfolio,
                asset_class=trigger.asset_class,
                candidates=candidates,
                side=OrderSide.SELL,
                budget=trigger.budget,
                current_class_weight=current_class,
                target_class_weight=target,
                gap=target - current_class,
                trigger_text=trigger.trigger_text,
                at=at,
                input_hash=input_hash,
                warnings=warnings,
                selected=selected,
                force_exit=trigger.force_exit,
                trigger_code=trigger.trigger_code,
            )
            if recommendation is not None:
                created.append(recommendation)
                actioned_classes.add(trigger.asset_class)
                planned_cash += recommendation.estimated_amount - recommendation.commission_estimate
                if recommendation.instrument_id is not None:
                    selected.add(recommendation.instrument_id)

        required_cash = reserve
        days_left = (goal.target_date - at.date()).days
        if (
            goal.currency == portfolio.base_currency
            and days_left <= self._settings.goal_funding_window_days
            and portfolio.total_value >= goal.target_amount
            and cash < goal.target_amount
        ):
            required_cash = max(reserve, goal.target_amount)
        for asset_class, gap in buys:
            target = allocations[asset_class][1]
            available_cash = max(ZERO, planned_cash - required_cash)
            budget = min(
                self._settings.max_order_amount,
                total * gap / HUNDRED,
                available_cash,
            )
            if budget < self._settings.min_trade_amount:
                warnings.append(
                    f"{asset_class.value}: не хватает свободных денег для покупки после резерва"
                )
                continue
            candidates = self._buy_candidates(
                session,
                positions,
                position_views,
                issuer_weights,
                asset_class,
                gap,
                threshold,
                portfolio,
                goal,
                at,
                screener=screener,
                warnings=warnings,
            )
            recommendation = self._build_action(
                session,
                account=account,
                goal=goal,
                portfolio=portfolio,
                asset_class=asset_class,
                candidates=candidates,
                side=OrderSide.BUY,
                budget=budget,
                current_class_weight=percents.get(asset_class, ZERO),
                target_class_weight=target,
                gap=gap,
                trigger_text="класс ниже целевой доли; покупка для исправления аллокации",
                at=at,
                input_hash=input_hash,
                warnings=warnings,
                selected=selected,
            )
            if recommendation is not None:
                created.append(recommendation)
                actioned_classes.add(asset_class)
                planned_cash -= recommendation.estimated_amount + recommendation.commission_estimate
                if recommendation.instrument_id is not None:
                    selected.add(recommendation.instrument_id)

        self._add_rebalance_plan(
            session,
            account=account,
            goal=goal,
            portfolio=portfolio,
            created=created,
            at=at,
            input_hash=input_hash,
        )

        if include_hold:
            for position in positions:
                if position.instrument_id in selected:
                    continue
                instrument = position.instrument
                if instrument.is_excluded:
                    continue
                view = position_views.get(instrument.id)
                if view is None:
                    continue
                bounds = allocations.get(instrument.asset_class)
                if bounds is None:
                    continue
                target = bounds[1]
                current_class = percents.get(instrument.asset_class, ZERO)
                gap = target - current_class
                if instrument.asset_class in actioned_classes:
                    hold_reason = "по классу выбрана другая бумага"
                    rule_title = "Действие по классу уже сформировано"
                elif is_stale(
                    view.current_price_at,
                    portfolio.as_of,
                    self._settings.quote_max_age_seconds,
                ):
                    hold_reason = "котировка устарела — синхронизируйте данные"
                    rule_title = "Нет свежей котировки"
                elif abs(gap) < threshold:
                    hold_reason = "доля класса находится в пределах порога"
                    rule_title = "Доля класса в допустимом коридоре"
                else:
                    hold_reason = (
                        "допустимая заявка не сформирована из-за лимитов, "
                        "размера лота или свободных средств"
                    )
                    rule_title = "Действие не сформировано"
                unit_price = instrument_unit_value(
                    instrument.asset_class,
                    view.current_price,
                    nominal=instrument.nominal,
                    nkd=view.current_nkd,
                )
                stale_note = (
                    ["котировка устарела или не имеет даты"]
                    if is_stale(
                        view.current_price_at,
                        portfolio.as_of,
                        self._settings.quote_max_age_seconds,
                    )
                    else []
                )
                created.append(
                    self._persist(
                        session,
                        account=account,
                        goal=goal,
                        portfolio=portfolio,
                        instrument=instrument,
                        action=RecommendationAction.HOLD,
                        lots=0,
                        quote_price=view.current_price,
                        price_per_lot=unit_price * max(1, instrument.lot),
                        estimated_amount=ZERO,
                        commission=ZERO,
                        current_weight=view.weight_percent,
                        target_weight=target,
                        expected_weight=view.weight_percent,
                        expected_class_weight=current_class,
                        reason=(
                            f"{instrument.ticker}: класс «{instrument.asset_class.label_ru}» "
                            f"занимает {_fmt_percent(current_class)}% при цели "
                            f"{_fmt_percent(target)}%; {hold_reason}"
                        ),
                        triggered_rules=[
                            {
                                "code": "HOLD_NO_ACTION",
                                "title": rule_title,
                                "passed": True,
                                "detail": (
                                    f"current={quantize_percent(current_class)} "
                                    f"target={quantize_percent(target)} "
                                    f"threshold={threshold}"
                                ),
                            }
                        ],
                        blocking_rules=[],
                        risks=stale_note,
                        score=None,
                        breakdown=[],
                        at=at,
                        input_hash=input_hash,
                    )
                )

        if len(created) > self._settings.max_recommendations:
            warnings.append(
                "количество рекомендаций ограничено настройкой: "
                f"{self._settings.max_recommendations}"
            )
            created = created[: self._settings.max_recommendations]
        session.flush()
        return RecommendationRun(
            account_id=account.id,
            goal=goal,
            portfolio=portfolio,
            recommendations=tuple(created),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _position_return_percent(position: Position, instrument: Instrument) -> Decimal | None:
        average = position.average_price
        if average <= ZERO:
            return None
        current = instrument_unit_value(
            instrument.asset_class,
            position.current_price,
            nominal=instrument.nominal,
            nkd=position.current_nkd,
        )
        return (current - average) / average * HUNDRED

    def _goal_funding_trigger(
        self,
        *,
        goal: Goal,
        portfolio: PortfolioView,
        positions: list[Position],
        position_views: Mapping[int, PositionView],
        percents: Mapping[AssetClass, Decimal],
        allocations: Mapping[AssetClass, tuple[Decimal, Decimal, Decimal]],
        cash: Decimal,
        at: datetime,
        warnings: list[str],
    ) -> _SellTrigger | None:
        if goal.currency != portfolio.base_currency:
            warnings.append(
                "валюта цели не совпадает с базовой; финансирование цели не рассчитывается"
            )
            return None
        days_left = (goal.target_date - at.date()).days
        if days_left > self._settings.goal_funding_window_days:
            return None
        if portfolio.total_value < goal.target_amount:
            warnings.append(
                "срок цели близко, но стоимость портфеля ниже целевой суммы; "
                "продажи для финансирования не формируются"
            )
            return None
        need = goal.target_amount - cash
        if need < self._settings.min_trade_amount:
            return None
        options: list[tuple[Decimal, Decimal, AssetClass]] = []
        for asset_class, (_minimum, target, _maximum) in allocations.items():
            if asset_class is AssetClass.CASH:
                continue
            class_value = sum(
                (
                    view.value_base
                    for position in positions
                    if position.instrument.asset_class is asset_class
                    for view in (position_views.get(position.instrument_id),)
                    if view is not None
                ),
                ZERO,
            )
            if class_value <= ZERO:
                continue
            options.append((percents.get(asset_class, ZERO) - target, class_value, asset_class))
        if not options:
            return None
        options.sort(key=lambda item: (-item[0], -item[1], item[2].value))
        _overweight, class_value, asset_class = options[0]
        budget = min(self._settings.max_order_amount, need, class_value)
        if budget < self._settings.min_trade_amount:
            warnings.append(f"{asset_class.value}: недостаточно стоимости для финансирования цели")
            return None
        timing = f"до цели {days_left} дн." if days_left >= 0 else "срок цели наступил"
        return _SellTrigger(
            asset_class=asset_class,
            budget=budget,
            trigger_text=(
                f"{timing}; для финансирования цели нужно "
                f"{_fmt_money(need, portfolio.base_currency)} наличными; "
                f"продажа из класса «{asset_class.label_ru}» для увеличения cash"
            ),
            force_exit=True,
            trigger_code="GOAL_FUNDING",
        )

    def _add_rebalance_plan(
        self,
        session: Session,
        *,
        account: Account,
        goal: Goal,
        portfolio: PortfolioView,
        created: list[Recommendation],
        at: datetime,
        input_hash: str,
    ) -> Recommendation | None:
        sells = [item for item in created if item.action is RecommendationAction.SELL]
        buys = [item for item in created if item.action is RecommendationAction.BUY]
        if not sells or not buys:
            return None
        sell_total = sum((item.estimated_amount for item in sells), ZERO)
        buy_total = sum((item.estimated_amount for item in buys), ZERO)
        commission = sum((item.commission_estimate for item in created), ZERO)
        turnover = sell_total + buy_total
        net_cash = sell_total - buy_total
        legs = [*sells, *buys]
        reason = (
            f"Ребалансировка: {len(sells)} продаж на "
            f"{_fmt_money(sell_total, portfolio.base_currency)} и {len(buys)} покупок на "
            f"{_fmt_money(buy_total, portfolio.base_currency)}; оборот "
            f"{_fmt_money(turnover, portfolio.base_currency)}, чистый денежный поток "
            f"{_fmt_signed_money(net_cash, portfolio.base_currency)}. "
            "План не является заявкой: одобряйте отдельные рекомендации."
        )
        rules = [
            {
                "code": "REBALANCE_LEG",
                "title": f"{item.ticker} {item.action.value} {item.lots} лот(ов)",
                "passed": True,
                "detail": (
                    f"amount={quantize_money(item.estimated_amount)} "
                    f"weight={quantize_percent(item.current_weight)}→"
                    f"{quantize_percent(item.expected_weight_after)}"
                ),
            }
            for item in legs
        ]
        fingerprint = hashlib.sha256(
            (input_hash + "|REBALANCE|" + at.isoformat()).encode()
        ).hexdigest()
        recommendation = Recommendation(
            account_id=account.id,
            goal_id=goal.id,
            instrument_id=None,
            action=RecommendationAction.REBALANCE,
            status=RecommendationStatus.NEW,
            order_type=OrderType.LIMIT,
            instrument_uid=None,
            figi=None,
            ticker="PORTFOLIO",
            currency=portfolio.base_currency,
            lots=0,
            lot_size=1,
            quote_price=ZERO,
            price_per_lot=ZERO,
            estimated_amount=quantize_money(turnover),
            commission_estimate=quantize_money(commission),
            reason=reason,
            triggered_rules=rules,
            blocking_rules=[],
            risks=[
                "план не является заявкой; для исполнения одобрите отдельные рекомендации",
                "одновременное исполнение ног изменит денежный поток и доли классов",
            ],
            expected_weight_after=ZERO,
            expected_class_weight_after=ZERO,
            current_weight=ZERO,
            target_weight=ZERO,
            score=None,
            score_breakdown=[],
            calculated_at=at,
            quote_time=None,
            data_freshness_seconds=0,
            expires_at=at + timedelta(minutes=self._settings.recommendation_ttl_minutes),
            mode=self._settings.trading_mode,
            fingerprint=fingerprint,
            input_hash=input_hash,
            rule_version="rules-v4",
        )
        session.add(recommendation)
        session.flush()
        created.append(recommendation)
        return recommendation

    def _build_action(
        self,
        session: Session,
        *,
        account: Account,
        goal: Goal,
        portfolio: PortfolioView,
        asset_class: AssetClass,
        candidates: list[_Candidate],
        side: OrderSide,
        budget: Decimal,
        current_class_weight: Decimal,
        target_class_weight: Decimal,
        gap: Decimal,
        trigger_text: str,
        at: datetime,
        input_hash: str,
        warnings: list[str],
        selected: set[int],
        force_exit: bool = False,
        trigger_code: str | None = None,
    ) -> Recommendation | None:
        blocked_codes: set[str] = set()
        for candidate in candidates:
            instrument = candidate.instrument
            price = candidate.price
            unit_value = instrument_unit_value(
                instrument.asset_class,
                price,
                nominal=instrument.nominal,
                nkd=candidate.nkd,
            )
            price_per_lot = unit_value * max(1, instrument.lot)
            lots_function = lots_ceil if side is OrderSide.SELL else lots_floor
            lots = lots_function(
                budget,
                price_per_lot * (1 + self._settings.commission_rate),
            )
            if side is OrderSide.SELL:
                if lots * price_per_lot > self._settings.max_order_amount:
                    lots = lots_floor(
                        budget,
                        price_per_lot * (1 + self._settings.commission_rate),
                    )
                lots = min(
                    lots,
                    int(candidate.quantity) // max(1, instrument.lot),
                )
            if lots <= 0:
                warnings.append(f"{instrument.ticker}: бюджета не хватает на один торговый лот")
                continue
            decision = self._risk.evaluate(
                session,
                account_id=account.id,
                instrument=instrument,
                side=side,
                lots=lots,
                price=price,
                quote_at=candidate.price_at,
                mode=self._settings.trading_mode,
                goal=goal,
                now=at,
                require_order_mode=False,
                allow_class_below_min=force_exit,
            )
            if not decision.allowed:
                blocked_codes.update(item.code for item in decision.blocking_rules)
                continue
            increment = decision.estimated_amount
            current_value = candidate.current_weight / HUNDRED * portfolio.total_value
            projected_value = (
                current_value + increment
                if side is OrderSide.BUY
                else max(ZERO, current_value - increment)
            )
            projected_weight = (
                projected_value / portfolio.total_value * HUNDRED
                if portfolio.total_value > ZERO
                else ZERO
            )
            deviation_kind = "перевес" if gap < ZERO else "недобор"
            screener_note = (
                f" Скринер: {candidate.screener.score:.1f}/100, место "
                f"{candidate.screener.rank}, на {candidate.screener.as_of.isoformat()}."
                if candidate.screener is not None
                else ""
            )
            reason = (
                f"{instrument.ticker}: класс «{instrument.asset_class.label_ru}» — "
                f"{_fmt_percent(current_class_weight)}% при цели "
                f"{_fmt_percent(target_class_weight)}% "
                f"({deviation_kind} {_fmt_percent(abs(gap))} п.п.); {trigger_text}. "
                + ("Покупка на " if side is OrderSide.BUY else "Продажа на ")
                + f"{_fmt_money(decision.estimated_amount, portfolio.base_currency)}, "
                + "прогноз доли класса "
                + f"{_fmt_percent(decision.projected_class_percent)}%."
                + screener_note
            )
            risks = list(decision.warnings)
            if decision.projected_issuer_percent > self._settings.max_issuer_percent:
                risks.append("концентрация эмитента выше настроенного лимита")
            triggered_rules = [item.as_dict() for item in decision.triggered_rules]
            if candidate.screener is not None:
                triggered_rules.append(
                    {
                        "code": "SCREENER_RATING",
                        "title": "Рейтинг скринера",
                        "passed": True,
                        "detail": candidate.screener.reason,
                    }
                )
                if candidate.screener.critical:
                    risks.append("скринер: критичные замечания к качеству данных")
            if force_exit and trigger_code is not None:
                triggered_rules.append(
                    {
                        "code": "MANDATORY_EXIT",
                        "title": "Принудительный выход",
                        "passed": True,
                        "detail": trigger_code,
                    }
                )
            return self._persist(
                session,
                account=account,
                goal=goal,
                portfolio=portfolio,
                instrument=instrument,
                action=(
                    RecommendationAction.BUY if side is OrderSide.BUY else RecommendationAction.SELL
                ),
                lots=lots,
                quote_price=price,
                price_per_lot=price_per_lot,
                estimated_amount=decision.estimated_amount,
                commission=decision.commission,
                current_weight=candidate.current_weight,
                target_weight=target_class_weight,
                expected_weight=projected_weight,
                expected_class_weight=decision.projected_class_percent,
                reason=reason,
                triggered_rules=triggered_rules,
                blocking_rules=[],
                risks=risks,
                score=candidate.score,
                breakdown=candidate.breakdown,
                at=at,
                input_hash=input_hash,
            )
        if not candidates:
            warnings.append(
                f"нет доступного кандидата для {side.value} в {asset_class.value}; "
                "проверьте свежесть котировок и доступность инструментов"
            )
        elif blocked_codes:
            warnings.append(
                "все кандидаты заблокированы риск-правилами: " + ", ".join(sorted(blocked_codes))
            )
        return None

    def _buy_candidates(
        self,
        session: Session,
        positions: list[Position],
        views: Mapping[int, PositionView],
        issuer_weights: dict[str, Decimal],
        asset_class: AssetClass,
        gap: Decimal,
        threshold: Decimal,
        portfolio: PortfolioView,
        goal: Goal,
        now: datetime,
        *,
        screener: ScreenerRatingSet,
        warnings: list[str],
    ) -> list[_Candidate]:
        bases: list[BaseCandidate] = []
        held_ids: set[int] = set()
        unrated = 0
        for position in positions:
            instrument = position.instrument
            view = views.get(instrument.id)
            if view is None or instrument.asset_class is not asset_class:
                continue
            held_ids.add(instrument.id)
            rating = None
            if asset_class is AssetClass.STOCK and screener.active:
                rating = screener.get(instrument)
                if rating is None:
                    unrated += 1
                    continue
            price = position.current_price
            price_at = position.current_price_at
            nkd = position.current_nkd
            quantity = position.quantity
            if price <= ZERO or is_stale(
                price_at,
                portfolio.as_of,
                self._settings.quote_max_age_seconds,
            ):
                continue
            if not self._buyable(instrument):
                continue
            bases.append(
                (
                    position,
                    instrument,
                    price,
                    price_at,
                    nkd,
                    quantity,
                    view.weight_percent,
                    rating,
                )
            )
        universe_query = select(Instrument).where(Instrument.asset_class == asset_class)
        if held_ids:
            universe_query = universe_query.where(Instrument.id.not_in(held_ids))
        universe = session.scalars(universe_query.order_by(Instrument.ticker))
        for instrument in universe:
            if not self._buyable(instrument):
                continue
            rating = None
            if asset_class is AssetClass.STOCK and screener.active:
                rating = screener.get(instrument)
                if rating is None:
                    unrated += 1
                    continue
            price = instrument.last_price or ZERO
            price_at = instrument.last_price_at
            if price <= ZERO or is_stale(
                price_at,
                portfolio.as_of,
                self._settings.quote_max_age_seconds,
            ):
                continue
            bases.append((None, instrument, price, price_at, ZERO, ZERO, ZERO, rating))
        if unrated:
            warnings.append(
                f"{asset_class.value}: {unrated} бумаг вне свежего скринера; "
                "покупки по ним не предлагаются"
            )
        growth_scores = load_growth_scores(
            session,
            self._settings,
            [base[1].id for base in bases],
            now=now,
        )
        result: list[_Candidate] = []
        for (
            base_position,
            instrument,
            price,
            price_at,
            nkd,
            quantity,
            current_weight,
            rating,
        ) in bases:
            score, breakdown = self._score_buy(
                instrument,
                gap,
                threshold,
                issuer_weights,
                goal,
                growth_scores.get(instrument.id),
                rating,
            )
            result.append(
                _Candidate(
                    position=base_position,
                    instrument=instrument,
                    score=score,
                    breakdown=breakdown,
                    current_weight=current_weight,
                    quantity=quantity,
                    price=price,
                    price_at=price_at,
                    nkd=nkd,
                    screener=rating,
                )
            )
        return sorted(result, key=lambda item: (-item.score, item.instrument.ticker))

    @staticmethod
    def _buyable(instrument: Instrument) -> bool:
        return (
            not instrument.is_excluded
            and instrument.is_buy_candidate
            and instrument.buy_available
            and instrument.api_trade_available
        )

    def _sell_candidates(
        self,
        positions: list[Position],
        views: Mapping[int, PositionView],
        issuer_weights: dict[str, Decimal],
        asset_class: AssetClass,
        gap: Decimal,
        threshold: Decimal,
        portfolio: PortfolioView,
        goal: Goal,
        *,
        issuer_key: str | None = None,
        instrument_id: int | None = None,
    ) -> list[_Candidate]:
        result: list[_Candidate] = []
        for position in positions:
            instrument = position.instrument
            view = views.get(instrument.id)
            if view is None or instrument.asset_class is not asset_class:
                continue
            if instrument_id is not None and instrument.id != instrument_id:
                continue
            issuer = instrument.issuer_key or instrument.ticker.upper()
            if issuer_key is not None and issuer != issuer_key:
                continue
            if not instrument.sell_available:
                continue
            if not instrument.api_trade_available or position.current_price <= ZERO:
                continue
            if is_stale(
                position.current_price_at,
                portfolio.as_of,
                self._settings.quote_max_age_seconds,
            ):
                continue
            issuer_weight = issuer_weights.get(issuer, ZERO)
            score_result = score_sell_candidate(
                class_overweight_percent=gap,
                rebalance_threshold_percent=threshold,
                issuer_weight_percent=issuer_weight,
                max_issuer_percent=self._settings.max_issuer_percent,
                risk_profile=goal.risk_profile,
                volatility_30d=instrument.volatility_30d,
                liquidity_flag=instrument.liquidity_flag,
            )
            result.append(
                _Candidate(
                    position=position,
                    instrument=instrument,
                    score=score_result.score,
                    breakdown=score_result.breakdown,
                    current_weight=view.weight_percent,
                    quantity=position.quantity,
                    price=position.current_price,
                    price_at=position.current_price_at,
                    nkd=position.current_nkd,
                )
            )
        return sorted(result, key=lambda item: (-item.score, item.instrument.ticker))

    def _score_buy(
        self,
        instrument: Instrument,
        gap: Decimal,
        threshold: Decimal,
        issuer_weights: dict[str, Decimal],
        goal: Goal,
        growth: GrowthScore | None,
        screener: ScreenerRating | None,
    ) -> tuple[Decimal, list[dict[str, object]]]:
        issuer = instrument.issuer_key or instrument.ticker.upper()
        result = score_buy_candidate(
            allocation_gap_percent=gap,
            rebalance_threshold_percent=threshold,
            issuer_weight_percent=issuer_weights.get(issuer, ZERO),
            risk_profile=goal.risk_profile,
            volatility_30d=instrument.volatility_30d,
            income_yield=instrument.income_yield,
            liquidity_flag=instrument.liquidity_flag,
            growth=growth,
            screener=screener,
        )
        return result.score, result.breakdown

    def _persist(
        self,
        session: Session,
        *,
        account: Account,
        goal: Goal,
        portfolio: PortfolioView,
        instrument: Instrument,
        action: RecommendationAction,
        lots: int,
        quote_price: Decimal,
        price_per_lot: Decimal,
        estimated_amount: Decimal,
        commission: Decimal,
        current_weight: Decimal,
        target_weight: Decimal,
        expected_weight: Decimal,
        expected_class_weight: Decimal,
        reason: str,
        triggered_rules: list[dict[str, object]],
        blocking_rules: list[dict[str, object]],
        risks: list[str],
        score: Decimal | None,
        breakdown: list[dict[str, object]],
        at: datetime,
        input_hash: str,
    ) -> Recommendation:
        quote_time = instrument.last_price_at
        fingerprint = hashlib.sha256(
            (
                input_hash
                + "|"
                + action.value
                + "|"
                + instrument.instrument_uid
                + "|"
                + str(lots)
                + "|"
                + str(quote_price)
                + "|"
                + at.isoformat()
            ).encode()
        ).hexdigest()
        recommendation = Recommendation(
            account_id=account.id,
            goal_id=goal.id,
            instrument_id=instrument.id,
            action=action,
            status=RecommendationStatus.NEW,
            order_type=OrderType.LIMIT,
            instrument_uid=instrument.instrument_uid,
            figi=instrument.figi,
            ticker=instrument.ticker,
            currency=portfolio.base_currency,
            lots=lots,
            lot_size=max(1, instrument.lot),
            quote_price=quote_price,
            price_per_lot=quantize_money(price_per_lot),
            estimated_amount=quantize_money(estimated_amount),
            commission_estimate=quantize_money(commission),
            reason=reason,
            triggered_rules=triggered_rules,
            blocking_rules=blocking_rules,
            risks=risks,
            expected_weight_after=quantize_percent(expected_weight),
            expected_class_weight_after=quantize_percent(expected_class_weight),
            current_weight=quantize_percent(current_weight),
            target_weight=quantize_percent(target_weight),
            score=score,
            score_breakdown=breakdown,
            calculated_at=at,
            quote_time=quote_time,
            data_freshness_seconds=age_seconds(quote_time, at),
            expires_at=at + timedelta(minutes=self._settings.recommendation_ttl_minutes),
            mode=self._settings.trading_mode,
            fingerprint=fingerprint,
            input_hash=input_hash,
            rule_version="rules-v4",
        )
        session.add(recommendation)
        session.flush()
        return recommendation

    def _expire_existing(self, session: Session, account_id: int) -> None:
        for recommendation in session.scalars(
            select(Recommendation).where(
                Recommendation.account_id == account_id,
                Recommendation.status == RecommendationStatus.NEW,
            )
        ):
            recommendation.status = RecommendationStatus.EXPIRED
        session.flush()

    @staticmethod
    def _cash_available(portfolio: PortfolioView) -> Decimal:
        row = next(
            (item for item in portfolio.cash if item.currency == portfolio.base_currency),
            None,
        )
        return row.free_value_base if row is not None else ZERO

    @staticmethod
    def _input_hash(
        portfolio: PortfolioView,
        goal: Goal,
        allocations: dict[AssetClass, tuple[Decimal, Decimal, Decimal]],
        positions: list[Position],
        threshold: Decimal,
        screener: ScreenerRatingSet | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "account_id": portfolio.account_id,
            "goal_id": goal.id,
            "risk_profile": goal.risk_profile.value,
            "total": str(portfolio.total_value),
            "cash": str(portfolio.free_cash_value),
            "threshold": str(threshold),
            "allocations": {
                key.value: [str(value) for value in bounds]
                for key, bounds in sorted(allocations.items(), key=lambda item: item[0].value)
            },
            "positions": sorted(
                (
                    {
                        "uid": position.instrument.instrument_uid,
                        "quantity": str(position.quantity),
                        "price": str(position.current_price),
                        "at": position.current_price_at.isoformat()
                        if position.current_price_at
                        else "",
                    }
                    for position in positions
                ),
                key=lambda item: str(item["uid"]),
            ),
        }
        if screener is not None and screener.active:
            payload["screener"] = {
                "as_of": screener.as_of.isoformat() if screener.as_of else "",
                "mode": screener.mode,
                "generated_at": (
                    screener.generated_at.isoformat() if screener.generated_at else ""
                ),
                "scores": sorted(
                    [rating.uid or rating.ticker, str(rating.score)]
                    for rating in screener.ratings
                ),
            }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
