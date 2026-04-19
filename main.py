from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from backtest import (
    BacktestEngine,
    format_instrument_strategy_comparison_table,
    format_strategy_comparison_table,
    save_instrument_strategy_comparison_csv,
    save_strategy_comparison_csv,
    save_summary_report,
    save_timeframe_strategy_comparison_csv,
    save_trades_csv,
    save_walk_forward_csv,
    save_walk_forward_report,
)
from brokers import (
    OANDA_CANDLE_MAX_COUNT,
    InstrumentMetadata,
    OandaApiError,
    OandaClient,
    PositionSnapshot,
    PricingSnapshot,
    granularity_to_seconds,
)
from config import Settings, load_settings
from risk import PositionPlan, build_position_plan
from storage import DecisionLog, RecentDecisionLog, TradingLogStore
from strategies import SignalDecision, generate_signal_for_strategy, get_all_strategies, get_strategy

LOGGER = logging.getLogger("trading_bot")

TESTED_CANDIDATE_STRATEGY = "rsi_reversion_filtered_v1"
TESTED_CANDIDATE_INSTRUMENT = "GBP_USD"
TESTED_CANDIDATE_TIMEFRAME = "H4"
TESTED_CANDIDATE_TREND_MODE = "against_trend"
TESTED_CANDIDATE_ATR_MODE = "below_median"
TESTED_CANDIDATE_SESSION = "off"


@dataclass(frozen=True)
class MarketSnapshot:
    balance: str | None
    currency: str | None
    instrument_details: InstrumentMetadata
    candle_time: str
    candle_close: float
    signal: SignalDecision
    generated_signal_action: str
    signal_was_forced: bool
    atr: float | None
    position: PositionSnapshot | None
    pricing: PricingSnapshot
    spread_pips: float


def configure_logging(settings: Settings) -> None:
    log_file = settings.log_dir / "bot.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def describe_position(position: PositionSnapshot | None) -> str:
    if position is None or position.net_units == 0:
        return "no open position"
    return (
        "open position detected "
        f"(long_units={position.long_units}, short_units={position.short_units}, net_units={position.net_units})"
    )


def mask_account_id(account_id: str) -> str:
    if len(account_id) <= 8:
        return account_id
    return f"{account_id[:7]}...{account_id[-4:]}"


def pip_size_from_pip_location(pip_location: int) -> float:
    return 10 ** pip_location


def calculate_spread_pips(bid: float, ask: float, pip_location: int) -> float:
    pip_size = pip_size_from_pip_location(pip_location)
    if pip_size <= 0:
        raise ValueError("pip size must be positive.")
    return (ask - bid) / pip_size


def _indicator_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _indicator_int(value: Any, fallback: int) -> int:
    if value is None or isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _format_indicator_number(value: Any, *, decimals: int) -> str:
    numeric_value = _indicator_float(value)
    if numeric_value is None:
        return "n/a"
    return f"{numeric_value:.{decimals}f}"


def format_indicator_lines(indicators: dict[str, Any], settings: Settings | None = None) -> list[str]:
    lines: list[str] = []
    shown_keys: set[str] = set()

    if "rsi" in indicators:
        default_period = settings.rsi_period if settings is not None else 14
        period = _indicator_int(indicators.get("rsi_period"), default_period)
        lines.append(f"RSI{period}: {_format_indicator_number(indicators.get('rsi'), decimals=2)}")
        shown_keys.update({"rsi", "rsi_period"})

    fast_period = indicators.get("ema_fast_period")
    slow_period = indicators.get("ema_slow_period")
    if fast_period is not None:
        period = _indicator_int(fast_period, settings.ema_fast_period if settings is not None else 20)
        key = f"ema{period}"
        lines.append(f"EMA{period}: {_format_indicator_number(indicators.get(key), decimals=5)}")
        shown_keys.update({"ema_fast_period", key, f"previous_ema{period}"})
    if slow_period is not None:
        period = _indicator_int(slow_period, settings.ema_slow_period if settings is not None else 50)
        key = f"ema{period}"
        lines.append(f"EMA{period}: {_format_indicator_number(indicators.get(key), decimals=5)}")
        shown_keys.update({"ema_slow_period", key, f"previous_ema{period}"})

    trend_period = indicators.get("trend_ema_period")
    if trend_period is not None:
        period = _indicator_int(trend_period, settings.trend_filter_ema_period if settings is not None else 200)
        key = f"ema{period}"
        if key not in shown_keys:
            lines.append(f"EMA{period}: {_format_indicator_number(indicators.get(key), decimals=5)}")
        shown_keys.update({"trend_ema_period", key})

    if "ema200" in indicators and "ema200" not in shown_keys:
        lines.append(f"EMA200: {_format_indicator_number(indicators.get('ema200'), decimals=5)}")
        shown_keys.add("ema200")

    if "breakout_lookback" in indicators:
        lookback = _indicator_int(indicators.get("breakout_lookback"), settings.breakout_lookback if settings else 0)
        lines.append(f"breakout lookback: {lookback}")
        lines.append(f"previous high: {_format_indicator_number(indicators.get('previous_high'), decimals=5)}")
        lines.append(f"previous low: {_format_indicator_number(indicators.get('previous_low'), decimals=5)}")
        shown_keys.update({"breakout_lookback", "previous_high", "previous_low"})

    if "atr" in indicators:
        default_period = settings.atr_period if settings is not None else 14
        period = _indicator_int(indicators.get("atr_period"), default_period)
        lines.append(f"ATR{period}: {_format_indicator_number(indicators.get('atr'), decimals=5)}")
        shown_keys.update({"atr", "atr_period"})

    if "trend_filter_mode" in indicators:
        status = indicators.get("trend_filter_status")
        line = f"trend filter: {indicators.get('trend_filter_mode')}"
        if status:
            line = f"{line} | status={status}"
            shown_keys.add("trend_filter_status")
        lines.append(line)
        shown_keys.add("trend_filter_mode")

    if "atr_median" in indicators:
        median_period = _indicator_int(
            indicators.get("atr_median_period"),
            settings.rsi_filter_atr_median_period if settings is not None else 100,
        )
        lines.append(
            f"ATR median({median_period}): {_format_indicator_number(indicators.get('atr_median'), decimals=5)}"
        )
        shown_keys.update({"atr_median", "atr_median_period"})

    if "atr_filter_mode" in indicators:
        status = indicators.get("atr_filter_status")
        line = f"ATR filter: {indicators.get('atr_filter_mode')}"
        if status:
            line = f"{line} | status={status}"
            shown_keys.add("atr_filter_status")
        lines.append(line)
        shown_keys.add("atr_filter_mode")

    if "session_filter" in indicators:
        status = indicators.get("session_filter_status")
        line = f"session filter: {indicators.get('session_filter')}"
        if status:
            line = f"{line} | status={status}"
            shown_keys.add("session_filter_status")
        lines.append(line)
        shown_keys.add("session_filter")

    for key in sorted(indicators):
        if key in shown_keys or key.startswith("previous_"):
            continue
        value = indicators[key]
        if isinstance(value, float):
            value_text = f"{value:.5f}"
        else:
            value_text = str(value)
        lines.append(f"{key}: {value_text}")

    return lines


