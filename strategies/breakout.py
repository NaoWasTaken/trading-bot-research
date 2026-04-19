from __future__ import annotations

from typing import Any

from .ema_cross import SignalDecision, calculate_atr


def generate_signal(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    lookback = settings.breakout_lookback
    required = lookback + 1
    if len(candles) < required:
        raise ValueError(f"Need at least {required} completed candles for breakout_v1.")

    current = candles[-1]
    previous = candles[-(lookback + 1) : -1]
    highest_high = max(float(candle["high"]) for candle in previous)
    lowest_low = min(float(candle["low"]) for candle in previous)
    close = float(current["close"])

    if close > highest_high:
        action = "BUY"
        reason = f"Close {close:.5f} broke above previous {lookback}-candle high {highest_high:.5f}."
    elif close < lowest_low:
        action = "SELL"
        reason = f"Close {close:.5f} broke below previous {lookback}-candle low {lowest_low:.5f}."
    else:
        action = "HOLD"
        reason = (
            f"Close {close:.5f} remains inside previous {lookback}-candle range "
            f"{lowest_low:.5f}-{highest_high:.5f}."
        )

    atr = calculate_atr(candles, settings.atr_period)

    return SignalDecision(
        action=action,
        reason=reason,
        price=close,
        ema_fast=close,
        ema_slow=close,
        previous_ema_fast=close,
        previous_ema_slow=close,
        atr=atr,
        candle_time=str(current["time"]),
        indicators={
            "breakout_lookback": lookback,
            "previous_high": highest_high,
            "previous_low": lowest_low,
            "atr_period": settings.atr_period,
            "atr": atr,
        },
    )
