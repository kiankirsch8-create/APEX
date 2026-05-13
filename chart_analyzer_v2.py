"""Chart analyzer v2 — data-driven tooling and historical backtests.

The autonomous learning loop lives in ``continuous_backtester`` (rolling backtests +
``learned_weights.json``). Point-in-time tools are in ``backtest_analyzer``.
"""
from __future__ import annotations

from backtest_analyzer import analyze_at_date, get_backtest_history, run_backtest_series
from continuous_backtester import (
    get_improving_state,
    get_learned,
    get_stats as get_continuous_backtest_stats,
    is_enabled as continuous_backtest_enabled,
    start_continuous_backtest,
    stop_continuous_backtest,
    trigger_improvement_now,
)

__all__ = [
    "analyze_at_date",
    "run_backtest_series",
    "get_backtest_history",
    "continuous_backtest_enabled",
    "get_continuous_backtest_stats",
    "get_learned",
    "get_improving_state",
    "start_continuous_backtest",
    "stop_continuous_backtest",
    "trigger_improvement_now",
]
