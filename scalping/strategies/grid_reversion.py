"""Counter-trend limit grid. Fixed parameters, one instance per block.

Places a ladder only while the last close is inside a Bollinger envelope and
bar-to-bar movement is no larger than one grid step. Each limit's take-profit
is ``tp_points`` back toward that close. A basket whose aggregate open loss
exceeds ``basket_sl_points`` is flattened and the pending ladder is cancelled.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import (
    BarContext,
    CancelIntent,
    CloseIntent,
    OrderIntent,
    Strategy,
)


class GridReversion(Strategy):
    name = "grid_reversion"

    def __init__(
        self,
        step_points: float = 20.0,
        levels: int = 3,
        tp_points: float = 20.0,
        basket_sl_points: float = 120.0,
        band_period: int = 20,
        band_std: float = 2.0,
        lots: float = 0.10,
        max_spread_points: float = 30.0,
    ):
        self.step_points = float(step_points)
        self.levels = int(levels)
        self.tp_points = float(tp_points)
        self.basket_sl_points = float(basket_sl_points)
        self.band_period = int(band_period)
        self.band_std = float(band_std)
        self.lots = float(lots)
        self.max_spread_points = float(max_spread_points)

    def parameters(self) -> dict:
        return {
            "step_points": self.step_points,
            "levels": self.levels,
            "tp_points": self.tp_points,
            "basket_sl_points": self.basket_sl_points,
            "band_period": self.band_period,
            "band_std": self.band_std,
            "lots": self.lots,
            "max_spread_points": self.max_spread_points,
        }

    def on_bar(self, ctx: BarContext) -> list:
        if _basket_loss_points(ctx) > self.basket_sl_points:
            return _flatten(ctx)
        if len(ctx.window) < self.band_period or self.levels < 1:
            return []
        close = ctx.window.close
        sample = close[-self.band_period :]
        middle = float(sample.mean())
        deviation = float(sample.std())
        upper = middle + self.band_std * deviation
        lower = middle - self.band_std * deviation
        price = float(close[-1])
        if price < lower or price > upper:
            return [CancelIntent(order.id) for order in ctx.pending]
        if ctx.positions:
            return []
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return []
        if ctx.pending:
            return []
        changes = np.diff(sample.astype(np.float64))
        realized = float(changes.std()) if changes.size else 0.0
        if realized > self.step_points * ctx.point:
            return []
        return _ladder(ctx, price, self.levels, self.step_points, self.tp_points, self.lots)


def _basket_loss_points(ctx: BarContext) -> float:
    """Sum of open losses in points. A position in profit reduces the total."""
    price = float(ctx.close)
    loss = 0.0
    for pos in ctx.positions:
        if pos.side == Side.BUY:
            loss += (pos.entry_price - price) / ctx.point
        else:
            loss += (price - pos.entry_price) / ctx.point
    return loss


def _flatten(ctx: BarContext) -> list:
    cancels = [CancelIntent(order.id) for order in ctx.pending]
    if ctx.positions:
        return [*cancels, CloseIntent(None)]
    return cancels


def _ladder(ctx: BarContext, price: float, levels: int, step_points: float, tp_points: float, lots: float) -> list:
    step = step_points * ctx.point
    target = tp_points * ctx.point
    orders = []
    for level in range(1, levels + 1):
        buy = price - level * step
        sell = price + level * step
        orders.append(
            OrderIntent(
                type=OrderType.LIMIT,
                side=Side.BUY,
                lots=lots,
                price=buy,
                tp=buy + target,
                tag=f"grid-buy-{level}",
            )
        )
        orders.append(
            OrderIntent(
                type=OrderType.LIMIT,
                side=Side.SELL,
                lots=lots,
                price=sell,
                tp=sell - target,
                tag=f"grid-sell-{level}",
            )
        )
    return orders
