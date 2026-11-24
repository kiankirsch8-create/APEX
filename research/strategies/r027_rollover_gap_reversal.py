"""R027: Rollover liquidity-gap reversal.

Around the 22:00 UTC rollover the book is thin (NY closed, Tokyo not open), so mid moves in
21:45-23:00 UTC are mostly quote withdrawal and small orders hitting an empty book. When Tokyo
liquidity arrives the mid should drift back toward the last pre-rollover price.

Rules (UTC, M1, nights Mon-Thu):
  ref  = close of the last M1 bar stamped before 21:45 on day D.
  At the first bar stamped in [23:00, 23:30) on day D: move = close - ref.
  If |move| >= threshold pips and spread <= 1.5 pips: MARKET opposite sign(move).
  TP = ref. SL = max(2*|move|, 6 pips) beyond entry on the move side.
  Close at the first bar >= 00:45 UTC (day D+1). One trade per night.

r027_rollover_gap_reversal:     threshold 2.5 pips (eval 1, as registered)
r027b_rollover_gap_reversal_4:  threshold 4.0 pips (pre-registered eval 2, dose-response)
See research/LEDGER.md R027.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


class R027RolloverGapReversal(Strategy):
    name = "r027_rollover_gap_reversal"
    threshold_pips = 2.5

    def __init__(self, min_sl_pips: float = 6.0, sl_mult: float = 2.0, lots: float = 0.10,
                 max_spread_points: float = 15.0):
        self.min_sl_pips, self.sl_mult = float(min_sl_pips), float(sl_mult)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref_day = None
        self._ref = None
        self._traded: set = set()

    def parameters(self) -> dict:
        return {"threshold_pips": self.threshold_pips, "min_sl_pips": self.min_sl_pips,
                "sl_mult": self.sl_mult, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        day = _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))
        hm = int(t[11:13]) * 60 + int(t[14:16])
        pip = 10 * ctx.point

        # ctx.time is the stamp of the bar that just completed.
        if 21 * 60 <= hm < 21 * 60 + 45:
            if self._ref_day != day:
                self._ref_day = day
            self._ref = ctx.close

        if ctx.positions:
            if 45 <= hm < 22 * 60:          # time stop: first bar >= 00:45 UTC
                for p in ctx.positions:
                    intents.append(CloseIntent(p.id))
            return intents

        if 23 * 60 <= hm < 23 * 60 + 30 and day not in self._traded:
            self._traded.add(day)
            if day.weekday() > 3 or self._ref_day != day or self._ref is None:
                return intents
            if ctx.spread_close > self.max_spread_points * ctx.point:
                return intents
            move = ctx.close - self._ref
            if abs(move) < self.threshold_pips * pip:
                return intents
            sl = max(self.sl_mult * abs(move), self.min_sl_pips * pip)
            if move > 0:
                intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots,
                                           sl=ctx.close + sl, tp=self._ref, tag="F"))
            else:
                intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots,
                                           sl=ctx.close - sl, tp=self._ref, tag="F"))
        return intents


class R027bRolloverGapReversal4(R027RolloverGapReversal):
    name = "r027b_rollover_gap_reversal_4"
    threshold_pips = 4.0
