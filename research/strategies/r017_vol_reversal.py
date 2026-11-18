"""R017: Volatility-scaled liquidity-provision reversal (Nagel 2012).

Short-term reversal pays liquidity providers; the premium rises with volatility.
Trade the finding-1 band fade only in the high-volatility regime.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; spread <= 2 pips.
  vol = sd of the last 96 M15 close-to-close changes (pips); need vol >= vol_min_pips
  (5.4 = ~top quartile of build values; r017b: 6.8 = ~top decile).
  mean/sd of the last 20 closes; close <= mean - 2 sd -> MARKET BUY; close >= mean + 2 sd -> MARKET SELL.
  TP = mean; SL = same distance beyond entry (1:1); close at market after 120 minutes.
  One position at a time.
See research/LEDGER.md R017.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R017VolReversal(Strategy):
    name = "r017_vol_reversal"

    def __init__(self, vol_min_pips: float = 5.4, vol_bars: int = 96, period: int = 20, num_std: float = 2.0,
                 hold_min: int = 120, lots: float = 0.10, max_spread_points: float = 20.0):
        self.vol_min_pips, self.vol_bars = float(vol_min_pips), int(vol_bars)
        self.period, self.num_std, self.hold_min = int(period), float(num_std), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"vol_min_pips": self.vol_min_pips, "vol_bars": self.vol_bars, "period": self.period,
                "num_std": self.num_std, "hold_min": self.hold_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)  # decision time = close of the M1 bar ending the bucket
        if ctx.positions:
            for p in ctx.positions:
                if now - _utc(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            return intents
        hm = now.hour * 60 + now.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        closes = ctx.window.close
        if len(closes) < self.vol_bars + 1:
            return intents
        pip = 10.0 * ctx.point
        vol = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1)) / pip
        if vol < self.vol_min_pips:
            return intents
        c = closes[-self.period:]
        mean, sd = float(c.mean()), float(c.std())
        if sd <= 0:
            return intents
        close = float(closes[-1])
        dist = abs(close - mean)
        if close <= mean - self.num_std * sd:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=mean, tag="L"))
        elif close >= mean + self.num_std * sd:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=mean, tag="S"))
        return intents


class R017bVolReversalTopDecile(R017VolReversal):
    name = "r017b_vol_reversal_top_decile"

    def __init__(self, vol_min_pips: float = 6.8, **kw):
        super().__init__(vol_min_pips=vol_min_pips, **kw)


R017VolReversalM15 = make_timeframe(R017VolReversal, 15)
R017bVolReversalTopDecileM15 = make_timeframe(R017bVolReversalTopDecile, 15)