def format_signal_indicator_lines(settings: Settings, signal: SignalDecision) -> list[str]:
    lines = format_indicator_lines(signal.indicators, settings)
    if lines:
        return lines

    # Compatibility fallback for old SignalDecision instances that predate the generic indicators dict.
    if settings.strategy_name.startswith("rsi_reversion"):
        return [
            f"RSI{settings.rsi_period}: {_format_indicator_number(signal.ema_fast, decimals=2)}",
            f"ATR{settings.atr_period}: {_format_indicator_number(signal.atr, decimals=5)}",
        ]

    return [
        f"EMA{settings.ema_fast_period}: {_format_indicator_number(signal.ema_fast, decimals=5)}",
        f"EMA{settings.ema_slow_period}: {_format_indicator_number(signal.ema_slow, decimals=5)}",
        f"ATR{settings.atr_period}: {_format_indicator_number(signal.atr, decimals=5)}",
    ]


def print_signal_indicators(settings: Settings, signal: SignalDecision, *, indent: str = "") -> None:
    print(f"{indent}indicators:")
    for line in format_signal_indicator_lines(settings, signal):
        print(f"{indent}  {line}")


def format_signal_indicator_summary(settings: Settings, signal: SignalDecision) -> str:
    return "; ".join(format_signal_indicator_lines(settings, signal))


def format_recent_indicator_summary(indicators: dict[str, Any]) -> str:
    return "; ".join(format_indicator_lines(indicators)) if indicators else "not logged"


def extract_order_id(response: dict) -> str | None:
    transaction_keys = (
        "orderCreateTransaction",
        "orderFillTransaction",
        "orderCancelTransaction",
    )
    id_keys = ("id", "orderID")

    for transaction_key in transaction_keys:
        transaction = response.get(transaction_key)
        if not isinstance(transaction, dict):
            continue
        for id_key in id_keys:
            value = transaction.get(id_key)
            if value:
                return str(value)

    last_transaction_id = response.get("lastTransactionID")
    if last_transaction_id:
        return str(last_transaction_id)
    return None


def log_cycle_result(store: TradingLogStore, record: DecisionLog) -> None:
    if should_skip_duplicate_forward_test_log(store, record):
        print("Already logged this completed candle; skipping duplicate log.")
        LOGGER.info(
            "Skipped duplicate forward-test log | instrument=%s | timeframe=%s | strategy=%s | candle=%s",
            record.instrument,
            record.timeframe,
            record.strategy_name,
            record.candle_time,
        )
        return

    store.log(record)
    LOGGER.info(
        "Logged decision to SQLite | action=%s | order_requested=%s | order_placed=%s | forward_test=%s",
        record.action,
        record.order_requested,
        record.order_placed,
        record.forward_test,
    )


def should_skip_duplicate_forward_test_log(store: TradingLogStore, record: DecisionLog) -> bool:
    if not record.forward_test or record.action not in {"HOLD", "BUY", "SELL"} or record.candle_time is None:
        return False

    latest_candle_time = store.get_latest_candle_time(
        instrument=record.instrument,
        timeframe=record.timeframe,
        strategy_name=record.strategy_name,
        forward_test_only=True,
    )
    return latest_candle_time == record.candle_time


def create_decision_log(
    *,
    settings: Settings,
    signal: SignalDecision,
    reason: str,
    order_requested: bool,
    order_placed: bool,
    has_open_position: bool,
    dry_run: bool,
    position_units: int | None = None,
    stop_loss_price: float | None = None,
    order_id: str | None = None,
    broker_response: dict | None = None,
    error_message: str | None = None,
) -> DecisionLog:
    return DecisionLog(
        timestamp=utc_now(),
        candle_time=signal.candle_time,
        instrument=settings.instrument,
        timeframe=settings.granularity,
        strategy_name=settings.strategy_name,
        action=signal.action,
        price=signal.price,
        ema_fast=signal.ema_fast,
        ema_slow=signal.ema_slow,
        atr=signal.atr,
        reason=reason,
        order_requested=order_requested,
        order_placed=order_placed,
        dry_run=dry_run,
        has_open_position=has_open_position,
        forward_test=settings.forward_test_mode,
        position_units=position_units,
        stop_loss_price=stop_loss_price,
        order_id=order_id,
        broker_response=broker_response,
        error_message=error_message,
        indicators=signal.indicators,
    )


def apply_force_signal(signal: SignalDecision, force_signal: str | None) -> tuple[SignalDecision, str, bool]:
    generated_signal_action = signal.action

    if force_signal is None:
        return signal, generated_signal_action, False

    forced_signal = SignalDecision(
        action=force_signal,
        reason=f"{signal.reason} FORCED SIGNAL FOR TESTING ONLY.",
        price=signal.price,
        ema_fast=signal.ema_fast,
        ema_slow=signal.ema_slow,
        previous_ema_fast=signal.previous_ema_fast,
        previous_ema_slow=signal.previous_ema_slow,
        atr=signal.atr,
        candle_time=signal.candle_time,
        indicators=signal.indicators,
    )
    return forced_signal, generated_signal_action, True


def print_forward_test_banner(settings: Settings) -> None:
    if not settings.forward_test_mode:
        return

    print("")
    print("FORWARD TEST MODE ACTIVE")
    print("Forward-test cycles will be tagged in SQLite/log output.")
    if settings.dry_run:
        print("DRY_RUN=true, so no orders will be submitted.")
    else:
        print("WARNING: DRY_RUN=false was explicitly configured; existing order safety gates still apply.")
    print("")


def candidate_differences(settings: Settings) -> list[str]:
    differences: list[str] = []
    if settings.strategy_name != TESTED_CANDIDATE_STRATEGY:
        differences.append(f"STRATEGY is {settings.strategy_name}, expected {TESTED_CANDIDATE_STRATEGY}")
    if settings.instrument != TESTED_CANDIDATE_INSTRUMENT:
        differences.append(f"INSTRUMENT is {settings.instrument}, expected {TESTED_CANDIDATE_INSTRUMENT}")
    if settings.granularity != TESTED_CANDIDATE_TIMEFRAME:
        differences.append(f"GRANULARITY is {settings.granularity}, expected {TESTED_CANDIDATE_TIMEFRAME}")
    if settings.rsi_filter_trend_mode != TESTED_CANDIDATE_TREND_MODE:
        differences.append(
            f"RSI_FILTER_TREND_MODE is {settings.rsi_filter_trend_mode}, expected {TESTED_CANDIDATE_TREND_MODE}"
        )
    if settings.rsi_filter_atr_mode != TESTED_CANDIDATE_ATR_MODE:
        differences.append(f"RSI_FILTER_ATR_MODE is {settings.rsi_filter_atr_mode}, expected {TESTED_CANDIDATE_ATR_MODE}")
    if settings.rsi_filter_session != TESTED_CANDIDATE_SESSION:
        differences.append(f"RSI_FILTER_SESSION is {settings.rsi_filter_session}, expected {TESTED_CANDIDATE_SESSION}")
    return differences


def candidate_guard_warnings(settings: Settings) -> list[str]:
    if settings.strategy_name != TESTED_CANDIDATE_STRATEGY:
        return []

    differences = candidate_differences(settings)
    if not differences:
        return []
    return [
        "WARNING: Current rsi_reversion_filtered_v1 setup differs from the tested candidate: "
        + "; ".join(differences)
        + "."
    ]


