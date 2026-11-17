"""Tick-level replay of TRULLAS-1 (fast 3-bar swings, 61.8% target) on Databento NQ trades.

The bar backtest (pros.py) decides WHICH orders exist (same signal code). This script replays every order on the real
trade tape instead of 1-minute bars:
- the order goes live at the start of the placement minute; if the last trade before that is already through the entry
  level, it's a miss (same rule as the bar test);
- entry: the first trade at/through the stop level; fill = that trade's price + `slip` ticks;
- cancel: before a fill, a trade beyond the swing extreme or 30 minutes without a fill;
- exit: stop = first trade at/through the stop, fill = that trade's price - `slip` ticks; target = first trade THROUGH the
  target by 1 tick (resting limit, queue-safe), fill at the target; flat at 12:00 NY at that trade's price - slip.
Costs $4.50 per NQ round turn. Prints results for slip = 0, 1, 2 ticks.
"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import pros as P

TICK = 0.25
b1 = P.load("1m"); X = P.Exec(b1)
orders = []
orig = X.trade
def rec(i0, d, entry, stop, tgt, flat_mod, **kw):
    r = orig(i0, d, entry, stop, tgt, flat_mod, **kw)
    orders.append(dict(t0=b1.t.values[i0], session=b1.session.values[i0], d=d, entry=entry, stop=stop, tgt=tgt, bar_result=r))
    return r
X.trade = rec
bar = P.trullas1(b1, X, piv_k=1, tgt_fib=0.618)
O = pd.DataFrame(orders)
# swing extreme for the cancel rule = stop -/+ 1 tick
O["x2"] = O.stop + O.d * TICK
print(f"orders placed: {len(O)}, bar-test fills: {O.bar_result.notna().sum()}", flush=True)

cache = {}
def tape(month):
    if month not in cache:
        cache.clear()
        x = pd.read_parquet(f"scalping_data/nq_trades/{month}.parquet", columns=["ts", "seq", "price", "degraded"])
        x = x[~x.degraded].sort_values(["ts", "seq"], kind="stable")
        t = pd.to_datetime(x.ts.values, utc=True).tz_convert("America/New_York").tz_localize(None).values
        cache[month] = (t, x.price.values)
    return cache[month]

def replay(o, slip):
    t0 = np.datetime64(pd.Timestamp(o.t0))
    t, px = tape(pd.Timestamp(o.t0).strftime("%Y-%m"))
    i = np.searchsorted(t, t0)
    if i == 0: return None
    d, ent, stp, tgt = o.d, o.entry, o.stop, o.tgt
    if (px[i - 1] >= ent) if d == 1 else (px[i - 1] <= ent): return None
    flat = pd.Timestamp(o.t0).normalize() + pd.Timedelta(hours=12)
    tend = np.searchsorted(t, np.datetime64(flat))
    tval = np.searchsorted(t, t0 + np.timedelta64(30, "m"))
    seg = px[i:tend]
    if d == 1: hit = np.flatnonzero(seg >= ent); canc = np.flatnonzero(seg < o.x2)
    else: hit = np.flatnonzero(seg <= ent); canc = np.flatnonzero(seg > o.x2)
    if len(hit) == 0: return None
    k = hit[0]
    if (len(canc) and canc[0] < k) or i + k >= tval: return None
    fill = seg[k] + d * slip * TICK
    rest = seg[k + 1:]
    if d == 1: s = np.flatnonzero(rest <= stp); g = np.flatnonzero(rest >= tgt + TICK)
    else: s = np.flatnonzero(rest >= stp); g = np.flatnonzero(rest <= tgt - TICK)
    s0 = s[0] if len(s) else 10**12; g0 = g[0] if len(g) else 10**12
    if s0 < g0: ex, why, kx = rest[s0] - d * slip * TICK, "stop", s0
    elif g0 < 10**12: ex, why, kx = tgt, "target", g0
    else: ex, why, kx = (rest[-1] if len(rest) else seg[k]) - d * slip * TICK, "flat", max(len(rest) - 1, 0)
    pnl = (ex - fill) * d * 20 - 4.50
    return dict(session=o.session, t_exit=t[min(i + k + 1 + kx, len(t) - 1)], t_fill=t[i + k], fill=fill, exit=ex, why=why, dist=abs(fill - stp), pnl=pnl, R=pnl / (abs(fill - stp) * 20))

O = O.sort_values("t0").reset_index(drop=True)
for slip in (0, 1, 2):
    rs = []; busy = np.datetime64("1970-01-01")
    for o in O.itertuples():
        if np.datetime64(pd.Timestamp(o.t0)) <= busy: continue      # one position at a time
        r = replay(o, slip)
        if r is not None: rs.append(r); busy = r["t_exit"]
    R = pd.DataFrame(rs)
    tt = R.R.mean() / (R.R.std(ddof=1) / np.sqrt(len(R)))
    per = {p: R[(R.session.str[:7] >= a) & (R.session.str[:7] <= b)].R.mean() for p, (a, b) in P.PER.items()}
    print(f"slip {slip} tick/side: {len(R)} fills, win {(R.pnl>0).mean()*100:.0f}%, avgR {R.R.mean():+.3f}, t {tt:.2f}, "
          f"net ${R.pnl.sum():+,.0f}/NQ, exits {R.why.value_counts().to_dict()}, periods " +
          ", ".join(f"{k} {v:+.2f}" for k, v in per.items()), flush=True)
    if slip == 1: R.to_csv("scalping_data/batch_pros/trullas1_fast618_tick_replay_slip1.csv", index=False)
bt = pd.DataFrame([r for r in O.bar_result if r is not None])
print(f"bar test for comparison: {len(bt)} fills, avgR {bt.R.mean():+.3f}")
