"""R028: DST-corrected re-test of R012 (scheduled-release order-flow continuation).

Harness clock issue (found 2026-11-24, not patched; see the run report): the HistData
importer assumes fixed EST (UTC-5), but the source timestamps follow New York local time
with DST. During US DST the stored "UTC" stamp is therefore one hour late (stored 13:30 =
true 12:30 UTC = NFP at 08:30 EDT). R012 converted stored time with zoneinfo and so took
its ref/entry one hour before the real release on summer days.

Correction used here (and only here):  NY local = stored - 5h  ->  true UTC.

Rules otherwise identical to R012: release times T: NY 08:30, 10:00, 14:00
(America/New_York), Frankfurt 14:30 (Europe/Berlin). ref = close of the bar stamped T-1.
At the first bar stamped in [T+2, T+10] on a weekday: move = close - ref; if |move| >=
trigger and spread <= 2 pips, MARKET in sign(move). SL = max(8 pips, |move|), no TP,
close 60 min after entry. One position at a time.

r028_release_continuation_dst:     8-pip trigger (R012's registered rule)
r028b_release_continuation_dst_4:  4-pip trigger (R012b), pre-registered eval 2
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")
_RELEASES = (
    (_NY, (8 * 60 + 30, 10 * 60, 14 * 60)),
    (ZoneInfo("Europe/Berlin"), (14 * 60 + 30,)),
)


def _stored(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


def _true_utc(t: str) -> _dt.datetime:
    """Stored stamp -> true UTC. Stored = NY local wall clock + 5h."""
    s = _stored(t)
    ny_wall = (s - _dt.timedelta(hours=5)).replace(tzinfo=None)
    return ny_wall.replace(tzinfo=_NY).astimezone(_UTC)


class R028ReleaseContinuationDST(Strategy):
    name = "r028_release_continuation_dst"
    trigger_pips = 8.0

    def __init__(self, min_sl_pips: float = 8.0, hold_min: int = 60,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_move_pips = float(self.trigger_pips)
        self.min_sl_pips, self.hold_min = float(min_sl_pips), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref: dict = {}
        self._done: set = set()

    def parameters(self) -> dict:
        return {"min_move_pips": self.min_move_pips, "min_sl_pips": self.min_sl_pips,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points,
                "clock": "stored-5h=NY local"}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        ts = _true_utc(ctx.time)
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
            now = _stored(ctx.time) + _dt.timedelta(minutes=1)
            for p in ctx.positions:
                if now - _stored(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
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


class R028bReleaseContinuationDST4(R028ReleaseContinuationDST):
    name = "r028b_release_continuation_dst_4"
    trigger_pips = 4.0
