"""Deterministic JSON, Markdown and HTML reports for the screener."""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.screener.domain import (
    BacktestResult,
    Candidate,
    ScreenerResult,
)

ZERO = Decimal("0")
ONE = Decimal("1")


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _implied_high(close: Decimal | None, distance: Decimal | None) -> Decimal | None:
    if close is None or distance is None or distance <= -ONE:
        return None
    return close / (ONE + distance)


def _candidate_dict(candidate: Candidate, rank: int) -> dict[str, Any]:
    features = candidate.features
    return {
        "rank": rank,
        "uid": candidate.share.uid,
        "ticker": candidate.ticker,
        "name": candidate.name,
        "issuer_key": candidate.share.issuer_key,
        "sector": candidate.share.sector,
        "class_code": candidate.share.class_code,
        "lot": candidate.share.lot,
        "buy_available": candidate.share.buy_available,
        "api_trade_available": candidate.share.api_trade_available,
        "for_qual_investor": candidate.share.for_qual_investor,
        "trading_status": candidate.share.trading_status,
        "score": _dec(candidate.score),
        "price": _dec(features.close),
        "median_turnover_60": _dec(features.median_turnover_60),
        "spread_pct": _dec(candidate.spread_pct),
        "sector_rank": candidate.sector_rank,
        "sma20": _dec(features.sma20),
        "sma50": _dec(features.sma50),
        "sma200": _dec(features.sma200),
        "high_63": _dec(_implied_high(features.close, features.dist_high_63)),
        "high_126": _dec(_implied_high(features.close, features.dist_high_126)),
        "ret_1m": _dec(features.ret_1m),
        "ret_3m": _dec(features.ret_3m),
        "ret_6m": _dec(features.ret_6m),
        "ret_6m_ex_5d": _dec(features.ret_6m_ex_5d),
        "ret_12m": _dec(features.ret_12m),
        "excess_3m": _dec(features.excess_3m),
        "excess_6m": _dec(features.excess_6m),
        "excess_6m_ex_5d": _dec(features.excess_6m_ex_5d),
        "excess_12m": _dec(features.excess_12m),
        "sector_excess_3m": _dec(features.sector_excess_3m),
        "volatility_60": _dec(features.volatility_60),
        "max_drawdown_126": _dec(features.max_drawdown_126),
        "turnover_ratio_5_60": _dec(features.turnover_ratio_5_60),
        "max_turnover_share_60": _dec(features.max_turnover_share_60),
        "large_gap_freq_60": _dec(features.large_gap_freq_60),
        "breakout_20": _dec(features.breakout_20),
        "breakout_63": _dec(features.breakout_63),
        "breakout_126": _dec(features.breakout_126),
        "breakout_volume_ratio_20": _dec(features.breakout_volume_ratio_20),
        "dist_high_63": _dec(features.dist_high_63),
        "dist_high_126": _dec(features.dist_high_126),
        "squeeze_20_60": _dec(features.squeeze_20_60),
        "above_sma200": features.above_sma200,
        "blocks": {
            "relative_strength": _dec(candidate.blocks.relative_strength),
            "trend_quality": _dec(candidate.blocks.trend_quality),
            "volume_confirmation": _dec(candidate.blocks.volume_confirmation),
            "consolidation_breakout": _dec(candidate.blocks.consolidation_breakout),
            "risk_moderation": _dec(candidate.blocks.risk_moderation),
        },
        "penalties": [
            {"code": item.code, "points": _dec(item.points), "reason": item.reason}
            for item in candidate.penalties
        ],
        "reasons": list(candidate.reasons),
        "warnings": list(candidate.warnings),
        "quality": {
            "history_sessions": candidate.quality.history_sessions,
            "activity_ratio": _dec(candidate.quality.activity_ratio),
            "missing_ratio": _dec(candidate.quality.missing_ratio),
            "zero_volume_ratio": _dec(candidate.quality.zero_volume_ratio),
            "last_bar_date": _date(candidate.quality.last_bar_date),
            "stale_sessions": candidate.quality.stale_sessions,
            "flags": list(candidate.quality.flags),
            "critical": candidate.quality.critical,
            "label": candidate.quality.label,
        },
        "fundamental_score": _dec(candidate.fundamental_score),
        "fundamental_notes": list(candidate.fundamental_notes),
        "fundamental_coverage": candidate.fundamental_coverage,
        "fundamental_category": candidate.fundamental_category,
        "fundamental_as_of": (
            _date(candidate.fundamental.as_of) if candidate.fundamental else None
        ),
        "data_fetched_at": (
            candidate.data_fetched_at.isoformat() if candidate.data_fetched_at else None
        ),
        "invalidation": list(candidate.invalidation),
        "provenance": {
            "price": {
                "source": "T-Invest daily candle",
                "as_of": _date(features.as_of),
                "fetched_at": _dt(
                    candidate.history[-1].fetched_at
                    if candidate.history
                    else candidate.bar.fetched_at
                ),
            },
            "volume": {
                "source": "T-Invest daily candle",
                "as_of": _date(features.as_of),
                "fetched_at": _dt(
                    candidate.history[-1].fetched_at
                    if candidate.history
                    else candidate.bar.fetched_at
                ),
            },
            "spread": {
                "source": "T-Invest orderbook",
                "as_of": (
                    _date(candidate.spread_fetched_at.date())
                    if candidate.spread_fetched_at is not None
                    else None
                ),
                "fetched_at": _dt(candidate.spread_fetched_at),
            },
            "fundamentals": {
                "source": "T-Invest fundamentals",
                "as_of": (_date(candidate.fundamental.as_of) if candidate.fundamental else None),
                "fetched_at": (
                    candidate.data_fetched_at.isoformat() if candidate.data_fetched_at else None
                ),
            },
            "score": {
                "source": "deterministic screener formula",
                "as_of": _date(features.as_of),
                "fetched_at": None,
            },
        },
        "stretch_sma20": (
            _dec(features.close / features.sma20 - ONE)
            if features.sma20 is not None and features.sma20 > ZERO
            else None
        ),
        "history": [
            {
                "date": _date(bar.trade_date),
                "close": _dec(bar.close),
                "high": _dec(bar.high),
                "low": _dec(bar.low),
                "volume": bar.volume_lots,
            }
            for bar in candidate.history[-120:]
        ],
    }


