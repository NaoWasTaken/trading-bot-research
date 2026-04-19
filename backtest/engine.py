from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Literal

from brokers import InstrumentMetadata
from config import Settings
from risk import PositionPlan, build_position_plan
from strategies import SignalDecision, StrategyDefinition, get_strategy
from strategies.rsi_reversion_filtered import (
    RSI_FILTER_TREND_PERIOD,
    apply_rsi_entry_filters,
    rsi_filters_enabled,
)

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class BacktestTrade:
    trade_id: int
    strategy_name: str
    instrument: str
    timeframe: str
    direction: Direction
    units: int
    entry_time: str
    exit_time: str
    duration_seconds: float
    entry_reference_price: float
    exit_reference_price: float
    entry_price: float
    exit_price: float
    stop_loss_price: float
    exit_reason: str
    entry_signal_reason: str
    exit_signal_reason: str
    gross_pnl: float
    cost: float
    net_pnl: float
    net_pips: float
    risk_pips: float
    r_multiple: float | None
    equity_after: float

    def to_csv_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PeriodBreakdown:
    period: str
    trades: int
    net_pnl: float
    return_pct: float


@dataclass(frozen=True)
class BacktestSummary:
    strategy_name: str
    instrument: str
    timeframe: str
    candle_count: int
    start_time: str
    end_time: str
    sample_span_days: float
    starting_equity: float
    final_equity: float
    total_return_pct: float
    starting_price: float
    ending_price: float
    buy_and_hold_return_pct: float
    total_trades: int
    win_rate: float
    average_win: float
    average_loss: float
    profit_factor: float
    max_drawdown: float
    worst_losing_streak: int
    average_trade_expectancy: float
    largest_win: float
    largest_loss: float
    net_pips: float
    average_pips_per_trade: float
    pips_won: float
    pips_lost: float
    total_r: float
    average_r: float
    percent_profits_from_best_5_trades: float
    profit_concentration_warning: str | None
    percent_time_in_market: float
    long_trades: int
    short_trades: int
    average_trade_duration_minutes: float
    median_trade_duration_minutes: float
    breakdown_frequency: str | None
    period_breakdowns: list[PeriodBreakdown]
    backtest_spread_pips: float
    backtest_slippage_pips: float
    stop_loss_enabled: bool


@dataclass(frozen=True)
class BacktestResult:
    summary: BacktestSummary
    trades: list[BacktestTrade]


