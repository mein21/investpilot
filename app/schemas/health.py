from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.enums import TradingMode


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    app: str
    client: Literal["mock", "rest"]
    sandbox: bool
    trading_mode: TradingMode
