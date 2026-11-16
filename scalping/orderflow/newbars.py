"""Bar cache for the TRULLAS / CREAMER / WILLIAMS batch (Databento NQ trades, Jun 2025 - Jun 2026).

Per month writes scalping_data/newbars/{1m,5m}_{YYYY-MM}.parquet and prof5_{YYYY-MM}.parquet:
- bars: NY-time minute, session (trading day), o h l c v, delta (aggressor buy - sell), buyv, sellv,
  bigbuy/bigsell = largest single aggressive order (same exchange ts + same side summed = one order, as in valentini2),
  bigbuy_px/bigsell_px = its price, poc = highest-volume 1-pt row inside the bar.
- prof5: volume per 1-point price row per 5m bar (for fixed-range profiles of swings).
"""
import os, sys
import numpy as np, pandas as pd

OUT = "scalping_data/newbars"
RS = 1.0


def bars_from(d, freq_ns):
    ts = d.ts.values; px = d.price.values; sz = d["size"].values.astype(np.int64); sd = d.side.values.astype(np.int64)
    key = ts // freq_ns
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]]); ends = np.r_[starts[1:], len(ts)]
    bar = np.repeat(np.arange(len(starts)), ends - starts)
    t0 = pd.to_datetime(key[starts] * freq_ns, utc=True).tz_convert("America/New_York").tz_localize(None)
    b = pd.DataFrame({"t": t0, "session": d.session.values[starts].astype(str), "o": px[starts], "c": px[ends - 1],
                      "h": np.maximum.reduceat(px, starts), "l": np.minimum.reduceat(px, starts),
                      "v": np.add.reduceat(sz, starts), "delta": np.add.reduceat(sz * sd, starts)})
    b["buyv"] = (b.v + b.delta) // 2; b["sellv"] = b.v - b.buyv
    # single orders
    ok = np.r_[True, (ts[1:] != ts[:-1]) | (sd[1:] != sd[:-1])]
    gs = np.flatnonzero(ok); ge = np.r_[gs[1:], len(ts)]
    osz = np.add.reduceat(sz, gs); oside = sd[gs]; obar = bar[gs]; opx = px[ge - 1]
    for s, name in ((1, "bigbuy"), (-1, "bigsell")):
        m = oside == s
        df = pd.DataFrame({"bar": obar[m], "sz": osz[m], "px": opx[m]}).sort_values(["bar", "sz"]).drop_duplicates("bar", keep="last")
        b[name] = 0; b[name + "_px"] = np.nan
        b.loc[df.bar.values, name] = df.sz.values; b.loc[df.bar.values, name + "_px"] = df.px.values
    # profile rows and POC
    rk = np.floor(px / RS).astype(np.int64); kmin = rk.min()
    comb = bar * 1_000_000 + (rk - kmin)
    u, inv = np.unique(comb, return_inverse=True)
    rows = pd.DataFrame({"bar": u // 1_000_000, "row": (u % 1_000_000 + kmin) * RS, "v": np.bincount(inv, weights=sz)})
    poc = rows.sort_values(["bar", "v"]).drop_duplicates("bar", keep="last")
    b["poc"] = np.nan; b.loc[poc.bar.values, "poc"] = poc.row.values + RS / 2
    return b, rows


def build_month(m):
    x = pd.read_parquet(f"scalping_data/nq_trades/{m}.parquet", columns=["ts", "seq", "price", "size", "side", "session", "degraded"])
    x = x[~x.degraded].sort_values(["ts", "seq"], kind="stable").reset_index(drop=True)
    b1, _ = bars_from(x, 60_000_000_000)
    b5, r5 = bars_from(x, 300_000_000_000)
    b1.to_parquet(f"{OUT}/1m_{m}.parquet"); b5.to_parquet(f"{OUT}/5m_{m}.parquet"); r5.to_parquet(f"{OUT}/prof5_{m}.parquet")
    return len(b1), len(b5)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    months = sorted(f[:7] for f in os.listdir("scalping_data/nq_trades") if f.endswith(".parquet"))
    for m in months:
        if os.path.exists(f"{OUT}/prof5_{m}.parquet"): continue
        print(m, build_month(m), flush=True)
