"""12-month batch: every model alone + all together, six rule sets, Jun 2025 - Jun 2026 (Databento NQ trades).
Runs in 4-month chunks (1 warm-up month + 3 kept months) to fit in memory. Writes scalping_data/batch12/."""
import os, sys, time, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import valentini2 as V

OUT = "scalping_data/batch12"; os.makedirs(OUT, exist_ok=True)
CHUNKS = [(None, ["2025-06", "2025-07", "2025-08"]), ("2025-08", ["2025-09", "2025-10", "2025-11"]),
          ("2025-11", ["2025-12", "2026-01", "2026-02"]), ("2026-02", ["2026-03", "2026-04", "2026-05"]),
          ("2026-05", ["2026-06"])]
SETS = {  # name: (bars, overrides)
    "A baseline":         ("1m", dict()),
    "B effort triggers":  ("1m", dict(trig="effort")),
    "C profile bias":     ("1m", dict(biasMode="pf")),
    "D his management":   ("1m", dict(mgmt="his")),
    "E 40-tick range":    ("range", dict(barKind="range")),
    "F all his rules":    ("range", dict(barKind="range", trig="effort", biasMode="pf", mgmt="his")),
}
MODELS = {"V1": dict(models=("V1",)), "V1 DeltaPrint": dict(models=("V1",), v1loc="dprint"), "V2": dict(models=("V2",)),
          "V3": dict(models=("V3",)), "V4": dict(models=("V4",)), "V7 IVB": dict(models=("V7",)),
          "V7 IVB no filter": dict(models=("V7",), v7filter=False), "V7 IVB pullback": dict(models=("V7",), v7mode="pullback"),
          "V8 value migration": dict(models=("V8",)), "ALL": dict(models=("V1", "V2", "V3", "V4", "V7", "V8"))}

res = {}
for wu, keep in CHUNKS:
    months = ([wu] if wu else []) + keep
    t0 = time.time(); d = V.load(months)
    built = {}
    for sname, (kind, ov) in SETS.items():
        if kind not in built: built[kind] = V.build(d, bars_kind=kind)
        for mname, mk in MODELS.items():
            t = V.run(d, *built[kind], **{**ov, **mk})
            if not t.empty:
                t = t[t.session.str[:7].isin(keep)]
                res.setdefault((sname, mname), []).append(t)
    print(f"chunk {keep[0]}..{keep[-1]} done in {time.time()-t0:.0f}s", flush=True)
    del d, built

rows = []
PER = {"research Jun-Dec 25": ("2025-06", "2025-12"), "validation Jan-Mar 26": ("2026-01", "2026-03"), "holdout Apr-Jun 26": ("2026-04", "2026-06")}
for (sname, mname), parts in res.items():
    t = pd.concat(parts, ignore_index=True).sort_values("entry_ts")
    t.to_csv(f"{OUT}/{sname[0]}_{''.join(c if c.isalnum() else '_' for c in mname)}.csv", index=False)
    w = t[t.pnl > 0]; gl = -t[t.pnl <= 0].pnl.sum()
    acc, mdd = V.account(t)
    r = dict(set=sname, model=mname, trades=len(t), win=round(len(w) / len(t), 3), avgR=round(t.R.mean(), 3),
             t=round(t.R.mean() / (t.R.std(ddof=1) / np.sqrt(len(t))), 2) if len(t) > 2 else np.nan,
             net=round(t.pnl.sum()), PF=round(w.pnl.sum() / gl, 2) if gl else np.inf,
             maxDD=round((t.pnl.cumsum() - t.pnl.cumsum().cummax()).min()), acct=round(acc, 1), acctDD=round(mdd, 1))
    for pn, (a, b) in PER.items():
        tp = t[(t.session.str[:7] >= a) & (t.session.str[:7] <= b)]
        r[pn + " n"] = len(tp); r[pn + " R"] = round(tp.R.mean(), 2) if len(tp) else np.nan
    rows.append(r)
S = pd.DataFrame(rows).sort_values(["set", "model"])
S.to_csv(f"{OUT}/summary.csv", index=False)
print(S.to_string(index=False))
