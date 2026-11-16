"""12-month test of G4 position building on the two candidate models; full V1+V4 portfolio report."""
import os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import valentini2 as V
OUT = "scalping_data/batch_g4"; os.makedirs(OUT, exist_ok=True)
CHUNKS = [(None, ["2025-06", "2025-07", "2025-08"]), ("2025-08", ["2025-09", "2025-10", "2025-11"]),
          ("2025-11", ["2025-12", "2026-01", "2026-02"]), ("2026-02", ["2026-03", "2026-04", "2026-05"]), ("2026-05", ["2026-06"])]
F = dict(barKind="range", trig="effort", biasMode="pf", mgmt="his")
CFG = {"V1 all his rules": dict(models=("V1",), **F), "V1 all his rules + G4": dict(models=("V1",), g4=True, **F),
       "V4 range bars": dict(models=("V4",), barKind="range"), "V4 range bars + G4": dict(models=("V4",), barKind="range", g4=True)}
res = {}
for wu, keep in CHUNKS:
    t0 = time.time(); d = V.load(([wu] if wu else []) + keep); pr = V.build(d, bars_kind="range")
    for name, kw in CFG.items():
        t = V.run(d, *pr, **kw)
        if not t.empty: res.setdefault(name, []).append(t[t.session.str[:7].isin(keep)])
    print(f"chunk {keep[0]} done {time.time()-t0:.0f}s", flush=True); del d, pr
T = {k: pd.concat(v, ignore_index=True).sort_values("entry_ts") for k, v in res.items()}
for k, t in T.items(): t.to_csv(f"{OUT}/{''.join(c if c.isalnum() else '_' for c in k)}.csv", index=False)

def report(name, t):
    pos = t.groupby(["entry_ts"]).size()  # placeholder
    base = t[t.leg == 0]
    grp = t.assign(pid=np.where(t.leg == 0, t.entry_ts, np.nan)).sort_values(["exit_ts", "leg"])
    # position P&L = parent + its legs (legs share exit_ts with the parent)
    pp = t.groupby(["exit_ts", "d"]).pnl.sum()
    wins = (pp > 0).mean()
    out = dict(name=name, positions=len(base), add_legs=int((t.leg > 0).sum()), win_rate=round(wins, 3),
               avgR_base=round(base.R.mean(), 3), net_per_NQ=round(t.pnl.sum()),
               PF=round(pp[pp > 0].sum() / -pp[pp <= 0].sum(), 2))
    for eq0 in (100_000, 10_000):
        r, mdd = V.account(t, eq0=eq0); out[f"${eq0//1000}k return %"] = round(r, 1); out[f"${eq0//1000}k maxDD %"] = round(mdd, 1)
    m = t.groupby(t.session.str[:7]).pnl.sum(); out["months +/total"] = f"{(m > 0).sum()}/{len(m)}"
    for pn, (a, b) in {"Jun-Dec25": ("2025-06", "2025-12"), "Jan-Mar26": ("2026-01", "2026-03"), "Apr-Jun26": ("2026-04", "2026-06")}.items():
        x = t[(t.session.str[:7] >= a) & (t.session.str[:7] <= b)]; out[pn + " $"] = round(x.pnl.sum())
    return out
rows = [report(k, t) for k, t in T.items()]
rows.append(report("PORTFOLIO V1(all his) + V4(range)", pd.concat([T["V1 all his rules"], T["V4 range bars"]]).sort_values("entry_ts")))
rows.append(report("PORTFOLIO V1(all his) + V4(range), both + G4", pd.concat([T["V1 all his rules + G4"], T["V4 range bars + G4"]]).sort_values("entry_ts")))
R = pd.DataFrame(rows); R.to_csv(f"{OUT}/summary.csv", index=False)
pd.set_option("display.width", 250); print(R.T.to_string())
P = pd.concat([T["V1 all his rules + G4"], T["V4 range bars + G4"]])
print(P.groupby(P.session.str[:7]).pnl.sum().round().to_string())