def print_candidate_guard_warnings(settings: Settings) -> None:
    for warning in candidate_guard_warnings(settings):
        print("")
        print(warning)
        print("")
        LOGGER.warning(warning)


def collect_market_snapshot(
    client: OandaClient,
    settings: Settings,
    *,
    force_signal: str | None,
) -> MarketSnapshot:
    account_summary = client.get_account_summary()["account"]
    instrument = client.get_instrument_details(settings.instrument)
    strategy = get_strategy(settings.strategy_name)
    candle_count = max(settings.candle_count, strategy.min_candles(settings))
    candles = client.get_candles(settings.instrument, settings.granularity, candle_count)

    if not candles:
        raise RuntimeError("No completed candles were returned from OANDA.")

    signal = generate_signal_for_strategy(settings.strategy_name, candles, settings)
    signal, generated_signal_action, signal_was_forced = apply_force_signal(signal, force_signal)
    if signal_was_forced:
        LOGGER.warning(
            "Signal override active | instrument=%s | generated_action=%s | forced_action=%s",
            settings.instrument,
            generated_signal_action,
            signal.action,
        )
    position = client.get_open_position(settings.instrument)
    pricing = client.get_current_pricing(settings.instrument)
    spread_pips = calculate_spread_pips(pricing.bid, pricing.ask, instrument.pip_location)

    return MarketSnapshot(
        balance=account_summary.get("balance"),
        currency=account_summary.get("currency"),
        instrument_details=instrument,
        candle_time=signal.candle_time,
        candle_close=signal.price,
        signal=signal,
        generated_signal_action=generated_signal_action,
        signal_was_forced=signal_was_forced,
        atr=signal.atr,
        position=position,
        pricing=pricing,
        spread_pips=spread_pips,
    )


def print_force_signal_warning(snapshot: MarketSnapshot) -> None:
    if not snapshot.signal_was_forced:
        return

    print("")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("WARNING: FORCED SIGNAL OVERRIDE ACTIVE FOR TESTING ONLY")
    print(f"generated signal: {snapshot.generated_signal_action}")
    print(f"forced signal: {snapshot.signal.action}")
    print("ALL NORMAL SAFETY CHECKS STILL APPLY.")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("")


def print_signal_snapshot(settings: Settings, snapshot: MarketSnapshot) -> None:
    print_force_signal_warning(snapshot)
    print("")
    print("DRY RUN MARKET SNAPSHOT" if settings.dry_run else "MARKET SNAPSHOT")
    print(f"account balance: {snapshot.balance or 'n/a'} {snapshot.currency or ''}".strip())
    print(f"latest completed candle time: {snapshot.candle_time}")
    print(f"latest {settings.instrument} close: {snapshot.candle_close:.5f}")
    print(f"pricing status: {snapshot.pricing.status}")
    print(f"bid: {snapshot.pricing.bid:.5f}")
    print(f"ask: {snapshot.pricing.ask:.5f}")
    print(f"spread: {snapshot.spread_pips:.2f} pips")
    print_signal_indicators(settings, snapshot.signal)
    print(f"current signal: {snapshot.signal.action}")
    print(f"reason for signal: {snapshot.signal.reason}")
    print("")


def print_order_precheck(
    settings: Settings,
    snapshot: MarketSnapshot,
    *,
    confirm_order: bool,
    position_plan: PositionPlan | None,
    plan_error: str | None,
) -> None:
    if snapshot.signal.action not in {"BUY", "SELL"}:
        return

    print("ORDER PRECHECK")
    print(f"action: {snapshot.signal.action}")
    print(f"units: {position_plan.units if position_plan is not None else 'n/a'}")
    print(f"entry reference price: {snapshot.signal.price:.5f}")
    print(
        f"stop loss: {position_plan.stop_loss_price_str}"
        if position_plan is not None
        else "stop loss: unavailable"
    )
    print(f"spread: {snapshot.spread_pips:.2f} pips")
    print(f"dry_run: {settings.dry_run}")
    print(f"confirm-order: {confirm_order}")
    if plan_error is not None:
        print(f"plan status: failed ({plan_error})")
    else:
        print("plan status: ready")
    print("")


def summarize_blockers(blockers: list[str]) -> str:
    return "; ".join(blockers)


def evaluate_trade_blockers(
    settings: Settings,
    snapshot: MarketSnapshot,
    *,
    has_open_position: bool,
    is_entry_signal: bool,
    position_plan: PositionPlan | None,
    plan_error: str | None,
) -> list[str]:
    blockers: list[str] = []

    if not is_entry_signal:
        blockers.append(f"{settings.strategy_name} signal is not a new entry signal")
    if has_open_position:
        blockers.append(describe_position(snapshot.position))
    if snapshot.pricing.status.lower() != "tradeable":
        blockers.append(f"pricing status is {snapshot.pricing.status}")
    if snapshot.spread_pips > settings.max_spread_pips:
        blockers.append(
            f"spread {snapshot.spread_pips:.2f} pips is above MAX_SPREAD_PIPS {settings.max_spread_pips:.2f}"
        )
    if plan_error is not None:
        blockers.append(f"position plan calculation failed: {plan_error}")
    elif position_plan is None:
        blockers.append("position plan is unavailable")
    elif position_plan.units == 0:
        blockers.append("position units are zero")

    return blockers


def print_check_connection_result(
    *,
    success: bool,
    settings: Settings,
    reason: str | None = None,
    balance: str | None = None,
    currency: str | None = None,
    candle_time: str | None = None,
    candle_close: float | None = None,
) -> None:
    print("")
    print("CHECK CONNECTION")
    print(f"status: {'SUCCESS' if success else 'FAILURE'}")
    print(f"environment: {settings.oanda_env}")
    print(f"instrument: {settings.instrument}")
    print(f"timeframe: {settings.granularity}")

    if success:
        print(f"account balance: {balance or 'n/a'} {currency or ''}".strip())
        if candle_time is not None:
            print(f"latest completed candle time: {candle_time}")
        if candle_close is not None:
            print(f"latest completed candle close: {candle_close:.5f}")
    elif reason is not None:
        print(f"reason: {reason}")

    print("")


def print_recent_logs(logs: list[RecentDecisionLog]) -> None:
    print("recent sqlite decision logs:")
    if not logs:
        print("  none found")
        return

    for record in logs:
        print(
            "  "
            f"#{record.id} | {record.timestamp} | {record.action} | {record.instrument} {record.timeframe} "
            f"| price={record.price:.5f} | dry_run={record.dry_run} "
            f"| forward_test={record.forward_test} | requested={record.order_requested} | placed={record.order_placed}"
        )
        print(f"  reason: {record.reason}")
        print(f"  indicators: {format_recent_indicator_summary(record.indicators)}")
        if record.error_message:
            print(f"  error: {record.error_message}")


