"""Fade a stretch away from an EMA. Fixed parameters, one instance per block.

The EMA is seeded with a simple mean on the closed lookback this signal needs
(``ema_period + z_period`` bars), so each call stays on that window and does
not read a future bar. Take-profit is that EMA. The stop is ``sl_points``
beyond the close.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, OrderIntent, Strategy


class EmaZscoreReversion(Strategy):
    name = "ema_zscore_reversion"

    def __init__(
        self,
        ema_period: int = 20,
        z_period: int = 20,
        z_enter: float = 2.0,
        sl_points: float = 25.0,
        lots: float = 0.10,
        max_spread_points: float = 30.0,
    ):
        self.ema_period = int(ema_period)
        self.z_period = int(z_period)
        self.z_enter = float(z_enter)
        self.sl_points = float(sl_points)
        self.lots = float(lots)
        self.max_spread_points = float(max_spread_points)

    def parameters(self) -> dict:
        return {
            "ema_period": self.ema_period,
            "z_period": self.z_period,
            "z_enter": self.z_enter,
            "sl_points": self.sl_points,
            "lots": self.lots,
            "max_spread_points": self.max_spread_points,
        }

    def on_bar(self, ctx: BarContext) -> list:
        if ctx.positions or ctx.pending:
            return []
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return []
        need = self.ema_period + self.z_period
        if self.z_period < 2 or len(ctx.window) < need:
            return []
        tail = np.asarray(ctx.window.close[-need:], dtype=np.float64)
        ema = _ema(tail, self.ema_period)
        residual = tail - ema
        sample = residual[-self.z_period :]
        if not np.isfinite(sample).all():
            return []
        deviation = float(sample.std())
        if deviation <= 0.0:
            return []
        z_score = (float(sample[-1]) - float(sample.mean())) / deviation
        price = float(tail[-1])
        stop = self.sl_points * ctx.point
        target = float(ema[-1])
        if z_score >= self.z_enter:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.SELL,
                    lots=self.lots,
                    sl=price + stop,
                    tp=target,
                    tag="z-short",
                )
            ]
        if z_score <= -self.z_enter:
            return [
                OrderIntent(
                    type=OrderType.MARKET,
                    side=Side.BUY,
                    lots=self.lots,
                    sl=price - stop,
                    tp=target,
                    tag="z-long",
                )
            ]
        return []


def _ema(close: np.ndarray, period: int) -> np.ndarray:
    """EMA seeded with the mean of the first ``period`` closes. Earlier values are NaN."""
    out = np.full(close.shape[0], np.nan, dtype=np.float64)
    if close.shape[0] < period or period < 1:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(close[:period].mean())
    for i in range(period, close.shape[0]):
        out[i] = alpha * float(close[i]) + (1.0 - alpha) * out[i - 1]
    return out