@dataclass
class OpenPosition:
    direction: Direction
    units: int
    entry_time: str
    entry_reference_price: float
    entry_price: float
    stop_loss_price: float
    entry_signal_reason: str
    entry_index: int


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        instrument: InstrumentMetadata,
        strategy_name: str | None = None,
    ) -> None:
        self.settings = settings
        self.instrument = instrument
        self.strategy: StrategyDefinition = get_strategy(strategy_name or settings.strategy_name)
        self.pip_size = 10 ** instrument.pip_location
        self.half_spread_price = (settings.backtest_spread_pips * self.pip_size) / 2
        self.slippage_price = settings.backtest_slippage_pips * self.pip_size

    def run(self, candles: list[dict[str, Any]]) -> BacktestResult:
        if not candles:
            raise ValueError("Backtest requires at least one completed candle.")

        required_candles = self.strategy.min_candles(self.settings)
        if len(candles) < required_candles:
            raise ValueError(f"Backtest requires at least {required_candles} completed candles.")

        equity = self.settings.backtest_starting_equity
        peak_equity = equity
        max_drawdown = 0.0
        losing_streak = 0
        worst_losing_streak = 0
        trades: list[BacktestTrade] = []
        position: OpenPosition | None = None

        signal_series = _build_signal_series(self.strategy.name, candles, self.settings)

        for index in range(required_candles - 1, len(candles)):
            candle = candles[index]

            if position is not None and index > position.entry_index:
                stop_price = self._stop_price_if_hit(position, candle)
                if stop_price is not None:
                    trade, equity = self._close_position(
                        position,
                        exit_time=str(candle["time"]),
                        exit_reference_price=stop_price,
                        exit_reason="STOP_LOSS",
                        exit_signal_reason="Stop loss touched by completed candle high/low.",
                        trade_id=len(trades) + 1,
                        equity_before=equity,
                    )
                    trades.append(trade)
                    peak_equity, max_drawdown = self._update_drawdown(equity, peak_equity, max_drawdown)
                    losing_streak, worst_losing_streak = self._update_losing_streak(
                        trade.net_pnl,
                        losing_streak,
                        worst_losing_streak,
                    )
                    position = None
                    continue

            signal = signal_series[index]
            if signal is None:
                raise ValueError(f"Signal was unavailable for {self.strategy.name} at candle index {index}.")

            if position is not None and self._is_exit_signal(position, signal):
                trade, equity = self._close_position(
                    position,
                    exit_time=signal.candle_time,
                    exit_reference_price=signal.price,
                    exit_reason=f"SIGNAL_{signal.action}",
                    exit_signal_reason=signal.reason,
                    trade_id=len(trades) + 1,
                    equity_before=equity,
                )
                trades.append(trade)
                peak_equity, max_drawdown = self._update_drawdown(equity, peak_equity, max_drawdown)
                losing_streak, worst_losing_streak = self._update_losing_streak(
                    trade.net_pnl,
                    losing_streak,
                    worst_losing_streak,
                )
                position = None

            if position is None and self._is_entry_signal(signal):
                position = self._open_position(signal, index)

        if position is not None:
            last_candle = candles[-1]
            trade, equity = self._close_position(
                position,
                exit_time=str(last_candle["time"]),
                exit_reference_price=float(last_candle["close"]),
                exit_reason="END_OF_BACKTEST",
                exit_signal_reason="Closed any remaining open position at the final completed candle.",
                trade_id=len(trades) + 1,
                equity_before=equity,
            )
            trades.append(trade)
            peak_equity, max_drawdown = self._update_drawdown(equity, peak_equity, max_drawdown)
            losing_streak, worst_losing_streak = self._update_losing_streak(
                trade.net_pnl,
                losing_streak,
                worst_losing_streak,
            )

        summary = self._build_summary(
            candles=candles,
            candle_count=len(candles),
            final_equity=equity,
            trades=trades,
            max_drawdown=max_drawdown,
            worst_losing_streak=worst_losing_streak,
        )
        return BacktestResult(summary=summary, trades=trades)

    def _open_position(self, signal: SignalDecision, index: int) -> OpenPosition:
        position_plan = build_position_plan(
            signal,
            self.instrument,
            fixed_units=self.settings.fixed_units,
            atr_multiplier=self.settings.atr_multiplier,
            fallback_stop_loss_pips=self.settings.stop_loss_pips,
        )

        if position_plan.units == 0:
            raise ValueError("Backtest position units cannot be zero.")

        direction: Direction = "LONG" if self.strategy.is_long_entry(signal, self.settings) else "SHORT"
        entry_price = self._entry_execution_price(signal.action, signal.price)

        return OpenPosition(
            direction=direction,
            units=abs(position_plan.units),
            entry_time=signal.candle_time,
            entry_reference_price=signal.price,
            entry_price=entry_price,
            stop_loss_price=position_plan.stop_loss_price,
            entry_signal_reason=signal.reason,
            entry_index=index,
        )

    def _close_position(
        self,
        position: OpenPosition,
        *,
        exit_time: str,
        exit_reference_price: float,
        exit_reason: str,
        exit_signal_reason: str,
        trade_id: int,
        equity_before: float,
    ) -> tuple[BacktestTrade, float]:
        exit_price = self._exit_execution_price(position.direction, exit_reference_price)
        duration_seconds = max(0.0, (_parse_time(exit_time) - _parse_time(position.entry_time)).total_seconds())

        if position.direction == "LONG":
            gross_pnl = (exit_reference_price - position.entry_reference_price) * position.units
            net_pnl = (exit_price - position.entry_price) * position.units
        else:
            gross_pnl = (position.entry_reference_price - exit_reference_price) * position.units
            net_pnl = (position.entry_price - exit_price) * position.units

        cost = gross_pnl - net_pnl
        if position.direction == "LONG":
            net_pips = (exit_price - position.entry_price) / self.pip_size
        else:
            net_pips = (position.entry_price - exit_price) / self.pip_size
        risk_pips = abs(position.entry_reference_price - position.stop_loss_price) / self.pip_size
        r_multiple = net_pips / risk_pips if risk_pips > 0 else None
        equity_after = equity_before + net_pnl

        trade = BacktestTrade(
            trade_id=trade_id,
            strategy_name=self.strategy.name,
            instrument=self.instrument.name,
            timeframe=self.settings.granularity,
            direction=position.direction,
            units=position.units,
            entry_time=position.entry_time,
            exit_time=exit_time,
            duration_seconds=duration_seconds,
            entry_reference_price=position.entry_reference_price,
            exit_reference_price=exit_reference_price,
            entry_price=entry_price_round(position.entry_price, self.instrument.display_precision),
            exit_price=entry_price_round(exit_price, self.instrument.display_precision),
            stop_loss_price=position.stop_loss_price,
            exit_reason=exit_reason,
            entry_signal_reason=position.entry_signal_reason,
            exit_signal_reason=exit_signal_reason,
            gross_pnl=gross_pnl,
            cost=cost,
            net_pnl=net_pnl,
            net_pips=net_pips,
            risk_pips=risk_pips,
            r_multiple=r_multiple,
            equity_after=equity_after,
        )
        return trade, equity_after

    def _entry_execution_price(self, action: str, reference_price: float) -> float:
        cost_adjustment = self.half_spread_price + self.slippage_price
        if action == "BUY":
            return reference_price + cost_adjustment
        if action == "SELL":
            return reference_price - cost_adjustment
        raise ValueError(f"Unsupported entry action for backtest: {action}")

    def _exit_execution_price(self, direction: Direction, reference_price: float) -> float:
        cost_adjustment = self.half_spread_price + self.slippage_price
        if direction == "LONG":
            return reference_price - cost_adjustment
        return reference_price + cost_adjustment

    def _stop_price_if_hit(self, position: OpenPosition, candle: dict[str, Any]) -> float | None:
        if position.direction == "LONG" and float(candle["low"]) <= position.stop_loss_price:
            return position.stop_loss_price
        if position.direction == "SHORT" and float(candle["high"]) >= position.stop_loss_price:
            return position.stop_loss_price
        return None

    def _is_entry_signal(self, signal: SignalDecision) -> bool:
        return self.strategy.is_long_entry(signal, self.settings) or self.strategy.is_short_entry(signal, self.settings)

    def _is_exit_signal(self, position: OpenPosition, signal: SignalDecision) -> bool:
        if position.direction == "LONG":
            return self.strategy.is_long_exit(signal, self.settings)
        return self.strategy.is_short_exit(signal, self.settings)

    @staticmethod
    def _update_drawdown(equity: float, peak_equity: float, max_drawdown: float) -> tuple[float, float]:
        peak_equity = max(peak_equity, equity)
        if peak_equity <= 0:
            return peak_equity, max_drawdown
        drawdown = (peak_equity - equity) / peak_equity
        return peak_equity, max(max_drawdown, drawdown)

    @staticmethod
    def _update_losing_streak(
        net_pnl: float,
        losing_streak: int,
        worst_losing_streak: int,
    ) -> tuple[int, int]:
        if net_pnl < 0:
            losing_streak += 1
            worst_losing_streak = max(worst_losing_streak, losing_streak)
        else:
            losing_streak = 0
        return losing_streak, worst_losing_streak

    def _build_summary(
        self,
        *,
        candles: list[dict[str, Any]],
        candle_count: int,
        final_equity: float,
        trades: list[BacktestTrade],
        max_drawdown: float,
        worst_losing_streak: int,
    ) -> BacktestSummary:
        starting_equity = self.settings.backtest_starting_equity
        net_pnls = [trade.net_pnl for trade in trades]
        net_pips_values = [trade.net_pips for trade in trades]
        r_values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
        wins = [pnl for pnl in net_pnls if pnl > 0]
        losses = [pnl for pnl in net_pnls if pnl < 0]
        winning_pips = [pips for pips in net_pips_values if pips > 0]
        losing_pips = [pips for pips in net_pips_values if pips < 0]
        start_time = str(candles[0]["time"])
        end_time = str(candles[-1]["time"])
        start_dt = _parse_time(start_time)
        end_dt = _parse_time(end_time)
        sample_seconds = max(0.0, (end_dt - start_dt).total_seconds())
        sample_span_days = sample_seconds / 86400
        starting_price = float(candles[0]["close"])
        ending_price = float(candles[-1]["close"])
        buy_and_hold_return_pct = ((ending_price - starting_price) / starting_price) * 100 if starting_price else 0.0

        total_trades = len(trades)
        total_return_pct = ((final_equity - starting_equity) / starting_equity) * 100
        win_rate = (len(wins) / total_trades) * 100 if total_trades else 0.0
        average_win = sum(wins) / len(wins) if wins else 0.0
        average_loss = sum(losses) / len(losses) if losses else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss == 0:
            profit_factor = math.inf if gross_profit > 0 else 0.0
        else:
            profit_factor = gross_profit / gross_loss
        average_trade_expectancy = sum(net_pnls) / total_trades if total_trades else 0.0
        largest_win = max(wins) if wins else 0.0
        largest_loss = min(losses) if losses else 0.0
        net_pips = sum(net_pips_values)
        average_pips_per_trade = net_pips / total_trades if total_trades else 0.0
        pips_won = sum(winning_pips)
        pips_lost = sum(losing_pips)
        total_r = sum(r_values)
        average_r = total_r / len(r_values) if r_values else 0.0
        percent_best_5 = _percent_profits_from_best_trades(wins, top_n=5)
        concentration_warning = None
        if percent_best_5 >= 60 and len(wins) >= 5:
            concentration_warning = (
                f"{percent_best_5:.2f}% of gross profits came from the best 5 trades; "
                "results may depend heavily on a few outliers."
            )
        time_in_market_seconds = sum(trade.duration_seconds for trade in trades)
        percent_time_in_market = (time_in_market_seconds / sample_seconds) * 100 if sample_seconds else 0.0
        long_trades = sum(1 for trade in trades if trade.direction == "LONG")
        short_trades = sum(1 for trade in trades if trade.direction == "SHORT")
        durations_minutes = [trade.duration_seconds / 60 for trade in trades]
        average_trade_duration_minutes = (
            sum(durations_minutes) / len(durations_minutes) if durations_minutes else 0.0
        )
        median_trade_duration_minutes = median(durations_minutes) if durations_minutes else 0.0
        breakdown_frequency, period_breakdowns = _build_period_breakdowns(
            trades,
            starting_equity=starting_equity,
            sample_span_days=sample_span_days,
        )

        return BacktestSummary(
            strategy_name=self.strategy.name,
            instrument=self.instrument.name,
            timeframe=self.settings.granularity,
            candle_count=candle_count,
            start_time=start_time,
            end_time=end_time,
            sample_span_days=sample_span_days,
            starting_equity=starting_equity,
            final_equity=final_equity,
            total_return_pct=total_return_pct,
            starting_price=starting_price,
            ending_price=ending_price,
            buy_and_hold_return_pct=buy_and_hold_return_pct,
            total_trades=total_trades,
            win_rate=win_rate,
            average_win=average_win,
            average_loss=average_loss,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown * 100,
            worst_losing_streak=worst_losing_streak,
            average_trade_expectancy=average_trade_expectancy,
            largest_win=largest_win,
            largest_loss=largest_loss,
            net_pips=net_pips,
            average_pips_per_trade=average_pips_per_trade,
            pips_won=pips_won,
            pips_lost=pips_lost,
            total_r=total_r,
            average_r=average_r,
            percent_profits_from_best_5_trades=percent_best_5,
            profit_concentration_warning=concentration_warning,
            percent_time_in_market=percent_time_in_market,
            long_trades=long_trades,
            short_trades=short_trades,
            average_trade_duration_minutes=average_trade_duration_minutes,
            median_trade_duration_minutes=median_trade_duration_minutes,
            breakdown_frequency=breakdown_frequency,
            period_breakdowns=period_breakdowns,
            backtest_spread_pips=self.settings.backtest_spread_pips,
            backtest_slippage_pips=self.settings.backtest_slippage_pips,
            stop_loss_enabled=True,
        )