def print_candidate_summary(settings: Settings, snapshot: MarketSnapshot, *, title: str = "CANDIDATE SUMMARY") -> None:
    print("")
    print(title)
    print(f"forward_test_mode: {settings.forward_test_mode}")
    print(f"strategy: {settings.strategy_name}")
    print(f"instrument: {settings.instrument}")
    print(f"timeframe: {settings.granularity}")
    print("filter settings:")
    print(f"  RSI_FILTER_TREND_MODE: {settings.rsi_filter_trend_mode}")
    print(f"  RSI_FILTER_ATR_MODE: {settings.rsi_filter_atr_mode}")
    print(f"  RSI_FILTER_ATR_MEDIAN_PERIOD: {settings.rsi_filter_atr_median_period}")
    print(f"  RSI_FILTER_SESSION: {settings.rsi_filter_session}")
    print("latest signal:")
    print(f"  candle time: {snapshot.candle_time}")
    print(f"  price: {snapshot.signal.price:.5f}")
    print(f"  action: {snapshot.signal.action}")
    print(f"  reason: {snapshot.signal.reason}")
    print_signal_indicators(settings, snapshot.signal, indent="  ")
    print("open position:")
    print(f"  {describe_position(snapshot.position)}")

    differences = candidate_differences(settings)
    if differences:
        print("candidate alignment: differs from tested candidate")
        for difference in differences:
            print(f"  {difference}")
    else:
        print("candidate alignment: matches tested candidate")
    print("")


def print_status(settings: Settings, snapshot: MarketSnapshot, logs: list[RecentDecisionLog]) -> None:
    print_force_signal_warning(snapshot)
    print("")
    print("BOT STATUS")
    print("config:")
    print(f"  oanda_env: {settings.oanda_env}")
    print(f"  oanda_account_id: {mask_account_id(settings.oanda_account_id)}")
    print(f"  dry_run: {settings.dry_run}")
    print(f"  forward_test_mode: {settings.forward_test_mode}")
    print(f"  strategy: {settings.strategy_name}")
    print(f"  selected_compare_strategy: {settings.selected_compare_strategy}")
    print(f"  instrument: {settings.instrument}")
    print(f"  forex_instruments: {', '.join(settings.forex_instruments)}")
    print(f"  timeframes: {', '.join(settings.timeframes)}")
    print(f"  granularity: {settings.granularity}")
    print(f"  candle_count: {settings.candle_count}")
    print(f"  fixed_units: {settings.fixed_units}")
    print(f"  ema_fast_period: {settings.ema_fast_period}")
    print(f"  ema_slow_period: {settings.ema_slow_period}")
    print(f"  trend_filter_ema_period: {settings.trend_filter_ema_period}")
    print(f"  breakout_lookback: {settings.breakout_lookback}")
    print(f"  rsi_period: {settings.rsi_period}")
    print(f"  rsi_oversold: {settings.rsi_oversold}")
    print(f"  rsi_overbought: {settings.rsi_overbought}")
    print(f"  rsi_exit_level: {settings.rsi_exit_level}")
    print(f"  rsi_allow_shorts: {settings.rsi_allow_shorts}")
    print(f"  rsi_filter_trend_mode: {settings.rsi_filter_trend_mode}")
    print(f"  rsi_filter_atr_mode: {settings.rsi_filter_atr_mode}")
    print(f"  rsi_filter_atr_median_period: {settings.rsi_filter_atr_median_period}")
    print(f"  rsi_filter_session: {settings.rsi_filter_session}")
    print(f"  atr_period: {settings.atr_period}")
    print(f"  atr_multiplier: {settings.atr_multiplier}")
    print(f"  stop_loss_pips: {settings.stop_loss_pips}")
    print(f"  max_spread_pips: {settings.max_spread_pips}")
    print(f"  backtest_candle_count: {settings.backtest_candle_count}")
    print(f"  backtest_start_date: {settings.backtest_start_date or 'not set'}")
    print(f"  backtest_end_date: {settings.backtest_end_date or 'not set'}")
    print(f"  backtest_starting_equity: {settings.backtest_starting_equity}")
    print(f"  backtest_spread_pips: {settings.backtest_spread_pips}")
    print(f"  backtest_slippage_pips: {settings.backtest_slippage_pips}")
    print(f"  walk_forward_instrument: {settings.walk_forward_instrument}")
    print(f"  walk_forward_timeframe: {settings.walk_forward_timeframe}")
    print(f"  walk_forward_strategy: {settings.walk_forward_strategy}")
    print(f"  walk_forward_start_date: {settings.walk_forward_start_date}")
    print(f"  walk_forward_end_date: {settings.walk_forward_end_date}")
    print(f"  walk_forward_window_months: {settings.walk_forward_window_months}")
    print(f"  poll_interval_seconds: {settings.poll_interval_seconds}")
    print(f"  db_path: {settings.db_path}")
    print("")
    print("account:")
    print(f"  balance: {snapshot.balance or 'n/a'}")
    print(f"  currency: {snapshot.currency or 'n/a'}")
    print("")
    print("market:")
    print(f"  latest completed candle time: {snapshot.candle_time}")
    print(f"  latest completed candle close: {snapshot.candle_close:.5f}")
    print(f"  pricing time: {snapshot.pricing.time}")
    print(f"  pricing status: {snapshot.pricing.status}")
    print(f"  bid: {snapshot.pricing.bid:.5f}")
    print(f"  ask: {snapshot.pricing.ask:.5f}")
    print(f"  spread: {snapshot.spread_pips:.2f} pips")
    print_signal_indicators(settings, snapshot.signal, indent="  ")
    if snapshot.signal_was_forced:
        print(f"  generated signal before override: {snapshot.generated_signal_action}")
    print(f"  current signal: {snapshot.signal.action}")
    print(f"  reason: {snapshot.signal.reason}")
    print("")
    print("position:")
    print(f"  {describe_position(snapshot.position)}")
    print_candidate_summary(settings, snapshot)
    print_recent_logs(logs)
    print("")


def run_check_connection(client: OandaClient, settings: Settings) -> int:
    try:
        account_summary = client.get_account_summary()["account"]
        candles = client.get_candles(settings.instrument, settings.granularity, 1)
        if not candles:
            raise RuntimeError("No completed candles were returned from OANDA.")

        latest_candle = candles[-1]
        print_check_connection_result(
            success=True,
            settings=settings,
            balance=account_summary.get("balance"),
            currency=account_summary.get("currency"),
            candle_time=str(latest_candle["time"]),
            candle_close=float(latest_candle["close"]),
        )
        return 0
    except Exception as exc:
        print_check_connection_result(success=False, settings=settings, reason=str(exc))
        return 1


def run_status(
    client: OandaClient,
    settings: Settings,
    store: TradingLogStore,
    *,
    force_signal: str | None,
) -> int:
    snapshot = collect_market_snapshot(client, settings, force_signal=force_signal)
    recent_logs = store.get_recent_logs(limit=5)
    print_status(settings, snapshot, recent_logs)
    return 0


def run_candidate_status(client: OandaClient, settings: Settings) -> int:
    snapshot = collect_market_snapshot(client, settings, force_signal=None)
    print_candidate_summary(settings, snapshot, title="CANDIDATE STATUS")
    return 0


def build_candle_sample_warning(candle_count: int, granularity: str) -> str | None:
    estimated_days = (candle_count * granularity_to_seconds(granularity)) / 86400
    if estimated_days >= 30:
        return None
    return f"{candle_count} {granularity} candles is only about {estimated_days:.1f} days of candle intervals."


