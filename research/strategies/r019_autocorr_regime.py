"""R019: Reversal-regime band fade.

The reversal premium paid to liquidity providers is time-varying and persistent.
Measure it directly as the trailing lag-1 autocorrelation of M15 returns, and
trade the finding-1 band fade only when recent returns have been reverting.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; spread <= 2 pips.
  ac = corr(r[t-1], r[t]) over the last 480 M15 close changes; require ac <= ac_max
  (-0.05 = bottom tercile of the 2018-21 distribution of ac alone; r019b: -0.10 = bottom decile).
  mean20/sd20 of last 20 closes; close <= mean20 - 2 sd20 -> MARKET BUY; close >= mean20 + 2 sd20 -> MARKET SELL.
  TP = mean20; SL = same distance beyond entry (1:1); close at market after 120 minutes.
  One position at a time.
See research/LEDGER.md R019.
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


class R019AutocorrRegime(Strategy):
    name = "r019_autocorr_regime"

    def __init__(self, ac_max: float = -0.05, ac_bars: int = 480, period: int = 20, num_std: float = 2.0,
                 hold_min: int = 120, lots: float = 0.10, max_spread_points: float = 20.0):
        self.ac_max, self.ac_bars = float(ac_max), int(ac_bars)
        self.period, self.num_std, self.hold_min = int(period), float(num_std), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"ac_max": self.ac_max, "ac_bars": self.ac_bars, "period": self.period, "num_std": self.num_std,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points}

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
        if len(closes) < self.ac_bars + 1:
            return intents
        c = closes[-self.period:]
        mean, sd = float(c.mean()), float(c.std())
        if sd <= 0:
            return intents
        close = float(closes[-1])
        long_sig = close <= mean - self.num_std * sd
        short_sig = close >= mean + self.num_std * sd
        if not (long_sig or short_sig):
            return intents
        r = np.diff(closes[-(self.ac_bars + 1):])
        a, b = r[:-1], r[1:]
        sa, sb = float(a.std()), float(b.std())
        if sa <= 0 or sb <= 0:
            return intents
        ac = float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))
        if ac > self.ac_max:
            return intents
        dist = abs(close - mean)
        if long_sig:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=mean, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=mean, tag="S"))
        return intents


class R019bAutocorrRegimeDecile(R019AutocorrRegime):
    name = "r019b_autocorr_regime_decile"

    def __init__(self, ac_max: float = -0.10, **kw):
        super().__init__(ac_max=ac_max, **kw)


R019AutocorrRegimeM15 = make_timeframe(R019AutocorrRegime, 15)
R019bAutocorrRegimeDecileM15 = make_timeframe(R019bAutocorrRegimeDecile, 15)