def entry_price_round(price: float, display_precision: int) -> float:
    return round(price, display_precision)


def _parse_time(value: str) -> datetime:
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
    return parsed.astimezone(timezone.utc)


def _build_period_breakdowns(
    trades: list[BacktestTrade],
    *,
    starting_equity: float,
    sample_span_days: float,
) -> tuple[str | None, list[PeriodBreakdown]]:
    if sample_span_days >= 90:
        frequency = "monthly"
    elif sample_span_days >= 14:
        frequency = "weekly"
    else:
        return None, []

    grouped: dict[str, list[BacktestTrade]] = {}
    for trade in trades:
        exit_dt = _parse_time(trade.exit_time)
        if frequency == "monthly":
            key = f"{exit_dt.year:04d}-{exit_dt.month:02d}"
        else:
            iso = exit_dt.isocalendar()
            key = f"{iso.year:04d}-W{iso.week:02d}"
        grouped.setdefault(key, []).append(trade)

    breakdowns = []
    for period in sorted(grouped):
        period_trades = grouped[period]
        net_pnl = sum(trade.net_pnl for trade in period_trades)
        return_pct = (net_pnl / starting_equity) * 100 if starting_equity else 0.0
        breakdowns.append(
            PeriodBreakdown(
                period=period,
                trades=len(period_trades),
                net_pnl=net_pnl,
                return_pct=return_pct,
            )
        )

    return frequency, breakdowns