def run_backtest(client: OandaClient, settings: Settings) -> int:
    strategy = get_strategy(settings.strategy_name)
    print("")
    print("BACKTEST MODE")
    print("No OANDA orders will be placed.")
    print(f"strategy: {strategy.name}")
    print(f"instrument: {settings.instrument}")
    print(f"timeframe: {settings.granularity}")
    print(f"requested candles: {settings.backtest_candle_count}")
    print(f"OANDA max candles per request: {OANDA_CANDLE_MAX_COUNT}")
    if settings.backtest_start_date or settings.backtest_end_date:
        print(f"date range start: {settings.backtest_start_date or 'derived from candle count'}")
        print(f"date range end: {settings.backtest_end_date or 'now'}")
    print("")

    instrument = client.get_instrument_details(settings.instrument)
    candles = client.get_historical_candles(
        settings.instrument,
        settings.granularity,
        count=settings.backtest_candle_count,
        from_time=settings.backtest_start_date,
        to_time=settings.backtest_end_date,
    )
    if not candles:
        raise RuntimeError("No completed candles were returned for backtest.")

    sample_warning = build_candle_sample_warning(len(candles), settings.granularity)
    if sample_warning:
        print(f"WARNING: {sample_warning}")
        print("")

    engine = BacktestEngine(settings=settings, instrument=instrument, strategy_name=strategy.name)
    result = engine.run(candles)

    storage_dir = settings.db_path.parent
    trades_path = storage_dir / "backtest_trades.csv"
    report_path = storage_dir / "backtest_report.txt"

    save_trades_csv(result.trades, trades_path)
    report = save_summary_report(result.summary, report_path)

    print(report)
    print("")
    print(f"saved trades: {trades_path}")
    print(f"saved report: {report_path}")
    print("")
    return 0


def run_strategy_comparison(client: OandaClient, settings: Settings) -> int:
    print("")
    print("STRATEGY COMPARISON MODE")
    print("No OANDA orders will be placed.")
    print(f"instrument: {settings.instrument}")
    print(f"timeframe: {settings.granularity}")
    print(f"requested candles: {settings.backtest_candle_count}")
    print("")

    instrument = client.get_instrument_details(settings.instrument)
    candles = client.get_historical_candles(
        settings.instrument,
        settings.granularity,
        count=settings.backtest_candle_count,
        from_time=settings.backtest_start_date,
        to_time=settings.backtest_end_date,
    )
    if not candles:
        raise RuntimeError("No completed candles were returned for strategy comparison.")

    sample_warning = build_candle_sample_warning(len(candles), settings.granularity)
    if sample_warning:
        print(f"WARNING: {sample_warning}")
        print("")

    summaries = []
    for strategy in get_all_strategies():
        try:
            result = BacktestEngine(settings=settings, instrument=instrument, strategy_name=strategy.name).run(candles)
            summaries.append(result.summary)
        except Exception as exc:
            LOGGER.warning("Strategy comparison skipped %s: %s", strategy.name, exc)
            print(f"WARNING: skipped {strategy.name}: {exc}")

    if not summaries:
        raise RuntimeError("No strategies completed successfully.")

    table = format_strategy_comparison_table(summaries)
    print(table)

    concentrated = [summary for summary in summaries if summary.profit_concentration_warning]
    for summary in concentrated:
        print(f"WARNING: {summary.strategy_name}: {summary.profit_concentration_warning}")

    comparison_path = settings.db_path.parent / "strategy_comparison.csv"
    save_strategy_comparison_csv(summaries, comparison_path)
    print("")
    print(f"saved comparison: {comparison_path}")
    print("")
    return 0


def sort_summaries_by_total_r(summaries: list) -> list:
    return sorted(summaries, key=lambda summary: summary.total_r, reverse=True)


def run_instrument_comparison(client: OandaClient, settings: Settings) -> int:
    print("")
    print("INSTRUMENT STRATEGY COMPARISON MODE")
    print("No OANDA orders will be placed.")
    print(f"timeframe: {settings.granularity}")
    print(f"requested candles per instrument: {settings.backtest_candle_count}")
    print(f"instruments: {', '.join(settings.forex_instruments)}")
    print("sort: total R descending")
    print("")

    summaries = []
    for instrument_name in settings.forex_instruments:
        print(f"Fetching {instrument_name}...")
        try:
            instrument = client.get_instrument_details(instrument_name)
            candles = client.get_historical_candles(
                instrument_name,
                settings.granularity,
                count=settings.backtest_candle_count,
                from_time=settings.backtest_start_date,
                to_time=settings.backtest_end_date,
            )
            if not candles:
                raise RuntimeError(f"No completed candles were returned for {instrument_name}.")

            sample_warning = build_candle_sample_warning(len(candles), settings.granularity)
            if sample_warning:
                print(f"WARNING: {instrument_name}: {sample_warning}")

            for strategy in get_all_strategies():
                try:
                    result = BacktestEngine(
                        settings=settings,
                        instrument=instrument,
                        strategy_name=strategy.name,
                    ).run(candles)
                    summaries.append(result.summary)
                except Exception as exc:
                    LOGGER.warning(
                        "Instrument comparison skipped %s %s: %s",
                        instrument_name,
                        strategy.name,
                        exc,
                    )
                    print(f"WARNING: skipped {instrument_name} {strategy.name}: {exc}")
        except Exception as exc:
            LOGGER.exception("Instrument comparison skipped %s: %s", instrument_name, exc)
            print(f"WARNING: skipped {instrument_name}: {exc}")

    if not summaries:
        raise RuntimeError("No instrument/strategy combinations completed successfully.")

    sorted_summaries = sort_summaries_by_total_r(summaries)
    print("")
    print(format_instrument_strategy_comparison_table(sorted_summaries))

    concentrated = [summary for summary in sorted_summaries if summary.profit_concentration_warning]
    for summary in concentrated:
        print(f"WARNING: {summary.instrument} {summary.strategy_name}: {summary.profit_concentration_warning}")

    comparison_path = settings.db_path.parent / "instrument_strategy_comparison.csv"
    save_instrument_strategy_comparison_csv(sorted_summaries, comparison_path)
    print("")
    print(f"saved comparison: {comparison_path}")
    print("")
    return 0


