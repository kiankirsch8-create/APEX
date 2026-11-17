"""Adapters: run the unchanged backtest code on the live tape and return one standard view per strategy.

View = dict(closed=[...], open=None|dict, pending=[...]); prices in NQ points, times as UTC ns ints.
- closed : dict(entry_ns, exit_ns, d, fill, exit, why)
- open   : dict(entry_ns, d, fill, stop, tgt, legs=[(fill, qty_fraction)], flat_mod)
- pending: dict(kind 'stop'|'limit'|'market', d, lvl, stop, tgt, dist, cancel_beyond, expire_ns, from_ns, oco, flat_mod)
Rules for the unfinished bar: Valentini and TRULLAS/CREAMER see completed bars only (their decisions happen at bar close);
WILLIAMS sees the partial minute too (it only needs the open and traded highs/lows).
"""
import os, sys
import numpy as np, pandas as pd
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "orderflow"))
import valentini2 as V
import pros as P
import newbars as NB

NY = "America/New_York"


def ny_to_ns(t):
    return int(pd.Timestamp(t).tz_localize(NY).value)


# ------------------------------------------------------------------------------------------------ Valentini
def valentini_view(d, kw, cache=None):
    bk = kw.get("barKind", "1m")
    if cache is not None and bk in cache: pr = cache[bk]           # several variants share one build per cycle
    else:
        pr = V.build(d, bars_kind=bk)
        if cache is not None: cache[bk] = pr
    bars, rows, rstart, big, tstart = pr
    n = len(bars) - 1                                    # drop the unfinished last bar
    if n < 50: return dict(closed=[], open=None, pending=[])
    bars_c = bars.iloc[:n].reset_index(drop=True)
    rows_c = rows[rows.bar < n]
    big_c = big[big.bar < n]
    ls = {}
    t = V.run(d, bars_c, rows_c, rstart[:n + 1], big_c, tstart[:n + 1], live_state=ls, **kw)
    closed = [] if t.empty else [dict(entry_ns=int(r.entry_ts), exit_ns=int(r.exit_ts), d=int(r.d), fill=r.fill, exit=r.exit,
                                      why=r.why) for r in t[t.leg == 0].itertuples()]
    op = None
    if ls.get("pos"):
        p_ = ls["pos"]
        op = dict(entry_ns=p_["entry_ts"], d=p_["d"], fill=p_["fill"], stop=p_["stop"], tgt=p_["tgt"],
                  legs=[(lg["fill"], lg["q"]) for lg in p_["legs"]], flat_mod=V.FLAT, exit_now=ls.get("pendingExit", False))
    bar_start = int(d.ts.values[tstart[n]])                 # first trade of the unfinished bar
    pend = []
    if op is None:
        if ls.get("pending"):
            pend.append(dict(kind="market", d=ls["pending"]["d"], lvl=np.nan, dist=ls["pending"]["dist"], stop=np.nan, tgt=np.nan,
                             from_ns=bar_start, expire_ns=None, cancel_beyond=None, oco=None, flat_mod=V.FLAT))
        elif ls.get("stopOrder"):
            so = ls["stopOrder"]
            pend.append(dict(kind="stop", d=so["d"], lvl=so["lvl"], dist=so["dist"], stop=so["lvl"] - so["d"] * so["dist"],
                             tgt=so["tgt"], from_ns=bar_start, expire_ns="bar", cancel_beyond=None, oco=None, flat_mod=V.FLAT))
        elif ls.get("limitOrder"):
            lo = ls["limitOrder"]
            pend.append(dict(kind="limit", d=lo["d"], lvl=lo["lvl"], dist=lo["dist"], stop=lo["stp"], tgt=lo["tgt"],
                             from_ns=bar_start, expire_ns="bar", cancel_beyond=lo["stp"], oco=None, flat_mod=V.FLAT))
    return dict(closed=closed, open=op, pending=pend, bar_kind=kw.get("barKind", "1m"), bar_start=bar_start, cut_ns=bar_start)


