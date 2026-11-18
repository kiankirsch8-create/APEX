"""R018: Trend-aligned pullback reversion.

Short-horizon excursions against the multi-day trend are more likely uninformed
liquidity demand (temporary impact) than excursions with it. Fade only those.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; spread <= 2 pips.
  mean20/sd20 of last 20 closes; trend = sign(mean20 - mean480), 480 M15 closes (~5 trading days).
  close <= mean20 - 2 sd20 and mean20 > mean480 -> MARKET BUY;
  close >= mean20 + 2 sd20 and mean20 < mean480 -> MARKET SELL.
  TP = mean20; SL = same distance beyond entry (1:1); close at market after 120 minutes.
  One position at a time.
  r018b (pre-registered eval 2): also require vol96 (sd of last 96 M15 changes) >= 5.4 pips (R017 regime).
See research/LEDGER.md R018.
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


class R018TrendPullback(Strategy):
    name = "r018_trend_pullback"

    def __init__(self, trend_bars: int = 480, vol_min_pips: float = 0.0, vol_bars: int = 96, period: int = 20,
                 num_std: float = 2.0, hold_min: int = 120, lots: float = 0.10, max_spread_points: float = 20.0):
        self.trend_bars, self.vol_min_pips, self.vol_bars = int(trend_bars), float(vol_min_pips), int(vol_bars)
        self.period, self.num_std, self.hold_min = int(period), float(num_std), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"trend_bars": self.trend_bars, "vol_min_pips": self.vol_min_pips, "vol_bars": self.vol_bars,
                "period": self.period, "num_std": self.num_std, "hold_min": self.hold_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)
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
        if len(closes) < max(self.trend_bars, self.vol_bars) + 1:
            return intents
        pip = 10.0 * ctx.point
        if self.vol_min_pips > 0:
            vol = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1)) / pip
            if vol < self.vol_min_pips:
                return intents
        c = closes[-self.period:]
        mean, sd = float(c.mean()), float(c.std())
        if sd <= 0:
            return intents
        slow = float(closes[-self.trend_bars:].mean())
        close = float(closes[-1])
        dist = abs(close - mean)
        if close <= mean - self.num_std * sd and mean > slow:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=mean, tag="L"))
        elif close >= mean + self.num_std * sd and mean < slow:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=mean, tag="S"))
        return intents


class R018bTrendPullbackHighVol(R018TrendPullback):
    name = "r018b_trend_pullback_highvol"

    def __init__(self, vol_min_pips: float = 5.4, **kw):
        super().__init__(vol_min_pips=vol_min_pips, **kw)


R018TrendPullbackM15 = make_timeframe(R018TrendPullback, 15)
R018bTrendPullbackHighVolM15 = make_timeframe(R018bTrendPullbackHighVol, 15)
