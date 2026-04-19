from __future__ import annotations

from typing import Any

from .ema_cross import SignalDecision, _ema_series, generate_signal as generate_ema_cross_signal


def generate_signal(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    trend_period = settings.trend_filter_ema_period
    if len(candles) < trend_period:
        raise ValueError(f"Need at least {trend_period} completed candles for ema_trend_filter_v1.")

    ema_cross_signal = generate_ema_cross_signal(
        candles,
        fast_period=settings.ema_fast_period,
        slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
    )
    closes = [float(candle["close"]) for candle in candles]
    ema200 = _ema_series(closes, trend_period)[-1]
    if ema200 is None:
        raise ValueError("EMA200 is unavailable for ema_trend_filter_v1.")

    close = closes[-1]
    if ema_cross_signal.action == "BUY" and close > ema200:
        action = "BUY"
        reason = f"{ema_cross_signal.reason} Close {close:.5f} is above EMA{trend_period} {ema200:.5f}."
    elif ema_cross_signal.action == "SELL" and close < ema200:
        action = "SELL"
        reason = f"{ema_cross_signal.reason} Close {close:.5f} is below EMA{trend_period} {ema200:.5f}."
    elif ema_cross_signal.action == "BUY":
        action = "HOLD"
        reason = (
            f"EMA crossover BUY filtered out because close {close:.5f} is not above "
            f"EMA{trend_period} {ema200:.5f}."
        )
    elif ema_cross_signal.action == "SELL":
        action = "HOLD"
        reason = (
            f"EMA crossover SELL filtered out because close {close:.5f} is not below "
            f"EMA{trend_period} {ema200:.5f}."
        )
    else:
        action = "HOLD"
        reason = f"No EMA crossover signal. Close {close:.5f}, EMA{trend_period} {ema200:.5f}."

    return SignalDecision(
        action=action,
        reason=reason,
        price=ema_cross_signal.price,
        ema_fast=ema_cross_signal.ema_fast,
        ema_slow=ema_cross_signal.ema_slow,
        previous_ema_fast=ema_cross_signal.previous_ema_fast,
        previous_ema_slow=ema_cross_signal.previous_ema_slow,
        atr=ema_cross_signal.atr,
        candle_time=ema_cross_signal.candle_time,
        indicators={
            **ema_cross_signal.indicators,
            f"ema{trend_period}": ema200,
            "trend_filter_mode": "ema_trend_filter",
        },
    )