def _percent_profits_from_best_trades(wins: list[float], *, top_n: int) -> float:
    gross_profit = sum(wins)
    if gross_profit <= 0:
        return 0.0
    best_profit = sum(sorted(wins, reverse=True)[:top_n])
    return (best_profit / gross_profit) * 100


def _build_signal_series(
    strategy_name: str,
    candles: list[dict[str, Any]],
    settings: Settings,
) -> list[SignalDecision | None]:
    if strategy_name == "ema_cross_v1":
        return _ema_cross_signal_series(candles, settings)
    if strategy_name == "trend_filter_v1":
        return _trend_filter_signal_series(candles, settings)
    if strategy_name == "breakout_v1":
        return _breakout_signal_series(candles, settings)
    if strategy_name == "rsi_reversion_v1":
        return _rsi_reversion_signal_series(candles, settings)
    if strategy_name == "rsi_reversion_filtered_v1":
        return _rsi_reversion_filtered_signal_series(candles, settings)
    if strategy_name == "ema_trend_filter_v1":
        return _ema_trend_filter_signal_series(candles, settings)
    raise ValueError(f"Backtest signal precomputation is not implemented for {strategy_name}.")


def _ema_cross_signal_series(candles: list[dict[str, Any]], settings: Settings) -> list[SignalDecision | None]:
    closes = _close_values(candles)
    fast_series = _ema_values(closes, settings.ema_fast_period)
    slow_series = _ema_values(closes, settings.ema_slow_period)
    atr_series = _atr_values(candles, settings.atr_period)
    required_index = max(settings.ema_fast_period, settings.ema_slow_period) + 1
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index in range(required_index, len(candles)):
        previous_fast = fast_series[index - 1]
        previous_slow = slow_series[index - 1]
        current_fast = fast_series[index]
        current_slow = slow_series[index]
        if previous_fast is None or previous_slow is None or current_fast is None or current_slow is None:
            continue

        if previous_fast <= previous_slow and current_fast > current_slow:
            action = "BUY"
            reason = (
                f"EMA {settings.ema_fast_period} crossed above EMA {settings.ema_slow_period} "
                "on the latest completed candle."
            )
        elif previous_fast >= previous_slow and current_fast < current_slow:
            action = "SELL"
            reason = (
                f"EMA {settings.ema_fast_period} crossed below EMA {settings.ema_slow_period} "
                "on the latest completed candle."
            )
        else:
            action = "HOLD"
            reason = "No EMA crossover on the latest completed candle."

        signals[index] = SignalDecision(
            action=action,
            reason=reason,
            price=closes[index],
            ema_fast=current_fast,
            ema_slow=current_slow,
            previous_ema_fast=previous_fast,
            previous_ema_slow=previous_slow,
            atr=atr_series[index],
            candle_time=str(candles[index]["time"]),
            indicators={
                "ema_fast_period": settings.ema_fast_period,
                "ema_slow_period": settings.ema_slow_period,
                f"ema{settings.ema_fast_period}": current_fast,
                f"ema{settings.ema_slow_period}": current_slow,
                f"previous_ema{settings.ema_fast_period}": previous_fast,
                f"previous_ema{settings.ema_slow_period}": previous_slow,
                "atr_period": settings.atr_period,
                "atr": atr_series[index],
            },
        )

    return signals


