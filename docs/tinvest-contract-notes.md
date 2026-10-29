# T-Invest API contract notes

Verified against the official contract repository, not against guesses.

- Source: `https://github.com/RussianInvestments/investAPI`
- Local clone: `/tmp/investAPI`
- Commit: `3eaf23a25f598fe483c913184acdbd9132bc68d2`
- Release: `1.43`, dated 2025-11-07
- OpenAPI: `src/docs/swagger-ui/openapi.yaml`

`/tmp` is temporary. These notes are the durable project reference. Anything
not checked here is marked as TODO and must not be assumed.

## Transport

- Production base URL: `https://invest-public-api.tinkoff.ru/rest`
- Sandbox base URL: `https://sandbox-invest-public-api.tinkoff.ru/rest`
- Authentication: `Authorization: Bearer <token>`
- Application header: `x-app-name: <name>`
- JSON body, lowerCamelCase field names.
- The REST gateway exposes RPCs as
  `POST /rest/tinkoff.public.invest.api.contract.v1.<Service>/<Method>`,
  including reads. Verified live: omitting the `tinkoff.public.invest.api
  .contract.v1` package segment returns HTTP 404. The client appends the
  package automatically when `TINVEST_BASE_URL` ends at `/rest`.
- Reads are semantically safe and may be retried. Order submission and other
  mutations are not retried automatically.
- TLS to `*.tinkoff.ru` uses the `Russian Trusted Root CA`; a host without it in
  its trust store must add that root explicitly.

## Used endpoints

| Purpose | Method | Key request fields |
| --- | --- | --- |
| Accounts | `UsersService/GetAccounts` | `{}` or `status` |
| Sandbox accounts | `SandboxService/GetSandboxAccounts` | `{}` or `status` |
| Portfolio | `OperationsService/GetPortfolio` | `accountId`, `currency` |
| Cash/positions | `OperationsService/GetPositions` | `accountId` |
| Operations | `OperationsService/GetOperations` | `accountId`, `from`, `to` |
| Instrument common data | `InstrumentsService/GetInstrumentBy` | `id`, `idType`, `classCode` |
| Share details | `InstrumentsService/ShareBy` | same |
| Bond details | `InstrumentsService/BondBy` | same |
| ETF details | `InstrumentsService/EtfBy` | same |
| Currency details | `InstrumentsService/CurrencyBy` | same |
| Instrument classes | `InstrumentsService/Shares`, `Bonds`, `Etfs`, `Currencies`, `Futures` | `instrumentStatus` |
| Last prices | `MarketDataService/GetLastPrices` | `instrumentId[]` |
| Order book | `MarketDataService/GetOrderBook` | `instrumentId`, `depth` |
| Candles | `MarketDataService/GetCandles` | `instrumentId`, `from`, `to`, `interval`, `limit` |
| Trading statuses | `MarketDataService/GetTradingStatuses` | `instrumentId[]` |
| Indicatives | `InstrumentsService/Indicatives` | `{}` |
| Dividends | `InstrumentsService/GetDividends` | `instrumentId`, `from`, `to` |
| Asset fundamentals | `InstrumentsService/GetAssetFundamentals` | `assets[]` |
| Submit order | `OrdersService/PostOrder` | see below |
| Order state | `OrdersService/GetOrderState` | `accountId`, `orderId` |
| Active orders | `OrdersService/GetOrders` | `accountId` |
| Cancel order | `OrdersService/CancelOrder` | `accountId`, `orderId` |
| Open sandbox account | `SandboxService/OpenSandboxAccount` | `name` |
| Sandbox pay-in | `SandboxService/SandboxPayIn` | `accountId`, `amount` |
| Close sandbox account | `SandboxService/CloseSandboxAccount` | `accountId` |

`idType` values used: `INSTRUMENT_ID_TYPE_UID`, `INSTRUMENT_ID_TYPE_FIGI`,
`INSTRUMENT_ID_TYPE_TICKER`. For `TICKER`, `classCode` is required.

## Account access

- `UsersService/GetAccounts` returns `accessLevel` per account.
- Verified live values: `ACCOUNT_ACCESS_LEVEL_FULL_ACCESS` and
  `ACCOUNT_ACCESS_LEVEL_READ_ONLY`.
- A read-only token can read portfolios and market data but cannot submit
  orders. Pre-trade risk treats every real-mode account without
  `ACCOUNT_ACCESS_LEVEL_FULL_ACCESS` as blocked (`ACCOUNT_TRADING_ACCESS`).

## Money and quantity shapes

- `Quotation`: integer `units` plus integer `nano` (1/1e9).
- `MoneyValue`: `currency` plus integer `units` plus integer `nano`.
- Integer fields may arrive as JSON strings in the REST gateway.
- `quantity` in `PostOrderRequest` is a number of **lots**, not shares.
- `lotsRequested` / `lotsExecuted` in order responses are lot counts.

## Order request

`PostOrderRequest` fields used:

