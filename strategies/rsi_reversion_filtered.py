from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any, Literal

from .ema_cross import SignalDecision, _ema_series, calculate_atr
from .rsi_reversion import generate_signal as generate_rsi_reversion_signal

RSI_FILTER_TREND_PERIOD = 200
EntrySide = Literal["LONG", "SHORT"]


def generate_signal(candles: list[dict[str, Any]], settings: Any) -> SignalDecision:
    base_signal = generate_rsi_reversion_signal(candles, settings)
    if not rsi_filters_enabled(settings):
        return base_signal

    closes = [float(candle["close"]) for candle in candles]
    close = closes[-1]
    ema200 = _latest_ema200(closes) if settings.rsi_filter_trend_mode != "off" else None
    current_atr = calculate_atr(candles, settings.atr_period)
    median_atr = (
        calculate_rolling_median_atr(
            candles,
            atr_period=settings.atr_period,
            median_period=settings.rsi_filter_atr_median_period,
        )
        if settings.rsi_filter_atr_mode != "off"
        else None
    )

    return apply_rsi_entry_filters(
        base_signal,
        settings=settings,
        close=close,
        ema200=ema200,
        current_atr=current_atr,
        median_atr=median_atr,
    )


def rsi_filters_enabled(settings: Any) -> bool:
    return (
        settings.rsi_filter_trend_mode != "off"
        or settings.rsi_filter_atr_mode != "off"
        or settings.rsi_filter_session != "off"
    )


def apply_rsi_entry_filters(
    signal: SignalDecision,
    *,
    settings: Any,
    close: float,
    ema200: float | None,
    current_atr: float | None,
    median_atr: float | None,
) -> SignalDecision:
    entry_side = rsi_entry_side(signal, settings)
    if not rsi_filters_enabled(settings):
        return signal

    if entry_side is None:
        return _replace_signal(
            signal,
            reason=signal.reason,
            indicators=_build_filter_indicators(
                signal,
                settings=settings,
                ema200=ema200,
                current_atr=current_atr,
                median_atr=median_atr,
                trend_failures=None,
                atr_failures=None,
                session_failures=None,
            ),
        )

    trend_failures = _trend_filter_failures(entry_side, close, ema200, settings)
    atr_failures = _atr_filter_failures(current_atr, median_atr, settings)
    session_failures = _session_filter_failures(signal.candle_time, settings)
    failures = [*trend_failures, *atr_failures, *session_failures]
    indicators = _build_filter_indicators(
        signal,
        settings=settings,
        ema200=ema200,
        current_atr=current_atr,
        median_atr=median_atr,
        trend_failures=trend_failures,
        atr_failures=atr_failures,
        session_failures=session_failures,
    )

    if failures:
        return _replace_signal(
            signal,
            action="HOLD",
            reason=f"{signal.reason} Filtered out by rsi_reversion_filtered_v1: {'; '.join(failures)}.",
            indicators=indicators,
        )

    return _replace_signal(
        signal,
        reason=f"{signal.reason} Passed enabled RSI regime filters.",
        indicators=indicators,
    )


def _build_filter_indicators(
    signal: SignalDecision,
    *,
    settings: Any,
    ema200: float | None,
    current_atr: float | None,
    median_atr: float | None,
    trend_failures: list[str] | None,
    atr_failures: list[str] | None,
    session_failures: list[str] | None,
) -> dict[str, Any]:
    indicators = dict(signal.indicators)
    indicators["atr"] = current_atr

    if settings.rsi_filter_trend_mode != "off":
        indicators["ema200"] = ema200
        indicators["trend_filter_mode"] = settings.rsi_filter_trend_mode
        indicators["trend_filter_status"] = _filter_status(trend_failures)

    if settings.rsi_filter_atr_mode != "off":
        indicators["atr_median"] = median_atr
        indicators["atr_median_period"] = settings.rsi_filter_atr_median_period
        indicators["atr_filter_mode"] = settings.rsi_filter_atr_mode
        indicators["atr_filter_status"] = _filter_status(atr_failures)

    if settings.rsi_filter_session != "off":
        indicators["session_filter"] = settings.rsi_filter_session
        indicators["session_filter_status"] = _filter_status(session_failures)

    return indicators


def _filter_status(failures: list[str] | None) -> str:
    if failures is None:
        return "not evaluated for non-entry signal"
    if failures:
        return "failed: " + "; ".join(failures)
    return "passed"


def rsi_entry_side(signal: SignalDecision, settings: Any) -> EntrySide | None:
    if signal.action == "BUY" and "oversold threshold" in signal.reason:
        return "LONG"
    if signal.action == "SELL" and settings.rsi_allow_shorts and "overbought threshold" in signal.reason:
        return "SHORT"
    return None


