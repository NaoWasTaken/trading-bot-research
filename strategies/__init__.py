from .ema_cross import SignalDecision
from .registry import STRATEGIES, StrategyDefinition, generate_signal_for_strategy, get_all_strategies, get_strategy

__all__ = [
    "STRATEGIES",
    "SignalDecision",
    "StrategyDefinition",
    "generate_signal_for_strategy",
    "get_all_strategies",
    "get_strategy",
]