def run_timeframe_comparison(client: OandaClient, settings: Settings) -> int:
    strategy = get_strategy(settings.selected_compare_strategy)

    print("")
    print("TIMEFRAME STRATEGY COMPARISON MODE")
    print("No OANDA orders will be placed.")
    print(f"strategy: {strategy.name}")
    print(f"requested candles per instrument/timeframe: {settings.backtest_candle_count}")
    print(f"instruments: {', '.join(settings.forex_instruments)}")
    print(f"timeframes: {', '.join(settings.timeframes)}")
    print("sort: total R descending")
    print("")

    summaries = []
    for timeframe in settings.timeframes:
        timeframe_settings = replace(settings, granularity=timeframe)
        for instrument_name in settings.forex_instruments:
            print(f"Fetching {instrument_name} {timeframe}...")
            try:
                instrument = client.get_instrument_details(instrument_name)
                candles = client.get_historical_candles(
                    instrument_name,
                    timeframe,
                    count=settings.backtest_candle_count,
                    from_time=settings.backtest_start_date,
                    to_time=settings.backtest_end_date,
                )
                if not candles:
                    raise RuntimeError(f"No completed candles were returned for {instrument_name} {timeframe}.")

                sample_warning = build_candle_sample_warning(len(candles), timeframe)
                if sample_warning:
                    print(f"WARNING: {instrument_name} {timeframe}: {sample_warning}")

                result = BacktestEngine(
                    settings=timeframe_settings,
                    instrument=instrument,
                    strategy_name=strategy.name,
                ).run(candles)
                summaries.append(result.summary)
            except Exception as exc:
                LOGGER.exception(
                    "Timeframe comparison skipped %s %s %s: %s",
                    instrument_name,
                    timeframe,
                    strategy.name,
                    exc,
                )
                print(f"WARNING: skipped {instrument_name} {timeframe} {strategy.name}: {exc}")

    if not summaries:
        raise RuntimeError("No instrument/timeframe combinations completed successfully.")

    sorted_summaries = sort_summaries_by_total_r(summaries)
    print("")
    print(format_instrument_strategy_comparison_table(sorted_summaries))

    concentrated = [summary for summary in sorted_summaries if summary.profit_concentration_warning]
    for summary in concentrated:
        print(f"WARNING: {summary.instrument} {summary.timeframe}: {summary.profit_concentration_warning}")

    comparison_path = settings.db_path.parent / "timeframe_strategy_comparison.csv"
    save_timeframe_strategy_comparison_csv(sorted_summaries, comparison_path)
    print("")
    print(f"saved comparison: {comparison_path}")
    print("")
    return 0


def run_walk_forward(client: OandaClient, settings: Settings) -> int:
    strategy = get_strategy(settings.walk_forward_strategy)
    start_date = parse_config_date(settings.walk_forward_start_date, "WALK_FORWARD_START_DATE")
    end_date = parse_config_date(settings.walk_forward_end_date, "WALK_FORWARD_END_DATE")
    windows = build_walk_forward_windows(start_date, end_date, settings.walk_forward_window_months)

    print("")
    print("WALK-FORWARD MODE")
    print("No OANDA orders will be placed.")
    print(f"instrument: {settings.walk_forward_instrument}")
    print(f"timeframe: {settings.walk_forward_timeframe}")
    print(f"strategy: {strategy.name}")
    print(f"date range: {start_date.isoformat()} to {end_date.isoformat()}")
    print(f"window months: {settings.walk_forward_window_months}")
    print(f"windows: {len(windows)}")
    print("")

    instrument = client.get_instrument_details(settings.walk_forward_instrument)
    window_settings = replace(
        settings,
        instrument=settings.walk_forward_instrument,
        granularity=settings.walk_forward_timeframe,
        strategy_name=strategy.name,
    )
    rows: list[dict[str, object]] = []
    summaries = []

    for window_start, window_end in windows:
        print(f"Running {window_start.isoformat()} to {window_end.isoformat()}...")
        try:
            candles = client.get_historical_candles(
                settings.walk_forward_instrument,
                settings.walk_forward_timeframe,
                from_time=window_start.isoformat(),
                to_time=window_end.isoformat(),
            )
            if not candles:
                raise RuntimeError("No completed candles were returned for this walk-forward window.")

            result = BacktestEngine(
                settings=window_settings,
                instrument=instrument,
                strategy_name=strategy.name,
            ).run(candles)
            summary = result.summary
            summaries.append(summary)
            rows.append(walk_forward_row(window_start, window_end, summary))
        except Exception as exc:
            LOGGER.exception(
                "Walk-forward window skipped %s to %s: %s",
                window_start.isoformat(),
                window_end.isoformat(),
                exc,
            )
            print(f"WARNING: skipped {window_start.isoformat()} to {window_end.isoformat()}: {exc}")

    if not rows:
        raise RuntimeError("No walk-forward windows completed successfully.")

    table = format_walk_forward_table(rows)
    summary_report = format_walk_forward_report(
        rows=rows,
        summaries=summaries,
        settings=settings,
        strategy_name=strategy.name,
    )
    report = f"{table}\n\n{summary_report}"

    csv_path = settings.db_path.parent / "walk_forward_report.csv"
    txt_path = settings.db_path.parent / "walk_forward_report.txt"
    save_walk_forward_csv(rows, csv_path)
    save_walk_forward_report(report, txt_path)

    print("")
    print(report)
    print("")
    print(f"saved csv: {csv_path}")
    print(f"saved report: {txt_path}")
    print("")
    return 0


def walk_forward_row(window_start: date, window_end: date, summary) -> dict[str, object]:
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "trades": summary.total_trades,
        "win_pct": summary.win_rate,
        "profit_factor": summary.profit_factor,
        "net_pips": summary.net_pips,
        "avg_pips_per_trade": summary.average_pips_per_trade,
        "total_r": summary.total_r,
        "max_drawdown_pct": summary.max_drawdown,
        "best5_pct": summary.percent_profits_from_best_5_trades,
    }


def parse_config_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD format.") from exc


