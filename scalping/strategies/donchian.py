"""Donchian breakout that enters with stop orders.

The channel is the high and low of the last ``period`` closed bars. Buy and
sell stops are replaced every bar, which is what a breakout needs and what
the pending-order path of the engine has to get right.
"""

from __future__ import annotations

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, OrderIntent, Strategy


class DonchianBreakout(Strategy):
    name = "donchian_breakout"

    def __init__(
        self,
        period: int = 20,
        sl_points: float = 50.0,
        tp_points: float = 80.0,
        lots: float = 0.10,
    ):
        self.period = int(period)
        self.sl_points = float(sl_points)
        self.tp_points = float(tp_points)
        self.lots = float(lots)

    def parameters(self) -> dict:
        return {
            "period": self.period,
            "sl_points": self.sl_points,
            "tp_points": self.tp_points,
            "lots": self.lots,
        }

    def on_bar(self, ctx: BarContext) -> list:
        cancels = [CancelIntent(order.id) for order in ctx.pending]
        if ctx.positions or len(ctx.window) < self.period:
            return cancels
        high = float(ctx.window.high[-self.period :].max())
        low = float(ctx.window.low[-self.period :].min())
        if high - low < ctx.point:
            return cancels
        risk = self.sl_points * ctx.point
        target = self.tp_points * ctx.point
        return [
            *cancels,
            OrderIntent(
                type=OrderType.STOP,
                side=Side.BUY,
                lots=self.lots,
                price=high,
                sl=high - risk,
                tp=high + target,
                tag="don-buy",
            ),
            OrderIntent(
                type=OrderType.STOP,
                side=Side.SELL,
                lots=self.lots,
                price=low,
                sl=low + risk,
                tp=low - target,
                tag="don-sell",
            ),
        ]
