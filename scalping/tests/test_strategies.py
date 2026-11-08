"""Reference strategies have to trade, and the costs have to show up in the result."""

from __future__ import annotations

import numpy as np
import pytest

from scalping.costs import CostModel
from scalping.engine import run_backtest
from scalping.instruments import get_instrument
from scalping.metrics import summarize_result
from scalping.strategies import BollingerReversion, DonchianBreakout
from scalping.tests.support import market_from_minutes


def _oscillating_minutes(n_flat: int = 30) -> list[list[tuple[float, float]]]:
    """Flat noise, one close through the lower band, then a rally back to the mean."""
    minutes: list[list[tuple[float, float]]] = []
    rng = np.random.default_rng(3)
    for i in range(n_flat):
        mid = 1.10000 + float(rng.normal(0.0, 0.00004))
        minutes.append([(mid - 0.00004, mid + 0.00004), (mid - 0.00004, mid + 0.00004)])
    drop = 1.09700
    minutes.append([(drop - 0.00004, drop + 0.00004)])
    # Next bars walk back through the middle of the band so the long can take profit.
    for mid in (1.09720, 1.09840, 1.09960, 1.10080, 1.10120):
        minutes.append([(mid - 0.00004, mid + 0.00004), (mid - 0.00004, mid + 0.00004)])
    return minutes


def test_bollinger_reversion_is_net_of_costs_and_sane():
    market = market_from_minutes(_oscillating_minutes())
    result = run_backtest(
        market,
        BollingerReversion(period=20, num_std=2.0, lots=0.10),
        get_instrument("EURUSD"),
        CostModel(commission_per_lot_side=3.5, slippage_points=2.0),
    )
    assert result.trades, "reference strategy produced no trades"
    stats = summarize_result(result)
    assert stats["trade_count"] == len(result.trades)
    assert 0.0 <= stats["win_rate"] <= 1.0
    assert stats["max_drawdown"] >= 0.0
    assert np.isfinite(stats["net_return"])
    assert np.isfinite(stats["expectancy"])
    assert np.isfinite(stats["sharpe"])
    assert stats["commission"] > 0.0
    net = sum(trade.net_pnl for trade in result.trades)
    gross = sum(trade.gross_pnl for trade in result.trades)
    commission = sum(trade.commission for trade in result.trades)
    assert net == pytest.approx(gross - commission)
    assert stats["net_pnl"] == pytest.approx(net)
    # Spread on the fill is the quote that was live, not a configured constant.
    assert all(trade.spread_entry == pytest.approx(0.00008) for trade in result.trades)
    assert any(trade.tag == "bb-long" for trade in result.trades)


def test_donchian_breakout_enters_with_a_stop():
    minutes: list[list[tuple[float, float]]] = []
    for i in range(25):
        mid = 1.10000 + i * 0.00001
        minutes.append([(mid - 0.00005, mid + 0.00005)])
    # Push through the channel high on the bar after the stop is armed.
    minutes.append([(1.10000, 1.10010), (1.10080, 1.10100)])
    minutes.append([(1.10090, 1.10110)])
    market = market_from_minutes(minutes)
    result = run_backtest(
        market,
        DonchianBreakout(period=20, sl_points=80, tp_points=30, lots=0.10),
        get_instrument("EURUSD"),
        CostModel(slippage_points=2.0, commission_per_lot_side=3.5),
    )
    assert result.trades, "breakout reference produced no trades"
    assert any(trade.tag == "don-buy" for trade in result.trades)
    stats = summarize_result(result)
    assert stats["trade_count"] >= 1
    assert stats["commission"] > 0.0
    assert np.isfinite(stats["net_return"])
    assert stats["max_drawdown"] >= 0.0
