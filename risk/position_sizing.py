from __future__ import annotations

from dataclasses import dataclass

from brokers import InstrumentMetadata
from strategies import SignalDecision


@dataclass(frozen=True)
class PositionPlan:
    direction: str
    units: int
    entry_price: float
    stop_loss_price: float
    stop_loss_price_str: str
    stop_loss_distance: float
    stop_loss_pips: float


def _pip_size(pip_location: int) -> float:
    return 10 ** pip_location


def build_position_plan(
    signal: SignalDecision,
    instrument: InstrumentMetadata,
    *,
    fixed_units: int,
    atr_multiplier: float,
    fallback_stop_loss_pips: float,
) -> PositionPlan:
    if signal.action not in {"BUY", "SELL"}:
        raise ValueError("Position plans can only be created for BUY or SELL signals.")

    pip_size = _pip_size(instrument.pip_location)
    fallback_distance = fallback_stop_loss_pips * pip_size
    atr_distance = (signal.atr or 0.0) * atr_multiplier
    stop_loss_distance = max(fallback_distance, atr_distance)

    if stop_loss_distance <= 0:
        raise ValueError("Stop loss distance must be positive.")

    units = abs(int(fixed_units))
    if units <= 0:
        raise ValueError("fixed_units must be greater than zero.")

    if signal.action == "BUY":
        signed_units = units
        stop_loss_price = signal.price - stop_loss_distance
    else:
        signed_units = -units
        stop_loss_price = signal.price + stop_loss_distance

    rounded_stop = round(stop_loss_price, instrument.display_precision)
    formatted_stop = f"{rounded_stop:.{instrument.display_precision}f}"

    return PositionPlan(
        direction=signal.action,
        units=signed_units,
        entry_price=signal.price,
        stop_loss_price=rounded_stop,
        stop_loss_price_str=formatted_stop,
        stop_loss_distance=stop_loss_distance,
        stop_loss_pips=stop_loss_distance / pip_size,
    )
