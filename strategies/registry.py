from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .breakout import generate_signal as generate_breakout_signal
from .ema_cross import SignalDecision, generate_signal as generate_ema_cross_signal
from .ema_trend_filter import generate_signal as generate_ema_trend_filter_signal
from .rsi_reversion import generate_signal as generate_rsi_reversion_signal
from .rsi_reversion_filtered import RSI_FILTER_TREND_PERIOD, generate_signal as generate_rsi_reversion_filtered_signal
from .trend_filter import generate_signal as generate_trend_filter_signal

SignalGenerator = Callable[[list[dict[str, Any]], Any], SignalDecision]
MinCandlesGetter = Callable[[Any], int]
AllowShortGetter = Callable[[Any], bool]
SignalPredicate = Callable[[SignalDecision, Any], bool]


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    generate_signal: SignalGenerator
    min_candles: MinCandlesGetter
    allow_short_entries: AllowShortGetter
    is_long_entry: SignalPredicate
    is_short_entry: SignalPredicate
    is_long_exit: SignalPredicate
    is_short_exit: SignalPredicate


def _ema_cross_min_candles(settings: Any) -> int:
    return max(settings.ema_fast_period, settings.ema_slow_period) + 2


def _trend_filter_min_candles(settings: Any) -> int:
    return settings.trend_filter_ema_period


def _breakout_min_candles(settings: Any) -> int:
    return settings.breakout_lookback + 1


def _rsi_min_candles(settings: Any) -> int:
    return settings.rsi_period + 1


def _rsi_filtered_min_candles(settings: Any) -> int:
    required = _rsi_min_candles(settings)
    if settings.rsi_filter_trend_mode != "off":
        required = max(required, RSI_FILTER_TREND_PERIOD)
    if settings.rsi_filter_atr_mode != "off":
        required = max(required, settings.atr_period + settings.rsi_filter_atr_median_period)
    return required


def _ema_trend_filter_min_candles(settings: Any) -> int:
    return max(settings.trend_filter_ema_period, settings.ema_fast_period, settings.ema_slow_period) + 2


def _always_allow_short(_settings: Any) -> bool:
    return True


def _never_allow_short(_settings: Any) -> bool:
    return False


def _rsi_allow_short(settings: Any) -> bool:
    return bool(settings.rsi_allow_shorts)


def _generate_ema_cross(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    return generate_ema_cross_signal(
        candles,
        fast_period=settings.ema_fast_period,
        slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
    )


def _default_long_entry(signal: SignalDecision, _settings: Any) -> bool:
    return signal.action == "BUY"


def _default_short_entry(signal: SignalDecision, settings: Any) -> bool:
    return signal.action == "SELL" and _always_allow_short(settings)


def _default_long_exit(signal: SignalDecision, _settings: Any) -> bool:
    return signal.action == "SELL"


def _default_short_exit(signal: SignalDecision, _settings: Any) -> bool:
    return signal.action == "BUY"


def _never_entry(_signal: SignalDecision, _settings: Any) -> bool:
    return False


def _rsi_long_entry(signal: SignalDecision, _settings: Any) -> bool:
    return signal.action == "BUY" and "oversold threshold" in signal.reason


def _rsi_short_entry(signal: SignalDecision, settings: Any) -> bool:
    return signal.action == "SELL" and bool(settings.rsi_allow_shorts) and "overbought threshold" in signal.reason


STRATEGIES: dict[str, StrategyDefinition] = {
    "ema_cross_v1": StrategyDefinition(
        name="ema_cross_v1",
        generate_signal=_generate_ema_cross,
        min_candles=_ema_cross_min_candles,
        allow_short_entries=_always_allow_short,
        is_long_entry=_default_long_entry,
        is_short_entry=_default_short_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_default_short_exit,
    ),
    "trend_filter_v1": StrategyDefinition(
        name="trend_filter_v1",
        generate_signal=generate_trend_filter_signal,
        min_candles=_trend_filter_min_candles,
        allow_short_entries=_never_allow_short,
        is_long_entry=_default_long_entry,
        is_short_entry=_never_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_never_entry,
    ),
    "breakout_v1": StrategyDefinition(
        name="breakout_v1",
        generate_signal=generate_breakout_signal,
        min_candles=_breakout_min_candles,
        allow_short_entries=_always_allow_short,
        is_long_entry=_default_long_entry,
        is_short_entry=_default_short_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_default_short_exit,
    ),
    "rsi_reversion_v1": StrategyDefinition(
        name="rsi_reversion_v1",
        generate_signal=generate_rsi_reversion_signal,
        min_candles=_rsi_min_candles,
        allow_short_entries=_rsi_allow_short,
        is_long_entry=_rsi_long_entry,
        is_short_entry=_rsi_short_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_default_short_exit,
    ),
    "rsi_reversion_filtered_v1": StrategyDefinition(
        name="rsi_reversion_filtered_v1",
        generate_signal=generate_rsi_reversion_filtered_signal,
        min_candles=_rsi_filtered_min_candles,
        allow_short_entries=_rsi_allow_short,
        is_long_entry=_rsi_long_entry,
        is_short_entry=_rsi_short_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_default_short_exit,
    ),
    "ema_trend_filter_v1": StrategyDefinition(
        name="ema_trend_filter_v1",
        generate_signal=generate_ema_trend_filter_signal,
        min_candles=_ema_trend_filter_min_candles,
        allow_short_entries=_always_allow_short,
        is_long_entry=_default_long_entry,
        is_short_entry=_default_short_entry,
        is_long_exit=_default_long_exit,
        is_short_exit=_default_short_exit,
    ),
}


def get_strategy(name: str) -> StrategyDefinition:
    try:
        return STRATEGIES[name]
    except KeyError as exc:
        available = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"Unknown strategy '{name}'. Available strategies: {available}") from exc


def get_all_strategies() -> list[StrategyDefinition]:
    return [STRATEGIES[name] for name in sorted(STRATEGIES)]


def generate_signal_for_strategy(
    strategy_name: str,
    candles: list[dict[str, Any]],
    settings: Any,
) -> SignalDecision:
    strategy = get_strategy(strategy_name)
    return strategy.generate_signal(candles, settings)
