"""Valentini V1/V4 (+G4) on the added months Dec 2024 - May 2025 (same configs as batch_g4.py). Writes scalping_data/batch_g4_ext/."""
import os, sys, time
import pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import valentini2 as V
OUT = "scalping_data/batch_g4_ext"; os.makedirs(OUT, exist_ok=True)
CHUNKS = [(None, ["2024-12", "2025-01", "2025-02"]), ("2025-02", ["2025-03", "2025-04", "2025-05"])]
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
for k, v in res.items():
    pd.concat(v, ignore_index=True).sort_values("entry_ts").to_csv(f"{OUT}/{''.join(c if c.isalnum() else '_' for c in k)}.csv", index=False)