def _trend_filter_signal_series(candles: list[dict[str, Any]], settings: Settings) -> list[SignalDecision | None]:
    closes = _close_values(candles)
    period = settings.trend_filter_ema_period
    ema_series = _ema_values(closes, period)
    atr_series = _atr_values(candles, settings.atr_period)
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index in range(period - 1, len(candles)):
        ema_value = ema_series[index]
        if ema_value is None:
            continue

        close = closes[index]
        if close > ema_value:
            action = "BUY"
            reason = f"Close {close:.5f} is above EMA{period} {ema_value:.5f}; long-only trend filter is bullish."
        elif close < ema_value:
            action = "SELL"
            reason = f"Close {close:.5f} is below EMA{period} {ema_value:.5f}; long-only trend filter exit signal."
        else:
            action = "HOLD"
            reason = f"Close {close:.5f} equals EMA{period} {ema_value:.5f}."

        signals[index] = SignalDecision(
            action=action,
            reason=reason,
            price=close,
            ema_fast=ema_value,
            ema_slow=ema_value,
            previous_ema_fast=ema_value,
            previous_ema_slow=ema_value,
            atr=atr_series[index],
            candle_time=str(candles[index]["time"]),
            indicators={
                "trend_ema_period": period,
                f"ema{period}": ema_value,
                "atr_period": settings.atr_period,
                "atr": atr_series[index],
            },
        )

    return signals


