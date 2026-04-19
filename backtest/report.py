from __future__ import annotations

import csv
import math
from pathlib import Path

from .engine import BacktestSummary, BacktestTrade


def save_trades_csv(trades: list[BacktestTrade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trade_id",
        "strategy_name",
        "instrument",
        "timeframe",
        "direction",
        "units",
        "entry_time",
        "exit_time",
        "duration_seconds",
        "entry_reference_price",
        "exit_reference_price",
        "entry_price",
        "exit_price",
        "stop_loss_price",
        "exit_reason",
        "entry_signal_reason",
        "exit_signal_reason",
        "gross_pnl",
        "cost",
        "net_pnl",
        "net_pips",
        "risk_pips",
        "r_multiple",
        "equity_after",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade.to_csv_row())


def format_summary(summary: BacktestSummary) -> str:
    profit_factor = "inf" if math.isinf(summary.profit_factor) else f"{summary.profit_factor:.2f}"
    lines = [
        "BACKTEST REPORT",
        f"strategy: {summary.strategy_name}",
        f"instrument: {summary.instrument}",
        f"timeframe: {summary.timeframe}",
        f"candle count: {summary.candle_count}",
        f"start time: {summary.start_time}",
        f"end time: {summary.end_time}",
        f"sample span days: {summary.sample_span_days:.2f}",
        f"starting equity: {summary.starting_equity:.2f}",
        f"final equity: {summary.final_equity:.2f}",
        f"total return %: {summary.total_return_pct:.2f}",
        "",
        "Buy and Hold:",
        f"starting price: {summary.starting_price:.5f}",
        f"ending price: {summary.ending_price:.5f}",
        f"buy-and-hold return %: {summary.buy_and_hold_return_pct:.2f}",
        "",
        "Strategy Performance:",
        f"total trades: {summary.total_trades}",
        f"win rate: {summary.win_rate:.2f}%",
        f"average win: {summary.average_win:.2f}",
        f"average loss: {summary.average_loss:.2f}",
        f"profit factor: {profit_factor}",
        f"max drawdown: {summary.max_drawdown:.2f}%",
        f"worst losing streak: {summary.worst_losing_streak}",
        f"average trade expectancy: {summary.average_trade_expectancy:.2f}",
        f"largest win: {summary.largest_win:.2f}",
        f"largest loss: {summary.largest_loss:.2f}",
        f"net pips: {summary.net_pips:.2f}",
        f"average pips per trade: {summary.average_pips_per_trade:.2f}",
        f"pips won: {summary.pips_won:.2f}",
        f"pips lost: {summary.pips_lost:.2f}",
        f"total R: {summary.total_r:.2f}",
        f"average R: {summary.average_r:.2f}",
        f"profits from best 5 trades: {summary.percent_profits_from_best_5_trades:.2f}%",
        "",
        "Exposure:",
        f"percent time in market: {summary.percent_time_in_market:.2f}%",
        f"long trades: {summary.long_trades}",
        f"short trades: {summary.short_trades}",
        f"average trade duration: {summary.average_trade_duration_minutes:.2f} minutes",
        f"median trade duration: {summary.median_trade_duration_minutes:.2f} minutes",
        "",
        "Assumptions:",
        f"backtest spread pips: {summary.backtest_spread_pips:.2f}",
        f"backtest slippage pips: {summary.backtest_slippage_pips:.2f}",
        f"stop loss enabled: {summary.stop_loss_enabled}",
        "",
        "Notes:",
        "- This is a historical simulation only and never places OANDA orders.",
        "- Entries/exits use completed midpoint candles with configured spread and slippage.",
        "- Stop losses are simulated using completed candle high/low data and the existing ATR/fallback stop logic.",
    ]

    if summary.profit_concentration_warning:
        warning_after = lines.index(
            f"profits from best 5 trades: {summary.percent_profits_from_best_5_trades:.2f}%"
        )
        lines.insert(warning_after + 1, f"WARNING: {summary.profit_concentration_warning}")

    if summary.period_breakdowns:
        lines.extend(
            [
                "",
                f"{summary.breakdown_frequency.title()} Breakdown:",
                "period | trades | net pnl | return %",
            ]
        )
        for period in summary.period_breakdowns:
            lines.append(f"{period.period} | {period.trades} | {period.net_pnl:.2f} | {period.return_pct:.2f}")
    else:
        lines.extend(
            [
                "",
                "Period Breakdown:",
                "Not enough data for weekly or monthly breakdown.",
            ]
        )

    return "\n".join(lines)


def save_summary_report(summary: BacktestSummary, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = format_summary(summary)
    path.write_text(report + "\n", encoding="utf-8")
    return report


def save_strategy_comparison_csv(summaries: list[BacktestSummary], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "strategy_name",
        "instrument",
        "timeframe",
        "candle_count",
        "final_equity",
        "total_return_pct",
        "buy_and_hold_return_pct",
        "total_trades",
        "win_rate",
        "profit_factor",
        "max_drawdown",
        "net_pips",
        "average_pips_per_trade",
        "pips_won",
        "pips_lost",
        "total_r",
        "average_r",
        "percent_time_in_market",
        "long_trades",
        "short_trades",
        "percent_profits_from_best_5_trades",
        "profit_concentration_warning",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for summary in summaries:
            writer.writerow({field: getattr(summary, field) for field in fieldnames})


def save_instrument_strategy_comparison_csv(summaries: list[BacktestSummary], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "instrument",
        "strategy",
        "timeframe",
        "return_pct",
        "trades",
        "win_pct",
        "profit_factor",
        "max_drawdown_pct",
        "net_pips",
        "avg_pips_per_trade",
        "total_r",
        "exposure_pct",
        "best5_pct",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for summary in summaries:
            writer.writerow(
                {
                    "instrument": summary.instrument,
                    "strategy": summary.strategy_name,
                    "timeframe": summary.timeframe,
                    "return_pct": summary.total_return_pct,
                    "trades": summary.total_trades,
                    "win_pct": summary.win_rate,
                    "profit_factor": summary.profit_factor,
                    "max_drawdown_pct": summary.max_drawdown,
                    "net_pips": summary.net_pips,
                    "avg_pips_per_trade": summary.average_pips_per_trade,
                    "total_r": summary.total_r,
                    "exposure_pct": summary.percent_time_in_market,
                    "best5_pct": summary.percent_profits_from_best_5_trades,
                }
            )


def save_timeframe_strategy_comparison_csv(summaries: list[BacktestSummary], path: Path) -> None:
    save_instrument_strategy_comparison_csv(summaries, path)


def save_walk_forward_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "window_start",
        "window_end",
        "trades",
        "win_pct",
        "profit_factor",
        "net_pips",
        "avg_pips_per_trade",
        "total_r",
        "max_drawdown_pct",
        "best5_pct",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})


def save_walk_forward_report(report: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report + "\n", encoding="utf-8")


def format_strategy_comparison_table(summaries: list[BacktestSummary]) -> str:
    headers = [
        "strategy",
        "return%",
        "trades",
        "win%",
        "pf",
        "dd%",
        "net pips",
        "avg pips",
        "total R",
        "exposure%",
        "best5%",
    ]
    rows = []
    for summary in summaries:
        profit_factor = "inf" if math.isinf(summary.profit_factor) else f"{summary.profit_factor:.2f}"
        rows.append(
            [
                summary.strategy_name,
                f"{summary.total_return_pct:.2f}",
                str(summary.total_trades),
                f"{summary.win_rate:.1f}",
                profit_factor,
                f"{summary.max_drawdown:.2f}",
                f"{summary.net_pips:.1f}",
                f"{summary.average_pips_per_trade:.2f}",
                f"{summary.total_r:.2f}",
                f"{summary.percent_time_in_market:.1f}",
                f"{summary.percent_profits_from_best_5_trades:.1f}",
            ]
        )

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def format_instrument_strategy_comparison_table(summaries: list[BacktestSummary]) -> str:
    headers = [
        "instrument",
        "strategy",
        "timeframe",
        "return%",
        "trades",
        "win%",
        "pf",
        "dd%",
        "net pips",
        "avg pips",
        "total R",
        "exposure%",
        "best5%",
    ]
    rows = []
    for summary in summaries:
        profit_factor = "inf" if math.isinf(summary.profit_factor) else f"{summary.profit_factor:.2f}"
        rows.append(
            [
                summary.instrument,
                summary.strategy_name,
                summary.timeframe,
                f"{summary.total_return_pct:.2f}",
                str(summary.total_trades),
                f"{summary.win_rate:.1f}",
                profit_factor,
                f"{summary.max_drawdown:.2f}",
                f"{summary.net_pips:.1f}",
                f"{summary.average_pips_per_trade:.2f}",
                f"{summary.total_r:.2f}",
                f"{summary.percent_time_in_market:.1f}",
                f"{summary.percent_profits_from_best_5_trades:.1f}",
            ]
        )

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)
