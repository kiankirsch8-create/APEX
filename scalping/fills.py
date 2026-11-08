"""Tick loop that decides fills.

The Python function is the reference. When numba is installed the same
function is compiled and used by the engine; tests check the two agree.

Rules live next to :func:`scalping.costs.execution_price`. On the tick a
position is opened, only an adverse stop-loss is allowed to fire (a gap
through the stop). A take-profit on that same tick is ignored, because a
single print does not prove the target was tradeable after the fill.
"""

from __future__ import annotations

import numpy as np

KIND_ENTRY = 1.0
KIND_SL = 2.0
KIND_TP = 3.0
KIND_MARKET = 4.0

OT_MARKET = 0
OT_LIMIT = 1
OT_STOP = 2

# events columns: tick, kind, pos_slot, ord_slot, price, lots, side, bid, ask
EVENT_WIDTH = 9

_COMPILED = None


def simulate_bar(
    bid,
    ask,
    pos_side,
    pos_lots,
    pos_entry,
    pos_sl,
    pos_tp,
    pos_close,
    pos_born,
    pos_lock,
    pos_hi,
    ord_type,
    ord_side,
    ord_px,
    ord_sl,
    ord_tp,
    ord_lots,
    ord_alive,
    ord_hi,
    slip,
    events,
):
    """Replay one bar's ticks. Mutates position and order slots. Returns event count.

    ``pos_hi`` and ``ord_hi`` are length-1 int arrays holding the last index
    to scan, or -1 when empty. Missing SL/TP is a non-positive price.
    """
    n_ticks = bid.shape[0]
    n_pos_cap = pos_side.shape[0]
    max_ev = events.shape[0]
    n_ev = 0
    slip = float(slip)

    for t in range(n_ticks):
        b = bid[t]
        a = ask[t]
        if b <= 0.0 or a <= 0.0 or a < b:
            continue
        if n_ev >= max_ev:
            break

        last_pos = pos_hi[0]
        i = 0
        while i <= last_pos:
            side = pos_side[i]
            if side == 0 or pos_born[i] == t:
                i += 1
                continue
            sl = pos_sl[i]
            tp = pos_tp[i]
            kind = 0.0
            price = 0.0
            if side == 1:
                if sl > 0.0 and b <= sl:
                    kind = KIND_SL
                    price = sl - slip if b >= sl else b - slip
                elif pos_close[i] == 1:
                    kind = KIND_MARKET
                    price = b - slip
                elif tp > 0.0 and b >= tp:
                    kind = KIND_TP
                    price = tp
            else:
                if sl > 0.0 and a >= sl:
                    kind = KIND_SL
                    price = sl + slip if a <= sl else a + slip
                elif pos_close[i] == 1:
                    kind = KIND_MARKET
                    price = a + slip
                elif tp > 0.0 and a <= tp:
                    kind = KIND_TP
                    price = tp
            if kind != 0.0:
                events[n_ev, 0] = t
                events[n_ev, 1] = kind
                events[n_ev, 2] = i
                events[n_ev, 3] = -1.0
                events[n_ev, 4] = price
                events[n_ev, 5] = pos_lots[i]
                events[n_ev, 6] = side
                events[n_ev, 7] = b
                events[n_ev, 8] = a
                n_ev += 1
                pos_side[i] = 0
                pos_close[i] = 0
                pos_lock[i] = 1
                if n_ev >= max_ev:
                    break
            i += 1

        if n_ev >= max_ev:
            break

        last_ord = ord_hi[0]
        j = 0
        while j <= last_ord:
            if ord_alive[j] == 0:
                j += 1
                continue
            typ = ord_type[j]
            side = ord_side[j]
            level = ord_px[j]
            filled = -1.0
            if typ == OT_MARKET:
                filled = (a + slip) if side == 1 else (b - slip)
            elif typ == OT_LIMIT:
                if side == 1:
                    if a <= level:
                        filled = level
                else:
                    if b >= level:
                        filled = level
            else:
                # Stop entry becomes a marketable order: ask+slippage for a buy,
                # bid−slippage for a sell. A touch of the level is that quote;
                # a gap through it fills at the worse quote, not at the level.
                if side == 1:
                    if a >= level:
                        filled = a + slip
                else:
                    if b <= level:
                        filled = b - slip
            if filled < 0.0:
                j += 1
                continue

            slot = -1
            k = 0
            while k < n_pos_cap:
                if pos_side[k] == 0 and pos_lock[k] == 0:
                    slot = k
                    break
                k += 1
            if slot < 0:
                j += 1
                continue

            tp = ord_tp[j]
            sl = ord_sl[j]
            if side == 1:
                if tp > 0.0 and tp <= filled:
                    tp = -1.0
            else:
                if tp > 0.0 and tp >= filled:
                    tp = -1.0

            pos_side[slot] = side
            pos_lots[slot] = ord_lots[j]
            pos_entry[slot] = filled
            pos_sl[slot] = sl
            pos_tp[slot] = tp
            pos_close[slot] = 0
            pos_born[slot] = t
            pos_lock[slot] = 1
            if slot > pos_hi[0]:
                pos_hi[0] = slot
            ord_alive[j] = 0

            events[n_ev, 0] = t
            events[n_ev, 1] = KIND_ENTRY
            events[n_ev, 2] = slot
            events[n_ev, 3] = j
            events[n_ev, 4] = filled
            events[n_ev, 5] = ord_lots[j]
            events[n_ev, 6] = side
            events[n_ev, 7] = b
            events[n_ev, 8] = a
            n_ev += 1
            if n_ev >= max_ev:
                break

            # Adverse stop on the entry print only. No take-profit yet.
            gap = False
            gap_px = 0.0
            if side == 1:
                if sl > 0.0 and b <= sl:
                    gap = True
                    gap_px = sl - slip if b >= sl else b - slip
            else:
                if sl > 0.0 and a >= sl:
                    gap = True
                    gap_px = sl + slip if a <= sl else a + slip
            if gap:
                events[n_ev, 0] = t
                events[n_ev, 1] = KIND_SL
                events[n_ev, 2] = slot
                events[n_ev, 3] = -1.0
                events[n_ev, 4] = gap_px
                events[n_ev, 5] = ord_lots[j]
                events[n_ev, 6] = side
                events[n_ev, 7] = b
                events[n_ev, 8] = a
                n_ev += 1
                pos_side[slot] = 0
                pos_lock[slot] = 1
                if n_ev >= max_ev:
                    break
            j += 1

    return n_ev