def build_walk_forward_windows(start_date: date, end_date: date, window_months: int) -> list[tuple[date, date]]:
    if window_months <= 0:
        raise ValueError("WALK_FORWARD_WINDOW_MONTHS must be greater than zero.")
    if start_date > end_date:
        raise ValueError("WALK_FORWARD_START_DATE must be before or equal to WALK_FORWARD_END_DATE.")

    windows: list[tuple[date, date]] = []
    current_start = start_date
    while current_start <= end_date:
        next_start = add_months(current_start, window_months)
        window_end = min(next_start - timedelta(days=1), end_date)
        windows.append((current_start, window_end))
        current_start = window_end + timedelta(days=1)

    return windows


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(value.day, days_in_month(year, month))
    return date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def format_walk_forward_table(rows: list[dict[str, object]]) -> str:
    headers = [
        "window start",
        "window end",
        "trades",
        "win%",
        "pf",
        "net pips",
        "avg pips",
        "total R",
        "dd%",
        "best5%",
    ]
    values = []
    for row in rows:
        values.append(
            [
                str(row["window_start"]),
                str(row["window_end"]),
                str(row["trades"]),
                f"{float(row['win_pct']):.1f}",
                format_profit_factor(float(row["profit_factor"])),
                f"{float(row['net_pips']):.1f}",
                f"{float(row['avg_pips_per_trade']):.2f}",
                f"{float(row['total_r']):.2f}",
                f"{float(row['max_drawdown_pct']):.2f}",
                f"{float(row['best5_pct']):.1f}",
            ]
        )

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in values)) if values else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in values:
        lines.append(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def format_walk_forward_report(
    *,
    rows: list[dict[str, object]],
    summaries: list,
    settings: Settings,
    strategy_name: str,
) -> str:
    profit_factors = [float(row["profit_factor"]) for row in rows]
    total_r_values = [float(row["total_r"]) for row in rows]
    profitable_windows = sum(1 for row in rows if float(row["total_r"]) > 0)
    losing_windows = sum(1 for row in rows if float(row["total_r"]) < 0)
    average_profit_factor = sum(profit_factors) / len(profit_factors) if profit_factors else 0.0
    median_profit_factor = median(profit_factors) if profit_factors else 0.0
    total_r = sum(total_r_values)
    worst_row = min(rows, key=lambda row: float(row["total_r"]))
    best_row = max(rows, key=lambda row: float(row["total_r"]))

    return "\n".join(
        [
            "WALK-FORWARD SUMMARY",
            f"instrument: {settings.walk_forward_instrument}",
            f"timeframe: {settings.walk_forward_timeframe}",
            f"strategy: {strategy_name}",
            f"date range: {settings.walk_forward_start_date} to {settings.walk_forward_end_date}",
            f"window months: {settings.walk_forward_window_months}",
            f"completed windows: {len(rows)}",
            f"profitable windows: {profitable_windows}",
            f"losing windows: {losing_windows}",
            f"average profit factor: {format_profit_factor(average_profit_factor)}",
            f"median profit factor: {format_profit_factor(median_profit_factor)}",
            f"total R across all windows: {total_r:.2f}",
            (
                f"worst window: {worst_row['window_start']} to {worst_row['window_end']} "
                f"| total R {float(worst_row['total_r']):.2f}"
            ),
            (
                f"best window: {best_row['window_start']} to {best_row['window_end']} "
                f"| total R {float(best_row['total_r']):.2f}"
            ),
        ]
    )


def format_profit_factor(value: float) -> str:
    if math.isinf(value):
        return "inf"
    return f"{value:.2f}"


def run_cycle(
    client: OandaClient,
    settings: Settings,
    store: TradingLogStore,
    *,
    confirm_order: bool,
    force_signal: str | None,
) -> None:
    snapshot = collect_market_snapshot(client, settings, force_signal=force_signal)
    has_open_position = snapshot.position is not None and snapshot.position.net_units != 0

    LOGGER.info(
        "Account summary | balance=%s %s",
        snapshot.balance,
        snapshot.currency,
    )
    LOGGER.info(
        "Signal | instrument=%s | candle=%s | close=%.5f | indicators=%s | action=%s",
        settings.instrument,
        snapshot.candle_time,
        snapshot.candle_close,
        format_signal_indicator_summary(settings, snapshot.signal),
        snapshot.signal.action,
    )
    LOGGER.info(
        "Pricing | instrument=%s | pricing_time=%s | bid=%.5f | ask=%.5f | spread_pips=%.2f | max_spread_pips=%.2f",
        settings.instrument,
        snapshot.pricing.time,
        snapshot.pricing.bid,
        snapshot.pricing.ask,
        snapshot.spread_pips,
        settings.max_spread_pips,
    )

    print_signal_snapshot(settings, snapshot)

    if snapshot.signal.action == "HOLD":
        reason = f"{snapshot.signal.reason} No order requested because signal is HOLD."
        LOGGER.info("No order requested | %s", reason)
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=False,
            order_placed=False,
            has_open_position=has_open_position,
            dry_run=settings.dry_run,
        )
        log_cycle_result(store, record)
        return

    strategy = get_strategy(settings.strategy_name)
    is_entry_signal = strategy.is_long_entry(snapshot.signal, settings) or strategy.is_short_entry(
        snapshot.signal,
        settings,
    )
    position_plan: PositionPlan | None = None
    plan_error: str | None = None
    try:
        position_plan = build_position_plan(
            snapshot.signal,
            snapshot.instrument_details,
            fixed_units=settings.fixed_units,
            atr_multiplier=settings.atr_multiplier,
            fallback_stop_loss_pips=settings.stop_loss_pips,
        )
    except Exception as exc:
        plan_error = str(exc)

    print_order_precheck(
        settings,
        snapshot,
        confirm_order=confirm_order,
        position_plan=position_plan,
        plan_error=plan_error,
    )

    trade_blockers = evaluate_trade_blockers(
        settings,
        snapshot,
        has_open_position=has_open_position,
        is_entry_signal=is_entry_signal,
        position_plan=position_plan,
        plan_error=plan_error,
    )
    position_units = position_plan.units if position_plan is not None else None
    stop_loss_price = position_plan.stop_loss_price if position_plan is not None else None

    if settings.dry_run:
        if trade_blockers:
            reason = (
                f"{snapshot.signal.reason} DRY_RUN=true, so no order was submitted. "
                f"Safety result: would refuse order because {summarize_blockers(trade_blockers)}."
            )
        else:
            assert position_plan is not None
            reason = (
                f"{snapshot.signal.reason} DRY_RUN=true, so no order was submitted. "
                f"Would place {snapshot.signal.action} {position_plan.units} units with stop loss "
                f"{position_plan.stop_loss_price_str} if DRY_RUN were false and --confirm-order were provided."
            )

        LOGGER.info("DRY_RUN is enabled | no order will be submitted.")
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=False,
            order_placed=False,
            has_open_position=has_open_position,
            dry_run=True,
            position_units=position_units,
            stop_loss_price=stop_loss_price,
            error_message=plan_error,
        )
        log_cycle_result(store, record)
        return

    order_requested = True
    if not confirm_order:
        reason = (
            f"{snapshot.signal.reason} Refused order because DRY_RUN=false but --confirm-order was not provided."
        )
        if trade_blockers:
            reason = f"{reason} Additional safety blockers: {summarize_blockers(trade_blockers)}."

        LOGGER.info("Order confirmation missing | no order submitted.")
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=order_requested,
            order_placed=False,
            has_open_position=has_open_position,
            dry_run=False,
            position_units=position_units,
            stop_loss_price=stop_loss_price,
            error_message=plan_error,
        )
        log_cycle_result(store, record)
        return

    if trade_blockers:
        reason = f"{snapshot.signal.reason} Refused order because {summarize_blockers(trade_blockers)}."
        LOGGER.info("Order blocked by safety checks | %s", reason)
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=order_requested,
            order_placed=False,
            has_open_position=has_open_position,
            dry_run=False,
            position_units=position_units,
            stop_loss_price=stop_loss_price,
            error_message=plan_error,
        )
        log_cycle_result(store, record)
        return

    assert position_plan is not None
    LOGGER.info(
        "Submitting practice order | action=%s | units=%s | entry_reference=%.5f | stop_loss=%s | spread_pips=%.2f",
        snapshot.signal.action,
        position_plan.units,
        position_plan.entry_price,
        position_plan.stop_loss_price_str,
        snapshot.spread_pips,
    )

    try:
        response = client.create_market_order(
            settings.instrument,
            position_plan.units,
            position_plan.stop_loss_price_str,
        )
        order_id = extract_order_id(response)
        reason = (
            f"{snapshot.signal.reason} Submitted {snapshot.signal.action} order to OANDA practice with "
            f"{position_plan.units} units and stop loss {position_plan.stop_loss_price_str}."
        )
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=order_requested,
            order_placed=True,
            has_open_position=has_open_position,
            dry_run=False,
            position_units=position_units,
            stop_loss_price=stop_loss_price,
            order_id=order_id,
            broker_response=response,
        )
        log_cycle_result(store, record)
        return
    except Exception as exc:
        broker_response = exc.payload if isinstance(exc, OandaApiError) else None
        reason = f"{snapshot.signal.reason} Order submission failed: {exc}"
        LOGGER.error("Practice order failed: %s", exc)
        record = create_decision_log(
            settings=settings,
            signal=snapshot.signal,
            reason=reason,
            order_requested=order_requested,
            order_placed=False,
            has_open_position=has_open_position,
            dry_run=False,
            position_units=position_units,
            stop_loss_price=stop_loss_price,
            broker_response=broker_response,
            error_message=str(exc),
        )
        log_cycle_result(store, record)
        return


