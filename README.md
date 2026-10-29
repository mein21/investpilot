# InvestPilot Local

Локальный детерминированный менеджер портфеля для T-Invest API. Runtime не
использует AI/LLM/ML, генерацию текста или внешние AI API. Рекомендации
считаются правилами и формулами, а каждая заявка требует явного подтверждения.

> Статус: рабочий локальный вертикальный срез. Есть синхронизация, оценка
> портфеля, P&L, цели, риск-движок, рекомендации, заявки, web UI, CLI,
> планировщик снимков и резервные копии. Живой REST-контур проверен на чтение
> (счета, портфель, инструменты, свечи) и на sandbox-операции (создание счёта,
> pay-in). Боевой токен перевыпущен с `ACCOUNT_ACCESS_LEVEL_FULL_ACCESS`;
> реальные заявки разрешены только в `CONFIRM` и после явного подтверждения.

## Возможности

- Синхронизация счетов, инструментов, позиций, cash, операций и FX из T-Invest
  mock или официального REST API. Переключатель счёта в шапке изолирует
  страницы и действия по выбранному `account_id`.
- Средняя стоимость, реализованный/нереализованный P&L, комиссии, дивиденды и
  купоны; флаг неполной истории.
- Оценка портфеля в базовой валюте, аллокация и концентрация эмитентов.
- Цели: сумма, дата, регулярный взнос, риск-профиль, диапазоны классов,
  stop-loss/take-profit и прогноз прогресса.
- Риск-проверки: лимиты заявки и дня, cash reserve, концентрация, доступность
  торгов, свежесть котировки, отклонение цены, аварийная остановка.
- Детерминированные BUY/SELL/HOLD/REBALANCE-рекомендации с TTL, fingerprint и
  audit trail. Продажа создаётся только при превышении `max_percent`, лимита
  эмитента, исключении инструмента, финансировании цели или stop-правилах.
- Прозрачный score в `app/rules/scoring.py`: у каждого компонента есть описание,
  диапазон 0–100, вес, вклад и причина. Для покупок без свежего screener run
  учитываются потребность в аллокации, риск-скорректированный импульс
  1/3/6/12 месяцев, качество тренда (SMA50/SMA200), соответствие риску,
  диверсификация, устойчивость, доходность и ликвидность. Score — правило
  ранжирования, не прогноз цены и не гарантия доходности.
- Детерминированный анализ роста бумаг по дневным свечам T-Invest
  (`app/rules/growth.py`, `app/services/growth.py`): momentum с поправкой на
  волатильность, доля дней выше SMA50/SMA200, дистанция до средних, максимальная
  просадка за 6 месяцев и средний оборот. Метрики кэшируются в
  `instrument_metrics`; для неликвидных или новых бумаг без истории применяется
  нейтральная оценка, а не выдуманные данные. Страница «Рекомендации» показывает
  топ перспективных бумаг и кнопку обновления анализа.
- Скринер российских акций с отдельным SQLite-кэшем (`app/screener`): жёсткие
  фильтры вселенной, качество данных, ликвидность, спред, детерминированный
  рейтинг и секторный лимит. CLI `screen` обновляет кэш, `screen-backtest`
  строит walk-forward проверку; веб-страница `/screener` только читает последний
  сохранённый расчёт и умеет пересчитать его без сети. Score — правило
  ранжирования, не вероятность роста. Формулы, пороги и ограничения описаны в
  [`docs/screener/methodology.md`](docs/screener/methodology.md).
- Свежий screener run (`SCREENER_DB_PATH`, не старше
  `SCREENER_RATING_MAX_AGE_HOURS`, по умолчанию 72 ч) используется в
  рекомендациях: BUY по акциям выбираются только из кандидатов скринера, а
  рейтинг скринера занимает 40% score вместо импульса/тренда/устойчивости.
  Остальные 60% — потребность в аллокации, риск, диверсификация, доходность и
  ликвидность. Если run отсутствует или устарел, включается growth-фолбэк с
  предупреждением; ETF, облигации и валюта считаются как раньше.
- `REBALANCE` — портфельный план из отдельных BUY/SELL-ног; сам план не является
  заявкой, одобрять нужно каждую ногу отдельно.
- Stop-loss/take-profit сравнивают текущую стоимость единицы с `average_price` и
  создают принудительный выход с правилом `MANDATORY_EXIT`; такой выход может
  опустить класс ниже `min_percent`.
- `max_drawdown_percent` считается от пика `PortfolioSnapshot` и блокирует новые
  покупки правилом `MAX_DRAWDOWN`.
- За `GOAL_FUNDING_WINDOW_DAYS=30` до даты достигнутой цели продаёт активы до
  целевой суммы cash; если портфель ещё не достиг цели, выводит предупреждение.
- В `OBSERVE` доступны только чтение и синхронизация: генерация рекомендаций и
  заявки отключены.
- Заявки: отдельное одобрение, отдельная отправка, идемпотентный UUID,
  запрет повторной отправки при неизвестном исходе, сверка и отмена.
- Web UI на Jinja2/HTMX/Chart.js, локальная аутентификация, CSRF, сессии,
  маскирование секретов, журнал действий.