def result_to_dict(result: ScreenerResult) -> dict[str, Any]:
    market_fetched_at = max(
        (
            bar.fetched_at
            for candidate in result.candidates
            for bar in candidate.history
            if bar.fetched_at is not None
        ),
        default=None,
    )
    orderbook_fetched_at = max(
        (
            candidate.spread_fetched_at
            for candidate in result.candidates
            if candidate.spread_fetched_at is not None
        ),
        default=None,
    )
    fundamentals_fetched_at = max(
        (
            candidate.data_fetched_at
            for candidate in result.candidates
            if candidate.data_fetched_at is not None
        ),
        default=None,
    )
    return {
        "kind": "screen",
        "as_of": _date(result.as_of),
        "generated_at": result.generated_at.isoformat(),
        "mode": result.mode,
        "benchmark": result.benchmark,
        "filter_stats": result.filter_stats.as_dict(),
        "provenance": {
            "market_data": {
                "source": "T-Invest API",
                "as_of": _date(result.as_of),
                "fetched_at": _dt(market_fetched_at),
                "note": "завершённые дневные свечи",
            },
            "fundamentals": {
                "source": "T-Invest API",
                "as_of": _date(result.as_of),
                "fetched_at": _dt(fundamentals_fetched_at),
                "note": "текущий снимок, не point-in-time",
            },
            "orderbook": {
                "source": "T-Invest API",
                "as_of": _date(result.as_of),
                "fetched_at": _dt(orderbook_fetched_at),
                "note": "текущий стакан",
            },
            "score": {
                "source": "deterministic screener formula",
                "as_of": _date(result.as_of),
            },
        },
        "regime": {
            "label": result.regime.label,
            "benchmark": result.regime.benchmark,
            "close": _dec(result.regime.close),
            "sma200": _dec(result.regime.sma200),
            "slope_20": _dec(result.regime.slope_20),
            "volatility_60": _dec(result.regime.volatility_60),
            "drawdown_252": _dec(result.regime.drawdown_252),
            "breadth_50": _dec(result.regime.breadth_50),
            "breadth_200": _dec(result.regime.breadth_200),
            "explanation": result.regime.explanation,
        },
        "universe": [
            {
                "uid": item.uid,
                "ticker": item.ticker,
                "name": item.name,
                "sector": item.sector,
                "class_code": item.class_code,
                "lot": item.lot,
                "active": item.is_active,
                "buy_available": item.buy_available,
                "qualified_only": item.for_qual_investor,
            }
            for item in result.universe
        ],
        "candidates": [
            _candidate_dict(candidate, rank)
            for rank, candidate in enumerate(result.candidates, start=1)
        ],
        "near_candidates": [
            _candidate_dict(candidate, rank)
            for rank, candidate in enumerate(result.near_candidates, start=1)
        ],
        "warnings": list(result.warnings),
        "config": result.config,
    }


def _portfolio_dict(portfolio: Any) -> dict[str, Any] | None:
    if portfolio is None:
        return None
    return {
        "initial_capital": _dec(portfolio.initial_capital),
        "final_equity": _dec(portfolio.final_equity),
        "total_return": _dec(portfolio.total_return),
        "benchmark_return": _dec(portfolio.benchmark_return),
        "excess_return": _dec(portfolio.excess_return),
        "cagr": _dec(portfolio.cagr),
        "volatility": _dec(portfolio.volatility),
        "sharpe": _dec(portfolio.sharpe),
        "sortino": _dec(portfolio.sortino),
        "max_drawdown": _dec(portfolio.max_drawdown),
        "turnover": _dec(portfolio.turnover),
        "exposure": _dec(portfolio.exposure),
        "commission_paid": _dec(portfolio.commission_paid),
        "slippage_paid": _dec(portfolio.slippage_paid),
        "cost_drag": _dec(portfolio.cost_drag),
        "periods": portfolio.periods,
        "trades": portfolio.trades,
        "warning": portfolio.warning,
        "points": [
            {
                "date": _date(point.trade_date),
                "equity": _dec(point.equity),
                "benchmark": _dec(point.benchmark),
                "exposure": _dec(point.exposure),
            }
            for point in portfolio.points
        ],
        "yearly": [
            {
                "year": item.year,
                "return": _dec(item.return_value),
                "benchmark_return": _dec(item.benchmark_return),
                "max_drawdown": _dec(item.max_drawdown),
            }
            for item in portfolio.yearly
        ],
    }


def _summary_dict(summary: Any) -> dict[str, Any]:
    return {
        "cohorts": summary.cohorts,
        "trades": summary.trades,
        "mean_net_return": _dec(summary.mean_net_return),
        "median_net_return": _dec(summary.median_net_return),
        "mean_gross_return": _dec(summary.mean_gross_return),
        "mean_benchmark_return": _dec(summary.mean_benchmark_return),
        "mean_excess": _dec(summary.mean_excess),
        "median_excess": _dec(summary.median_excess),
        "cost_drag": _dec(summary.cost_drag),
        "mean_max_adverse": _dec(summary.mean_max_adverse),
        "median_max_adverse": _dec(summary.median_max_adverse),
        "worst_max_adverse": _dec(summary.worst_max_adverse),
        "excess_ci_low": _dec(summary.excess_ci_low),
        "excess_ci_high": _dec(summary.excess_ci_high),
        "target_hit_rate": _dec(summary.target_hit_rate),
        "target_ci_low": _dec(summary.target_ci_low),
        "target_ci_high": _dec(summary.target_ci_high),
        "target_block_ci_low": _dec(summary.target_block_ci_low),
        "target_block_ci_high": _dec(summary.target_block_ci_high),
        "excess_hit_rate": _dec(summary.excess_hit_rate),
        "beat_benchmark_rate": _dec(summary.beat_benchmark_rate),
        "beat_ci_low": _dec(summary.beat_ci_low),
        "beat_ci_high": _dec(summary.beat_ci_high),
        "beat_block_ci_low": _dec(summary.beat_block_ci_low),
        "beat_block_ci_high": _dec(summary.beat_block_ci_high),
        "worst_cohort_excess": _dec(summary.worst_cohort_excess),
        "best_cohort_excess": _dec(summary.best_cohort_excess),
        "insufficient": summary.insufficient,
        "sample_warning": summary.sample_warning,
    }


def _diagnostic_rows(diagnostics: Any) -> list[list[str]]:
    labels = {
        "feature_correlation": "Корреляция признаков",
        "feature_ic": "IC признака",
        "block_ic": "IC блока",
        "ablation": "Ablation блока",
        "regime": "Режим рынка",
        "market_filter": "Рыночный фильтр",
    }
    rows: list[list[str]] = []
    for item in diagnostics:
        kind = str(item.get("kind", ""))
        label = labels.get(kind, kind)
        if kind == "feature_correlation":
            subject = f"{item.get('left')} ↔ {item.get('right')}"
            estimate = f"ρ = {_num(item.get('rho'))}"
            interval = "—"
            sample = str(item.get("observations", ""))
            note = str(item.get("note", ""))
        elif kind in {"feature_ic", "block_ic"}:
            subject = str(item.get("feature") or item.get("block") or "")
            estimate = f"IC = {_num(item.get('mean_ic'))}"
            interval = f"{_num(item.get('ci_low'))} … {_num(item.get('ci_high'))}"
            sample = str(item.get("cohorts", ""))
            note = "Средний Spearman по когортам."
        elif kind == "ablation":
            subject = str(item.get("block", ""))
            estimate = f"превышение {_pct(item.get('mean_excess'), signed=True)}"
            interval = f"Δ {_pct(item.get('delta_vs_base'), signed=True)}"
            sample = str(item.get("trades", ""))
            note = str(item.get("note", ""))
        elif kind == "regime":
            subject = str(item.get("regime", ""))
            estimate = (
                f"чистая {_pct(item.get('mean_net'), signed=True)}; "
                f"превышение {_pct(item.get('mean_excess'), signed=True)}"
            )
            interval = f"обыграли {_pct(item.get('beat_rate'))}"
            sample = f"{item.get('cohorts', '')} / {item.get('trades', '')}"
            note = ""
        elif kind == "market_filter":
            subject = "risk-off когорты"
            estimate = f"{item.get('risk_off_cohorts', '')} из {item.get('total_cohorts', '')}"
            interval = (
                f"score ≥ {_num(item.get('risk_off_min_score'))}, "
                f"top {item.get('risk_off_max_candidates', '')}"
            )
            sample = "—"
            note = str(item.get("note", ""))
        else:
            subject = "—"
            estimate = "—"
            interval = "—"
            sample = "—"
            note = "—"
        rows.append([label, subject, estimate, interval, sample, note])
    return rows


