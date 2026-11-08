"""Per-block performance, net of spread, slippage, and commission.

Consistency across blocks rewards a strategy that is positive in most blocks
and does not depend on one block for the whole result:

    positive_fraction = share of block returns strictly above zero
    penalty = std / (|mean| + std)
    consistency_score = positive_fraction * (1 - penalty)

``std`` is the population standard deviation of the per-block net returns.
A strategy that makes the same positive return every block scores 1. A
strategy that alternates equal gains and losses scores 0.
"""

from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np

from scalping.engine import BacktestResult


def summarize_result(result: BacktestResult) -> dict:
    nets = np.array([trade.net_pnl for trade in result.trades], dtype=np.float64)
    count = int(nets.size)
    wins = nets[nets > 0.0] if count else nets
    losses = nets[nets < 0.0] if count else nets
    gross_profit = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0
    if gross_loss > 0.0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0.0:
        profit_factor = None
    else:
        profit_factor = 0.0
    net_pnl = float(nets.sum()) if count else 0.0
    initial = float(result.initial_equity)
    net_return = net_pnl / initial if initial else 0.0
    commission = float(sum(trade.commission for trade in result.trades)) if count else 0.0
    gross = float(sum(trade.gross_pnl for trade in result.trades)) if count else 0.0
    dd_pct, dd_abs = _max_drawdown(_equity_curve(result))
    sharpe = _sharpe_daily(result.bar_time, result.equity_bar, initial)
    spreads = [trade.spread_entry for trade in result.trades]
    return {
        "net_pnl": net_pnl,
        "net_return": net_return,
        "profit_factor": profit_factor,
        "profit_factor_infinite": profit_factor is None and gross_profit > 0.0,
        "win_rate": (float(wins.size) / count) if count else 0.0,
        "expectancy": (net_pnl / count) if count else 0.0,
        "max_drawdown": dd_pct,
        "max_drawdown_abs": dd_abs,
        "trade_count": count,
        "wins": int(wins.size) if count else 0,
        "losses": int(losses.size) if count else 0,
        "sharpe": sharpe,
        "gross_pnl": gross,
        "commission": commission,
        "spread_entry_mean": float(np.mean(spreads)) if spreads else 0.0,
        "final_equity": float(result.final_equity),
        "initial_equity": initial,
    }


def consistency(block_returns: list[float]) -> dict:
    """Score a strategy from one net return per chronological block."""
    returns = np.asarray(block_returns, dtype=np.float64)
    n = int(returns.size)
    if n == 0:
        return {
            "consistency_score": 0.0,
            "positive_block_fraction": 0.0,
            "return_mean": 0.0,
            "return_std": 0.0,
            "blocks": 0,
            "method": _METHOD,
        }
    positive = float(np.mean(returns > 0.0))
    mean = float(returns.mean())
    std = float(returns.std(ddof=0))
    penalty = std / (abs(mean) + std + 1e-12)
    return {
        "consistency_score": positive * (1.0 - penalty),
        "positive_block_fraction": positive,
        "return_mean": mean,
        "return_std": std,
        "blocks": n,
        "method": _METHOD,
    }


def block_returns_from_cells(cells: list[dict], block_order: list[str]) -> list[float]:
    """Equal-weight the pairs inside a block, then keep chronological order."""
    grouped: dict[str, list[float]] = OrderedDict((block_id, []) for block_id in block_order)
    for cell in cells:
        grouped.setdefault(cell["block"], []).append(float(cell["net_return"]))
    return [float(np.mean(grouped[block_id])) for block_id in grouped if grouped[block_id]]


_METHOD = (
    "consistency_score = positive_block_fraction "
    "* (1 - std / (|mean| + std)) of per-block net returns"
)


def _equity_curve(result: BacktestResult) -> np.ndarray:
    """Chronological mark-to-market. Trade closes are already inside the bar curve."""
    if result.equity_bar.size == 0:
        return np.array([result.initial_equity], dtype=np.float64)
    return np.concatenate(
        [
            np.array([result.initial_equity], dtype=np.float64),
            result.equity_bar.astype(np.float64),
        ]
    )


def _max_drawdown(equity: np.ndarray) -> tuple[float, float]:
    if equity.size == 0:
        return 0.0, 0.0
    peak = np.maximum.accumulate(equity)
    drop = peak - equity
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(peak > 0.0, drop / peak, 0.0)
    idx = int(np.argmax(frac))
    return float(frac[idx]), float(drop[idx])


def _sharpe_daily(bar_time: np.ndarray, equity: np.ndarray, initial: float) -> float:
    """Annualized Sharpe of end-of-day equity changes. Zero when a day is missing."""
    if equity.size < 2:
        return 0.0
    days = bar_time.astype("datetime64[D]")
    last = np.ones(equity.shape[0], dtype=bool)
    if equity.shape[0] > 1:
        last[:-1] = days[1:] != days[:-1]
    daily = np.concatenate([np.array([initial], dtype=np.float64), equity[last]])
    previous = daily[:-1]
    current = daily[1:]
    mask = previous > 0.0
    if int(mask.sum()) < 2:
        return 0.0
    returns = (current[mask] - previous[mask]) / previous[mask]
    if returns.size < 2:
        return 0.0
    std = float(returns.std(ddof=1))
    if std < 1e-12:
        return 0.0
    return float(returns.mean() / std * math.sqrt(252.0))


def json_ready(value):
    """Replace non-finite floats so the document is strict JSON."""
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 8)
    if isinstance(value, (np.floating,)):
        return json_ready(float(value))
    if isinstance(value, (np.integer,)):
        return int(value)
    return value
