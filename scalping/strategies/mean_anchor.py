"""Bounce back to a price-mean anchor. Fixed parameters, one instance per block.

The anchor is the mean of the last ``anchor_period`` closes. Bar volume is on
``ctx.window.volume`` for a later VWAP; this strategy does not use it. A market
order fires only when price is ``entry_points`` beyond the anchor and the
anchor's one-bar slope is within ``trend_slope_max``.
"""

from __future__ import annotations

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, OrderIntent, Strategy


class MeanAnchorReversion(Strategy):
    name = "mean_anchor_reversion"

    def __init__(
        self,
        anchor_period: int = 20,
        entry_points: float = 15.0,
        sl_points: float = 20.0,
        trend_slope_max: float = 2.0,
        lots: float = 0.10,
        max_spread_points: float = 30.0,
    ):
        self.anchor_period = int(anchor_period)
        self.entry_points = float(entry_points)
        self.sl_points = float(sl_points)
        self.trend_slope_max = float(trend_slope_max)
        self.lots = float(lots)
        self.max_spread_points = float(max_spread_points)

    def parameters(self) -> dict:
        return {
            "anchor_period": self.anchor_period,
            "entry_points": self.entry_points,
            "sl_points": self.sl_points,
            "trend_slope_max": self.trend_slope_max,
            "lots": self.lots,
            "max_spread_points": self.max_spread_points,
        }

    def on_bar(self, ctx: BarContext) -> list:
        if ctx.positions or ctx.pending:
            return []
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return []
        if len(ctx.window) < self.anchor_period + 1:
            return []
        close = ctx.window.close
        anchor = float(close[-self.anchor_period :].mean())
        previous = float(close[-self.anchor_period - 1 : -1].mean())
        slope_points = (anchor - previous) / ctx.point
        if abs(slope_points) > self.trend_slope_max:
            return []
        stretch = self.entry_points * ctx.point
        stop = self.sl_points * ctx.point
        price = float(close[-1])
        if price <= anchor - stretch:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.BUY,
                    lots=self.lots,
                    sl=float(ctx.low) - stop,
                    tp=anchor,
                    tag="anchor-long",
                )
            ]
        if price >= anchor + stretch:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.SELL,
                    lots=self.lots,
                    sl=float(ctx.high) + stop,
                    tp=anchor,
                    tag="anchor-short",
                )
            ]
        return []
