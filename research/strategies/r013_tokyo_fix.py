"""R013: Tokyo 09:55 JST fix (Ito & Yamada 2017; Krohn, Mueller & Whelan 2024).

Japanese importers buy USD at the 09:55 JST bank fixing; banks pre-hedge, so USD rises
into the fix and gives some back afterwards. For EURUSD:

  Leg A: SELL at market at the first bar with JST time in [08:00, 08:15), close at the
         first bar with JST time >= 09:54.
  Leg B: BUY at market at the first bar with JST time in [09:56, 10:10), close at the
         first bar with JST time >= 12:00.

JST = UTC+9 (no DST). JST weekdays only, spread <= 2 pips, catastrophe SL 30 pips, no TP,
one position at a time, one entry per leg per JST day, close on JST date change.

r013b_tokyo_fix_gotobi: the same rules on gotobi days only (pre-registered eval 2):
JST day-of-month divisible by 5, or the last JST weekday before such a day when it
falls on a weekend. See research/LEDGER.md R013.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_JST = _dt.timezone(_dt.timedelta(hours=9))


def _is_gotobi(d: _dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    if d.day % 5 == 0:
        return True
    # Friday standing in for a weekend gotobi day (Sat = +1, Sun = +2).
    if d.weekday() == 4:
        for k in (1, 2):
            n = d + _dt.timedelta(days=k)
            if n.month == d.month and n.day % 5 == 0:
                return True
    return False


class R013TokyoFix(Strategy):
    name = "r013_tokyo_fix"
    gotobi_only = False

    def __init__(self, a_entry: int = 8 * 60, a_exit: int = 9 * 60 + 54, b_entry: int = 9 * 60 + 56,
                 b_exit: int = 12 * 60, entry_window: int = 15, b_entry_window: int = 14,
                 sl_pips: float = 30.0, lots: float = 0.10, max_spread_points: float = 20.0):
        self.a_entry, self.a_exit, self.b_entry, self.b_exit = int(a_entry), int(a_exit), int(b_entry), int(b_exit)
        self.entry_window, self.b_entry_window = int(entry_window), int(b_entry_window)
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self._done: set = set()

    def parameters(self) -> dict:
        return {"a_entry": self.a_entry, "a_exit": self.a_exit, "b_entry": self.b_entry, "b_exit": self.b_exit,
                "entry_window": self.entry_window, "b_entry_window": self.b_entry_window,
                "sl_pips": self.sl_pips, "lots": self.lots, "max_spread_points": self.max_spread_points,
                "gotobi_only": self.gotobi_only}

    @staticmethod
    def _jst(t: str) -> _dt.datetime:
        ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
        return ts.astimezone(_JST)

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        loc = self._jst(ctx.time)
        hm, day = loc.hour * 60 + loc.minute, loc.date()
        if ctx.positions:
            for p in ctx.positions:
                ent = self._jst(p.entry_time)
                exit_min = self.a_exit if p.tag == "A" else self.b_exit
                if hm >= exit_min or ent.date() != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if day.weekday() >= 5 or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        if self.gotobi_only and not _is_gotobi(day):
            return intents
        sl = self.sl_pips * 10 * ctx.point
        if self.a_entry <= hm < self.a_entry + self.entry_window and (day, "A") not in self._done:
            self._done.add((day, "A"))
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=ctx.close + sl, tag="A"))
        elif self.b_entry <= hm < self.b_entry + self.b_entry_window and (day, "B") not in self._done:
            self._done.add((day, "B"))
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=ctx.close - sl, tag="B"))
        return intents


class R013bTokyoFixGotobi(R013TokyoFix):
    name = "r013b_tokyo_fix_gotobi"
    gotobi_only = True
