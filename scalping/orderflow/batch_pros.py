"""Run TRULLAS-1/2, CREAMER-1, WILLIAMS-1 on Jun 2025 - Jun 2026 NQ; 5 risk levels. Writes scalping_data/batch_pros/."""
import os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import pros as P

OUT = "scalping_data/batch_pros"; os.makedirs(OUT, exist_ok=True)
t0 = time.time()
b1 = P.load("1m"); b5 = P.load("5m")
parts = []; off = 0
for m in P.MONTHS:
    pr = pd.read_parquet(f"{P.NB}/prof5_{m}.parquet"); n = len(pd.read_parquet(f"{P.NB}/5m_{m}.parquet", columns=["o"]))
    pr["bar"] += off; off += n; parts.append(pr)
prof5 = pd.concat(parts, ignore_index=True)
X = P.Exec(b1)
print(f"loaded {len(b1)} 1m / {len(b5)} 5m bars in {time.time()-t0:.0f}s", flush=True)

RUNS = {
    "TRULLAS-1 main (1m, 38.2%)": lambda: P.trullas1(b1, X),
    "TRULLAS-1 target 50%": lambda: P.trullas1(b1, X, tgt_fib=0.5),
    "TRULLAS-1 target 61.8%": lambda: P.trullas1(b1, X, tgt_fib=0.618),
    "TRULLAS-1 fast swings (3-bar, 38.2%)": lambda: P.trullas1(b1, X, piv_k=1),
    "TRULLAS-1 fast swings (3-bar, 61.8%)": lambda: P.trullas1(b1, X, piv_k=1, tgt_fib=0.618),
    "TRULLAS-1 5m bars (38.2%)": lambda: P.trullas1(b1, X, tf="5m"),
    "TRULLAS-1 5m bars (61.8%)": lambda: P.trullas1(b1, X, tf="5m", tgt_fib=0.618),
    "TRULLAS-1 no trend filter": lambda: P.trullas1(b1, X, trend=False),
    "TRULLAS-2 sweep (50%)": lambda: P.trullas2(b1, X),
    "TRULLAS-2 sweep (66%)": lambda: P.trullas2(b1, X, tgt_fib=0.66),
    "CREAMER-1 reversal": lambda: P.creamer(b1, b5, prof5, X, mode="reversal"),
    "CREAMER-1 reversal loose (any opposite delta)": lambda: P.creamer(b1, b5, prof5, X, mode="reversal", effK=0.0),
    "CREAMER-1 reversal loosest (+ no VA / POC rule)": lambda: P.creamer(b1, b5, prof5, X, mode="reversal", effK=0.0, needVA=False, needPOC=False),
    "CREAMER-1 continuation": lambda: P.creamer(b1, b5, prof5, X, mode="continuation"),
    "WILLIAMS-1 close-open (down close)": lambda: P.williams(b1, X, "closeopen"),
    "WILLIAMS-1 close-open (up close, control)": lambda: P.williams(b1, X, "closeopen_up"),
    "WILLIAMS-1 day of week (Mon/Tue/Thu)": lambda: P.williams(b1, X, "dow"),
    "WILLIAMS-1 control: buy every open": lambda: P.williams(b1, X, "everyday"),
    "WILLIAMS-1 blastoff": lambda: P.williams(b1, X, "blastoff"),
    "WILLIAMS-1 expansion hold-to-close": lambda: P.williams(b1, X, "expansion"),
}
only = sys.argv[1:]
rows = []; res = {}
for name, fn in RUNS.items():
    if only and not any(o in name for o in only): continue
    t1 = time.time(); t = fn(); res[name] = t
    tag = "".join(c if c.isalnum() else "_" for c in name)
    if not t.empty: t.drop(columns=["kx"]).to_csv(f"{OUT}/{tag}.csv", index=False)
    rows.append(P.summarize(name, t))
    if name.startswith("TRULLAS-1 main") and not t.empty:
        rows.append(P.summarize("TRULLAS-1 main, confluence sizing (1-4)", t, conf=True))
    print(f"{name}: {len(t)} trades, {time.time()-t1:.0f}s", flush=True)
    # breakdown by kind
    if not t.empty and t.kind.nunique() > 1:
        for k, g in t.groupby("kind"):
            print(f"    {k:28s} n={len(g):4d} avgR={g.R.mean():+.3f} win={(g.pnl>0).mean()*100:.0f}% net=${g.pnl.sum():+,.0f}")
    if name.startswith("TRULLAS-1 main") and not t.empty:
        for k, g in t.groupby("conf"):
            print(f"    confluence {k}: n={len(g):4d} avgR={g.R.mean():+.3f} win={(g.pnl>0).mean()*100:.0f}%")
S = pd.DataFrame(rows)
S.to_csv(f"{OUT}/summary{'_' + '_'.join(only) if only else ''}.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print(S.to_string(index=False))
