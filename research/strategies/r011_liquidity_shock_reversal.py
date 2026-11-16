"""R011: unscheduled M1 liquidity-shock reversal.

Shock bar = last completed M1 bar with |close-open| >= max(6 pips, 6 x median |close-open|
of the prior 60 bars), not within [T-2min, T+30min] of a scheduled release time
(NY 08:30/10:00/14:00, Frankfurt 08:00/10:00/11:00/13:45/14:30, DST-aware), not in
20:45-23:15 UTC, Mon-Fri, spread <= 2 pips. MARKET order opposite to the shock at its
close; TP = 50% of the body back from entry, SL = 1.0 x body beyond entry; close after
30 minutes. One position at a time; 30 min cool-down after an exit.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")
_FRA = ZoneInfo("Europe/Berlin")
_NY_T = (8 * 60 + 30, 10 * 60, 14 * 60)
_FRA_T = (8 * 60, 10 * 60, 11 * 60, 13 * 60 + 45, 14 * 60 + 30)


def _ts(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


def _near_release(ts: _dt.datetime) -> bool:
    for tz, times in ((_NY, _NY_T), (_FRA, _FRA_T)):
        loc = ts.astimezone(tz)
        hm = loc.hour * 60 + loc.minute
        for T in times:
            if T - 2 <= hm <= T + 30:
                return True
    return False


class R011LiquidityShockReversal(Strategy):
    name = "r011_liquidity_shock_reversal"

    def __init__(self, min_body_pips: float = 6.0, mult: float = 6.0, lookback: int = 60,
                 tp_frac: float = 0.5, sl_frac: float = 1.0, hold_min: int = 30, cooldown_min: int = 30,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_body_pips, self.mult, self.lookback = float(min_body_pips), float(mult), int(lookback)
        self.tp_frac, self.sl_frac = float(tp_frac), float(sl_frac)
        self.hold_min, self.cooldown_min = int(hold_min), int(cooldown_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._last_exit: _dt.datetime | None = None
        self._had_pos = False

    def parameters(self) -> dict:
        return {"min_body_pips": self.min_body_pips, "mult": self.mult, "lookback": self.lookback,
                "tp_frac": self.tp_frac, "sl_frac": self.sl_frac, "hold_min": self.hold_min,
                "cooldown_min": self.cooldown_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        ts = _ts(ctx.time)
        now = ts + _dt.timedelta(minutes=1)  # decision time = close of this bar
        if ctx.positions:
            self._had_pos = True
            for p in ctx.positions:
                if now - _ts(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if self._had_pos:  # also set right after an order, so same-bar exits start the cool-down
            self._had_pos = False
            self._last_exit = now
        if self._last_exit is not None and now - self._last_exit < _dt.timedelta(minutes=self.cooldown_min):
            return intents
        if ts.weekday() >= 5:
            return intents
        hm = ts.hour * 60 + ts.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            return intents
        if len(ctx.window) < self.lookback + 1:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        w = ctx.window
        bodies = np.abs(w.close[-self.lookback - 1:-1] - w.open[-self.lookback - 1:-1])
        body = float(ctx.close - ctx.open)
        pip = 10 * ctx.point
        thr = max(self.min_body_pips * pip, self.mult * float(np.median(bodies)))
        if abs(body) < thr:
            return intents
        if _near_release(ts):
            return intents
        px = float(ctx.close)
        b = abs(body)
        self._had_pos = True
        if body > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots,
                                       sl=px + self.sl_frac * b, tp=px - self.tp_frac * b, tag="S"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots,
                                       sl=px - self.sl_frac * b, tp=px + self.tp_frac * b, tag="L"))
        return intents