def _verdict_dict(verdict: Any) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {
        "status": verdict.status,
        "headline": verdict.headline,
        "details": list(verdict.details),
        "best_simple_alternative": verdict.best_simple_alternative,
        "best_simple_excess": _dec(verdict.best_simple_excess),
    }


def _holdout_dict(holdout: Any) -> dict[str, Any] | None:
    if holdout is None:
        return None
    return {
        "start": _date(holdout.start),
        "end": _date(holdout.end),
        "selected_min_score": _dec(holdout.selected_min_score),
        "selected_candidates": holdout.selected_candidates,
        "summary": _summary_dict(holdout.summary),
        "portfolio": _portfolio_dict(holdout.portfolio),
        "cohorts": [
            {
                "signal_date": _date(item.signal_date),
                "entry_date": _date(item.entry_date),
                "exit_date": _date(item.exit_date),
                "candidates": item.candidates,
                "trades": item.trades,
                "mean_net_return": _dec(item.mean_net_return),
                "mean_benchmark_return": _dec(item.mean_benchmark_return),
                "mean_excess": _dec(item.mean_excess),
                "target_hit_rate": _dec(item.target_hit_rate),
                "excess_hit_rate": _dec(item.excess_hit_rate),
            }
            for item in holdout.cohorts
        ],
    }


def backtest_to_dict(result: BacktestResult) -> dict[str, Any]:
    return {
        "kind": "backtest",
        "start": _date(result.start),
        "end": _date(result.end),
        "generated_at": result.generated_at.isoformat(),
        "benchmark": result.benchmark,
        "summary": _summary_dict(result.summary),
        "verdict": _verdict_dict(result.verdict),
        "portfolio": _portfolio_dict(result.portfolio),
        "holdout": _holdout_dict(result.holdout),
        "diagnostics": list(result.diagnostics),
        "cohorts": [
            {
                "signal_date": _date(item.signal_date),
                "entry_date": _date(item.entry_date),
                "exit_date": _date(item.exit_date),
                "candidates": item.candidates,
                "trades": item.trades,
                "mean_net_return": _dec(item.mean_net_return),
                "mean_benchmark_return": _dec(item.mean_benchmark_return),
                "mean_excess": _dec(item.mean_excess),
                "target_hit_rate": _dec(item.target_hit_rate),
                "excess_hit_rate": _dec(item.excess_hit_rate),
            }
            for item in result.cohorts
        ],
        "trades": [
            {
                "cohort": _date(item.cohort),
                "signal_date": _date(item.signal_date),
                "entry_date": _date(item.entry_date),
                "exit_date": _date(item.exit_date),
                "ticker": item.ticker,
                "uid": item.uid,
                "score": _dec(item.score),
                "entry_price": _dec(item.entry_price),
                "exit_price": _dec(item.exit_price),
                "gross_return": _dec(item.gross_return),
                "net_return": _dec(item.net_return),
                "benchmark_return": _dec(item.benchmark_return),
                "excess_return": _dec(item.excess_return),
                "dividends": _dec(item.dividends),
                "target_hit": item.target_hit,
                "excess_hit": item.excess_hit,
                "max_adverse": _dec(item.max_adverse),
                "days_held": item.days_held,
            }
            for item in result.trades
        ],
        "alternatives": [
            {
                "name": item.name,
                "mean_net_return": _dec(item.mean_net_return),
                "mean_excess": _dec(item.mean_excess),
                "target_hit_rate": _dec(item.target_hit_rate),
                "excess_hit_rate": _dec(item.excess_hit_rate),
                "trades": item.trades,
                "note": item.note,
                "portfolio_total_return": _dec(item.portfolio_total_return),
                "portfolio_max_drawdown": _dec(item.portfolio_max_drawdown),
                "portfolio_cagr": _dec(item.portfolio_cagr),
            }
            for item in result.alternatives
        ],
        "sensitivity": list(result.sensitivity),
        "warnings": list(result.warnings),
        "config": result.config,
    }


def _pct(value: Any, *, digits: int = 1, signed: bool = False) -> str:
    if value is None or value == "":
        return "—"
    number = Decimal(str(value)) * 100
    quantum = Decimal(1).scaleb(-digits)
    if number.quantize(quantum) == ZERO:
        number = ZERO
    sign = "+" if signed and number > ZERO else ""
    return f"{sign}{number:.{digits}f}%".replace(".", ",")


def _cost_pct(value: Decimal | None) -> str:
    return "—" if value is None else _pct(-value, signed=True)


def _money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    number = Decimal(str(value))
    if abs(number) >= Decimal("1000000"):
        return f"{number / Decimal('1000000'):.1f} млн ₽".replace(".", ",")
    return f"{number:.0f} ₽"


