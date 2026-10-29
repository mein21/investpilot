"""All ORM models must be imported before ``Base.metadata.create_all``."""

from app.models.account import Account, SyncRun
from app.models.audit import AppSetting, AuditLog, RiskEvent, RiskLimit
from app.models.goal import Goal, GoalAllocation
from app.models.instrument import Instrument
from app.models.instrument_metric import InstrumentMetric
from app.models.notification import Notification
from app.models.operation import Operation
from app.models.order import Order, OrderExecution
from app.models.position import CashBalance, FxRate, Position
from app.models.recommendation import Recommendation
from app.models.snapshot import PortfolioSnapshot
from app.models.user import User

__all__ = [
    "Account",
    "AppSetting",
    "AuditLog",
    "CashBalance",
    "FxRate",
    "Goal",
    "GoalAllocation",
    "Instrument",
    "InstrumentMetric",
    "Notification",
    "Operation",
    "Order",
    "OrderExecution",
    "PortfolioSnapshot",
    "Position",
    "Recommendation",
    "RiskEvent",
    "RiskLimit",
    "SyncRun",
    "User",
]
