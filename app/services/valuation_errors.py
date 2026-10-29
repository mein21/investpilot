from __future__ import annotations


class MissingFxRateError(ValueError):
    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"missing FX rate {source}->{target}")
        self.source = source
        self.target = target