def run_cycle_with_error_handling(
    client: OandaClient,
    settings: Settings,
    store: TradingLogStore,
    *,
    confirm_order: bool,
    force_signal: str | None,
    raise_on_error: bool,
) -> bool:
    try:
        run_cycle(client, settings, store, confirm_order=confirm_order, force_signal=force_signal)
        return True
    except Exception as exc:
        LOGGER.exception("Cycle failed: %s", exc)
        error_record = DecisionLog(
            timestamp=utc_now(),
            candle_time=None,
            instrument=settings.instrument,
            timeframe=settings.granularity,
            strategy_name=settings.strategy_name,
            action="ERROR",
            price=0.0,
            ema_fast=None,
            ema_slow=None,
            atr=None,
            reason="Bot cycle failed before completion.",
            order_requested=False,
            order_placed=False,
            dry_run=settings.dry_run,
            has_open_position=False,
            error_message=str(exc),
            forward_test=settings.forward_test_mode,
        )
        store.log(error_record)
        if raise_on_error:
            raise
        return False


def seconds_until_next_run(interval_seconds: int, buffer_seconds: int = 2) -> int:
    current_time = time.time()
    next_slot = (math.floor(current_time / interval_seconds) + 1) * interval_seconds
    wait_seconds = int(max(1, next_slot - current_time + buffer_seconds))
    return wait_seconds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the OANDA paper trading bot.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run a single read-only signal cycle and exit.",
    )
    mode.add_argument(
        "--check-connection",
        action="store_true",
        help="Check the OANDA practice connection and latest completed candle without logging a trade decision.",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print the current bot status, account snapshot, and recent SQLite logs.",
    )
    mode.add_argument(
        "--candidate-status",
        action="store_true",
        help="Print only the selected candidate setup and latest strategy signal.",
    )
    mode.add_argument(
        "--backtest",
        action="store_true",
        help="Run a historical backtest for the selected STRATEGY and write CSV/report outputs.",
    )
    mode.add_argument(
        "--backtest-only",
        action="store_true",
        help="Alias for --backtest. This mode never interacts with order placement logic.",
    )
    mode.add_argument(
        "--compare-strategies",
        action="store_true",
        help="Backtest all registered strategies over the same candles and save a comparison CSV.",
    )
    mode.add_argument(
        "--compare-instruments",
        action="store_true",
        help="Backtest all registered strategies across FOREX_INSTRUMENTS and save a comparison CSV.",
    )
    mode.add_argument(
        "--compare-timeframes",
        action="store_true",
        help="Backtest SELECTED_COMPARE_STRATEGY across FOREX_INSTRUMENTS and TIMEFRAMES.",
    )
    mode.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run walk-forward windows for WALK_FORWARD_STRATEGY on the configured instrument/timeframe.",
    )
    parser.add_argument(
        "--confirm-order",
        action="store_true",
        help="Required together with DRY_RUN=false before the bot may place a paper order.",
    )
    parser.add_argument(
        "--force-signal",
        choices=("BUY", "SELL", "HOLD"),
        help="Force the signal action for testing only. All normal safety checks still apply.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Startup failure: {exc}")
        return 1

    configure_logging(settings)

    LOGGER.info(
        "Starting bot | instrument=%s | timeframe=%s | strategy=%s | dry_run=%s | forward_test=%s | env=%s",
        settings.instrument,
        settings.granularity,
        settings.strategy_name,
        settings.dry_run,
        settings.forward_test_mode,
        settings.oanda_env,
    )

    print_forward_test_banner(settings)
    print_candidate_guard_warnings(settings)

    if settings.forward_test_mode and args.force_signal is not None:
        print("ERROR: --force-signal is not allowed while FORWARD_TEST_MODE=true.")
        LOGGER.error("Blocked --force-signal because FORWARD_TEST_MODE=true.")
        return 1

    try:
        with OandaClient(
            api_key=settings.oanda_api_key,
            account_id=settings.oanda_account_id,
            base_url=settings.oanda_rest_url,
        ) as client:
            if args.check_connection:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --check-connection.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --check-connection because no strategy signal is generated.")
                return run_check_connection(client, settings)

            if args.status:
                store = TradingLogStore(settings.db_path)
                store.initialize()
                return run_status(client, settings, store, force_signal=args.force_signal)

            if args.candidate_status:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --candidate-status.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --candidate-status.")
                return run_candidate_status(client, settings)

            if args.backtest or args.backtest_only:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --backtest/--backtest-only.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --backtest because backtests use strategy signals only.")
                if args.confirm_order:
                    LOGGER.warning("Ignoring --confirm-order for --backtest because backtests never place orders.")
                return run_backtest(client, settings)

            if args.compare_strategies:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --compare-strategies.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --compare-strategies.")
                if args.confirm_order:
                    LOGGER.warning("Ignoring --confirm-order for --compare-strategies because comparisons never place orders.")
                return run_strategy_comparison(client, settings)

            if args.compare_instruments:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --compare-instruments.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --compare-instruments.")
                if args.confirm_order:
                    LOGGER.warning(
                        "Ignoring --confirm-order for --compare-instruments because comparisons never place orders."
                    )
                return run_instrument_comparison(client, settings)

            if args.compare_timeframes:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --compare-timeframes.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --compare-timeframes.")
                if args.confirm_order:
                    LOGGER.warning(
                        "Ignoring --confirm-order for --compare-timeframes because comparisons never place orders."
                    )
                return run_timeframe_comparison(client, settings)

            if args.walk_forward:
                if args.force_signal is not None:
                    print("")
                    print("WARNING: --force-signal is ignored with --walk-forward.")
                    print("")
                    LOGGER.warning("Ignoring --force-signal for --walk-forward.")
                if args.confirm_order:
                    LOGGER.warning("Ignoring --confirm-order for --walk-forward because it never places orders.")
                return run_walk_forward(client, settings)

            store = TradingLogStore(settings.db_path)
            store.initialize()

            if args.once:
                run_cycle_with_error_handling(
                    client,
                    settings,
                    store,
                    confirm_order=args.confirm_order,
                    force_signal=args.force_signal,
                    raise_on_error=True,
                )
                return 0

            while True:
                run_cycle_with_error_handling(
                    client,
                    settings,
                    store,
                    confirm_order=args.confirm_order,
                    force_signal=args.force_signal,
                    raise_on_error=False,
                )
                wait_seconds = seconds_until_next_run(settings.poll_interval_seconds)
                LOGGER.info("Sleeping %s seconds until the next cycle.", wait_seconds)
                time.sleep(wait_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped by user.")
        return 0
    except (OandaApiError, ValueError, RuntimeError) as exc:
        LOGGER.error("Bot stopped: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.error("Bot stopped unexpectedly: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
