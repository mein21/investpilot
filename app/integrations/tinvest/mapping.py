"""Mapping from verified T-Invest DTOs to broker-neutral domain objects."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.core.enums import AssetClass, OrderSide, OrderType
from app.core.time import ensure_utc
from app.integrations.tinvest.dto import (
    AccountDTO,
    CandleDTO,
    InstrumentDetailsDTO,
    InstrumentDTO,
    LastPriceDTO,
    OperationDTO,
    OrderStageDTO,
    OrderStateDTO,
    PortfolioResponseDTO,
    PositionsResponseDTO,
    PostOrderResponseDTO,
    TradingStatusDTO,
    UniverseInstrumentDTO,
)
from app.integrations.tinvest.exceptions import TInvestValidationError
from app.integrations.tinvest.types import (
    AccountInfo,
    BrokerOrderStatus,
    CandleInfo,
    CashInfo,
    InstrumentInfo,
    OperationInfo,
    OrderExecutionInfo,
    OrderResult,
    PortfolioInfo,
    PortfolioPositionInfo,
    PositionsInfo,
    PriceInfo,
    TradeInfo,
    TradingStatusInfo,
)

ASSET_CLASS_BY_TYPE = {
    "share": AssetClass.STOCK,
    "stock": AssetClass.STOCK,
    "bond": AssetClass.BOND,
    "etf": AssetClass.ETF,
    "currency": AssetClass.CURRENCY,
}
OTHER_TYPES = {
    "futures",
    "option",
    "sp",
    "structured_note",
    "clearing_certificate",
    "index",
    "commodity",
    "other",
}

ORDER_STATUS_MAP = {
    "EXECUTION_REPORT_STATUS_FILL": BrokerOrderStatus.FILL,
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL": BrokerOrderStatus.PARTIALLY_FILLED,
    "EXECUTION_REPORT_STATUS_REJECTED": BrokerOrderStatus.REJECTED,
    "EXECUTION_REPORT_STATUS_CANCELLED": BrokerOrderStatus.CANCELLED,
    "EXECUTION_REPORT_STATUS_NEW": BrokerOrderStatus.NEW,
    "EXECUTION_REPORT_STATUS_UNSPECIFIED": BrokerOrderStatus.UNKNOWN,
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise TInvestValidationError("broker timestamp has no timezone") from exc


def _normalized_type(value: str) -> str:
    return value.strip().lower().removeprefix("instrument_type_").replace("-", "_")


def asset_class_from_type(instrument_type: str, instrument_kind: str = "") -> AssetClass:
    for candidate in (instrument_type, instrument_kind):
        normalized = _normalized_type(candidate)
        if normalized in ASSET_CLASS_BY_TYPE:
            return ASSET_CLASS_BY_TYPE[normalized]
        if normalized in OTHER_TYPES:
            return AssetClass.OTHER
    return AssetClass.OTHER


def map_account(dto: AccountDTO, *, is_sandbox: bool) -> AccountInfo:
    if not dto.id:
        raise TInvestValidationError("account id is missing in broker response")
    return AccountInfo(
        tinvest_account_id=dto.id,
        name=dto.name,
        account_type=dto.type,
        status=dto.status,
        access_level=dto.access_level,
        opened_date=_aware(dto.opened_date),
        closed_date=_aware(dto.closed_date),
        is_sandbox=is_sandbox,
    )


def map_universe_instrument(
    dto: UniverseInstrumentDTO,
    *,
    asset_class: AssetClass,
    instrument_type: str,
) -> InstrumentInfo:
    identifier = dto.uid or dto.figi
    if not identifier:
        raise TInvestValidationError("universe instrument has neither uid nor figi")
    if not dto.ticker:
        raise TInvestValidationError(f"universe instrument {identifier} has no ticker")
    nominal: Decimal | None = None
    if dto.nominal is not None:
        candidate = dto.nominal.to_decimal()
        if candidate > 0:
            nominal = candidate
    maturity_date: date | None = None
    if dto.maturity_date is not None:
        maturity_at = _aware(dto.maturity_date)
        maturity_date = maturity_at.date() if maturity_at else None
    return InstrumentInfo(
        instrument_uid=identifier,
        figi=dto.figi or None,
        ticker=dto.ticker,
        class_code=dto.class_code,
        isin=dto.isin or None,
        name=dto.name,
        instrument_type=instrument_type,
        asset_class=asset_class,
        currency=dto.currency or "RUB",
        lot=max(1, dto.lot),
        exchange=dto.exchange,
        issuer_key=dto.asset_uid or dto.ticker.upper(),
        sector=dto.sector,
        country_of_risk=dto.country_of_risk or None,
        maturity_date=maturity_date,
        nominal=nominal,
        coupon_quantity_per_year=dto.coupon_quantity_per_year,
        liquidity_flag=dto.liquidity_flag,
        trading_status=dto.trading_status,
        buy_available=bool(dto.buy_available_flag),
        sell_available=bool(dto.sell_available_flag),
        api_trade_available=bool(dto.api_trade_available_flag),
    )


def map_instrument(
    common: InstrumentDTO,
    details: InstrumentDetailsDTO | None = None,
) -> InstrumentInfo:
    uid = common.uid or (details.uid if details else "")
    figi = common.figi or (details.figi if details else "")
    if not uid and not figi:
        raise TInvestValidationError("instrument has neither uid nor figi")
    identifier = uid or figi
    ticker = common.ticker or (details.ticker if details else "")
    if not ticker:
        raise TInvestValidationError(f"instrument {identifier} has no ticker")

    asset_class = asset_class_from_type(common.instrument_type, common.instrument_kind)
    nominal: Decimal | None = None
    if details is not None and details.nominal is not None:
        nominal = details.nominal.to_decimal()
        if nominal <= 0:
            nominal = None

    income_yield: Decimal | None = None
    if details is not None and details.coupon_interest_rate is not None:
        candidate = details.coupon_interest_rate.to_decimal()
        if candidate >= 0:
            income_yield = candidate

    maturity_date: date | None = None
    if details is not None and details.maturity_date is not None:
        maturity_at = _aware(details.maturity_date)
        maturity_date = maturity_at.date() if maturity_at else None

    def merged_flag(common_value: bool | None, detail_value: bool | None) -> bool:
        if detail_value is not None:
            return detail_value
        return bool(common_value)

    issuer_key = (details.asset_uid if details else "") or common.asset_uid or ticker.upper()
    return InstrumentInfo(
        instrument_uid=identifier,
        figi=figi or None,
        ticker=ticker,
        class_code=common.class_code or (details.class_code if details else ""),
        isin=(common.isin or (details.isin if details else "")) or None,
        name=common.name or (details.name if details else ""),
        instrument_type=common.instrument_type or _normalized_type(common.instrument_kind),
        asset_class=asset_class,
        currency=common.currency or (details.currency if details else "") or "RUB",
        lot=max(1, common.lot or (details.lot if details else 1) or 1),
        exchange=common.exchange or (details.exchange if details else ""),
        issuer_key=issuer_key,
        sector=(details.sector if details else None),
        country_of_risk=(common.country_of_risk or (details.country_of_risk if details else ""))
        or None,
        maturity_date=maturity_date,
        nominal=nominal,
        coupon_quantity_per_year=(details.coupon_quantity_per_year if details else None),
        income_yield=income_yield,
        liquidity_flag=(
            details.liquidity_flag
            if details is not None and details.liquidity_flag is not None
            else common.liquidity_flag
        ),
        trading_status=(
            (details.trading_status if details and details.trading_status else "")
            or common.trading_status
        ),
        buy_available=merged_flag(
            common.buy_available_flag,
            details.buy_available_flag if details else None,
        ),
        sell_available=merged_flag(
            common.sell_available_flag,
            details.sell_available_flag if details else None,
        ),
        api_trade_available=merged_flag(
            common.api_trade_available_flag,
            details.api_trade_available_flag if details else None,
        ),
        extra={
            "focus_type": details.focus_type if details else None,
            "div_yield_flag": details.div_yield_flag if details else None,
        },
    )


def map_portfolio(dto: PortfolioResponseDTO, *, received_at: datetime) -> PortfolioInfo:
    positions: list[PortfolioPositionInfo] = []
    for item in dto.positions:
        positions.append(
            PortfolioPositionInfo(
                instrument_uid=item.instrument_uid or item.figi,
                figi=item.figi or None,
                ticker=item.ticker,
                class_code=item.class_code,
                instrument_type=item.instrument_type,
                quantity=item.quantity.to_decimal(),
                blocked_lots=item.blocked_lots.to_decimal(),
                average_price=item.average_position_price.to_decimal(),
                average_price_fifo=(
                    item.average_position_price_fifo.to_decimal()
                    if item.average_position_price_fifo is not None
                    else None
                ),
                current_price=item.current_price.to_decimal(),
                current_nkd=item.current_nkd.to_decimal(),
                expected_yield=item.expected_yield.to_decimal(),
                currency=(
                    item.current_price.currency or item.average_position_price.currency or "RUB"
                ),
            )
        )
    total = (
        dto.total_amount_portfolio.to_decimal()
        if dto.total_amount_portfolio is not None
        else Decimal("0")
    )
    currency = (
        dto.total_amount_portfolio.currency if dto.total_amount_portfolio is not None else "RUB"
    )
    daily = dto.daily_yield.to_decimal() if dto.daily_yield is not None else None
    return PortfolioInfo(
        account_id=dto.account_id,
        positions=tuple(positions),
        total_amount_portfolio=total,
        currency=currency or "RUB",
        daily_yield=daily,
        received_at=ensure_utc(received_at),
    )


def map_positions(dto: PositionsResponseDTO, *, received_at: datetime) -> PositionsInfo:
    blocked_by_currency = {item.currency: item for item in dto.blocked}
    cash: list[CashInfo] = []
    for item in dto.money:
        blocked = blocked_by_currency.get(item.currency)
        cash.append(
            CashInfo(
                currency=item.currency,
                amount=item.to_decimal(),
                blocked=blocked.to_decimal() if blocked is not None else Decimal("0"),
            )
        )
    return PositionsInfo(
        account_id=dto.account_id,
        cash=tuple(cash),
        received_at=ensure_utc(received_at),
    )


def map_price(dto: LastPriceDTO, *, received_at: datetime) -> PriceInfo:
    return PriceInfo(
        instrument_uid=dto.instrument_uid or dto.figi,
        figi=dto.figi or None,
        ticker=dto.ticker,
        class_code=dto.class_code,
        price=dto.price.to_decimal(),
        at=_aware(dto.time) or ensure_utc(received_at),
    )


def map_candle(dto: CandleDTO) -> CandleInfo:
    return CandleInfo(
        open=dto.open.to_decimal(),
        high=dto.high.to_decimal(),
        low=dto.low.to_decimal(),
        close=dto.close.to_decimal(),
        volume=dto.volume,
        at=ensure_utc(dto.time),
        is_complete=dto.is_complete,
    )


def map_trading_status(dto: TradingStatusDTO) -> TradingStatusInfo:
    return TradingStatusInfo(
        instrument_uid=dto.instrument_uid or dto.figi,
        figi=dto.figi or None,
        ticker=dto.ticker,
        class_code=dto.class_code,
        trading_status=dto.trading_status,
        limit_order_available=dto.limit_order_available_flag,
        market_order_available=dto.market_order_available_flag,
        api_trade_available=dto.api_trade_available_flag,
        bestprice_order_available=dto.bestprice_order_available_flag,
    )


def map_operation(dto: OperationDTO) -> OperationInfo:
    occurred_at = _aware(dto.date)
    if occurred_at is None:
        raise TInvestValidationError("operation date is missing")
    trades = tuple(
        TradeInfo(
            trade_id=item.trade_id or None,
            quantity=item.quantity,
            price=item.price.to_decimal(),
            at=_aware(item.date_time) or occurred_at,
        )
        for item in dto.trades
    )
    return OperationInfo(
        operation_id=dto.id,
        parent_operation_id=dto.parent_operation_id or None,
        instrument_uid=dto.instrument_uid or None,
        figi=dto.figi or None,
        currency=dto.currency or dto.payment.currency or "RUB",
        payment=dto.payment.to_decimal(),
        price=dto.price.to_decimal(),
        state=dto.state,
        quantity=dto.quantity,
        quantity_rest=dto.quantity_rest,
        occurred_at=occurred_at,
        type_label=dto.type,
        operation_type=dto.operation_type,
        instrument_type=dto.instrument_type,
        trades=trades,
    )


def map_order_status(raw: str) -> BrokerOrderStatus:
    return ORDER_STATUS_MAP.get(raw, BrokerOrderStatus.UNKNOWN)


def _map_stage(stage: OrderStageDTO, lot_size: int) -> OrderExecutionInfo | None:
    when = _aware(stage.execution_time)
    if when is None or stage.quantity <= 0:
        return None
    price = stage.price.to_decimal()
    amount = price * Decimal(stage.quantity) * Decimal(max(1, lot_size))
    return OrderExecutionInfo(
        trade_id=stage.trade_id or None,
        lots=stage.quantity,
        lot_size=max(1, lot_size),
        price=price,
        amount=amount,
        commission=Decimal("0"),
        executed_at=when,
        is_partial=False,
    )


def map_order_result(
    dto: PostOrderResponseDTO | OrderStateDTO,
    *,
    lot_size: int = 1,
) -> OrderResult:
    if not dto.order_id:
        raise TInvestValidationError("broker order response has no order id")
    status = map_order_status(dto.execution_report_status)
    total = (
        dto.total_order_amount.to_decimal() if dto.total_order_amount is not None else Decimal("0")
    )
    initial = (
        dto.initial_order_price.to_decimal()
        if dto.initial_order_price is not None
        else Decimal("0")
    )
    executed_price = (
        dto.executed_order_price.to_decimal()
        if dto.executed_order_price is not None
        else Decimal("0")
    )
    initial_commission = (
        dto.initial_commission.to_decimal() if dto.initial_commission is not None else Decimal("0")
    )
    executed_commission = (
        dto.executed_commission.to_decimal()
        if dto.executed_commission is not None
        else Decimal("0")
    )
    currency = ""
    for money in (dto.total_order_amount, dto.initial_order_price, dto.initial_commission):
        if money is not None and money.currency:
            currency = money.currency
            break
    stages = dto.stages if isinstance(dto, OrderStateDTO) else []
    executions = tuple(
        execution for stage in stages if (execution := _map_stage(stage, lot_size)) is not None
    )
    return OrderResult(
        broker_order_id=dto.order_id,
        client_order_id=dto.order_request_id,
        status=status,
        raw_status=dto.execution_report_status,
        lots_requested=dto.lots_requested,
        lots_executed=dto.lots_executed,
        initial_order_price=initial,
        executed_order_price=executed_price,
        total_order_amount=total,
        initial_commission=initial_commission,
        executed_commission=executed_commission,
        currency=currency or "RUB",
        message=dto.message,
        executions=executions,
        order_date=_aware(dto.order_date) if isinstance(dto, OrderStateDTO) else None,
    )


def order_side_to_broker(side: OrderSide) -> str:
    return "ORDER_DIRECTION_BUY" if side is OrderSide.BUY else "ORDER_DIRECTION_SELL"


def order_type_to_broker(order_type: OrderType) -> str:
    return {
        OrderType.LIMIT: "ORDER_TYPE_LIMIT",
        OrderType.MARKET: "ORDER_TYPE_MARKET",
        OrderType.BESTPRICE: "ORDER_TYPE_BESTPRICE",
    }[order_type]


def order_type_from_broker(value: str) -> OrderType:
    return {
        "ORDER_TYPE_LIMIT": OrderType.LIMIT,
        "ORDER_TYPE_MARKET": OrderType.MARKET,
        "ORDER_TYPE_BESTPRICE": OrderType.BESTPRICE,
    }.get(value, OrderType.LIMIT)