def _breakout_signal_series(candles: list[dict[str, Any]], settings: Settings) -> list[SignalDecision | None]:
    closes = _close_values(candles)
    highs = [float(candle["high"]) for candle in candles]
    lows = [float(candle["low"]) for candle in candles]
    atr_series = _atr_values(candles, settings.atr_period)
    lookback = settings.breakout_lookback
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index in range(lookback, len(candles)):
        close = closes[index]
        highest_high = max(highs[index - lookback : index])
        lowest_low = min(lows[index - lookback : index])

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

        signals[index] = SignalDecision(
            action=action,
            reason=reason,
            price=close,
            ema_fast=close,
            ema_slow=close,
            previous_ema_fast=close,
            previous_ema_slow=close,
            atr=atr_series[index],
            candle_time=str(candles[index]["time"]),
            indicators={
                "breakout_lookback": lookback,
                "previous_high": highest_high,
                "previous_low": lowest_low,
                "atr_period": settings.atr_period,
                "atr": atr_series[index],
            },
        )

    return signals


def _rsi_reversion_signal_series(candles: list[dict[str, Any]], settings: Settings) -> list[SignalDecision | None]:
    closes = _close_values(candles)
    rsi_series = _rsi_values(closes, settings.rsi_period)
    atr_series = _atr_values(candles, settings.atr_period)
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index in range(settings.rsi_period, len(candles)):
        rsi = rsi_series[index]
        if rsi is None:
            continue

        close = closes[index]
        if rsi < settings.rsi_oversold:
            action = "BUY"
            reason = f"RSI{settings.rsi_period} {rsi:.2f} is below oversold threshold {settings.rsi_oversold:.2f}."
        elif rsi > settings.rsi_overbought and settings.rsi_allow_shorts:
            action = "SELL"
            reason = (
                f"RSI{settings.rsi_period} {rsi:.2f} is above overbought threshold "
                f"{settings.rsi_overbought:.2f}; short enabled."
            )
        elif rsi > settings.rsi_exit_level:
            action = "SELL"
            reason = (
                f"RSI{settings.rsi_period} {rsi:.2f} is above exit threshold "
                f"{settings.rsi_exit_level:.2f}; long exit signal."
            )
        elif rsi < settings.rsi_exit_level and settings.rsi_allow_shorts:
            action = "BUY"
            reason = (
                f"RSI{settings.rsi_period} {rsi:.2f} is below exit threshold "
                f"{settings.rsi_exit_level:.2f}; short exit signal."
            )
        else:
            action = "HOLD"
            reason = f"RSI{settings.rsi_period} {rsi:.2f} is between reversion thresholds."

        signals[index] = SignalDecision(
            action=action,
            reason=reason,
            price=close,
            ema_fast=rsi,
            ema_slow=rsi,
            previous_ema_fast=rsi,
            previous_ema_slow=rsi,
            atr=atr_series[index],
            candle_time=str(candles[index]["time"]),
            indicators={
                "rsi_period": settings.rsi_period,
                "rsi": rsi,
                "atr_period": settings.atr_period,
                "atr": atr_series[index],
            },
        )

    return signals


