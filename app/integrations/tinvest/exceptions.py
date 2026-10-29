"""Broker errors that never contain tokens, headers or raw request bodies."""

from __future__ import annotations


class TInvestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | str | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.description = description


class TInvestNotConfigured(TInvestError):
    pass


class TInvestAuthError(TInvestError):
    pass


class TInvestForbiddenError(TInvestError):
    pass


class TInvestNotFoundError(TInvestError):
    pass


class TInvestRateLimitError(TInvestError):
    pass


class TInvestUnavailableError(TInvestError):
    pass


class TInvestOrderOutcomeUnknown(TInvestError):
    """The broker may have accepted the order; never retry automatically."""


class TInvestTimeoutError(TInvestOrderOutcomeUnknown):
    """Order submission timed out; the outcome is unknown."""


class TInvestValidationError(TInvestError):
    pass