def _warmup(fn) -> None:
    bid = np.array([1.10, 1.10], dtype=np.float64)
    ask = np.array([1.1002, 1.1002], dtype=np.float64)
    z = np.zeros(2, dtype=np.int64)
    f = np.zeros(2, dtype=np.float64)
    sl = np.full(2, -1.0, dtype=np.float64)
    born = np.full(2, -1, dtype=np.int64)
    hi = np.array([-1], dtype=np.int64)
    ohi = np.array([0], dtype=np.int64)
    alive = np.ones(1, dtype=np.int64)
    events = np.zeros((8, EVENT_WIDTH), dtype=np.float64)
    fn(
        bid,
        ask,
        z.copy(),
        f.copy(),
        f.copy(),
        sl.copy(),
        sl.copy(),
        z.copy(),
        born.copy(),
        z.copy(),
        hi,
        np.array([OT_MARKET], dtype=np.int64),
        np.array([1], dtype=np.int64),
        f[:1].copy(),
        sl[:1].copy(),
        sl[:1].copy(),
        np.array([0.1], dtype=np.float64),
        alive,
        ohi,
        0.0,
        events,
    )


def get_simulate():
    """Return the compiled tick loop, or the Python loop if numba is absent."""
    global _COMPILED
    if _COMPILED is not None:
        return _COMPILED
    try:
        from numba import njit

        compiled = njit(cache=False)(simulate_bar)
        _warmup(compiled)
        _COMPILED = compiled
    except Exception:
        _COMPILED = simulate_bar
    return _COMPILED


def using_numba() -> bool:
    fn = get_simulate()
    return fn is not simulate_bar