def _rsi_reversion_filtered_signal_series(
    candles: list[dict[str, Any]],
    settings: Settings,
) -> list[SignalDecision | None]:
    base_signals = _rsi_reversion_signal_series(candles, settings)
    if not rsi_filters_enabled(settings):
        return base_signals

    closes = _close_values(candles)
    ema200_series = (
        _ema_values(closes, RSI_FILTER_TREND_PERIOD)
        if settings.rsi_filter_trend_mode != "off"
        else [None] * len(candles)
    )
    atr_series = _atr_values(candles, settings.atr_period)
    median_atr_series = (
        _rolling_median_values(atr_series, settings.rsi_filter_atr_median_period)
        if settings.rsi_filter_atr_mode != "off"
        else [None] * len(candles)
    )
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index, base_signal in enumerate(base_signals):
        if base_signal is None:
            continue
        signals[index] = apply_rsi_entry_filters(
            base_signal,
            settings=settings,
            close=closes[index],
            ema200=ema200_series[index],
            current_atr=atr_series[index],
            median_atr=median_atr_series[index],
        )

    return signals


def _ema_trend_filter_signal_series(candles: list[dict[str, Any]], settings: Settings) -> list[SignalDecision | None]:
    closes = _close_values(candles)
    ema_cross_signals = _ema_cross_signal_series(candles, settings)
    trend_period = settings.trend_filter_ema_period
    trend_series = _ema_values(closes, trend_period)
    required_index = max(trend_period, settings.ema_fast_period, settings.ema_slow_period) + 1
    signals: list[SignalDecision | None] = [None] * len(candles)

    for index in range(required_index, len(candles)):
        ema_cross_signal = ema_cross_signals[index]
        ema200 = trend_series[index]
        if ema_cross_signal is None or ema200 is None:
            continue

        close = closes[index]
        if ema_cross_signal.action == "BUY" and close > ema200:
            action = "BUY"
            reason = (
                f"{ema_cross_signal.reason} Close {close:.5f} is above EMA{trend_period} {ema200:.5f}."
            )
        elif ema_cross_signal.action == "SELL" and close < ema200:
            action = "SELL"
            reason = (
                f"{ema_cross_signal.reason} Close {close:.5f} is below EMA{trend_period} {ema200:.5f}."
            )
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

        signals[index] = SignalDecision(
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

    return signals


def _close_values(candles: list[dict[str, Any]]) -> list[float]:
    return [float(candle["close"]) for candle in candles]


def _ema_values(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("EMA period must be positive.")

    series: list[float | None] = [None] * len(values)
    if len(values) < period:
        return series

    ema = sum(values[:period]) / period
    series[period - 1] = ema
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        ema = ((values[index] - ema) * multiplier) + ema
        series[index] = ema

    return series


def _atr_values(candles: list[dict[str, Any]], period: int) -> list[float | None]:
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


def _rsi_values(closes: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("RSI period must be positive.")

    series: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return series

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    series[period] = _rsi_from_averages(average_gain, average_loss)

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        series[index] = _rsi_from_averages(average_gain, average_loss)

    return series


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _rolling_median_values(values: list[float | None], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("Rolling median period must be positive.")

    series: list[float | None] = [None] * len(values)
    for index in range(len(values)):
        window = values[max(0, index - period + 1) : index + 1]
        available = [value for value in window if value is not None]
        if len(available) == period:
            series[index] = float(median(available))
    return series
