"""Official T-Invest REST adapter.

The public gateway exposes every RPC as ``POST /<Service>/<Method>``. Read
methods are semantically safe and may be retried; order submission and other
mutations are never retried automatically.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.clock import Clock, SystemClock
from app.core.enums import AssetClass, OrderType
from app.core.money import decimal_to_quotation
from app.core.time import ensure_utc
from app.integrations.tinvest.client import (
    CandleInterval,
    IdType,
    InstrumentKind,
    TInvestClient,
)
from app.integrations.tinvest.dto import (
    ErrorResponseDTO,
    GetAccountsResponseDTO,
    GetCandlesResponseDTO,
    GetLastPricesResponseDTO,
    GetOperationsResponseDTO,
    GetOrdersResponseDTO,
    GetTradingStatusesResponseDTO,
    InstrumentDetailsResponseDTO,
    InstrumentResponseDTO,
    InstrumentsListResponseDTO,
    OpenSandboxAccountResponseDTO,
    OrderStateDTO,
    PortfolioResponseDTO,
    PositionsResponseDTO,
    PostOrderResponseDTO,
    SandboxPayInResponseDTO,
)
from app.integrations.tinvest.exceptions import (
    TInvestAuthError,
    TInvestError,
    TInvestForbiddenError,
    TInvestNotFoundError,
    TInvestOrderOutcomeUnknown,
    TInvestRateLimitError,
    TInvestTimeoutError,
    TInvestUnavailableError,
    TInvestValidationError,
)
from app.integrations.tinvest.mapping import (
    map_account,
    map_candle,
    map_instrument,
    map_operation,
    map_order_result,
    map_portfolio,
    map_positions,
    map_price,
    map_trading_status,
    map_universe_instrument,
    order_side_to_broker,
    order_type_to_broker,
)
from app.integrations.tinvest.types import (
    AccountInfo,
    CandleInfo,
    InstrumentInfo,
    OperationInfo,
    OrderRequest,
    OrderResult,
    PortfolioInfo,
    PositionsInfo,
    PriceInfo,
    TradingStatusInfo,
)

LOGGER = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

READ_CHUNK_SIZE = 100
CONTRACT_PACKAGE = "tinkoff.public.invest.api.contract.v1"
UNIVERSE_KIND_MAP: dict[str, tuple[str, AssetClass, str]] = {
    "shares": ("Shares", AssetClass.STOCK, "share"),
    "bonds": ("Bonds", AssetClass.BOND, "bond"),
    "etfs": ("Etfs", AssetClass.ETF, "etf"),
    "currencies": ("Currencies", AssetClass.CURRENCY, "currency"),
    "futures": ("Futures", AssetClass.OTHER, "futures"),
}
CANDLE_INTERVAL_MAP = {
    "1min": "CANDLE_INTERVAL_1_MIN",
    "5min": "CANDLE_INTERVAL_5_MIN",
    "15min": "CANDLE_INTERVAL_15_MIN",
    "hour": "CANDLE_INTERVAL_HOUR",
    "day": "CANDLE_INTERVAL_DAY",
    "week": "CANDLE_INTERVAL_WEEK",
    "month": "CANDLE_INTERVAL_MONTH",
}
MAX_RETRY_AFTER_SECONDS = 5.0
MAX_ERROR_MESSAGE = 300

ID_TYPE_MAP: dict[IdType, str] = {
    "uid": "INSTRUMENT_ID_TYPE_UID",
    "figi": "INSTRUMENT_ID_TYPE_FIGI",
    "ticker": "INSTRUMENT_ID_TYPE_TICKER",
}

DETAILS_METHOD_BY_TYPE = {
    "share": "InstrumentsService/ShareBy",
    "stock": "InstrumentsService/ShareBy",
    "bond": "InstrumentsService/BondBy",
    "etf": "InstrumentsService/EtfBy",
    "currency": "InstrumentsService/CurrencyBy",
}


def _normalized_type(value: str) -> str:
    return value.strip().lower().removeprefix("instrument_type_").replace("-", "_")


def _utc_iso(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _chunks(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class TInvestRestClient(TInvestClient):
    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        app_name: str = "InvestPilotLocal",
        sandbox: bool = True,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        currency: str = "RUB",
        proxy: str | None = None,
        verify: str | bool = True,
        clock: Clock | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        token = token.strip()
        if not token:
            raise TInvestValidationError("T-Invest token is empty")
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise TInvestValidationError("T-Invest base URL must use HTTPS")
        if max_retries < 0:
            raise TInvestValidationError("max_retries must be non-negative")
        self._token = token
        self._base_url = base_url
        self._app_name = app_name
        self._sandbox = sandbox
        self._currency = currency.upper()
        self._max_retries = max_retries
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._clock = clock or SystemClock()
        self._sleep = sleep
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers=self._headers(),
            proxy=proxy or None,
            verify=verify,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "x-app-name": self._app_name,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{self._app_name}/0.1",
        }

    @property
    def is_sandbox(self) -> bool:
        return self._sandbox

    def __repr__(self) -> str:
        return (
            f"TInvestRestClient(base_url={self._base_url!r}, sandbox={self._sandbox!r}, token=***)"
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ----- transport ----------------------------------------------------

    def _url(self, service_method: str) -> str:
        method = service_method.lstrip("/")
        if self._base_url.endswith(CONTRACT_PACKAGE):
            return f"{self._base_url}.{method}"
        return f"{self._base_url}/{CONTRACT_PACKAGE}.{method}"

    def _post(
        self,
        service_method: str,
        payload: dict[str, Any],
        *,
        retry: bool,
        uncertain_outcome: bool = False,
    ) -> dict[str, Any]:
        attempts = self._max_retries + 1 if retry else 1
        last_network_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._http.post(
                    self._url(service_method),
                    json=payload,
                    headers=self._headers(),
                )
            except httpx.TimeoutException as exc:
                last_network_error = exc
                if retry and attempt + 1 < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                if uncertain_outcome:
                    raise TInvestTimeoutError(
                        "T-Invest order submission timed out; outcome is unknown",
                        code="TIMEOUT",
                    ) from exc
                raise TInvestUnavailableError(
                    "T-Invest request timed out",
                    code="TIMEOUT",
                ) from exc
            except httpx.TransportError as exc:
                last_network_error = exc
                if retry and attempt + 1 < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                if uncertain_outcome:
                    raise TInvestOrderOutcomeUnknown(
                        "T-Invest connection failed during order submission; outcome is unknown",
                        code="TRANSPORT",
                    ) from exc
                raise TInvestUnavailableError(
                    "T-Invest connection failed",
                    code="TRANSPORT",
                ) from exc

            if response.status_code >= 500:
                if uncertain_outcome:
                    raise TInvestOrderOutcomeUnknown(
                        "T-Invest returned a server error during order submission; "
                        "outcome is unknown",
                        status_code=response.status_code,
                    )
                if retry and attempt + 1 < attempts:
                    self._sleep(self._retry_after(response, attempt))
                    continue
                raise TInvestUnavailableError(
                    "T-Invest service is unavailable",
                    status_code=response.status_code,
                )

            if response.status_code == 429:
                if retry and attempt + 1 < attempts:
                    self._sleep(self._retry_after(response, attempt))
                    continue
                raise TInvestRateLimitError(
                    "T-Invest rate limit exceeded",
                    status_code=response.status_code,
                )

            if response.status_code >= 400:
                raise self._http_error(response)

            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError as exc:
                raise TInvestValidationError(
                    "T-Invest returned a non-JSON response",
                    status_code=response.status_code,
                ) from exc
            if not isinstance(data, dict):
                raise TInvestValidationError(
                    "T-Invest returned an unexpected JSON shape",
                    status_code=response.status_code,
                )
            return data

        raise TInvestUnavailableError(
            "T-Invest request failed after retries",
            code="RETRIES_EXHAUSTED",
        ) from last_network_error

    def _backoff(self, attempt: int) -> float:
        return self._retry_backoff_seconds * (2**attempt)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "").strip()
        if raw:
            try:
                return min(max(float(raw), 0.0), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return self._backoff(attempt)

    def _http_error(self, response: httpx.Response) -> TInvestError:
        code: int | str | None = None
        message = ""
        description: str | None = None
        try:
            data = response.json()
            if isinstance(data, dict):
                error = ErrorResponseDTO.model_validate(data)
                code = error.code
                message = error.message
                description = error.description
        except (ValueError, ValidationError):
            pass
        message = message.strip()[:MAX_ERROR_MESSAGE] or "T-Invest request failed"
        status = response.status_code
        if status == 401:
            return TInvestAuthError(
                "T-Invest authentication failed",
                status_code=status,
                code=code,
                description=description,
            )
        if status == 403:
            return TInvestForbiddenError(
                "T-Invest access is forbidden",
                status_code=status,
                code=code,
                description=description,
            )
        if status == 404:
            return TInvestNotFoundError(
                message,
                status_code=status,
                code=code,
                description=description,
            )
        if status == 429:
            return TInvestRateLimitError(
                "T-Invest rate limit exceeded",
                status_code=status,
                code=code,
                description=description,
            )
        if status == 400:
            return TInvestValidationError(
                message,
                status_code=status,
                code=code,
                description=description,
            )
        return TInvestError(
            message,
            status_code=status,
            code=code,
            description=description,
        )

    def _validated(self, model: type[T], data: dict[str, Any]) -> T:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise TInvestValidationError(f"T-Invest returned an invalid {model.__name__}") from exc

    def _read(
        self,
        service_method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._post(service_method, payload, retry=True)

    # ----- accounts and portfolio --------------------------------------

    def get_accounts(self) -> list[AccountInfo]:
        method = (
            "SandboxService/GetSandboxAccounts" if self._sandbox else "UsersService/GetAccounts"
        )
        dto = self._validated(GetAccountsResponseDTO, self._read(method, {}))
        return [map_account(account, is_sandbox=self._sandbox) for account in dto.accounts]

    def get_portfolio(self, account_id: str) -> PortfolioInfo:
        dto = self._validated(
            PortfolioResponseDTO,
            self._read(
                "OperationsService/GetPortfolio",
                {"accountId": account_id, "currency": self._currency},
            ),
        )
        return map_portfolio(dto, received_at=self._clock.now())

    def get_positions(self, account_id: str) -> PositionsInfo:
        dto = self._validated(
            PositionsResponseDTO,
            self._read(
                "OperationsService/GetPositions",
                {"accountId": account_id},
            ),
        )
        return map_positions(dto, received_at=self._clock.now())

    def get_operations(
        self,
        account_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[OperationInfo]:
        dto = self._validated(
            GetOperationsResponseDTO,
            self._read(
                "OperationsService/GetOperations",
                {
                    "accountId": account_id,
                    "from": _utc_iso(date_from),
                    "to": _utc_iso(date_to),
                },
            ),
        )
        return [map_operation(operation) for operation in dto.operations]

    # ----- instruments and market data --------------------------------

    def get_instrument(
        self,
        instrument_id: str,
        id_type: IdType = "uid",
        class_code: str | None = None,
    ) -> InstrumentInfo:
        if id_type not in ID_TYPE_MAP:
            raise TInvestValidationError("unsupported T-Invest id type")
        request: dict[str, Any] = {
            "id": instrument_id,
            "idType": ID_TYPE_MAP[id_type],
        }
        if class_code:
            request["classCode"] = class_code
        common = self._validated(
            InstrumentResponseDTO,
            self._read("InstrumentsService/GetInstrumentBy", request),
        ).instrument
        method = DETAILS_METHOD_BY_TYPE.get(
            _normalized_type(common.instrument_type or common.instrument_kind)
        )
        details = None
        if method is not None:
            try:
                details = self._validated(
                    InstrumentDetailsResponseDTO,
                    self._read(method, request),
                ).instrument
            except TInvestNotFoundError:
                LOGGER.warning(
                    "instrument details are unavailable for %s; using common data",
                    instrument_id,
                )
        return map_instrument(common, details)

    def get_candles(
        self,
        instrument_id: str,
        date_from: datetime,
        date_to: datetime,
        interval: CandleInterval = "day",
    ) -> list[CandleInfo]:
        method = CANDLE_INTERVAL_MAP.get(interval)
        if method is None:
            raise TInvestValidationError("unsupported candle interval")
        dto = self._validated(
            GetCandlesResponseDTO,
            self._read(
                "MarketDataService/GetCandles",
                {
                    "instrumentId": instrument_id,
                    "from": _utc_iso(date_from),
                    "to": _utc_iso(date_to),
                    "interval": method,
                },
            ),
        )
        return [map_candle(candle) for candle in dto.candles]

    def list_instruments(self, kind: InstrumentKind = "shares") -> list[InstrumentInfo]:
        entry = UNIVERSE_KIND_MAP.get(kind)
        if entry is None:
            raise TInvestValidationError("unsupported instrument kind")
        method, asset_class, instrument_type = entry
        dto = self._validated(
            InstrumentsListResponseDTO,
            self._read(
                f"InstrumentsService/{method}",
                {"instrumentStatus": "INSTRUMENT_STATUS_BASE"},
            ),
        )
        return [
            map_universe_instrument(
                item,
                asset_class=asset_class,
                instrument_type=instrument_type,
            )
            for item in dto.instruments
        ]

    def get_last_prices(self, instrument_ids: list[str]) -> list[PriceInfo]:
        if not instrument_ids:
            return []
        received_at = self._clock.now()
        result: list[PriceInfo] = []
        for chunk in _chunks(instrument_ids, READ_CHUNK_SIZE):
            dto = self._validated(
                GetLastPricesResponseDTO,
                self._read(
                    "MarketDataService/GetLastPrices",
                    {"instrumentId": list(chunk)},
                ),
            )
            result.extend(map_price(price, received_at=received_at) for price in dto.last_prices)
        return result

    def get_trading_statuses(
        self,
        instrument_ids: list[str],
    ) -> list[TradingStatusInfo]:
        if not instrument_ids:
            return []
        result: list[TradingStatusInfo] = []
        for chunk in _chunks(instrument_ids, READ_CHUNK_SIZE):
            dto = self._validated(
                GetTradingStatusesResponseDTO,
                self._read(
                    "MarketDataService/GetTradingStatuses",
                    {"instrumentId": list(chunk)},
                ),
            )
            result.extend(map_trading_status(status) for status in dto.trading_statuses)
        return result

    # ----- orders -------------------------------------------------------

    def post_order(self, request: OrderRequest) -> OrderResult:
        if not request.client_order_id or len(request.client_order_id) > 36:
            raise TInvestValidationError(
                "client_order_id must be a non-empty UID of at most 36 characters"
            )
        if request.lots <= 0:
            raise TInvestValidationError("lots must be positive")
        if request.order_type is OrderType.LIMIT and (
            request.limit_price is None or request.limit_price <= 0
        ):
            raise TInvestValidationError("limit order requires a positive limit price")
        payload: dict[str, Any] = {
            "accountId": request.account_id,
            "instrumentId": request.instrument_id,
            "quantity": str(request.lots),
            "direction": order_side_to_broker(request.side),
            "orderType": order_type_to_broker(request.order_type),
            "orderId": request.client_order_id,
            "timeInForce": request.time_in_force,
            "priceType": request.price_type,
            "confirmMarginTrade": False,
        }
        if request.order_type is OrderType.LIMIT:
            assert request.limit_price is not None
            units, nano = decimal_to_quotation(request.limit_price)
            payload["price"] = {"units": str(units), "nano": nano}
        raw = self._post(
            "OrdersService/PostOrder",
            payload,
            retry=False,
            uncertain_outcome=True,
        )
        try:
            dto = self._validated(PostOrderResponseDTO, raw)
            return map_order_result(dto)
        except TInvestValidationError as exc:
            raise TInvestOrderOutcomeUnknown(
                "T-Invest accepted the HTTP request but its order response is invalid; "
                "outcome is unknown",
                status_code=200,
            ) from exc

    def get_order_state(
        self,
        account_id: str,
        broker_order_id: str,
        lot_size: int = 1,
    ) -> OrderResult:
        if lot_size < 1:
            raise TInvestValidationError("lot_size must be positive")
        dto = self._validated(
            OrderStateDTO,
            self._read(
                "OrdersService/GetOrderState",
                {
                    "accountId": account_id,
                    "orderId": broker_order_id,
                },
            ),
        )
        return map_order_result(dto, lot_size=lot_size)

    def get_orders(self, account_id: str) -> list[OrderResult]:
        dto = self._validated(
            GetOrdersResponseDTO,
            self._read(
                "OrdersService/GetOrders",
                {"accountId": account_id},
            ),
        )
        return [map_order_result(order) for order in dto.orders]

    def cancel_order(self, account_id: str, broker_order_id: str) -> None:
        self._post(
            "OrdersService/CancelOrder",
            {
                "accountId": account_id,
                "orderId": broker_order_id,
            },
            retry=False,
        )

    # ----- sandbox ------------------------------------------------------

    def _require_sandbox(self) -> None:
        if not self._sandbox:
            raise TInvestValidationError("sandbox operation requires sandbox mode")

    def open_sandbox_account(self, name: str) -> str:
        self._require_sandbox()
        dto = self._validated(
            OpenSandboxAccountResponseDTO,
            self._post(
                "SandboxService/OpenSandboxAccount",
                {"name": name},
                retry=False,
            ),
        )
        if not dto.account_id:
            raise TInvestValidationError("sandbox account response has no account id")
        return dto.account_id

    def sandbox_pay_in(
        self,
        account_id: str,
        currency: str,
        amount: Decimal,
    ) -> Decimal:
        self._require_sandbox()
        units, nano = decimal_to_quotation(amount)
        dto = self._validated(
            SandboxPayInResponseDTO,
            self._post(
                "SandboxService/SandboxPayIn",
                {
                    "accountId": account_id,
                    "amount": {
                        "currency": currency.upper(),
                        "units": str(units),
                        "nano": nano,
                    },
                },
                retry=False,
            ),
        )
        if dto.balance is None:
            raise TInvestValidationError("sandbox pay-in response has no balance")
        return dto.balance.to_decimal()

    def close_sandbox_account(self, account_id: str) -> None:
        self._require_sandbox()
        self._post(
            "SandboxService/CloseSandboxAccount",
            {"accountId": account_id},
            retry=False,
        )
