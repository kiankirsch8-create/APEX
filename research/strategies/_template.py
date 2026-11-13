"""TEMPLATE: copy to research/strategies/r###_short_name.py and edit.

Files starting with "_" are not loaded. Every top-level Strategy subclass with
a unique ``name`` in a loaded file is registered automatically, and so is
every ``make_timeframe(...)`` variant assigned at module level.

Rules for research strategies:
* Fixed parameters only: no refitting, no reading other blocks, no lookahead.
  ``ctx.window`` holds completed bars only; that is the only market data allowed.
* Name it after the ledger idea: "r007_asia_band_fade", plus "@M15" etc. via make_timeframe.
* Changing the code after a build pass changes its source hash, and the validator
  will reject it. Create a new strategy name for a new variant.

API cheat sheet
---------------
ctx.window.open/high/low/close/spread_close/volume   numpy arrays, oldest -> newest, completed bars only
ctx.time (ISO str, UTC)   ctx.point (0.00001 EURUSD)   ctx.spread_close (live spread, price units)
ctx.positions -> [PositionView(id, side, lots, entry_price, sl, tp, entry_time, tag)]
ctx.pending   -> [OrderView(id, type, side, lots, price, sl, tp, ...)]
Return a list of intents:
  OrderIntent(type=OrderType.MARKET|LIMIT|STOP, side=Side.BUY|SELL, lots, price=None|level, sl, tp, tag)
  CancelIntent(order_id)   CloseIntent(position_id=None means all)   ModifyIntent(position_id, sl, tp)
Fills: market/stop pay spread + 0.2 pip slippage; limit fills at its price, no slippage.
Pending orders never expire: cancel stale ones yourself (the example does).
A take-profit already through the fill price gets the order rejected.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe


class ExampleBandLimitFade(Strategy):
    """Example only: rest a limit order at the outer band, target the mean."""

    name = "example_band_limit_fade"

    def __init__(self, period: int = 20, num_std: float = 2.5, sl_points: float = 60.0,
                 asia_only: bool = True, lots: float = 0.10, max_spread_points: float = 20.0):
        self.period, self.num_std, self.sl_points = int(period), float(num_std), float(sl_points)
        self.asia_only, self.lots, self.max_spread_points = bool(asia_only), float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"period": self.period, "num_std": self.num_std, "sl_points": self.sl_points,
                "asia_only": self.asia_only, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]  # refresh the resting order every decision bar
        if ctx.positions or len(ctx.window) < self.period:
            return intents
        hour = int(ctx.time[11:13])
        if self.asia_only and not (hour >= 22 or hour < 7):
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        close = ctx.window.close[-self.period:]
        mean, sd = float(close.mean()), float(close.std())
        if sd <= 0:
            return intents
        sl = self.sl_points * ctx.point
        lower, upper = mean - self.num_std * sd, mean + self.num_std * sd
        intents.append(OrderIntent(OrderType.LIMIT, Side.BUY, self.lots, price=lower, sl=lower - sl, tp=mean, tag="L"))
        intents.append(OrderIntent(OrderType.LIMIT, Side.SELL, self.lots, price=upper, sl=upper + sl, tp=mean, tag="S"))
        return intents


ExampleBandLimitFadeM15 = make_timeframe(ExampleBandLimitFade, 15)
