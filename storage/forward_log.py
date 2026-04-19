from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import DecisionLog

FORWARD_TEST_LOG_COLUMNS = [
    "run_timestamp",
    "candle_time",
    "instrument",
    "timeframe",
    "strategy",
    "action",
    "reason",
    "price",
    "rsi",
    "ema200",
    "atr",
    "atr_median",
    "trend_filter_status",
    "atr_filter_status",
    "pricing_status",
    "bid",
    "ask",
    "spread_pips",
    "max_spread_pips",
    "dry_run",
    "forward_test",
    "order_requested",
    "order_placed",
    "has_open_position",
    "error_message",
]


@dataclass(frozen=True)
class ForwardLogResult:
    path: Path
    appended: bool
    duplicate: bool


def append_forward_test_log(
    *,
    csv_path: Path,
    record: DecisionLog,
    pricing_status: str,
    bid: float,
    ask: float,
    spread_pips: float,
    max_spread_pips: float,
) -> ForwardLogResult:
    csv_path = Path(csv_path)
    if not record.forward_test:
        return ForwardLogResult(path=csv_path, appended=False, duplicate=False)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if _contains_duplicate(csv_path, record):
        print("Forward-test CSV already contains this candle; skipping duplicate row.")
        return ForwardLogResult(path=csv_path, appended=False, duplicate=True)

    should_write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FORWARD_TEST_LOG_COLUMNS)
        if should_write_header:
            writer.writeheader()
        writer.writerow(
            _build_row(
                record=record,
                pricing_status=pricing_status,
                bid=bid,
                ask=ask,
                spread_pips=spread_pips,
                max_spread_pips=max_spread_pips,
            )
        )

    return ForwardLogResult(path=csv_path, appended=True, duplicate=False)


def _contains_duplicate(csv_path: Path, record: DecisionLog) -> bool:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return False

    with csv_path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            if (
                row.get("candle_time") == (record.candle_time or "")
                and row.get("instrument") == record.instrument
                and row.get("timeframe") == record.timeframe
                and row.get("strategy") == record.strategy_name
            ):
                return True
    return False


def _build_row(
    *,
    record: DecisionLog,
    pricing_status: str,
    bid: float,
    ask: float,
    spread_pips: float,
    max_spread_pips: float,
) -> dict[str, Any]:
    indicators = record.indicators or {}
    return {
        "run_timestamp": record.timestamp,
        "candle_time": record.candle_time or "",
        "instrument": record.instrument,
        "timeframe": record.timeframe,
        "strategy": record.strategy_name,
        "action": record.action,
        "reason": record.reason,
        "price": _optional_value(record.price),
        "rsi": _optional_value(indicators.get("rsi")),
        "ema200": _optional_value(indicators.get("ema200")),
        "atr": _optional_value(indicators.get("atr")),
        "atr_median": _optional_value(indicators.get("atr_median")),
        "trend_filter_status": _optional_value(indicators.get("trend_filter_status")),
        "atr_filter_status": _optional_value(indicators.get("atr_filter_status")),
        "pricing_status": pricing_status,
        "bid": _optional_value(bid),
        "ask": _optional_value(ask),
        "spread_pips": _optional_value(spread_pips),
        "max_spread_pips": _optional_value(max_spread_pips),
        "dry_run": _bool_value(record.dry_run),
        "forward_test": _bool_value(record.forward_test),
        "order_requested": _bool_value(record.order_requested),
        "order_placed": _bool_value(record.order_placed),
        "has_open_position": _bool_value(record.has_open_position),
        "error_message": record.error_message or "",
    }


def _optional_value(value: Any) -> Any:
    return "" if value is None else value


def _bool_value(value: bool) -> str:
    return "true" if value else "false"
