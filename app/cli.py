"""Command-line entrypoint for administration and diagnostics."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.bootstrap import (
    BootstrapError,
    build_tinvest_client,
    ensure_admin,
    ensure_demo_goal,
)
from app.core.config import Settings
from app.core.enums import Severity
from app.db.session import build_engine, build_session_factory, init_db, session_scope
from app.models.account import Account
from app.models.goal import Goal
from app.screener.backtest import BacktestService
from app.screener.config import ScreenerConfig
from app.screener.report import (
    backtest_to_dict,
    result_to_dict,
    timestamp_slug,
    write_backtest_html,
    write_backtest_markdown,
    write_html,
    write_json,
    write_markdown,
)
from app.screener.screen import ScreenerService
from app.screener.store import ScreenerStore
from app.services.audit import record_audit
from app.services.backup import BackupError, BackupService
from app.services.growth import GrowthService
from app.services.recommendations import RecommendationService
from app.services.snapshots import capture_all_snapshots
from app.services.sync import SyncReport, SyncService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="investpilot",
        description="InvestPilot Local: local deterministic T-Invest portfolio manager",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create database tables and the local admin user")
    sub.add_parser("seed-demo", help="sync deterministic mock data and create a demo goal")
    sub.add_parser("sync", help="synchronize accounts, positions and operations")
    sub.add_parser("universe", help="fetch instrument classes and mark them buy candidates")
    sub.add_parser("recommend", help="generate recommendations for the first account")
    growth = sub.add_parser(
        "growth",
        help="refresh cached momentum/trend metrics for buy candidates",
    )
    growth.add_argument("--limit", type=int, default=None, help="max instruments in this run")
    growth.add_argument("--force", action="store_true", help="ignore cache freshness")
    sub.add_parser("backup", help="create a SQLite backup and apply retention")
    sub.add_parser("status", help="print a safe configuration summary")

    screen = sub.add_parser("screen", help="rank Russian stocks with the deterministic screener")
    screen.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True)
    screen.add_argument("--offline", action="store_true", help="use only the local cache")
    screen.add_argument("--limit", type=int, default=None, help="debug limit for market data")
    screen.add_argument("--top", type=int, default=None, help="max candidates")
    screen.add_argument("--min-score", type=Decimal, default=None)
    screen.add_argument(
        "--mode",
        choices=("technical", "technical_fundamental"),
        default=None,
    )
    screen.add_argument("--as-of", type=date.fromisoformat, default=None)
    screen.add_argument("--db", type=Path, default=None)
    screen.add_argument("--html", type=Path, default=None)
    screen.add_argument("--json", type=Path, default=None)
    screen.add_argument("--md", type=Path, default=None)
    screen.add_argument("--no-orderbook", action="store_true")
    screen.add_argument("--refresh-dividends", action="store_true")
    screen.add_argument("--refresh-fundamentals", action="store_true")

    backtest = sub.add_parser(
        "screen-backtest",
        help="walk-forward validation of the deterministic screener",
    )
    backtest.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True)
    backtest.add_argument("--offline", action="store_true")
    backtest.add_argument("--limit", type=int, default=None)
    backtest.add_argument("--top", type=int, default=None)
    backtest.add_argument("--min-score", type=Decimal, default=None)
    backtest.add_argument("--start", type=date.fromisoformat, default=None)
    backtest.add_argument("--end", type=date.fromisoformat, default=None)
    backtest.add_argument("--train", type=int, default=None, help="train cohorts")
    backtest.add_argument("--step", type=int, default=None, help="sessions per cohort")
    backtest.add_argument("--holdout", type=int, default=None, help="final holdout cohorts")
    backtest.add_argument("--db", type=Path, default=None)
    backtest.add_argument("--html", type=Path, default=None)
    backtest.add_argument("--json", type=Path, default=None)
    backtest.add_argument("--md", type=Path, default=None)
    backtest.add_argument("--refresh-dividends", action="store_true")

    serve = sub.add_parser("serve", help="run the local web application")
    serve.add_argument("--host", default="127.0.0.1", help="bind host (loopback by default)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true", help="development auto-reload")
    serve.add_argument(
        "--allow-external",
        action="store_true",
        help="allow binding to a non-loopback address",
    )
    return parser


def _settings() -> Settings:
    return Settings()


def _database(settings: Settings) -> tuple[Engine, sessionmaker[Session]]:
    settings.ensure_directories()
    engine = build_engine(settings.database_url, echo=settings.debug)
    init_db(engine)
    return engine, build_session_factory(engine)


def _print_report(report: SyncReport) -> None:
    print(f"sync status: {report.status.value}")
    print(
        f"accounts={report.accounts_synced} instruments={report.instruments_synced} "
        f"operations={report.operations_synced}"
    )
    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}")


def _cmd_init_db(settings: Settings) -> int:
    engine, factory = _database(settings)
    try:
        with session_scope(factory) as session:
            user = ensure_admin(session, settings)
        print(f"database ready: {settings.database_url}")
        if user is not None:
            print(f"admin user ready: {user.username}")
        else:
            print("authentication is disabled")
    finally:
        engine.dispose()
    return 0


def _cmd_seed_demo(settings: Settings) -> int:
    if settings.tinvest_client != "mock":
        print("seed-demo requires TINVEST_CLIENT=mock", file=sys.stderr)
        return 2
    demo_settings = settings.model_copy(update={"seed_demo_data": True})
    engine, factory = _database(demo_settings)
    client = build_tinvest_client(demo_settings)
    try:
        with session_scope(factory) as session:
            ensure_admin(session, demo_settings)
        report = SyncService(factory, client, demo_settings).run()
        captured = capture_all_snapshots(factory, demo_settings, source="cli-seed")
        with session_scope(factory) as session:
            goal = ensure_demo_goal(session, demo_settings)
            record_audit(
                session,
                demo_settings,
                event_type="cli.seed_demo",
                actor="cli",
                message=f"Демо-данные: {report.status.value}",
                response_payload={
                    "accounts": report.accounts_synced,
                    "positions_instruments": report.instruments_synced,
                    "operations": report.operations_synced,
                    "snapshots": captured,
                    "goal_id": goal.id if goal is not None else None,
                },
            )
        _print_report(report)
        if goal is not None:
            print(f"demo goal ready: {goal.name} (id={goal.id})")
        return 0 if report.status.value != "FAILED" else 1
    finally:
        client.close()
        engine.dispose()


def _cmd_sync(settings: Settings) -> int:
    engine, factory = _database(settings)
    client = build_tinvest_client(settings)
    try:
        report = SyncService(factory, client, settings).run()
        captured = capture_all_snapshots(factory, settings, source="cli-sync")
        with session_scope(factory) as session:
            record_audit(
                session,
                settings,
                event_type="cli.sync",
                actor="cli",
                entity_type="sync_run",
                entity_id=report.run_id,
                message=f"CLI синхронизация: {report.status.value}",
                severity=Severity.INFO if report.status.value == "OK" else Severity.WARNING,
                response_payload={
                    "accounts": report.accounts_synced,
                    "instruments": report.instruments_synced,
                    "operations": report.operations_synced,
                    "errors": list(report.errors),
                    "snapshots": captured,
                },
            )
        _print_report(report)
        return 0 if report.status.value != "FAILED" else 1
    except BootstrapError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()
        engine.dispose()


def _cmd_recommend(settings: Settings) -> int:
    engine, factory = _database(settings)
    try:
        with session_scope(factory) as session:
            account = session.scalar(select(Account).order_by(Account.id))
            if account is None:
                print("no accounts; run sync first", file=sys.stderr)
                return 1
            goal = session.scalar(
                select(Goal)
                .where(Goal.account_id == account.id, Goal.is_active.is_(True))
                .order_by(Goal.id)
            )
            if goal is None:
                print("нет активной цели; создайте её в веб-интерфейсе", file=sys.stderr)
                return 1
            run = RecommendationService(settings).generate(
                session,
                account_id=account.id,
                goal_id=goal.id,
            )
            record_audit(
                session,
                settings,
                event_type="cli.recommend",
                actor="cli",
                entity_type="account",
                entity_id=account.id,
                message=f"CLI рекомендации: {len(run.recommendations)}",
                response_payload={
                    "warnings": list(run.warnings),
                    "items": [
                        {
                            "action": item.action.value,
                            "ticker": item.ticker,
                            "lots": item.lots,
                            "amount": str(item.estimated_amount),
                        }
                        for item in run.recommendations
                    ],
                },
            )
        for item in run.recommendations:
            print(
                f"{item.action.value:4} {item.ticker:10} lots={item.lots:<4} "
                f"amount={item.estimated_amount} {item.currency} · {item.reason[:100]}"
            )
        for warning in run.warnings:
            print(f"warning: {warning}")
        return 0
    finally:
        engine.dispose()


def _cmd_universe(settings: Settings) -> int:
    engine, factory = _database(settings)
    try:
        client = build_tinvest_client(settings)
    except BootstrapError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        engine.dispose()
        return 2
    try:
        count, warnings = SyncService(factory, client, settings).load_universe()
        with session_scope(factory) as session:
            record_audit(
                session,
                settings,
                event_type="cli.universe",
                actor="cli",
                message=f"Загружено инструментов: {count}",
                response_payload={"count": count, "warnings": list(warnings)},
            )
        print(f"universe instruments loaded: {count}")
        for warning in warnings:
            print(f"warning: {warning}")
        return 0
    finally:
        client.close()
        engine.dispose()


def _cmd_growth(settings: Settings, limit: int | None, force: bool) -> int:
    engine, factory = _database(settings)
    try:
        client = build_tinvest_client(settings)
    except BootstrapError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        engine.dispose()
        return 2
    try:
        report = GrowthService(factory, client, settings).scan(limit=limit, force=force)
        with session_scope(factory) as session:
            record_audit(
                session,
                settings,
                event_type="cli.growth",
                actor="cli",
                message=(
                    f"Анализ роста: {report.analyzed} из {report.scanned} "
                    f"(пропущено свежих: {report.skipped_fresh})"
                ),
                response_payload={
                    "scanned": report.scanned,
                    "analyzed": report.analyzed,
                    "insufficient": report.insufficient,
                    "failed": report.failed,
                    "warnings": list(report.warnings),
                },
            )
        print(
            f"growth: analyzed={report.analyzed} insufficient={report.insufficient} "
            f"failed={report.failed} skipped_fresh={report.skipped_fresh}"
        )
        for warning in report.warnings:
            print(f"warning: {warning}")
        return 0 if report.failed == 0 else 1
    finally:
        client.close()
        engine.dispose()


def _cmd_backup(settings: Settings) -> int:
    engine, factory = _database(settings)
    try:
        result = BackupService(settings).create()
        with session_scope(factory) as session:
            record_audit(
                session,
                settings,
                event_type="cli.backup",
                actor="cli",
                message=f"Резервная копия: {result.path.name}",
                response_payload={
                    "path": str(result.path),
                    "size_bytes": result.size_bytes,
                    "removed": [str(path) for path in result.removed],
                },
            )
        print(f"backup: {result.path} ({result.size_bytes} bytes)")
        for path in result.removed:
            print(f"removed: {path}")
        return 0
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _cmd_status(settings: Settings) -> int:
    print(f"app: {settings.app_name} ({settings.app_env})")
    print(f"database: {settings.database_url}")
    print(f"client: {settings.tinvest_client}")
    print(f"sandbox: {settings.tinvest_sandbox}")
    print(f"trading_mode: {settings.trading_mode.value}")
    print(f"real_trading_enabled: {settings.real_trading_enabled}")
    print(f"auth_enabled: {settings.auth_enabled}")
    print(f"scheduler_enabled: {settings.scheduler_enabled}")
    print(f"token_configured: {settings.tinvest_token_value is not None}")
    return 0


def _screener_config(args: argparse.Namespace, settings: Settings) -> ScreenerConfig:
    base = ScreenerConfig(db_path=args.db or settings.screener_db_path)
    updates = {
        "api_limit": args.limit,
    }
    if getattr(args, "top", None) is not None:
        updates["max_candidates"] = args.top
    if getattr(args, "min_score", None) is not None:
        updates["min_score"] = args.min_score
    if getattr(args, "mode", None) is not None:
        updates["mode"] = args.mode
    if getattr(args, "no_orderbook", False):
        updates["fetch_orderbook"] = False
    if getattr(args, "refresh_dividends", False):
        updates["refresh_dividends"] = True
    if getattr(args, "refresh_fundamentals", False):
        updates["refresh_fundamentals"] = True
    if getattr(args, "train", None) is not None:
        updates["backtest_train_cohorts"] = max(1, args.train)
    if getattr(args, "step", None) is not None:
        updates["backtest_step_sessions"] = max(1, args.step)
    if getattr(args, "holdout", None) is not None:
        updates["backtest_holdout_cohorts"] = max(0, args.holdout)
    return replace(base, **updates)


def _default_report_path(prefix: str, as_of: date, suffix: str) -> Path:
    return Path("./data/reports") / f"{prefix}-{as_of.isoformat()}.{suffix}"


def _cmd_screen(settings: Settings, args: argparse.Namespace) -> int:
    config = _screener_config(args, settings)
    store = ScreenerStore(config.db_path)
    try:
        result = ScreenerService(store, config, settings).run(
            refresh=args.refresh and not args.offline,
            offline=args.offline,
            as_of=args.as_of,
        )
        payload = result_to_dict(result)
        html_path = args.html or _default_report_path("screen", result.as_of, "html")
        json_path = args.json or _default_report_path("screen", result.as_of, "json")
        md_path = args.md or _default_report_path("screen", result.as_of, "md")
        write_html(result, html_path)
        write_markdown(result, md_path)
        write_json(payload, json_path)
        print(
            f"screen as_of={result.as_of} benchmark={result.benchmark} "
            f"eligible={result.filter_stats.quality_ok} candidates={len(result.candidates)}"
        )
        for rank, candidate in enumerate(result.candidates[:20], start=1):
            print(
                f"{rank:2d} {candidate.ticker:8s} score={candidate.score:5.1f} "
                f"sector={candidate.share.sector or '-':14s} "
                f"turnover={candidate.features.median_turnover_60 or 0:.0f} "
                f"excess6={((candidate.features.excess_6m or 0) * 100):+.1f}%"
            )
        if not result.candidates:
            print("Подходящих кандидатов по текущим правилам нет.")
        for warning in result.warnings[:6]:
            print(f"warning: {warning}")
        print(f"reports: {html_path} {md_path} {json_path}")
        return 0
    finally:
        store.close()


def _cmd_screen_backtest(settings: Settings, args: argparse.Namespace) -> int:
    config = _screener_config(args, settings)
    store = ScreenerStore(config.db_path)
    try:
        result = BacktestService(store, config, settings).run(
            refresh=args.refresh and not args.offline,
            offline=args.offline,
            start=args.start,
            end=args.end,
        )
        payload = backtest_to_dict(result)
        slug = timestamp_slug(result.generated_at)
        html_path = args.html or Path("./data/reports") / f"backtest-{slug}.html"
        json_path = args.json or Path("./data/reports") / f"backtest-{slug}.json"
        md_path = args.md or Path("./data/reports") / f"backtest-{slug}.md"
        write_backtest_html(result, html_path)
        write_backtest_markdown(result, md_path)
        write_json(payload, json_path)
        summary = result.summary
        print(
            f"backtest {result.start}..{result.end} cohorts={summary.cohorts} "
            f"trades={summary.trades} mean_net={summary.mean_net_return} "
            f"mean_excess={summary.mean_excess} target={summary.target_hit_rate}"
        )
        for item in result.alternatives:
            print(f"alt {item.name:28s} trades={item.trades:4d} excess={item.mean_excess}")
        for warning in result.warnings:
            print(f"warning: {warning}")
        print(f"reports: {html_path} {md_path} {json_path}")
        return 0
    finally:
        store.close()


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    loopback = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback and not args.allow_external:
        print(
            "refusing to bind to a non-loopback host without --allow-external",
            file=sys.stderr,
        )
        return 2
    import uvicorn

    print(f"serving {settings.app_name} on http://{args.host}:{args.port}")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings()
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    handlers = {
        "init-db": lambda: _cmd_init_db(settings),
        "seed-demo": lambda: _cmd_seed_demo(settings),
        "sync": lambda: _cmd_sync(settings),
        "universe": lambda: _cmd_universe(settings),
        "recommend": lambda: _cmd_recommend(settings),
        "growth": lambda: _cmd_growth(settings, args.limit, args.force),
        "screen": lambda: _cmd_screen(settings, args),
        "screen-backtest": lambda: _cmd_screen_backtest(settings, args),
        "backup": lambda: _cmd_backup(settings),
        "status": lambda: _cmd_status(settings),
        "serve": lambda: _cmd_serve(settings, args),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
