"""Block metrics and the cross-block consistency score."""

from __future__ import annotations

import math

import numpy as np
import pytest

from scalping.engine import BacktestResult, Trade
from scalping.metrics import consistency, json_ready, summarize_result


def _trade(net: float, gross: float | None = None, commission: float = 1.0) -> Trade:
    gross = net + commission if gross is None else gross
    return Trade(
        symbol="EURUSD",
        side="buy",
        lots=0.1,
        entry_time="2020-01-06T10:00:00.000Z",
        exit_time="2020-01-06T10:05:00.000Z",
        entry_price=1.1,
        exit_price=1.101,
        exit_reason="tp",
        spread_entry=0.0001,
        spread_exit=0.0001,
        slippage_entry=0.00002,
        slippage_exit=0.0,
        gross_pnl=gross,
        commission=commission,
        net_pnl=net,
        tag="",
    )


def _result(trades: list[Trade], equity: list[float], initial: float = 100_000.0) -> BacktestResult:
    times = np.datetime64("2020-01-06T10:00") + np.arange(len(equity)).astype("timedelta64[m]")
    return BacktestResult(
        trades=trades,
        equity_bar=np.array(equity, dtype=np.float64),
        equity_trade=np.array([], dtype=np.float64),
        bar_time=times,
        initial_equity=initial,
        final_equity=equity[-1] if equity else initial,
    )


def test_empty_result_is_zeroed():
    stats = summarize_result(_result([], [100_000.0, 100_000.0]))
    assert stats["trade_count"] == 0
    assert stats["net_return"] == 0.0
    assert stats["profit_factor"] == 0.0
    assert stats["win_rate"] == 0.0
    assert stats["expectancy"] == 0.0
    assert stats["max_drawdown"] == 0.0
    assert stats["sharpe"] == 0.0


def test_profit_factor_win_rate_and_drawdown():
    trades = [_trade(10.0), _trade(5.0), _trade(-4.0)]
    equity = [100_000.0, 100_010.0, 100_015.0, 99_000.0, 100_011.0]
    stats = summarize_result(_result(trades, equity))
    assert stats["trade_count"] == 3
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["profit_factor"] == pytest.approx(15.0 / 4.0)
    assert stats["expectancy"] == pytest.approx(11.0 / 3.0)
    assert stats["net_pnl"] == pytest.approx(11.0)
    assert stats["net_return"] == pytest.approx(11.0 / 100_000.0)
    assert stats["max_drawdown"] == pytest.approx((100_015.0 - 99_000.0) / 100_015.0)
    assert stats["commission"] == pytest.approx(3.0)


def test_profit_factor_infinite_becomes_null_in_json():
    stats = summarize_result(_result([_trade(8.0)], [100_000.0, 100_008.0]))
    assert stats["profit_factor"] is None
    assert stats["profit_factor_infinite"] is True
    ready = json_ready(stats)
    assert ready["profit_factor"] is None
    assert math.isfinite(ready["net_return"])


def test_consistency_rewards_stable_positive_blocks():
    flat = consistency([0.01, 0.01, 0.01, 0.01])
    assert flat["consistency_score"] == pytest.approx(1.0)
    assert flat["positive_block_fraction"] == pytest.approx(1.0)

    mixed = consistency([0.02, -0.02, 0.02, -0.02])
    assert mixed["consistency_score"] == pytest.approx(0.0, abs=1e-8)

    noisy = consistency([0.01, 0.05])
    assert noisy["positive_block_fraction"] == pytest.approx(1.0)
    assert 0.0 < noisy["consistency_score"] < 1.0

    losing = consistency([-0.01, -0.02, 0.005])
    assert losing["positive_block_fraction"] == pytest.approx(1 / 3)
    assert losing["consistency_score"] < losing["positive_block_fraction"]
