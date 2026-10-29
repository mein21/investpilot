"""Jinja filters and human-readable labels for the web interface."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    RecommendationAction,
    RecommendationStatus,
    RiskProfile,
    Severity,
    TradingMode,
)
from app.core.time import ensure_utc

ASSET_LABELS = {
    AssetClass.STOCK: "Акции",
    AssetClass.BOND: "Облигации",
    AssetClass.ETF: "Фонды",
    AssetClass.CURRENCY: "Валюта",
    AssetClass.CASH: "Деньги",
    AssetClass.OTHER: "Прочее",
}
ACTION_LABELS = {
    RecommendationAction.BUY: "Купить",
    RecommendationAction.SELL: "Продать",
    RecommendationAction.HOLD: "Держать",
    RecommendationAction.REBALANCE: "Ребалансировка",
}
ACTION_TONES = {
    RecommendationAction.BUY: "positive",
    RecommendationAction.SELL: "negative",
    RecommendationAction.HOLD: "neutral",
    RecommendationAction.REBALANCE: "accent",
}
RECOMMENDATION_STATUS_LABELS = {
    RecommendationStatus.NEW: "Новая",
    RecommendationStatus.APPROVED: "Одобрена",
    RecommendationStatus.SENT: "Отправлена",
    RecommendationStatus.EXECUTED: "Исполнена",
    RecommendationStatus.REJECTED: "Отклонена",
    RecommendationStatus.EXPIRED: "Истекла",
    RecommendationStatus.FAILED: "Ошибка",
}
ORDER_STATUS_LABELS = {
    OrderStatus.DRAFT: "Черновик",
    OrderStatus.APPROVED: "Одобрена",
    OrderStatus.SENT: "Отправлена",
    OrderStatus.PARTIALLY_FILLED: "Частично исполнена",
    OrderStatus.EXECUTED: "Исполнена",
    OrderStatus.CANCELLED: "Отменена",
    OrderStatus.REJECTED: "Отклонена",
    OrderStatus.FAILED: "Ошибка",
    OrderStatus.SUBMIT_UNKNOWN: "Исход неизвестен",
}
STATUS_TONES = {
    OrderStatus.DRAFT: "neutral",
    OrderStatus.APPROVED: "accent",
    OrderStatus.SENT: "accent",
    OrderStatus.PARTIALLY_FILLED: "warning",
    OrderStatus.EXECUTED: "positive",
    OrderStatus.CANCELLED: "neutral",
    OrderStatus.REJECTED: "negative",
    OrderStatus.FAILED: "negative",
    OrderStatus.SUBMIT_UNKNOWN: "warning",
}
SIDE_LABELS = {OrderSide.BUY: "Покупка", OrderSide.SELL: "Продажа"}
RISK_LABELS = {
    RiskProfile.CONSERVATIVE: "Консервативный",
    RiskProfile.BALANCED: "Сбалансированный",
    RiskProfile.AGGRESSIVE: "Агрессивный",
}
SEVERITY_LABELS = {
    Severity.INFO: "Информация",
    Severity.WARNING: "Предупреждение",
    Severity.ERROR: "Ошибка",
    Severity.CRITICAL: "Критично",
}
TRADING_MODE_LABELS = {
    TradingMode.OBSERVE: "Наблюдение",
    TradingMode.RECOMMEND: "Рекомендации",
    TradingMode.PAPER: "Бумажная торговля",
    TradingMode.CONFIRM: "Подтверждение",
}
REGIME_LABELS = {
    "risk_on": "Risk-on",
    "neutral": "Нейтральный",
    "risk_off": "Risk-off",
    "unknown": "Не определён",
}
SCREENER_FUNDAMENTAL_CATEGORY_LABELS = {
    "financial": "Финансовый сектор",
    "real_estate": "Недвижимость",
    "capital_intensive": "Капиталоёмкий сектор",
    "general": "Общие правила",
}
SCREENER_MODE_LABELS = {
    "technical": "Технический",
    "technical_fundamental": "Технический + фундаментал",
}
SCREENER_BLOCK_LABELS = {
    "relative_strength": "Относительная сила",
    "trend_quality": "Качество тренда",
    "volume_confirmation": "Подтверждение объёмом",
    "consolidation_breakout": "Консолидация и пробой",
    "risk_moderation": "Умеренность риска",
}
TRADING_STATUS_LABELS = {
    "SECURITY_TRADING_STATUS_UNSPECIFIED": "Не определён",
    "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING": "Недоступен для торгов",
    "SECURITY_TRADING_STATUS_OPENING_PERIOD": "Период открытия",
    "SECURITY_TRADING_STATUS_CLOSING_PERIOD": "Период закрытия",
    "SECURITY_TRADING_STATUS_BREAK_IN_TRADING": "Перерыв в торговле",
    "SECURITY_TRADING_STATUS_NORMAL_TRADING": "Нормальная торговля",
    "SECURITY_TRADING_STATUS_CLOSING_AUCTION": "Аукцион закрытия",
    "SECURITY_TRADING_STATUS_DARK_POOL_AUCTION": "Аукцион крупных пакетов",
    "SECURITY_TRADING_STATUS_DISCRETE_AUCTION": "Дискретный аукцион",
    "SECURITY_TRADING_STATUS_OPENING_AUCTION_PERIOD": "Аукцион открытия",
    "SECURITY_TRADING_STATUS_TRADING_AT_CLOSING_AUCTION_PRICE": "Торги по цене закрытия",
    "SECURITY_TRADING_STATUS_SESSION_ASSIGNED": "Сессия назначена",
    "SECURITY_TRADING_STATUS_SESSION_CLOSE": "Сессия закрыта",
    "SECURITY_TRADING_STATUS_SESSION_OPEN": "Сессия открыта",
    "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING": "Дилерская торговля",
    "SECURITY_TRADING_STATUS_DEALER_BREAK_IN_TRADING": "Перерыв дилерской торговли",
    "SECURITY_TRADING_STATUS_DEALER_NOT_AVAILABLE_FOR_TRADING": "Дилерская торговля недоступна",
}
CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥", "HKD": "HK$"}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def money(value: Any, currency: str = "RUB", *, signed: bool = False) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    quantized = number.quantize(Decimal("0.01"))
    sign = ""
    if signed and quantized > 0:
        sign = "+"
    text = f"{abs(quantized):,.2f}".replace(",", "\u00a0").replace(".", ",")
    if quantized < 0:
        sign = "−"
    if sign == "−":
        text = f"{sign}{text}"
    elif sign == "+":
        text = f"+{text}"
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())
    return f"{text}\u00a0{symbol}"


def quantity(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    if number == number.to_integral_value():
        return f"{int(number):,}".replace(",", "\u00a0")
    return (
        f"{number.normalize():,.4f}".rstrip("0")
        .rstrip(".")
        .replace(",", "\u00a0")
        .replace(".", ",")
    )


def percent(value: Any, *, signed: bool = False) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    sign = ""
    if signed and number > 0:
        sign = "+"
    return f"{sign}{number.quantize(Decimal('0.01'))}%".replace(".", ",")


def ratio_percent(value: Any, *, signed: bool = False) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    return percent(number * Decimal("100"), signed=signed)


def number(value: Any, digits: int = 1) -> str:
    number_value = _decimal(value)
    if number_value is None:
        return "—"
    quantized = number_value.quantize(Decimal(1).scaleb(-digits))
    text = f"{quantized:,.{digits}f}".replace(",", "\u00a0").replace(".", ",")
    return text.replace("-", "−")


def date_ru(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return "—"
    return value.strftime("%d.%m.%Y")


def dt_ru(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if not isinstance(value, datetime):
        return "—"
    return ensure_utc(value).strftime("%d.%m.%Y %H:%M UTC")


def short_dt(value: Any) -> str:
    if not isinstance(value, datetime):
        return "—"
    return ensure_utc(value).strftime("%d.%m %H:%M")


def asset_label(value: Any) -> str:
    return ASSET_LABELS.get(value, str(value))


def action_label(value: Any) -> str:
    return ACTION_LABELS.get(value, str(value))


def action_tone(value: Any) -> str:
    return ACTION_TONES.get(value, "neutral")


def recommendation_status_label(value: Any) -> str:
    return RECOMMENDATION_STATUS_LABELS.get(value, str(value))


def order_status_label(value: Any) -> str:
    return ORDER_STATUS_LABELS.get(value, str(value))


def status_tone(value: Any) -> str:
    return STATUS_TONES.get(value, "neutral")


def side_label(value: Any) -> str:
    return SIDE_LABELS.get(value, str(value))


def risk_label(value: Any) -> str:
    return RISK_LABELS.get(value, str(value))


def severity_label(value: Any) -> str:
    return SEVERITY_LABELS.get(value, str(value))


def trading_mode_label(value: Any) -> str:
    return TRADING_MODE_LABELS.get(value, str(value))


def regime_label(value: Any) -> str:
    return REGIME_LABELS.get(value, str(value))


def screener_mode_label(value: Any) -> str:
    return SCREENER_MODE_LABELS.get(value, str(value))


def screener_fundamental_category_label(value: Any) -> str:
    return SCREENER_FUNDAMENTAL_CATEGORY_LABELS.get(value, str(value or "—"))


def trading_status_label(value: Any) -> str:
    return TRADING_STATUS_LABELS.get(str(value), str(value or "—"))


def sparkline_points(value: Any, width: int = 200, height: int = 48) -> str:
    """Build SVG polyline points from cached bars, normalised to a viewBox."""

    if not value:
        return ""
    closes: list[Decimal] = []
    for item in value[-120:]:
        raw = item.get("close") if isinstance(item, dict) else getattr(item, "close", None)
        number = _decimal(raw)
        if number is not None and number > 0:
            closes.append(number)
    if len(closes) < 2:
        return ""
    low = min(closes)
    high = max(closes)
    span = high - low
    pad = Decimal("3")
    usable_height = Decimal(height) - pad * 2
    if span <= 0:
        return " ".join(
            f"{Decimal(index) * Decimal(width) / Decimal(len(closes) - 1):.1f},"
            f"{Decimal(height) / 2:.1f}"
            for index in range(len(closes))
        )
    points: list[str] = []
    for index, close in enumerate(closes):
        x = Decimal(index) * Decimal(width) / Decimal(len(closes) - 1)
        y = pad + (high - close) / span * usable_height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def sparkline_levels(
    value: Any,
    levels: Any,
    width: int = 200,
    height: int = 48,
) -> list[dict[str, str]]:
    """Return horizontal SVG levels aligned with :func:`sparkline_points`."""

    if not value or not isinstance(levels, dict):
        return []
    closes: list[Decimal] = []
    for item in value[-120:]:
        raw = item.get("close") if isinstance(item, dict) else getattr(item, "close", None)
        number = _decimal(raw)
        if number is not None and number > 0:
            closes.append(number)
    if len(closes) < 2:
        return []
    low = min(closes)
    high = max(closes)
    span = high - low
    if span <= 0:
        return []
    pad = Decimal("3")
    usable_height = Decimal(height) - pad * 2
    rows: list[dict[str, str]] = []
    for label, raw in levels.items():
        number = _decimal(raw)
        if number is None or number < low or number > high:
            continue
        y = pad + (high - number) / span * usable_height
        rows.append(
            {
                "label": str(label),
                "y": f"{y:.1f}",
                "price": f"{number:.2f}".replace(".", ","),
            }
        )
    return rows


def sparkline_volume_points(value: Any, width: int = 200, height: int = 32) -> str:
    """Build an SVG polyline for volume bars from a cached history payload."""

    if not value:
        return ""
    volumes: list[Decimal] = []
    for item in value[-120:]:
        raw = item.get("volume") if isinstance(item, dict) else getattr(item, "volume_lots", None)
        number = _decimal(raw)
        if number is not None and number >= 0:
            volumes.append(number)
    if len(volumes) < 2:
        return ""
    peak = max(volumes)
    if peak <= 0:
        return ""
    pad = Decimal("2")
    usable_height = Decimal(height) - pad * 2
    points: list[str] = []
    for index, volume in enumerate(volumes):
        x = Decimal(index) * Decimal(width) / Decimal(len(volumes) - 1)
        y = pad + (peak - volume) / peak * usable_height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def score_band_for(backtest: Any, score: Any) -> dict[str, Any] | None:
    """Return the out-of-sample score band that contains ``score``."""

    if not isinstance(backtest, dict):
        return None
    value = _decimal(score)
    if value is None:
        return None
    for row in backtest.get("sensitivity") or []:
        if not isinstance(row, dict) or row.get("kind") != "score_band":
            continue
        low = _decimal(row.get("min_score"))
        high = _decimal(row.get("max_score"))
        if low is not None and high is not None and low <= value <= high:
            return row
    return None
