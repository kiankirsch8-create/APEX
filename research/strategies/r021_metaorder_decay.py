"""R021: Metaorder impact decay - fade the transient part of long one-directional M15 runs.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; spread <= 2 pips; one position at a time.
  d_i = close_i - close_{i-1}. At bar t: d_t opposite in sign to d_{t-1};
  d_{t-L}..d_{t-1} all strictly the same sign, full run length L >= min_run (5; r021b: 7);
  R = close_{t-1} - close_{t-1-L}, |R| >= 10 pips.
  Target T = close_{t-1} - R/3. Skip if close_t already at/beyond T.
  MARKET order against the run, TP = T, SL = same distance beyond entry (1:1),
  close at market after 240 minutes. Need >= 40 bars of history.
See research/LEDGER.md R021.
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


class R021MetaorderDecay(Strategy):
    name = "r021_metaorder_decay"

    def __init__(self, min_run: int = 5, min_move_pips: float = 10.0, retrace_frac: float = 1.0 / 3.0,
                 hold_min: int = 240, lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_run, self.min_move_pips, self.retrace_frac = int(min_run), float(min_move_pips), float(retrace_frac)
        self.hold_min, self.lots, self.max_spread_points = int(hold_min), float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"min_run": self.min_run, "min_move_pips": self.min_move_pips, "retrace_frac": self.retrace_frac,
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
        if len(closes) < 40:
            return intents
        d = np.diff(closes[-40:])
        last, prev = d[-1], d[-2]
        if last == 0 or prev == 0 or np.sign(last) == np.sign(prev):
            return intents
        s = np.sign(prev)
        L = 0
        for x in d[-2::-1]:
            if np.sign(x) == s:
                L += 1
            else:
                break
        if L < self.min_run:
            return intents
        pip = 10.0 * ctx.point
        run_end = float(closes[-2])
        R = run_end - float(closes[-2 - L])
        if abs(R) < self.min_move_pips * pip:
            return intents
        target = run_end - self.retrace_frac * R
        close = float(closes[-1])
        dist = abs(close - target)
        if s > 0:  # up-run -> sell
            if close <= target:
                return intents
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=target, tag="S"))
        else:
            if close >= target:
                return intents
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=target, tag="L"))
        return intents


class R021bMetaorderDecayRun7(R021MetaorderDecay):
    name = "r021b_metaorder_decay_run7"

    def __init__(self, min_run: int = 7, **kw):
        super().__init__(min_run=min_run, **kw)


R021MetaorderDecayM15 = make_timeframe(R021MetaorderDecay, 15)
R021bMetaorderDecayRun7M15 = make_timeframe(R021bMetaorderDecayRun7, 15)
