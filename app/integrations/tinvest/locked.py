"""Serialize access to a shared broker client across web and scheduler threads."""

from __future__ import annotations

import threading
from typing import Any, cast

from app.integrations.tinvest.client import TInvestClient


class LockedTInvestClient:
    def __init__(self, delegate: TInvestClient) -> None:
        self._delegate = delegate
        self._lock = threading.RLock()

    @property
    def is_mock(self) -> bool:
        return bool(self._delegate.is_mock)

    @property
    def is_sandbox(self) -> bool:
        return bool(self._delegate.is_sandbox)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if not callable(attribute):
            return attribute

        def locked(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return locked

    def close(self) -> None:
        with self._lock:
            self._delegate.close()

    def __repr__(self) -> str:
        return f"LockedTInvestClient({self._delegate!r})"


def locked_client(client: TInvestClient) -> TInvestClient:
    if isinstance(client, LockedTInvestClient):
        return cast(TInvestClient, client)
    return cast(TInvestClient, LockedTInvestClient(client))


__all__ = ["LockedTInvestClient", "locked_client"]