- In-app уведомления о заявках, ошибках синхронизации и аварийной остановке.
- Buy-кандидаты выбираются среди всей загруженной вселенной, а не только
  текущих позиций; при свежем screener run акции дополнительно ограничены его
  кандидатами. Кнопка «Загрузить все инструменты» (и CLI `investpilot
  universe`) тянет классы из `UNIVERSE_KINDS` (по умолчанию акции, фонды,
  валюты; при желании облигации) и включает их в рекомендации; отдельную бумагу
  можно добавить по тикеру.
- Сценарный анализ по шокам классов и статистика истории портфеля по snapshots.
- Backtest buy-and-hold текущего портфеля по историческим дневным свечам
  T-Invest (страница `/backtest`, период 30–730 дней).
- PAPER-состояние mock-клиента сохраняется между перезапусками в
  `data/investpilot.mock-state.json` (настраивается `MOCK_STATE_PATH`).
- Планировщик: sync, universe, growth-анализ, рекомендации, снимки портфеля,
  SQLite backup с retention; заявки автоматически не отправляются.
- Локальный логин ограничен по числу попыток (`LOGIN_MAX_ATTEMPTS` за
  `LOGIN_WINDOW_SECONDS`).
- CLI для инициализации, демо-данных, sync, рекомендаций, анализа роста,
  backup и serve.

## Требования

- Python 3.12+
- Linux/macOS
- Docker и Docker Compose — опционально
- Токен T-Invest только в `.env` или секрет-хранилище

## Быстрый старт с mock

```bash
cd /home/mein/investpilot-local
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

В `.env` замените как минимум:

```dotenv
ADMIN_PASSWORD=длинный_локальный_пароль
SESSION_SECRET=случайная_строка_не_короче_32_символов
```

Сгенерировать секрет:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Для демо оставьте:

```dotenv
TINVEST_CLIENT=mock
TINVEST_SANDBOX=true
SEED_DEMO_DATA=true
AUTH_ENABLED=true
TRADING_MODE=PAPER
```

Запуск:

```bash
.venv/bin/investpilot serve --host 127.0.0.1 --port 8080
```

Откройте `http://127.0.0.1:8080`, войдите под `ADMIN_USERNAME` /
`ADMIN_PASSWORD`. Mock-счёт, позиции, операции и демо-цель создаются при первом
старте. `TRADING_MODE=PAPER` разрешает пройти полный цикл «рекомендация →
одобрение → заявка → mock-исполнение». После исполнения заявки нажмите
«Синхронизировать», чтобы обновить позиции. Mock хранит состояние в памяти
процесса и после перезапуска возвращает демо-позиции; для сохранения бумажного
портфеля между перезапусками используйте T-Invest Sandbox.

## CLI

```bash
.venv/bin/investpilot status
.venv/bin/investpilot init-db
.venv/bin/investpilot seed-demo
.venv/bin/investpilot sync
.venv/bin/investpilot recommend
.venv/bin/investpilot growth
.venv/bin/investpilot screen --no-orderbook
.venv/bin/investpilot screen-backtest --offline --no-refresh --train 1
.venv/bin/investpilot backup
.venv/bin/investpilot serve
```

`seed-demo` работает только с `TINVEST_CLIENT=mock` и не трогает брокерский
счёт. `serve` по умолчанию слушает только loopback; внешний bind требует
`--allow-external`.

Скринер хранит данные отдельно от основного портфеля по пути
`SCREENER_DB_PATH` (по умолчанию `./data/screener.db`). Страница `/screener`
показывает последний сохранённый run; кнопка «Пересчитать по кэшу» не делает
сетевых запросов. Для загрузки рыночных данных сначала выполните
`investpilot screen`. На странице `/recommendations` кнопка «Обновить с рынка»
сначала обновляет screener через T-Invest, затем пересчитывает рекомендации;
«Пересчитать по кэшу» работает без сети. Рекомендации используют последний
`screen`-run, если он не старше `SCREENER_RATING_MAX_AGE_HOURS`; иначе
включается growth-фолбэк.

## T-Invest REST

```dotenv
TINVEST_CLIENT=rest
TINVEST_TOKEN=...
TINVEST_SANDBOX=true
TRADING_MODE=RECOMMEND
```

- Read-only токена достаточно для `OBSERVE` и `RECOMMEND`.
- Для заявок нужен токен с торговыми правами, `TRADING_MODE=CONFIRM`.
- `REAL_TRADING_ENABLED=true` требуется только для реального контура; sandbox
  работает без него.
- `AUTO` намеренно отсутствует в допустимых режимах и отклоняется при старте.
- Заявки не повторяются автоматически. При `SUBMIT_UNKNOWN` доступна только
  сверка (`reconcile`), повторная отправка блокируется.

Проверенные endpoint-ы и формулы: [`docs/tinvest-contract-notes.md`](docs/tinvest-contract-notes.md).

## Планировщик и backup