```json
{
  "accountId": "...",
  "instrumentId": "uid or figi",
  "quantity": "1",
  "direction": "ORDER_DIRECTION_BUY",
  "orderType": "ORDER_TYPE_LIMIT",
  "orderId": "client-generated UUID",
  "timeInForce": "TIME_IN_FORCE_DAY",
  "priceType": "PRICE_TYPE_CURRENCY",
  "confirmMarginTrade": false,
  "price": {"units": "300", "nano": 250000000}
}
```

- `orderId` is the idempotency key, max 36 characters.
- `price` is required for a limit order. Market/best-price handling still needs
  a live sandbox check before the first real order slice.
- `ORDER_TYPE_LIMIT`, `ORDER_TYPE_MARKET`, `ORDER_TYPE_BESTPRICE` are verified
  values.
- Status values: `EXECUTION_REPORT_STATUS_FILL`, `PARTIALLYFILL`,
  `REJECTED`, `CANCELLED`, `NEW`, `UNSPECIFIED`.
- Order state stages (`stages[]`) carry `price`, `quantity`, `tradeId`,
  `executionTime`; partial executions must not be hidden.
- `PostOrder` must never be retried after a timeout, transport failure, 5xx or
  invalid 200 response. The outcome is represented as unknown.

## Pricing formulas

From the official docs in the cloned repository
(`src/docs/faq_marketdata.md`, `src/docs/points.md`):

- Shares/ETF: `price * lot`.
- Bonds: `price / 100 * nominal`; add `current_nkd` for the full cash amount.
- Currency: `price * lot / nominal`.
- Bond prices are quoted in percent of nominal, not in rubles per bond.
- `aci_value` in order responses is NKD per lot; the order total adds
  `aci_value * lots`.

## Candles

`GetCandlesRequest` fields used: `instrumentId`, `from`, `to`, `interval`.
- `instrumentId` accepts `figi`, `instrument_uid`, or `ticker_class_code`.
- `interval` values used: `CANDLE_INTERVAL_1_MIN`, `_5_MIN`, `_15_MIN`,
  `_HOUR`, `_DAY`, `_WEEK`, `_MONTH`.
- Response `candles[]` is `v1HistoricCandle`: `open`/`high`/`low`/`close` as
  `Quotation`, `volume` as int64 string (lots), `time`, `isComplete`.
- Read-only; used by the buy-and-hold backtest, the deterministic growth
  analysis (momentum, SMA trend, drawdown) and the screener. For the screener,
  `limit` is 2400 daily bars; the service returns at most six years.
- Screener pacing: InstrumentsService ~200 requests/min (`0.32 s` minimum
  interval), MarketDataService ~600 requests/min (`0.12 s` minimum interval).
  `GetAssetFundamentals` is called in chunks of 100 asset UIDs.
- Screener loads `InstrumentsService/Shares` with `INSTRUMENT_STATUS_ALL` to
  preserve delisted listings for history; the current cache holds 8048
  listings, of which 1912 are active.

## Instrument universe

List endpoints return `{instruments: [...]}` with a common shape (`uid`, `figi`,
`ticker`, `classCode`, `isin`, `lot`, `currency`, `name`, `exchange`, `sector`,
`countryOfRisk`, `tradingStatus`, `buyAvailableFlag`, `sellAvailableFlag`,
`apiTradeAvailableFlag`, `liquidityFlag`). Bonds additionally expose
`couponQuantityPerYear`, `maturityDate`, `nominal`. Request:
`{"instrumentStatus": "INSTRUMENT_STATUS_BASE"}`. Live check loaded 3847 items
across shares/bonds/etfs/currencies; used to build the buy universe.
The screener uses `INSTRUMENT_STATUS_ALL` for shares so inactive listings can
carry historical bars; current selection still filters `isActive` and
`buyAvailableFlag`.

## Retry policy implemented

- Safe reads: retry on connection failure, timeout, HTTP 429 and HTTP 5xx.
- Respect `Retry-After` for 429, capped.
- Order submission: exactly one attempt. Timeout/transport/5xx/invalid success
  response raises an explicit unknown-outcome error.
- Cancellation/sandbox mutations: one attempt, no automatic retry.

## Sandbox

- Use `SandboxService/GetSandboxAccounts` when sandbox mode is active.
- `TINVEST_SANDBOX=true` is the default.
- Sandbox account creation, pay-in and close are implemented in the REST
  adapter and mock adapter; live sandbox calls are TODO until a disposable test
  account is available.
- Real trading is disabled by default and requires explicit configuration plus
  confirmation.

## Deliberate TODOs

- `GetOperationsByCursor` is not used yet; the simple `GetOperations` window is
  enough for the first sync slice.
- Batch instrument lookup is not used; the adapter fetches instruments one by
  one. This is correct but may need batching for large portfolios.
- Live sandbox read path exercised with a real token: `GetSandboxAccounts`,
  `OpenSandboxAccount`, `SandboxPayIn`, `GetInstrumentBy`, `GetLastPrices`,
  `GetTradingStatuses` all return 200. Sandbox account creation and pay-in work.
- Live sandbox `PostOrder` was rejected with
  `Instrument is not available for trading` because the market was closed
  (trading status `SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING` for all
  tested instruments). Order placement/status/cancel still needs a check during
  MOEX trading hours.
- Token scope requirements for read-only versus order access are not documented
  here yet.
