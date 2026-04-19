from __future__ import annotations

from typing import Any

from .ema_cross import SignalDecision, calculate_atr


def calculate_rsi(closes: list[float], period: int) -> float:
    if period <= 0:
        raise ValueError("RSI period must be positive.")
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes to calculate RSI.")

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def generate_signal(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    period = settings.rsi_period
    if len(candles) < period + 1:
        raise ValueError(f"Need at least {period + 1} completed candles for rsi_reversion_v1.")

    closes = [float(candle["close"]) for candle in candles]
    close = closes[-1]
    rsi = calculate_rsi(closes, period)

    if rsi < settings.rsi_oversold:
        action = "BUY"
        reason = f"RSI{period} {rsi:.2f} is below oversold threshold {settings.rsi_oversold:.2f}."
    elif rsi > settings.rsi_overbought and settings.rsi_allow_shorts:
        action = "SELL"
        reason = f"RSI{period} {rsi:.2f} is above overbought threshold {settings.rsi_overbought:.2f}; short enabled."
    elif rsi > settings.rsi_exit_level:
        action = "SELL"
        reason = f"RSI{period} {rsi:.2f} is above exit threshold {settings.rsi_exit_level:.2f}; long exit signal."
    elif rsi < settings.rsi_exit_level and settings.rsi_allow_shorts:
        action = "BUY"
        reason = f"RSI{period} {rsi:.2f} is below exit threshold {settings.rsi_exit_level:.2f}; short exit signal."
    else:
        action = "HOLD"
        reason = f"RSI{period} {rsi:.2f} is between reversion thresholds."

    atr = calculate_atr(candles, settings.atr_period)

    return SignalDecision(
        action=action,
        reason=reason,
        price=close,
        ema_fast=rsi,
        ema_slow=rsi,
        previous_ema_fast=rsi,
        previous_ema_slow=rsi,
        atr=atr,
        candle_time=str(candles[-1]["time"]),
        indicators={
            "rsi_period": period,
            "rsi": rsi,
            "atr_period": settings.atr_period,
            "atr": atr,
        },
    )