```dotenv
SCHEDULER_ENABLED=true
SYNC_INTERVAL_MINUTES=5
GROWTH_SCAN_INTERVAL_MINUTES=30
GROWTH_SCAN_LIMIT=100
MARKET_DATA_INTERVAL_MINUTES=60
SNAPSHOT_INTERVAL_MINUTES=60
BACKUP_INTERVAL_HOURS=24
UNIVERSE_AUTO_LOAD_ENABLED=true
UNIVERSE_AUTO_LOAD_INTERVAL_HOURS=24
RECOMMENDATIONS_AUTO_ENABLED=true
RECOMMENDATIONS_AUTO_INTERVAL_MINUTES=30
RECOMMENDATIONS_SCREENER_ENABLED=true
SCREENER_RATING_MAX_AGE_HOURS=72
BACKUP_DIR=./backups
BACKUP_KEEP=7
```

Планировщик работает в фоне web-процесса. Первый запуск заданий происходит
примерно через 30 секунд после старта. Автоматически выполняются sync, загрузка
universe, анализ роста и генерация рекомендаций; заявки при этом **не
отправляются** и по-прежнему требуют явного подтверждения. Рекомендации
обновляются каждые 30 минут и используют свежий growth-кэш и screener run.
SQLite backup использует online backup API и не требует остановки приложения.
Ручной backup:
`investpilot backup`. Для PostgreSQL автоматический backup не реализован.

## Автозапуск (systemd user service)

Готовый unit лежит в `deploy/investpilot.service`. Установка:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/investpilot.service ~/.config/systemd/user/investpilot.service
systemctl --user daemon-reload
systemctl --user enable --now investpilot.service
loginctl enable-linger "$USER"
```

Проверка: `systemctl --user status investpilot.service`, журнал:
`journalctl --user -u investpilot.service -f`. Служба перезапускается при сбое
и стартует автоматически после входа/загрузки (при включённом linger). Порт и
bind-адрес заданы в `ExecStart`; при необходимости измените их в unit-файле.

## Docker

```bash
cp .env.example .env
# заполните ADMIN_PASSWORD и SESSION_SECRET
docker compose up -d --build
```

Приложение доступно только на `127.0.0.1:8080`; данные и backup лежат в named
volumes. Контейнер работает non-root, с read-only root filesystem и без
capabilities. Для PostgreSQL:

```bash
POSTGRES_PASSWORD=... docker compose --profile postgres up -d
```

и задайте `DATABASE_URL=postgresql+psycopg://investpilot:...@db:5432/investpilot`.

## Проверки

```bash
.venv/bin/ruff format app tests
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/pytest tests -q
```

## Безопасность

- Токен T-Invest не попадает в HTML, cookies, логи и audit payload.
- Пароли хранятся как scrypt-хеши; сессии подписаны HMAC, cookie HttpOnly,
  SameSite=Strict и Secure-флагом из настроек.
- Все изменяющие формы защищены CSRF. CSP запрещает внешние скрипты.
- Web по умолчанию слушает loopback. Внешний bind требует явного флага.
- Заявка создаётся только после approval и отправляется отдельным действием.
- Перед отправкой повторно проверяются цена, свежесть котировки, режим торгов,
  баланс, дневные лимиты, концентрация и аварийная остановка.
- `data/`, `backups/`, `.env` и логи исключены из git.

## Структура

```text
app/
  api/            HTTP routes, auth dependencies, CSRF
  core/           настройки, деньги, время, безопасность, логирование
  db/             engine/session и точные SQL-типы
  integrations/   TInvestClient, mock, REST, DTO и mapping
  models/         SQLAlchemy-модели
  risk/           детерминированный риск-движок
  rules/          детерминированные правила, включая score инструментов
  schemas/        Pydantic-схемы транспортных ответов
  notifications/  in-app уведомления
  services/       sync, portfolio, P&L, goals, recommendations, orders,
                  snapshots, scheduler, backup, audit, analysis
  web/            Jinja2/HTMX/Chart.js templates и static assets
tests/            unit/integration-тесты
docs/             контракты T-Invest
```

## Ограничения

- Живой sandbox order flow не завершён: read-путь и sandbox pay-in проверены, но
  `PostOrder` отклоняется вне торговых часов MOEX; нужна проверка в сессию.
- Боевой счёт токена имеет `ACCOUNT_ACCESS_LEVEL_FULL_ACCESS`; реальные заявки
  разрешены только в `CONFIRM` и после отдельного подтверждения, `AUTO` запрещён.
- Вселенная грузится классами (`UNIVERSE_KINDS`); фьючерсы/опционы в неё не
  включаются. Для облигаций номинал берётся из ответа класса, детали по каждой
  бумаге отдельно не запрашиваются.
- `REBALANCE` — только план; отдельной заявки по нему нет.
- Ожидаемые веса нескольких sell-рекомендаций считаются независимо.
- Backtest — buy-and-hold текущих позиций по дневным свечам; без реинвестирования
  дивидендов, комиссий, налогов и исторических FX. Для длительных периодов
  дневная свеча лимитирована 730 днями.
- Уведомления только in-app; email/Telegram/push не реализованы.
- `GetOperations` ограничен окном `TINVEST_OPERATIONS_DAYS`; курсорная
  синхронизация операций запланирована отдельно.
- Автоматический backup для PostgreSQL не реализован.
