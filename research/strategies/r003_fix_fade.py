"""R003: fade large pre-fix moves around the WM/Reuters 16:00 London fix.

See research/LEDGER.md R003 for mechanism and the pre-registered rules.
Runs on M1 (no timeframe wrapper). DST handled with zoneinfo Europe/London.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CloseIntent, OrderIntent, Strategy

_LDN = ZoneInfo("Europe/London")


class R003FixFade(Strategy):
    name = "r003_fix_fade"

    def __init__(self, min_move_pips: float = 8.0, tp_retrace: float = 0.5, sl_move: float = 1.0,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_move_pips, self.tp_retrace, self.sl_move = float(min_move_pips), float(tp_retrace), float(sl_move)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref: dict = {}
        self._done: set = set()

    def parameters(self) -> dict:
        return {"min_move_pips": self.min_move_pips, "tp_retrace": self.tp_retrace, "sl_move": self.sl_move,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        t = datetime.fromisoformat(ctx.time[:19]).replace(tzinfo=timezone.utc).astimezone(_LDN)
        day, hm = t.date(), t.hour * 60 + t.minute
        if ctx.positions:
            if hm >= 17 * 60 + 30 or hm < 16 * 60:
                return [CloseIntent(p.id) for p in ctx.positions]
            return []
        if t.weekday() >= 5:
            return []
        if 15 * 60 <= hm < 15 * 60 + 30:  # last bar stamped before 15:30 = price at ~15:30
            self._ref[day] = ctx.close
            return []
        if not (16 * 60 + 4 <= hm <= 16 * 60 + 15) or day in self._done or day not in self._ref:
            return []
        self._done.add(day)
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return []
        move, pip = ctx.close - self._ref[day], 10 * ctx.point
        if abs(move) < self.min_move_pips * pip:
            return []
        side = Side.SELL if move > 0 else Side.BUY
        sign = -1.0 if move > 0 else 1.0  # direction of the expected reversion
        px = ctx.close
        return [OrderIntent(OrderType.MARKET, side, self.lots, sl=px - sign * self.sl_move * abs(move),
                            tp=px + sign * self.tp_retrace * abs(move), tag="fix")]
