"""Domain enums shared by models, rules, services and templates."""

from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    STOCK = "STOCK"
    BOND = "BOND"
    ETF = "ETF"
    CURRENCY = "CURRENCY"
    CASH = "CASH"
    OTHER = "OTHER"

    @property
    def label_ru(self) -> str:
        return {
            AssetClass.STOCK: "Акции",
            AssetClass.BOND: "Облигации",
            AssetClass.ETF: "Фонды",
            AssetClass.CURRENCY: "Валюта",
            AssetClass.CASH: "Деньги",
            AssetClass.OTHER: "Прочее",
        }[self]


class TradingMode(StrEnum):
    OBSERVE = "OBSERVE"
    RECOMMEND = "RECOMMEND"
    PAPER = "PAPER"
    CONFIRM = "CONFIRM"
    # The feature flag exists deliberately, but startup rejects it.
    AUTO = "AUTO"

    @property
    def label_ru(self) -> str:
        return {
            TradingMode.OBSERVE: "Наблюдение",
            TradingMode.RECOMMEND: "Рекомендации",
            TradingMode.PAPER: "Бумажная торговля",
            TradingMode.CONFIRM: "Подтверждение",
            TradingMode.AUTO: "Авто (запрещено)",
        }[self]

    @property
    def can_create_orders(self) -> bool:
        return self in (TradingMode.PAPER, TradingMode.CONFIRM)


class RiskProfile(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"
    CUSTOM = "CUSTOM"

    @property
    def label_ru(self) -> str:
        return {
            RiskProfile.CONSERVATIVE: "Консервативный",
            RiskProfile.BALANCED: "Сбалансированный",
            RiskProfile.AGGRESSIVE: "Агрессивный",
            RiskProfile.CUSTOM: "Пользовательский",
        }[self]


class RecommendationAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    REBALANCE = "REBALANCE"


class RecommendationStatus(StrEnum):
    NEW = "NEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    BESTPRICE = "BESTPRICE"


class OrderStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.REJECTED,
            OrderStatus.EXECUTED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        )

    @property
    def counts_in_daily_limits(self) -> bool:
        return self in (
            OrderStatus.SENT,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.EXECUTED,
            OrderStatus.SUBMIT_UNKNOWN,
        )


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    SANDBOX = "SANDBOX"
    REAL = "REAL"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SyncStatus(StrEnum):
    RUNNING = "RUNNING"
    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