def _price(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return f"{Decimal(str(value)):.2f}".replace(".", ",")


def _num(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    return f"{Decimal(str(value)):.{digits}f}".replace(".", ",")


def _count(value: Any) -> str:
    return "—" if value is None else str(value)


def _tone(score: Decimal) -> str:
    if score >= Decimal("75"):
        return "good"
    if score >= Decimal("60"):
        return "ok"
    return "weak"


def _filter_rows(result: ScreenerResult) -> list[tuple[str, int]]:
    stats = result.filter_stats
    return [
        ("Все инструменты Shares", stats.shares_total),
        ("MOEX + RUB + обыкновенные/привилегированные", stats.moex_rub_common_preferred),
        ("Активные", stats.active),
        ("Доступна покупка", stats.buy_available),
        ("История ≥ 252 сессий", stats.history_ok),
        ("Активность ≥ 95%", stats.activity_ok),
        ("Оборот ≥ 30 млн ₽", stats.liquidity_ok),
        ("Качество данных", stats.quality_ok),
        (
            "Спред в норме" if stats.spread_checked else "Спред (не проверен)",
            stats.spread_ok,
        ),
        *(
            [("Фундаментал покрыт", stats.fundamental_ok)]
            if result.mode == "technical_fundamental"
            else []
        ),
        ("Рейтинг выше порога", stats.score_ok),
    ]


def result_to_markdown(result: ScreenerResult) -> str:
    lines = [
        f"# Скринер российских акций — {result.as_of.isoformat()}",
        "",
        f"- Режим: **{result.mode}**",
        f"- Бенчмарк: **{result.benchmark}**",
        f"- Режим рынка: **{result.regime.label}** — {result.regime.explanation}",
        f"- Кандидатов: **{len(result.candidates)}**",
        "",
        "## Воронка фильтров",
        "",
        "| Этап | Осталось |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in _filter_rows(result))
    lines.extend(
        [
            "",
            "## Кандидаты",
            "",
            f"| # | Тикер | Сектор | Рейтинг | Цена | Оборот 60 | 3м к {result.benchmark} | "
            f"6м к {result.benchmark} | Волат. | Просадка | Спред |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, candidate in enumerate(result.candidates, start=1):
        lines.append(
            "| {rank} | {ticker} | {sector} | {score} | {price} | {turnover} | "
            "{exc3} | {exc6} | {vol} | {dd} | {spread} |".format(
                rank=rank,
                ticker=candidate.ticker,
                sector=candidate.share.sector or "—",
                score=f"{candidate.score:.0f}",
                price=_price(candidate.features.close),
                turnover=_money(candidate.features.median_turnover_60),
                exc3=_pct(candidate.features.excess_3m, signed=True),
                exc6=_pct(candidate.features.excess_6m, signed=True),
                vol=_pct(candidate.features.volatility_60),
                dd=_pct(candidate.features.max_drawdown_126, signed=True),
                spread=_pct(candidate.spread_pct, digits=2),
            )
        )
    if not result.candidates:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — |")
    lines.extend(["", "## Почему эти бумаги", ""])
    for rank, candidate in enumerate(result.candidates[:10], start=1):
        lines.append(f"### {rank}. {candidate.ticker} — {candidate.score:.0f}/100")
        lines.extend(f"- {reason}" for reason in candidate.reasons)
        if candidate.warnings:
            lines.extend(f"- ⚠ {warning}" for warning in candidate.warnings)
        lines.append("")
    if result.near_candidates:
        lines.extend(["## Близкие к порогу", "", "| Тикер | Рейтинг | Причина |", "|---|---:|---|"])
        for candidate in result.near_candidates:
            lines.append(
                f"| {candidate.ticker} | {candidate.score:.0f} | "
                f"{candidate.reasons[0] if candidate.reasons else '—'} |"
            )
        lines.append("")
    lines.extend(["## Ограничения", ""])
    lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines)


def _html_table(headers: list[str], rows: list[list[str]], *, css: str = "") -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<table class="{css}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _candidate_cards(result: ScreenerResult) -> str:
    cards: list[str] = []
    for rank, candidate in enumerate(result.candidates[:5], start=1):
        blocks = [
            ("A. Относительная сила", candidate.blocks.relative_strength),
            ("B. Качество тренда", candidate.blocks.trend_quality),
            ("C. Объём", candidate.blocks.volume_confirmation),
            ("D. Пробой", candidate.blocks.consolidation_breakout),
            ("E. Риск", candidate.blocks.risk_moderation),
        ]
        block_html = "".join(
            (
                f'<div class="bar-label"><span>{html.escape(name)}</span>'
                f"<span>{value:.0f}</span></div>"
                f'<div class="bar"><i style="width:{max(0, min(100, float(value))):.0f}%">'
                f"</i></div>"
                if value is not None
                else f'<div class="bar-label"><span>{html.escape(name)}</span><span>—</span></div>'
                f'<div class="bar"><i style="width:0%"></i></div>'
            )
            for name, value in blocks
        )
        reasons = "".join(f"<li>{html.escape(item)}</li>" for item in candidate.reasons)
        warnings = "".join(
            f'<li class="warn">{html.escape(item)}</li>' for item in candidate.warnings
        )
        cards.append(
            f'<article class="card"><div class="card-head"><div><span class="rank">#{rank}</span>'
            f"<h3>{html.escape(candidate.ticker)}</h3><p>{html.escape(candidate.name)}</p></div>"
            f'<div class="score {_tone(candidate.score)}">{candidate.score:.0f}</div></div>'
            f'<div class="metrics"><span>Цена <b>{_price(candidate.features.close)}</b></span>'
            f"<span>Оборот <b>{_money(candidate.features.median_turnover_60)}</b></span>"
            f"<span>3м к {result.benchmark} "
            f"<b>{_pct(candidate.features.excess_3m, signed=True)}</b></span>"
            f"<span>6м к {result.benchmark} "
            f"<b>{_pct(candidate.features.excess_6m, signed=True)}</b></span>"
            f"<span>Волатильность <b>{_pct(candidate.features.volatility_60)}</b></span>"
            f"<span>Просадка <b>{_pct(candidate.features.max_drawdown_126, signed=True)}</b></span>"
            f"<span>Спред <b>{_pct(candidate.spread_pct, digits=2)}</b></span></div>"
            f'<div class="blocks">{block_html}</div>'
            f"<ul>{reasons}</ul>"
            f"{f'<ul class=warnings>{warnings}</ul>' if warnings else ''}"
            f"</article>"
        )
    return "".join(cards)


CSS = """
:root{--bg:#0b1020;--panel:#121a2e;--panel2:#18233d;--ink:#e9eefb;--muted:#93a1bf;
--line:#25324f;--accent:#6ea8fe;--good:#43c59e;--ok:#f3c969;--weak:#f07178}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:Inter,Segoe UI,Roboto,Arial,sans-serif;line-height:1.45}
main{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:30px;margin:0 0 6px}h2{font-size:20px;margin:32px 0 12px}
h3{font-size:17px;margin:0}p{color:var(--muted);margin:4px 0}
.sub{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi b{display:block;font-size:22px;color:var(--ink);margin-top:4px}
.warnbox{background:#2a2114;border:1px solid #6b5324;border-radius:12px;padding:14px;margin:18px 0}
.warnbox strong{color:#f3c969}.panel{background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:16px;margin:14px 0;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 10px;
border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--muted);font-weight:600;background:var(--panel2)}
tr:last-child td{border-bottom:0}.card{background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:16px;margin:14px 0}.card-head{display:flex;justify-content:space-between;
gap:12px;align-items:flex-start}.rank{color:var(--muted);font-size:12px}.score{font-size:30px;
font-weight:700;padding:4px 12px;border-radius:10px;background:var(--panel2)}
.score.good{color:var(--good)}.score.ok{color:var(--ok)}.score.weak{color:var(--weak)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;
margin:12px 0;color:var(--muted);font-size:13px}.metrics b{color:var(--ink)}
.block-label{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:8px}
.bar{height:7px;background:var(--panel2);border-radius:99px;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#6ea8fe,#43c59e)}
ul{margin:10px 0 0 18px;padding:0;color:var(--muted)}li{margin:3px 0}
.warnings li.warn{color:var(--ok)}.pill{display:inline-block;padding:3px 9px;border-radius:99px;
background:var(--panel2);color:var(--muted);font-size:12px;margin-right:6px}
.funnel{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}
.funnel div{background:var(--panel2);border-radius:10px;padding:10px;
font-size:13px;color:var(--muted)}
.funnel b{display:block;color:var(--ink);font-size:18px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def result_to_html(result: ScreenerResult) -> str:
    stats = result.filter_stats
    regime = result.regime
    funnel = "".join(
        f"<div>{html.escape(name)}<b>{count}</b></div>" for name, count in _filter_rows(result)
    )
    rows = []
    for rank, candidate in enumerate(result.candidates, start=1):
        rows.append(
            [
                str(rank),
                html.escape(candidate.ticker),
                html.escape(candidate.share.sector or "—"),
                f"<b>{candidate.score:.0f}</b>",
                _price(candidate.features.close),
                _money(candidate.features.median_turnover_60),
                _pct(candidate.features.excess_3m, signed=True),
                _pct(candidate.features.excess_6m, signed=True),
                _pct(candidate.features.volatility_60),
                _pct(candidate.features.max_drawdown_126, signed=True),
                _pct(candidate.spread_pct, digits=2),
            ]
        )
    if not rows:
        rows = [["—"] * 11]
    table = _html_table(
        [
            "#",
            "Тикер",
            "Сектор",
            "Рейтинг",
            "Цена",
            "Оборот 60",
            f"3м к {result.benchmark}",
            f"6м к {result.benchmark}",
            "Волат.",
            "Просадка",
            "Спред",
        ],
        rows,
    )
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in result.warnings)
    cards = _candidate_cards(result)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Скринер акций {result.as_of.isoformat()}</title><style>{CSS}</style></head>
<body><main>
<h1>Скринер российских акций</h1>
<p class="sub">Дата данных: <b>{result.as_of.strftime("%d.%m.%Y")}</b> ·
режим: <b>{html.escape(result.mode)}</b> ·
бенчмарк: <b>{html.escape(result.benchmark)}</b></p>
<div class="grid">
<div class="kpi">Режим рынка<b>{html.escape(regime.label)}</b></div>
<div class="kpi">Допустимых бумаг<b>{stats.quality_ok}</b></div>
<div class="kpi">Кандидатов<b>{len(result.candidates)}</b></div>
<div class="kpi">{html.escape(result.benchmark)} выше SMA200<b>
{"да" if regime.sma200 and regime.close > regime.sma200 else "нет"}</b></div>
<div class="kpi">Широта выше SMA50<b>{_pct(regime.breadth_50)}</b></div>
</div>
<div class="warnbox"><strong>Это не прогноз и не инвестиционная рекомендация.</strong>
Рейтинг — детерминированное правило ранжирования, а не вероятность роста. Заявки не выставляются.
</div>
<h2>Воронка фильтров</h2><div class="funnel">{funnel}</div>
<h2>Кандидаты</h2><div class="panel">{table}</div>
<h2>Карточки</h2>
{cards if cards else '<div class="panel">Подходящих кандидатов по текущим правилам нет.</div>'}
<h2>Ограничения и предупреждения</h2><div class="panel"><ul>{warnings}</ul></div>
<h2>Методика</h2><div class="panel"><p>Рейтинг 0–100: A 30%, B 20%, C 20%, D 20%, E 10%.
Все признаки считаются по завершённым свечам до даты сигнала включительно.
Отсутствующие данные не заменяются нулями или средними. Фундаментал в режиме
technical_fundamental — текущий снимок T-Invest, не point-in-time.</p>
<p>Горизонт проверки: 63 сессии. Цель +20% считается по максимуму внутри горизонта;
превышение бенчмарка +10 п.п. — после комиссии и проскальзывания.</p>
<p>Бенчмарк {html.escape(result.benchmark)} — total return. История выбранного бенчмарка ограничена,
поэтому историческая проверка короткая. Список бумаг — текущий,
survivorship bias не устранён.</p></div>
<footer>Сгенерировано {result.generated_at.strftime("%d.%m.%Y %H:%M UTC")}.
Локальный InvestPilot · только чтение данных.</footer>
</main></body></html>"""


def write_html(result: ScreenerResult, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result_to_html(result), encoding="utf-8")
    return target


def write_markdown(result: ScreenerResult, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result_to_markdown(result), encoding="utf-8")
    return target


def write_json(payload: dict[str, Any], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def backtest_to_markdown(result: BacktestResult) -> str:
    summary = result.summary
    lines = [
        f"# Walk-forward бэктест — {result.start.isoformat()} … {result.end.isoformat()}",
        "",
        f"- Бенчмарк: **{result.benchmark}**",
        f"- Когорт: **{summary.cohorts}**, сделок: **{summary.trades}**",
        f"- Средняя чистая доходность: **{_pct(summary.mean_net_return, signed=True)}**; "
        f"средняя до издержек: **{_pct(summary.mean_gross_return, signed=True)}**; "
        f"издержки на сделку: **{_cost_pct(summary.cost_drag)}**",
        f"- Среднее превышение бенчмарка: **{_pct(summary.mean_excess, signed=True)}**",
        f"- MAE (макс. неблагоприятная просадка): медиана "
        f"**{_pct(summary.median_max_adverse, signed=True)}**, худшая "
        f"**{_pct(summary.worst_max_adverse, signed=True)}**, средняя "
        f"**{_pct(summary.mean_max_adverse, signed=True)}**",
        f"- 95% bootstrap интервал превышения: **{_pct(summary.excess_ci_low, signed=True)} … "
        f"{_pct(summary.excess_ci_high, signed=True)}**",
        f"- Достигли +20%: **{_pct(summary.target_hit_rate)}** "
        f"(Wilson {_pct(summary.target_ci_low)}…{_pct(summary.target_ci_high)}; "
        f"block bootstrap {_pct(summary.target_block_ci_low)}…"
        f"{_pct(summary.target_block_ci_high)})",
        f"- Превысили бенчмарк на +10 п.п.: **{_pct(summary.excess_hit_rate)}**",
        f"- Обыграли бенчмарк: **{_pct(summary.beat_benchmark_rate)}** "
        f"(Wilson {_pct(summary.beat_ci_low)}…{_pct(summary.beat_ci_high)}; "
        f"block bootstrap {_pct(summary.beat_block_ci_low)}…"
        f"{_pct(summary.beat_block_ci_high)})",
    ]
    if summary.sample_warning:
        lines.extend(["", f"> ⚠ {summary.sample_warning}"])
    if result.verdict is not None:
        verdict = result.verdict
        lines.extend(["", "## Вывод", "", f"**{verdict.headline}**"])
        lines.extend(f"- {detail}" for detail in verdict.details)
    if result.portfolio is not None:
        portfolio = result.portfolio
        lines.extend(
            [
                "",
                "## Портфель",
                "",
                f"- Капитал: **{_num(portfolio.initial_capital)}x → "
                f"{_num(portfolio.final_equity)}x**",
                f"- Доходность: **{_pct(portfolio.total_return, signed=True)}**; "
                f"бенчмарк: **{_pct(portfolio.benchmark_return, signed=True)}**; "
                f"превышение: **{_pct(portfolio.excess_return, signed=True)}**",
                f"- CAGR: **{_pct(portfolio.cagr, signed=True)}**; "
                f"волатильность: **{_pct(portfolio.volatility)}**; "
                f"Sharpe: **{_num(portfolio.sharpe)}**; Sortino: **{_num(portfolio.sortino)}**",
                f"- Максимальная просадка: **{_pct(portfolio.max_drawdown)}**; "
                f"оборот: **{_num(portfolio.turnover)}x/год**; "
                f"средняя экспозиция: **{_pct(portfolio.exposure)}**",
                f"- Издержки портфеля: комиссия **{_num(portfolio.commission_paid)}x**, "
                f"проскальзывание **{_num(portfolio.slippage_paid)}x**, "
                f"доля среднего капитала **{_pct(portfolio.cost_drag)}**",
                f"- Дней в кривой: **{portfolio.periods}**, сделок: **{portfolio.trades}**",
                "",
                "> Портфель: равный вес внутри когорты, не более `max_position_weight` "
                "на бумагу, свободный кэш не инвестируется. Поэтому портфельная доходность "
                "не равна средней доходности отдельной сделки.",
            ]
        )
        if portfolio.warning:
            lines.append(f"- ⚠ {portfolio.warning}")
        if portfolio.yearly:
            lines.extend(
                [
                    "",
                    "| Год | Портфель | Бенчмарк | Макс. просадка |",
                    "|---:|---:|---:|---:|",
                ]
            )
            for year_row in portfolio.yearly:
                lines.append(
                    f"| {year_row.year} | {_pct(year_row.return_value, signed=True)} | "
                    f"{_pct(year_row.benchmark_return, signed=True)} | "
                    f"{_pct(year_row.max_drawdown)} |"
                )
    if result.holdout is not None:
        holdout = result.holdout
        holdout_summary = holdout.summary
        holdout_portfolio = holdout.portfolio
        lines.extend(
            [
                "",
                "## Финальный holdout",
                "",
                f"- Период: **{holdout.start.isoformat()} … {holdout.end.isoformat()}**; "
                f"когорт: **{holdout_summary.cohorts}**, сделок: **{holdout_summary.trades}**",
                f"- Замороженные параметры: score ≥ **{_num(holdout.selected_min_score)}**, "
                f"top **{holdout.selected_candidates}**",
                f"- Средняя чистая: **{_pct(holdout_summary.mean_net_return, signed=True)}**; "
                f"превышение: **{_pct(holdout_summary.mean_excess, signed=True)}** "
                f"(95% ДИ {_pct(holdout_summary.excess_ci_low, signed=True)}…"
                f"{_pct(holdout_summary.excess_ci_high, signed=True)})",
                f"- +20%: **{_pct(holdout_summary.target_hit_rate)}**; "
                f"обыграли бенчмарк: **{_pct(holdout_summary.beat_benchmark_rate)}**",
                f"- Портфель: **{_pct(holdout_portfolio.total_return, signed=True)}**; "
                f"бенчмарк: **{_pct(holdout_portfolio.benchmark_return, signed=True)}**; "
                f"макс. просадка: **{_pct(holdout_portfolio.max_drawdown)}**",
                "",
                "> Holdout — последние когорты; параметры выбраны только на предыдущих. "
                "Основная сводка выше не включает holdout: пересечения нет.",
            ]
        )
        if holdout_summary.sample_warning:
            lines.append(f"- ⚠ {holdout_summary.sample_warning}")
    lines.extend(
        [
            "",
            "## Альтернативы",
            "",
            "| Стратегия | Средняя чистая | Превышение | Портфель | Max DD | "
            "+20% | +10 п.п. | Сделок | Примечание |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for alternative in result.alternatives:
        lines.append(
            f"| {alternative.name} | {_pct(alternative.mean_net_return, signed=True)} | "
            f"{_pct(alternative.mean_excess, signed=True)} | "
            f"{_pct(alternative.portfolio_total_return, signed=True)} | "
            f"{_pct(alternative.portfolio_max_drawdown)} | "
            f"{_pct(alternative.target_hit_rate)} | "
            f"{_pct(alternative.excess_hit_rate)} | {alternative.trades} | "
            f"{alternative.note} |"
        )
    filters = [item for item in result.sensitivity if item.get("kind") == "filter"]
    if filters:
        lines.extend(
            [
                "",
                "## Чувствительность фильтров",
                "",
                "| Фильтр | Значение | Сделок | Среднее превышение | Δ к базе | "
                "+20% | Примечание |",
                "|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for row in filters:
            lines.append(
                f"| {row.get('filter', '')} | {row.get('value', '')} | "
                f"{_count(row.get('trades'))} | "
                f"{_pct(row.get('mean_excess'), signed=True)} | "
                f"{_pct(row.get('delta_vs_base'), signed=True)} | "
                f"{_pct(row.get('target_rate'))} | {row.get('note', '')} |"
            )
    deciles = [item for item in result.sensitivity if item.get("kind") == "score_decile"]
    if deciles:
        lines.extend(
            [
                "",
                "## Рейтинг по децилям",
                "",
                "| Группа | Сделок | Рейтинг | Чистая | Медиана | Превышение | "
                "Медиана превышения | +20% | Обыграли |",
                "|---|---:|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in deciles:
            lines.append(
                f"| {row.get('bucket', '')} | {row.get('trades', '')} | "
                f"{_num(row.get('min_score'))}–{_num(row.get('max_score'))} | "
                f"{_pct(row.get('mean_net'), signed=True)} | "
                f"{_pct(row.get('median_net'), signed=True)} | "
                f"{_pct(row.get('mean_excess'), signed=True)} | "
                f"{_pct(row.get('median_excess'), signed=True)} | "
                f"{_pct(row.get('target_rate'))} | {_pct(row.get('beat_rate'))} |"
            )
    bands = [item for item in result.sensitivity if item.get("kind") == "score_band"]
    if bands:
        lines.extend(
            [
                "",
                "## Рейтинг по группам",
                "",
                "| Группа | Сделок | Когорт | Среднее превышение | 95% ДИ | "
                "+20% | 95% ДИ | Обыграли | 95% ДИ | Примечание |",
                "|---|---:|---:|---:|---|---:|---|---:|---|---|",
            ]
        )
        for row in bands:
            lines.append(
                f"| {row.get('bucket', '')} | {row.get('trades', '')} | "
                f"{row.get('cohorts', '')} | "
                f"{_pct(row.get('mean_excess'), signed=True)} | "
                f"{_pct(row.get('mean_excess_ci_low'), signed=True)}…"
                f"{_pct(row.get('mean_excess_ci_high'), signed=True)} | "
                f"{_pct(row.get('target_rate'))} | "
                f"{_pct(row.get('target_ci_low'))}…{_pct(row.get('target_ci_high'))} | "
                f"{_pct(row.get('beat_rate'))} | "
                f"{_pct(row.get('beat_ci_low'))}…{_pct(row.get('beat_ci_high'))} | "
                f"{row.get('note', '')} |"
            )
    diagnostics = _diagnostic_rows(result.diagnostics)
    if diagnostics:
        lines.extend(
            [
                "",
                "## Диагностика сигналов",
                "",
                "| Тип | Объект | Оценка | Интервал / Δ | Когорт / сделок | Примечание |",
                "|---|---|---|---:|---:|---|",
            ]
        )
        for diagnostic in diagnostics:
            lines.append("| " + " | ".join(diagnostic) + " |")
    lines.extend(
        [
            "",
            "## Когорты",
            "",
            "| Сигнал | Вход | Выход | Кандидатов | Сделок | Чистая | Бенчмарк | "
            "Превышение | +20% |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for cohort in result.cohorts:
        lines.append(
            f"| {cohort.signal_date} | {cohort.entry_date} | {cohort.exit_date} | "
            f"{cohort.candidates} | {cohort.trades} | "
            f"{_pct(cohort.mean_net_return, signed=True)} | "
            f"{_pct(cohort.mean_benchmark_return, signed=True)} | "
            f"{_pct(cohort.mean_excess, signed=True)} | {_pct(cohort.target_hit_rate)} |"
        )
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines)


def backtest_to_html(result: BacktestResult) -> str:
    summary = result.summary
    alt_rows = [
        [
            html.escape(item.name),
            _pct(item.mean_net_return, signed=True),
            _pct(item.mean_excess, signed=True),
            _pct(item.portfolio_total_return, signed=True),
            _pct(item.portfolio_max_drawdown),
            _pct(item.target_hit_rate),
            _pct(item.excess_hit_rate),
            str(item.trades),
            html.escape(item.note),
        ]
        for item in result.alternatives
    ]
    cohort_rows = [
        [
            item.signal_date.isoformat(),
            item.entry_date.isoformat(),
            item.exit_date.isoformat(),
            str(item.candidates),
            str(item.trades),
            _pct(item.mean_net_return, signed=True),
            _pct(item.mean_benchmark_return, signed=True),
            _pct(item.mean_excess, signed=True),
            _pct(item.target_hit_rate),
        ]
        for item in result.cohorts
    ]
    sensitivity_rows = [
        [
            html.escape(str(item.get("min_score", ""))),
            html.escape(str(item.get("max_candidates", ""))),
            html.escape(_count(item.get("trades"))),
            _pct(item.get("mean_excess"), signed=True),
            _pct(item.get("target_rate")),
        ]
        for item in result.sensitivity
        if item.get("kind") == "parameter"
    ]
    filter_rows = [
        [
            html.escape(str(item.get("filter", ""))),
            html.escape(str(item.get("value", ""))),
            html.escape(_count(item.get("trades"))),
            _pct(item.get("mean_excess"), signed=True),
            _pct(item.get("delta_vs_base"), signed=True),
            _pct(item.get("target_rate")),
            html.escape(str(item.get("note", ""))),
        ]
        for item in result.sensitivity
        if item.get("kind") == "filter"
    ]
    decile_rows = [
        [
            html.escape(str(item.get("bucket", ""))),
            html.escape(str(item.get("trades", ""))),
            f"{_num(item.get('min_score'))}–{_num(item.get('max_score'))}",
            _pct(item.get("mean_net"), signed=True),
            _pct(item.get("median_net"), signed=True),
            _pct(item.get("mean_excess"), signed=True),
            _pct(item.get("median_excess"), signed=True),
            _pct(item.get("target_rate")),
            _pct(item.get("beat_rate")),
        ]
        for item in result.sensitivity
        if item.get("kind") == "score_decile"
    ]
    fold_rows = [
        [
            html.escape(str(item.get("test_start", ""))),
            html.escape(str(item.get("test_end", ""))),
            html.escape(str(item.get("train_cohorts", ""))),
            html.escape(str(item.get("purged_train_cohorts", ""))),
            html.escape(str(item.get("selected_min_score", ""))),
            html.escape(str(item.get("selected_candidates", ""))),
            html.escape(str(item.get("trades", ""))),
            _pct(item.get("test_mean_excess"), signed=True),
        ]
        for item in result.sensitivity
        if item.get("kind") == "fold"
    ]
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in result.warnings)
    portfolio_kpis = ""
    portfolio_section = ""
    holdout_section = ""
    if result.portfolio is not None:
        portfolio = result.portfolio
        portfolio_kpis = (
            f'<div class="kpi">Портфель<b>{_pct(portfolio.total_return, signed=True)}</b></div>'
            f'<div class="kpi">CAGR<b>{_pct(portfolio.cagr, signed=True)}</b></div>'
            f'<div class="kpi">Sharpe<b>{_num(portfolio.sharpe)}</b></div>'
            f'<div class="kpi">Макс. просадка<b>{_pct(portfolio.max_drawdown)}</b></div>'
        )
        metric_rows = [
            ["Итоговая доходность", _pct(portfolio.total_return, signed=True)],
            ["Доходность бенчмарка", _pct(portfolio.benchmark_return, signed=True)],
            ["Превышение", _pct(portfolio.excess_return, signed=True)],
            ["CAGR", _pct(portfolio.cagr, signed=True)],
            ["Волатильность", _pct(portfolio.volatility)],
            ["Sharpe", _num(portfolio.sharpe)],
            ["Sortino", _num(portfolio.sortino)],
            ["Максимальная просадка", _pct(portfolio.max_drawdown)],
            ["Оборот", f"{_num(portfolio.turnover)}x/год"],
            ["Средняя экспозиция", _pct(portfolio.exposure)],
            [
                "Комиссия / проскальзывание",
                f"{_num(portfolio.commission_paid)}x / {_num(portfolio.slippage_paid)}x",
            ],
            ["Издержки к среднему капиталу", _pct(portfolio.cost_drag)],
            ["Дней / сделок", f"{portfolio.periods} / {portfolio.trades}"],
        ]
        yearly_rows = [
            [
                str(item.year),
                _pct(item.return_value, signed=True),
                _pct(item.benchmark_return, signed=True),
                _pct(item.max_drawdown),
            ]
            for item in portfolio.yearly
        ]
        warning = f"<p>⚠ {html.escape(portfolio.warning)}</p>" if portfolio.warning else ""
        portfolio_section = (
            '<h2>Портфель</h2><div class="panel">'
            + _html_table(["Метрика", "Значение"], metric_rows)
            + warning
            + "</div>"
            + (
                '<h2>Портфель по годам</h2><div class="panel">'
                + _html_table(["Год", "Портфель", "Бенчмарк", "Макс. просадка"], yearly_rows)
                + "</div>"
                if yearly_rows
                else ""
            )
        )
    if result.holdout is not None:
        holdout = result.holdout
        holdout_summary = holdout.summary
        holdout_portfolio = holdout.portfolio
        holdout_rows = [
            [
                "Период",
                f"{holdout.start.isoformat()} … {holdout.end.isoformat()}",
            ],
            [
                "Замороженные параметры",
                f"score ≥ {_num(holdout.selected_min_score)}, top {holdout.selected_candidates}",
            ],
            ["Когорт / сделок", f"{holdout_summary.cohorts} / {holdout_summary.trades}"],
            [
                "Средняя чистая / превышение",
                f"{_pct(holdout_summary.mean_net_return, signed=True)} / "
                f"{_pct(holdout_summary.mean_excess, signed=True)}",
            ],
            [
                "95% ДИ превышения",
                f"{_pct(holdout_summary.excess_ci_low, signed=True)} … "
                f"{_pct(holdout_summary.excess_ci_high, signed=True)}",
            ],
            [
                "+20% / обыграли",
                f"{_pct(holdout_summary.target_hit_rate)} / "
                f"{_pct(holdout_summary.beat_benchmark_rate)}",
            ],
            [
                "MAE медиана / худшая",
                f"{_pct(holdout_summary.median_max_adverse, signed=True)} / "
                f"{_pct(holdout_summary.worst_max_adverse, signed=True)}",
            ],
            [
                "Портфель / бенчмарк",
                f"{_pct(holdout_portfolio.total_return, signed=True)} / "
                f"{_pct(holdout_portfolio.benchmark_return, signed=True)}",
            ],
            ["Макс. просадка", _pct(holdout_portfolio.max_drawdown)],
        ]
        holdout_note = (
            f"<p>⚠ {html.escape(holdout_summary.sample_warning)}</p>"
            if holdout_summary.sample_warning
            else ""
        )
        holdout_section = (
            '<h2>Финальный holdout</h2><div class="panel">'
            + _html_table(["Метрика", "Значение"], holdout_rows)
            + holdout_note
            + "<p>Параметры заморожены на предыдущих когортах; основная сводка выше "
            "не включает holdout — пересечения нет.</p></div>"
        )
    diagnostic_rows = [
        [html.escape(cell) for cell in row] for row in _diagnostic_rows(result.diagnostics)
    ]
    diagnostics_section = (
        '<h2>Диагностика сигналов</h2><div class="panel">'
        + _html_table(
            [
                "Тип",
                "Объект",
                "Оценка",
                "Интервал / Δ",
                "Когорт / сделок",
                "Примечание",
            ],
            diagnostic_rows,
        )
        + "</div>"
        if diagnostic_rows
        else ""
    )
    verdict_section = ""
    if result.verdict is not None:
        verdict = result.verdict
        details = "".join(f"<li>{html.escape(item)}</li>" for item in verdict.details)
        verdict_section = (
            '<h2>Вывод</h2><div class="panel">'
            f"<p><strong>{html.escape(verdict.headline)}</strong></p>"
            f"<ul>{details}</ul></div>"
        )
    band_rows = [
        [
            html.escape(str(item.get("bucket", ""))),
            str(item.get("trades", "")),
            str(item.get("cohorts", "")),
            _pct(item.get("mean_excess"), signed=True),
            f"{_pct(item.get('mean_excess_ci_low'), signed=True)}…"
            f"{_pct(item.get('mean_excess_ci_high'), signed=True)}",
            _pct(item.get("target_rate")),
            f"{_pct(item.get('target_ci_low'))}…{_pct(item.get('target_ci_high'))}",
            _pct(item.get("beat_rate")),
            f"{_pct(item.get('beat_ci_low'))}…{_pct(item.get('beat_ci_high'))}",
            html.escape(str(item.get("note", ""))),
        ]
        for item in result.sensitivity
        if item.get("kind") == "score_band"
    ]
    trade_rows = [
        [
            html.escape(item.ticker),
            item.signal_date.isoformat(),
            item.entry_date.isoformat(),
            item.exit_date.isoformat(),
            f"{item.score:.0f}",
            _price(item.entry_price),
            _price(item.exit_price),
            _pct(item.net_return, signed=True),
            _pct(item.benchmark_return, signed=True),
            _pct(item.excess_return, signed=True),
            "да" if item.target_hit else "нет",
            "да" if item.excess_hit else "нет",
        ]
        for item in result.trades[:100]
    ]
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Walk-forward {result.start.isoformat()}</title><style>{CSS}</style></head>
<body><main>
<h1>Walk-forward бэктест</h1>
<p class="sub">{result.start.strftime("%d.%m.%Y")} — {result.end.strftime("%d.%m.%Y")} ·
бенчмарк <b>{html.escape(result.benchmark)}</b></p>
<div class="grid">
<div class="kpi">Когорт<b>{summary.cohorts}</b></div>
<div class="kpi">Сделок<b>{summary.trades}</b></div>
<div class="kpi">Средняя чистая<b>{_pct(summary.mean_net_return, signed=True)}</b></div>
<div class="kpi">Среднее превышение<b>{_pct(summary.mean_excess, signed=True)}</b></div>
<div class="kpi">+20%<b>{_pct(summary.target_hit_rate)}</b></div>
<div class="kpi">+10 п.п.<b>{_pct(summary.excess_hit_rate)}</b></div>
<div class="kpi">MAE медиана<b>{_pct(summary.median_max_adverse, signed=True)}</b></div>
{portfolio_kpis}
</div>
<div class="warnbox"><strong>Out-of-sample, но не гарантия.</strong>
Параметры выбирались только на прошлых когортах. Цель +20% — по максимуму внутри
горизонта; доходность — после издержек. 95% ДИ доли: +20% —
{_pct(summary.target_ci_low)}…{_pct(summary.target_ci_high)}, обыграли —
{_pct(summary.beat_ci_low)}…{_pct(summary.beat_ci_high)}.
{html.escape(summary.sample_warning or "")}</div>
{verdict_section}
{portfolio_section}
{holdout_section}
<h2>Альтернативы</h2><div class="panel">{
        _html_table(
            [
                "Стратегия",
                "Средняя чистая",
                "Превышение",
                "Портфель",
                "Max DD",
                "+20%",
                "+10 п.п.",
                "Сделок",
                "Примечание",
            ],
            alt_rows,
        )
    }</div>
<h2>Когорты</h2><div class="panel">{
        _html_table(
            [
                "Сигнал",
                "Вход",
                "Выход",
                "Кандидатов",
                "Сделок",
                "Чистая",
                "Бенчмарк",
                "Превышение",
                "+20%",
            ],
            cohort_rows,
        )
    }</div>
<h2>Чувствительность к порогу и числу кандидатов</h2><div class="panel">{
        _html_table(
            ["min_score", "max_candidates", "Сделок", "Среднее превышение", "+20%"],
            sensitivity_rows,
        )
    }</div>
<h2>Чувствительность фильтров</h2><div class="panel">{
        _html_table(
            [
                "Фильтр",
                "Значение",
                "Сделок",
                "Среднее превышение",
                "Δ к базе",
                "+20%",
                "Примечание",
            ],
            filter_rows,
        )
    }</div>
<h2>Рейтинг по децилям</h2><div class="panel">{
        _html_table(
            [
                "Группа",
                "Сделок",
                "Рейтинг",
                "Чистая",
                "Медиана",
                "Превышение",
                "Медиана превышения",
                "+20%",
                "Обыграли",
            ],
            decile_rows,
        )
    }</div>
<h2>Рейтинг по группам</h2><div class="panel">{
        _html_table(
            [
                "Группа",
                "Сделок",
                "Когорт",
                "Среднее превышение",
                "95% ДИ",
                "+20%",
                "95% ДИ",
                "Обыграли",
                "95% ДИ",
                "Примечание",
            ],
            band_rows,
        )
    }</div>
{diagnostics_section}
<h2>Walk-forward фолды</h2><div class="panel">{
        _html_table(
            [
                "Тест с",
                "Тест по",
                "Обучение",
                "Убрано",
                "min_score",
                "Кандидатов",
                "Сделок",
                "Превышение",
            ],
            fold_rows,
        )
    }</div>
<h2>Сделки (первые 100)</h2><div class="panel">{
        _html_table(
            [
                "Тикер",
                "Сигнал",
                "Вход",
                "Выход",
                "Рейтинг",
                "Вход",
                "Выход",
                "Чистая",
                "Бенчмарк",
                "Превышение",
                "+20%",
                "+10 п.п.",
            ],
            trade_rows,
        )
    }</div>
<h2>Ограничения</h2><div class="panel"><ul>{warnings}</ul></div>
<h2>Методика</h2><div class="panel"><p>Сигнал после закрытия; вход на открытии следующей
сессии; горизонт 63 сессии; выход по закрытию. Комиссия и проскальзывание на вход и выход.
Бенчмарк — total return {html.escape(result.benchmark)}. Фундаментал в истории не используется.
Список бумаг текущий, поэтому survivorship bias остаётся.
Портфель: равный вес внутри когорты, лимит на бумагу и свободный кэш; при перекрытии
когорт новые входы ограничены доступным кэшем.</p></div>
<footer>Сгенерировано {result.generated_at.strftime("%d.%m.%Y %H:%M UTC")}.</footer>
</main></body></html>"""


def write_backtest_html(result: BacktestResult, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(backtest_to_html(result), encoding="utf-8")
    return target


def write_backtest_markdown(result: BacktestResult, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(backtest_to_markdown(result), encoding="utf-8")
    return target


def load_json_run(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def timestamp_slug(value: datetime) -> str:
    return value.strftime("%Y%m%d-%H%M%S")
