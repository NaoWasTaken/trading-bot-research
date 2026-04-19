from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SignalAction = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class SignalDecision:
    action: SignalAction
    reason: str
    price: float
    ema_fast: float
    ema_slow: float
    previous_ema_fast: float
    previous_ema_slow: float
    atr: float | None
    candle_time: str
    indicators: dict[str, Any] = field(default_factory=dict)


def _ema_series(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("EMA period must be positive.")
    if len(values) < period:
        raise ValueError(f"Need at least {period} prices to calculate EMA.")

    series: list[float | None] = [None] * (period - 1)
    ema = sum(values[:period]) / period
    series.append(ema)

    multiplier = 2 / (period + 1)
    for price in values[period:]:
        ema = ((price - ema) * multiplier) + ema
        series.append(ema)

    return series


def calculate_atr(candles: list[dict[str, float | str | int]], period: int) -> float | None:
    if period <= 0:
        raise ValueError("ATR period must be positive.")
    if len(candles) < period + 1:
        return None

    true_ranges: list[float] = []
    previous_close = float(candles[0]["close"])

    for candle in candles[1:]:
        high = float(candle["high"])
        low = float(candle["low"])
        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = float(candle["close"])

    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = ((atr * (period - 1)) + value) / period

    return atr


def generate_signal(
    candles: list[dict[str, float | str | int]],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    atr_period: int = 14,
) -> SignalDecision:
    required_candles = max(fast_period, slow_period) + 2
    if len(candles) < required_candles:
        raise ValueError(f"Need at least {required_candles} completed candles to generate a signal.")

    closes = [float(candle["close"]) for candle in candles]
    fast_ema_series = _ema_series(closes, fast_period)
    slow_ema_series = _ema_series(closes, slow_period)

    previous_fast = fast_ema_series[-2]
    previous_slow = slow_ema_series[-2]
    current_fast = fast_ema_series[-1]
    current_slow = slow_ema_series[-1]

    if previous_fast is None or previous_slow is None or current_fast is None or current_slow is None:
        raise ValueError("EMA series does not contain enough completed values.")

    if previous_fast <= previous_slow and current_fast > current_slow:
        action: SignalAction = "BUY"
        reason = f"EMA {fast_period} crossed above EMA {slow_period} on the latest completed candle."
    elif previous_fast >= previous_slow and current_fast < current_slow:
        action = "SELL"
        reason = f"EMA {fast_period} crossed below EMA {slow_period} on the latest completed candle."
    else:
        action = "HOLD"
        reason = "No EMA crossover on the latest completed candle."

    atr = calculate_atr(candles, atr_period)

    return SignalDecision(
        action=action,
        reason=reason,
        price=closes[-1],
        ema_fast=current_fast,
        ema_slow=current_slow,
        previous_ema_fast=previous_fast,
        previous_ema_slow=previous_slow,
        atr=atr,
        candle_time=str(candles[-1]["time"]),
        indicators={
            "ema_fast_period": fast_period,
            "ema_slow_period": slow_period,
            f"ema{fast_period}": current_fast,
            f"ema{slow_period}": current_slow,
            f"previous_ema{fast_period}": previous_fast,
            f"previous_ema{slow_period}": previous_slow,
            "atr_period": atr_period,
            "atr": atr,
        },
    )
