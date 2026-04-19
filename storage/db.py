from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DecisionLog:
    timestamp: str
    candle_time: str | None
    instrument: str
    timeframe: str
    strategy_name: str
    action: str
    price: float
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    reason: str
    order_requested: bool
    order_placed: bool
    dry_run: bool
    has_open_position: bool
    position_units: int | None = None
    stop_loss_price: float | None = None
    order_id: str | None = None
    broker_response: dict[str, Any] | None = None
    error_message: str | None = None
    forward_test: bool = False
    indicators: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecentDecisionLog:
    id: int
    timestamp: str
    candle_time: str | None
    instrument: str
    timeframe: str
    action: str
    price: float
    reason: str
    order_requested: bool
    order_placed: bool
    dry_run: bool
    forward_test: bool
    indicators: dict[str, Any]
    error_message: str | None


class TradingLogStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    candle_time TEXT,
                    instrument TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    ema_fast REAL,
                    ema_slow REAL,
                    atr REAL,
                    reason TEXT NOT NULL,
                    order_requested INTEGER NOT NULL,
                    order_placed INTEGER NOT NULL,
                    dry_run INTEGER NOT NULL,
                    has_open_position INTEGER NOT NULL,
                    position_units INTEGER,
                    stop_loss_price REAL,
                    order_id TEXT,
                    broker_response TEXT,
                    error_message TEXT,
                    forward_test INTEGER NOT NULL DEFAULT 0,
                    indicators TEXT
                )
                """
            )
            _ensure_column(
                connection,
                table_name="trade_logs",
                column_name="forward_test",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            _ensure_column(
                connection,
                table_name="trade_logs",
                column_name="indicators",
                column_definition="TEXT",
            )
            connection.commit()

    def get_recent_logs(self, limit: int = 5) -> list[RecentDecisionLog]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    id,
                    timestamp,
                    candle_time,
                    instrument,
                    timeframe,
                    action,
                    price,
                    reason,
                    order_requested,
                    order_placed,
                    dry_run,
                    forward_test,
                    indicators,
                    error_message
                FROM trade_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            RecentDecisionLog(
                id=row["id"],
                timestamp=row["timestamp"],
                candle_time=row["candle_time"],
                instrument=row["instrument"],
                timeframe=row["timeframe"],
                action=row["action"],
                price=row["price"],
                reason=row["reason"],
                order_requested=bool(row["order_requested"]),
                order_placed=bool(row["order_placed"]),
                dry_run=bool(row["dry_run"]),
                forward_test=bool(row["forward_test"]),
                indicators=_decode_json_dict(row["indicators"]),
                error_message=row["error_message"],
            )
            for row in rows
        ]

    def get_latest_candle_time(
        self,
        *,
        instrument: str,
        timeframe: str,
        strategy_name: str,
        forward_test_only: bool = False,
    ) -> str | None:
        query = """
            SELECT candle_time
            FROM trade_logs
            WHERE instrument = ?
              AND timeframe = ?
              AND strategy_name = ?
        """
        params: list[Any] = [instrument, timeframe, strategy_name]
        if forward_test_only:
            query += " AND forward_test = 1"
        query += " ORDER BY id DESC LIMIT 1"

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(query, params).fetchone()

        return str(row[0]) if row is not None and row[0] is not None else None

    def log(self, record: DecisionLog) -> None:
        broker_response = json.dumps(record.broker_response) if record.broker_response else None
        indicators = json.dumps(record.indicators, sort_keys=True) if record.indicators else None

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO trade_logs (
                    timestamp,
                    candle_time,
                    instrument,
                    timeframe,
                    strategy_name,
                    action,
                    price,
                    ema_fast,
                    ema_slow,
                    atr,
                    reason,
                    order_requested,
                    order_placed,
                    dry_run,
                    has_open_position,
                    position_units,
                    stop_loss_price,
                    order_id,
                    broker_response,
                    error_message,
                    forward_test,
                    indicators
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.candle_time,
                    record.instrument,
                    record.timeframe,
                    record.strategy_name,
                    record.action,
                    record.price,
                    record.ema_fast,
                    record.ema_slow,
                    record.atr,
                    record.reason,
                    int(record.order_requested),
                    int(record.order_placed),
                    int(record.dry_run),
                    int(record.has_open_position),
                    record.position_units,
                    record.stop_loss_price,
                    record.order_id,
                    broker_response,
                    record.error_message,
                    int(record.forward_test),
                    indicators,
                ),
            )
            connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _decode_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