# ------------------------------------------------------------------------------------------------ bar families
class BarCache:
    """1m/5m bars (+5m profile rows) per completed session, built once; the live session is rebuilt each cycle."""
    def __init__(self):
        self.s = {}

    def bars(self, tape, n_sessions):
        sess = sorted(tape.done)[-n_sessions:]
        for s in sess:
            if s not in self.s:
                g = tape.done[s]
                b1, _ = NB.bars_from(g, 60_000_000_000); b5, r5 = NB.bars_from(g, 300_000_000_000)
                self.s[s] = (b1, b5, r5)
        for s in list(self.s):
            if s not in sess: del self.s[s]
        parts = [self.s[s] for s in sess]
        if tape.buf:
            g = pd.DataFrame(tape.buf, columns=["ts", "seq", "price", "size", "side"]); g["session"] = tape.cur
            if tape.cur in tape.done:                   # backfilled part of today + live part
                g = pd.concat([tape.done[tape.cur], g], ignore_index=True)
                parts = parts[:-1] if sess and sess[-1] == tape.cur else parts
            g = g.sort_values(["ts", "seq"], kind="stable").reset_index(drop=True)
            b1, _ = NB.bars_from(g, 60_000_000_000); b5, r5 = NB.bars_from(g, 300_000_000_000)
            parts.append((b1, b5, r5))
        b1 = pd.concat([p[0] for p in parts], ignore_index=True)
        b5 = pd.concat([p[1] for p in parts], ignore_index=True)
        rr = []; off = 0
        for p in parts:
            r = p[2].copy(); r["bar"] += off; off += len(p[1]); rr.append(r)
        prof = pd.concat(rr, ignore_index=True)
        for b in (b1, b5): b["mod"] = b.t.dt.hour * 60 + b.t.dt.minute
        return b1, b5, prof


def _std_pros(t, b1_cut_ns):
    closed, op, pend = [], None, []
    if t is None or t.empty: return dict(closed=closed, open=op, pending=pend)
    for r in t.itertuples():
        why = r.why
        if why == "pending":
            exp = None if getattr(r, "valid_left", 10**6) >= 10**6 else b1_cut_ns + int(r.valid_left) * 60_000_000_000
            pend.append(dict(kind="stop", d=int(r.d), lvl=r.entry, dist=r.dist, stop=r.stop,
                             tgt=(np.nan if r.tgt is None or (isinstance(r.tgt, float) and np.isnan(r.tgt)) else r.tgt),
                             from_ns=b1_cut_ns, expire_ns=exp, cancel_beyond=r.stop, oco=getattr(r, "oco", None) if isinstance(getattr(r, "oco", None), str) else None,
                             flat_mod=int(r.flat_mod)))
        elif why == "open":
            op = dict(entry_ns=ny_to_ns(r.entry_t), d=int(r.d), fill=r.fill, stop=r.stop,
                      tgt=(np.nan if r.tgt is None else r.tgt), legs=[], flat_mod=int(r.flat_mod), exit_now=False)
        else:
            closed.append(dict(entry_ns=ny_to_ns(r.entry_t), exit_ns=ny_to_ns(r.exit_t), d=int(r.d), fill=r.fill, exit=r.exit, why=why))
    return dict(closed=closed, open=op, pending=pend)


def pros_view(family, kw, b1, b5, prof, partial_ok):
    if not partial_ok:                                   # drop the unfinished minute (and the unfinished 5m bar)
        now_min = b1.t.iloc[-1]
        b1 = b1[b1.t < now_min].reset_index(drop=True)
        n5 = len(b5)
        cut5 = b5.t.iloc[-1]
        keep5 = b5.t < cut5
        b5 = b5[keep5].reset_index(drop=True); prof = prof[prof.bar < keep5.sum()]
    if len(b1) < 100: return dict(closed=[], open=None, pending=[])
    X = P.Exec(b1, live=True)
    if family == "trullas1": t = P.trullas1(b1, X, **kw)
    elif family == "trullas2": t = P.trullas2(b1, X, **kw)
    elif family == "creamer": t = P.creamer(b1, b5, prof, X, **kw)
    elif family == "williams": t = P.williams(b1, X, **kw)
    else: raise ValueError(family)
    cut = ny_to_ns(b1.t.iloc[-1]) + 60_000_000_000         # start of the first minute NOT in the bars
    # partial view (Williams): the unfinished minute is already in the bars, so its orders are live from that minute's start
    v = _std_pros(t, cut if not partial_ok else ny_to_ns(b1.t.iloc[-1]))
    v["cut_ns"] = cut if not partial_ok else None          # trades after cut_ns are invisible to this view
    return v
