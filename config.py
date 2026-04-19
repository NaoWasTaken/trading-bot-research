from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FOREX_INSTRUMENTS = "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CAD,NZD_USD,EUR_JPY"
DEFAULT_TIMEFRAMES = "M15,M30,H1,H4"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None, default: str) -> list[str]:
    source = value if value is not None else default
    items = [item.strip().upper() for item in source.split(",")]
    return [item for item in items if item]


def _validate_choice(name: str, value: str, choices: set[str]) -> None:
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {allowed}")


@dataclass(frozen=True)
class Settings:
    oanda_api_key: str
    oanda_account_id: str
    oanda_env: str
    dry_run: bool
    forward_test_mode: bool
    strategy_name: str
    selected_compare_strategy: str
    instrument: str
    forex_instruments: list[str]
    timeframes: list[str]
    granularity: str
    candle_count: int
    fixed_units: int
    ema_fast_period: int
    ema_slow_period: int
    trend_filter_ema_period: int
    breakout_lookback: int
    rsi_period: int
    rsi_oversold: float
    rsi_overbought: float
    rsi_exit_level: float
    rsi_allow_shorts: bool
    rsi_filter_trend_mode: str
    rsi_filter_atr_mode: str
    rsi_filter_atr_median_period: int
    rsi_filter_session: str
    atr_period: int
    atr_multiplier: float
    stop_loss_pips: float
    max_spread_pips: float
    backtest_candle_count: int
    backtest_start_date: str | None
    backtest_end_date: str | None
    backtest_starting_equity: float
    backtest_spread_pips: float
    backtest_slippage_pips: float
    walk_forward_instrument: str
    walk_forward_timeframe: str
    walk_forward_strategy: str
    walk_forward_start_date: str
    walk_forward_end_date: str
    walk_forward_window_months: int
    poll_interval_seconds: int
    db_path: Path
    log_dir: Path

    @property
    def oanda_rest_url(self) -> str:
        if self.oanda_env == "practice":
            return "https://api-fxpractice.oanda.com"
        raise ValueError("This bot only supports OANDA practice accounts for now.")


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    oanda_api_key = os.getenv("OANDA_API_KEY", "").strip()
    oanda_account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()
    oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()

    if not oanda_api_key:
        raise ValueError("Missing OANDA_API_KEY in .env")
    if not oanda_account_id:
        raise ValueError("Missing OANDA_ACCOUNT_ID in .env")
    if oanda_env != "practice":
        raise ValueError("Only OANDA_ENV=practice is supported in this version.")

    forward_test_mode = _parse_bool(os.getenv("FORWARD_TEST_MODE"), default=False)
    dry_run = _parse_bool(os.getenv("DRY_RUN"), default=True)

    settings = Settings(
        oanda_api_key=oanda_api_key,
        oanda_account_id=oanda_account_id,
        oanda_env=oanda_env,
        dry_run=dry_run,
        forward_test_mode=forward_test_mode,
        strategy_name=(os.getenv("STRATEGY", "ema_cross_v1").strip() or "ema_cross_v1").lower(),
        selected_compare_strategy=(
            os.getenv("SELECTED_COMPARE_STRATEGY", "rsi_reversion_v1").strip() or "rsi_reversion_v1"
        ).lower(),
        instrument=os.getenv("INSTRUMENT", "EUR_USD").strip().upper(),
        forex_instruments=_parse_csv(os.getenv("FOREX_INSTRUMENTS"), DEFAULT_FOREX_INSTRUMENTS),
        timeframes=_parse_csv(os.getenv("TIMEFRAMES"), DEFAULT_TIMEFRAMES),
        granularity=os.getenv("GRANULARITY", "M5").strip().upper(),
        candle_count=int(os.getenv("CANDLE_COUNT", "200")),
        fixed_units=int(os.getenv("FIXED_UNITS", "100")),
        ema_fast_period=20,
        ema_slow_period=50,
        trend_filter_ema_period=int(os.getenv("TREND_FILTER_EMA_PERIOD", "200")),
        breakout_lookback=int(os.getenv("BREAKOUT_LOOKBACK", "50")),
        rsi_period=int(os.getenv("RSI_PERIOD", "14")),
        rsi_oversold=float(os.getenv("RSI_OVERSOLD", "30")),
        rsi_overbought=float(os.getenv("RSI_OVERBOUGHT", "70")),
        rsi_exit_level=float(os.getenv("RSI_EXIT_LEVEL", "50")),
        rsi_allow_shorts=_parse_bool(os.getenv("RSI_ALLOW_SHORTS"), default=False),
        rsi_filter_trend_mode=os.getenv("RSI_FILTER_TREND_MODE", "off").strip().lower(),
        rsi_filter_atr_mode=os.getenv("RSI_FILTER_ATR_MODE", "off").strip().lower(),
        rsi_filter_atr_median_period=int(os.getenv("RSI_FILTER_ATR_MEDIAN_PERIOD", "100")),
        rsi_filter_session=os.getenv("RSI_FILTER_SESSION", "off").strip().lower(),
        atr_period=int(os.getenv("ATR_PERIOD", "14")),
        atr_multiplier=float(os.getenv("ATR_MULTIPLIER", "1.5")),
        stop_loss_pips=float(os.getenv("STOP_LOSS_PIPS", "10")),
        max_spread_pips=float(os.getenv("MAX_SPREAD_PIPS", "2.0")),
        backtest_candle_count=int(os.getenv("BACKTEST_CANDLE_COUNT", "1000")),
        backtest_start_date=os.getenv("BACKTEST_START_DATE") or None,
        backtest_end_date=os.getenv("BACKTEST_END_DATE") or None,
        backtest_starting_equity=float(os.getenv("BACKTEST_STARTING_EQUITY", "10000")),
        backtest_spread_pips=float(os.getenv("BACKTEST_SPREAD_PIPS", "1.5")),
        backtest_slippage_pips=float(os.getenv("BACKTEST_SLIPPAGE_PIPS", "0.2")),
        walk_forward_instrument=os.getenv("WALK_FORWARD_INSTRUMENT", "GBP_USD").strip().upper(),
        walk_forward_timeframe=os.getenv("WALK_FORWARD_TIMEFRAME", "H4").strip().upper(),
        walk_forward_strategy=(
            os.getenv("WALK_FORWARD_STRATEGY", "rsi_reversion_v1").strip() or "rsi_reversion_v1"
        ).lower(),
        walk_forward_start_date=os.getenv("WALK_FORWARD_START_DATE", "2020-01-01").strip(),
        walk_forward_end_date=os.getenv("WALK_FORWARD_END_DATE", "2026-04-17").strip(),
        walk_forward_window_months=int(os.getenv("WALK_FORWARD_WINDOW_MONTHS", "6")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
        db_path=BASE_DIR / "storage" / "trading_bot.sqlite3",
        log_dir=BASE_DIR / "logs",
    )

    _validate_choice("RSI_FILTER_TREND_MODE", settings.rsi_filter_trend_mode, {"off", "with_trend", "against_trend"})
    _validate_choice("RSI_FILTER_ATR_MODE", settings.rsi_filter_atr_mode, {"off", "below_median", "above_median"})
    _validate_choice("RSI_FILTER_SESSION", settings.rsi_filter_session, {"off", "london", "new_york", "london_new_york"})
    if settings.rsi_filter_atr_median_period <= 0:
        raise ValueError("RSI_FILTER_ATR_MEDIAN_PERIOD must be greater than zero.")

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
