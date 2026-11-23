"""R026: Run pullback - buy the transient counter-bar inside a persistent M15 run.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; spread <= 2 pips; one position at a time; need >= 100 bars.
  d_i = close_i - close_{i-1}. Run: d_{t-L}..d_{t-1} strictly the same sign s, L >= 5,
  R = close_{t-1} - close_{t-1-L}, |R| >= 10 pips (R021's run definition).
  Counter-bar: sign(d_t) == -s and |d_t| >= k * vol, vol = sd (ddof 1) of the 96 changes before d_t.
  k = 0.75 (r026), 1.25 (r026b, pre-registered dose-response).
  MARKET order in the run direction; TP = close_{t-1}; SL = same distance beyond entry (1:1);
  close at market after 240 minutes.
See research/LEDGER.md R026.
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


class R026RunPullback(Strategy):
    name = "r026_run_pullback"

    def __init__(self, k_vol: float = 0.75, min_run: int = 5, min_move_pips: float = 10.0, vol_bars: int = 96,
                 hold_min: int = 240, lots: float = 0.10, max_spread_points: float = 20.0):
        self.k_vol, self.min_run, self.min_move_pips = float(k_vol), int(min_run), float(min_move_pips)
        self.vol_bars, self.hold_min = int(vol_bars), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"k_vol": self.k_vol, "min_run": self.min_run, "min_move_pips": self.min_move_pips,
                "vol_bars": self.vol_bars, "hold_min": self.hold_min, "lots": self.lots,
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
        if len(closes) < max(100, self.vol_bars + 2):
            return intents
        d = np.diff(closes[-(self.vol_bars + 2):])  # vol_bars + 1 changes; last is d_t
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
        vol = float(np.std(d[:-1], ddof=1))
        if vol <= 0 or abs(last) < self.k_vol * vol:
            return intents
        close = float(closes[-1])
        dist = abs(run_end - close)
        if s > 0:  # up-run, down counter-bar -> buy
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=run_end, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=run_end, tag="S"))
        return intents


class R026bRunPullbackK125(R026RunPullback):
    name = "r026b_run_pullback_k125"

    def __init__(self, k_vol: float = 1.25, **kw):
        super().__init__(k_vol=k_vol, **kw)


R026RunPullbackM15 = make_timeframe(R026RunPullback, 15)
R026bRunPullbackK125M15 = make_timeframe(R026bRunPullbackK125, 15)