def calculate_rolling_median_atr(
    candles: list[dict[str, Any]],
    *,
    atr_period: int,
    median_period: int,
) -> float | None:
    atr_values = _atr_series(candles, atr_period)
    recent_values = [value for value in atr_values[-median_period:] if value is not None]
    if len(recent_values) < median_period:
        return None
    return float(median(recent_values))


def _latest_ema200(closes: list[float]) -> float | None:
    if len(closes) < RSI_FILTER_TREND_PERIOD:
        return None
    return _ema_series(closes, RSI_FILTER_TREND_PERIOD)[-1]


def _trend_filter_failures(
    entry_side: EntrySide,
    close: float,
    ema200: float | None,
    settings: Any,
) -> list[str]:
    mode = settings.rsi_filter_trend_mode
    if mode == "off":
        return []
    if ema200 is None:
        return ["EMA200 is unavailable"]

    if mode == "with_trend":
        if entry_side == "LONG" and close <= ema200:
            return [f"with_trend requires long entries above EMA200 ({close:.5f} <= {ema200:.5f})"]
        if entry_side == "SHORT" and close >= ema200:
            return [f"with_trend requires short entries below EMA200 ({close:.5f} >= {ema200:.5f})"]
    elif mode == "against_trend":
        if entry_side == "LONG" and close >= ema200:
            return [f"against_trend requires long entries below EMA200 ({close:.5f} >= {ema200:.5f})"]
        if entry_side == "SHORT" and close <= ema200:
            return [f"against_trend requires short entries above EMA200 ({close:.5f} <= {ema200:.5f})"]

    return []


def _atr_filter_failures(current_atr: float | None, median_atr: float | None, settings: Any) -> list[str]:
    mode = settings.rsi_filter_atr_mode
    if mode == "off":
        return []
    if current_atr is None:
        return ["current ATR is unavailable"]
    if median_atr is None:
        return ["rolling median ATR is unavailable"]

    if mode == "below_median" and current_atr >= median_atr:
        return [f"ATR filter requires ATR below median ({current_atr:.5f} >= {median_atr:.5f})"]
    if mode == "above_median" and current_atr <= median_atr:
        return [f"ATR filter requires ATR above median ({current_atr:.5f} <= {median_atr:.5f})"]
    return []


def _session_filter_failures(candle_time: str, settings: Any) -> list[str]:
    mode = settings.rsi_filter_session
    if mode == "off":
        return []

    hour = _parse_utc_hour(candle_time)
    in_london = 7 <= hour < 16
    in_new_york = 13 <= hour < 22
    if mode == "london" and not in_london:
        return [f"session filter requires London hours, got {hour:02d}:00 UTC"]
    if mode == "new_york" and not in_new_york:
        return [f"session filter requires New York hours, got {hour:02d}:00 UTC"]
    if mode == "london_new_york" and not (in_london or in_new_york):
        return [f"session filter requires London or New York hours, got {hour:02d}:00 UTC"]
    return []


def _atr_series(candles: list[dict[str, Any]], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("ATR period must be positive.")

    series: list[float | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return series

    true_ranges: list[float] = [0.0] * len(candles)
    previous_close = float(candles[0]["close"])
    for index in range(1, len(candles)):
        high = float(candles[index]["high"])
        low = float(candles[index]["low"])
        true_ranges[index] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        previous_close = float(candles[index]["close"])

    atr = sum(true_ranges[1 : period + 1]) / period
    series[period] = atr
    for index in range(period + 1, len(candles)):
        atr = ((atr * (period - 1)) + true_ranges[index]) / period
        series[index] = atr

    return series


def _parse_utc_hour(value: str) -> int:
    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        date_part, rest = normalized.split(".", 1)
        if "+" in rest:
            fraction, zone = rest.split("+", 1)
            normalized = f"{date_part}.{fraction[:6]}+{zone}"
        elif "-" in rest:
            fraction, zone = rest.split("-", 1)
            normalized = f"{date_part}.{fraction[:6]}-{zone}"
        else:
            normalized = f"{date_part}.{rest[:6]}"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).hour


def _replace_signal(
    signal: SignalDecision,
    *,
    action: str | None = None,
    reason: str,
    indicators: dict[str, Any] | None = None,
) -> SignalDecision:
    return SignalDecision(
        action=action or signal.action,
        reason=reason,
        price=signal.price,
        ema_fast=signal.ema_fast,
        ema_slow=signal.ema_slow,
        previous_ema_fast=signal.previous_ema_fast,
        previous_ema_slow=signal.previous_ema_slow,
        atr=signal.atr,
        candle_time=signal.candle_time,
        indicators=indicators if indicators is not None else signal.indicators,
    )
