from __future__ import annotations

from typing import Any

from .ema_cross import SignalDecision, _ema_series, calculate_atr


def generate_signal(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    period = settings.trend_filter_ema_period
    if len(candles) < period:
        raise ValueError(f"Need at least {period} completed candles for trend_filter_v1.")

    closes = [float(candle["close"]) for candle in candles]
    ema_series = _ema_series(closes, period)
    ema_value = ema_series[-1]
    if ema_value is None:
        raise ValueError("EMA200 is unavailable for trend_filter_v1.")

    close = closes[-1]
    if close > ema_value:
        action = "BUY"
        reason = f"Close {close:.5f} is above EMA{period} {ema_value:.5f}; long-only trend filter is bullish."
    elif close < ema_value:
        action = "SELL"
        reason = f"Close {close:.5f} is below EMA{period} {ema_value:.5f}; long-only trend filter exit signal."
    else:
        action = "HOLD"
        reason = f"Close {close:.5f} equals EMA{period} {ema_value:.5f}."

    atr = calculate_atr(candles, settings.atr_period)

    return SignalDecision(
        action=action,
        reason=reason,
        price=close,
        ema_fast=ema_value,
        ema_slow=ema_value,
        previous_ema_fast=ema_value,
        previous_ema_slow=ema_value,
        atr=atr,
        candle_time=str(candles[-1]["time"]),
        indicators={
            "trend_ema_period": period,
            f"ema{period}": ema_value,
            "atr_period": settings.atr_period,
            "atr": atr,
        },
    )
