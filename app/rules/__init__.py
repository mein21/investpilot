"""Deterministic investment rules, kept separate from services and transport."""

from app.rules.scoring import (
    BUY_COMPONENT_SPECS,
    SELL_COMPONENT_SPECS,
    ComponentSpec,
    ScoreComponent,
    ScoreResult,
    score_buy_candidate,
    score_sell_candidate,
)

__all__ = [
    "BUY_COMPONENT_SPECS",
    "SELL_COMPONENT_SPECS",
    "ComponentSpec",
    "ScoreComponent",
    "ScoreResult",
    "score_buy_candidate",
    "score_sell_candidate",
]
