"""Bollinger mean-reversion, used to prove the engine end to end.

Fixed parameters. Nothing here looks at another block or refits itself.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, OrderIntent, Strategy


class BollingerReversion(Strategy):
    name = "bollinger_reversion"

    def __init__(
        self,
        period: int = 20,
        num_std: float = 2.0,
        atr_period: int = 14,
        sl_atr_mult: float = 1.5,
        lots: float = 0.10,
        max_spread_points: float = 30.0,
    ):
        self.period = int(period)
        self.num_std = float(num_std)
        self.atr_period = int(atr_period)
        self.sl_atr_mult = float(sl_atr_mult)
        self.lots = float(lots)
        self.max_spread_points = float(max_spread_points)

    def parameters(self) -> dict:
        return {
            "period": self.period,
            "num_std": self.num_std,
            "atr_period": self.atr_period,
            "sl_atr_mult": self.sl_atr_mult,
            "lots": self.lots,
            "max_spread_points": self.max_spread_points,
        }

    def on_bar(self, ctx: BarContext) -> list:
        if ctx.positions or ctx.pending:
            return []
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return []
        need = max(self.period, self.atr_period + 1)
        if len(ctx.window) < need:
            return []
        close = ctx.window.close
        sample = close[-self.period :]
        middle = float(sample.mean())
        deviation = float(sample.std())
        if deviation <= 0.0:
            return []
        upper = middle + self.num_std * deviation
        lower = middle - self.num_std * deviation
        atr = _atr(ctx.window.high, ctx.window.low, close, self.atr_period)
        distance = max(self.sl_atr_mult * atr, 5.0 * ctx.point)
        price = float(close[-1])
        if price < lower:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.BUY,
                    lots=self.lots,
                    sl=price - distance,
                    tp=middle,
                    tag="bb-long",
                )
            ]
        if price > upper:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.SELL,
                    lots=self.lots,
                    sl=price + distance,
                    tp=middle,
                    tag="bb-short",
                )
            ]
        return []


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> float:
    prev = close[:-1]
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - prev), np.abs(low[1:] - prev)))
    if tr.size < period:
        return float(tr.mean()) if tr.size else 0.0
    return float(tr[-period:].mean())
