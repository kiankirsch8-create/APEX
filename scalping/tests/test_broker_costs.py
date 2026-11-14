"""Broker cost profile: spread floor and overnight financing."""

from __future__ import annotations

import pandas as pd
import pytest

from scalping.costs import CostModel, OrderType, Side
from scalping.data.bars import ticks_to_m1
from scalping.engine import run_backtest
from scalping.instruments import get_instrument
from scalping.market import build_market, with_min_spread
from scalping.strategies.base import OrderIntent
from scalping.tests.support import Scripted, market_from_minutes

EURUSD = get_instrument("EURUSD")


def _market_at(times_and_quotes):
    rows = [{"timestamp": pd.Timestamp(t, tz="UTC"), "bid": b, "ask": a, "bid_volume": 0.0, "ask_volume": 0.0}
            for t, b, a in times_and_quotes]
    frame = pd.DataFrame(rows)
    return build_market(frame, ticks_to_m1(frame))


def test_spread_floor_widens_tight_ticks_around_the_mid_only():
    m = market_from_minutes([[(1.10000, 1.10002), (1.10000, 1.10012)]])
    w = with_min_spread(m, 0.00007)
    assert w.ask[0] - w.bid[0] == pytest.approx(0.00007)
    assert (w.ask[0] + w.bid[0]) / 2 == pytest.approx(1.10001)      # mid unchanged
    assert w.ask[1] - w.bid[1] == pytest.approx(0.00012)             # already wider: untouched
    assert w.close[0] == m.close[0]


def test_market_buy_pays_the_floored_spread_and_no_commission():
    market = market_from_minutes([[(1.10000, 1.10002)], [(1.10010, 1.10012)], [(1.10020, 1.10022)]])
    strat = Scripted({0: [OrderIntent(OrderType.MARKET, Side.BUY, 0.10, sl=1.09, tp=None)]})
    costs = CostModel(commission_per_lot_side=0.0, slippage_points=0.0, min_spread_points=7.0)
    trade = run_backtest(market, strat, EURUSD, costs).trades[0]
    assert trade.entry_price == pytest.approx(1.10011 + 0.000035)    # mid + half of 0.7 pip
    assert trade.exit_price == pytest.approx(1.10021 - 0.000035)
    assert trade.commission == 0.0


def test_financing_charges_each_2200_utc_rollover_held_across():
    # Fri 21:00 -> Mon 01:00 crosses Fri, Sat and Sun 22:00 = 3 nights
    market = _market_at([("2024-03-01T21:00:05", 1.08000, 1.08002), ("2024-03-01T21:01:05", 1.08000, 1.08002),
                         ("2024-03-04T01:00:05", 1.08000, 1.08002)])
    strat = Scripted({0: [OrderIntent(OrderType.MARKET, Side.BUY, 0.10, sl=1.07, tp=None)]})
    costs = CostModel(commission_per_lot_side=0.0, slippage_points=0.0, financing_long_pct=0.0081,
                      financing_short_pct=0.00012)
    trade = run_backtest(market, strat, EURUSD, costs).trades[0]
    assert trade.financing == pytest.approx(10_000 * 0.0081 / 100 * 3)   # EUR notional, base = EUR
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.financing)


def test_intraday_trade_pays_no_financing():
    market = _market_at([("2024-03-05T10:00:05", 1.08, 1.08002), ("2024-03-05T10:01:05", 1.08, 1.08002),
                         ("2024-03-05T15:00:05", 1.08, 1.08002)])
    strat = Scripted({0: [OrderIntent(OrderType.MARKET, Side.SELL, 0.10, sl=1.09, tp=None)]})
    costs = CostModel(financing_long_pct=0.0081, financing_short_pct=0.00012)
    assert run_backtest(market, strat, EURUSD, costs).trades[0].financing == 0.0
