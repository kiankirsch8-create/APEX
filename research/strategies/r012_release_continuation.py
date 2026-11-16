"""R012: scheduled-release order-flow continuation.

Release times T: NY 08:30, 10:00, 14:00 (America/New_York), Frankfurt 14:30 (Europe/Berlin).
ref = close of the bar stamped T-1. At the close of the first bar stamped >= T+2 (3 min
after the release) on a weekday: move = close - ref; if |move| >= 8 pips and spread <=
2 pips, MARKET in sign(move). SL = max(8 pips, |move|) beyond entry, no TP, close 60 min
after entry. One position at a time.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_RELEASES = (
    (ZoneInfo("America/New_York"), (8 * 60 + 30, 10 * 60, 14 * 60)),
    (ZoneInfo("Europe/Berlin"), (14 * 60 + 30,)),
)


def _ts(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R012ReleaseContinuation(Strategy):
    name = "r012_release_continuation"

    def __init__(self, min_move_pips: float = 8.0, min_sl_pips: float = 8.0, hold_min: int = 60,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_move_pips, self.min_sl_pips, self.hold_min = float(min_move_pips), float(min_sl_pips), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref: dict = {}       # (date, tzname, T) -> ref close
        self._done: set = set()

    def parameters(self) -> dict:
        return {"min_move_pips": self.min_move_pips, "min_sl_pips": self.min_sl_pips,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        ts = _ts(ctx.time)
        now = ts + _dt.timedelta(minutes=1)
        signal = None
        for tz, times in _RELEASES:
            loc = ts.astimezone(tz)
            hm = loc.hour * 60 + loc.minute
            for T in times:
                key = (loc.date(), str(tz), T)
                if hm == T - 1:
                    self._ref[key] = float(ctx.close)
                elif T + 2 <= hm <= T + 10 and key not in self._done and key in self._ref:
                    self._done.add(key)
                    if loc.weekday() < 5:
                        signal = self._ref[key]
        if ctx.positions:
            for p in ctx.positions:
                if now - _ts(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if signal is None or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        px = float(ctx.close)
        move = px - signal
        if abs(move) < self.min_move_pips * pip:
            return intents
        sl = max(self.min_sl_pips * pip, abs(move))
        if move > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tag="S"))
        return intents
