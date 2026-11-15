"""Build a clean front-month NQ trade file from Databento GLBX.MDP3 trades (DBN, zstd).

Input : monthly files glbx-mdp3-YYYYMMDD-YYYYMMDD.trades.dbn.zst (parent NQ.FUT, all expiries + spreads)
Output: scalping_data/nq_trades/YYYY-MM.parquet, one row per trade of the front-month outright.

Rules (all mechanical, no strategy content):
- Outrights only: symbols without '-' (spreads such as NQM5-NQU5 have synthetic prices).
- Session date = CME trading day: Chicago time + 7h, so the 17:00 CT open belongs to the next day.
- Front month per session date = the outright with the highest traded volume that day.
- side: Databento aggressor side. 'B' = buyer aggressor (+1), 'A' = seller aggressor (-1), 'N' = unknown (0).
- Degraded days from condition.json are flagged (degraded=True), not dropped.
"""
import glob, json, os, sys
import numpy as np, pandas as pd, databento as db

FILES = sys.argv[1].split(",")   # one or more DBN files (comma-separated)
COND = sys.argv[2] if len(sys.argv) > 2 else None
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "scalping_data", "nq_trades")
os.makedirs(OUT, exist_ok=True)

degraded = set()
if COND:
    degraded = {c["date"] for c in json.load(open(COND)) if c["condition"] != "available"}

SIDE = {"B": 1, "A": -1, "N": 0}
COLS = ["ts_event", "ts_recv", "sequence", "price", "size", "side", "symbol"]
for path in FILES:
    parts = []  # read in chunks so a large month fits in memory; drop spreads per chunk
    for ch in db.DBNStore.from_file(path).to_df(map_symbols=True, count=1_500_000):
        ch = ch.reset_index()
        parts.append(ch.loc[~ch["symbol"].str.contains("-"), COLS])
    df = pd.concat(parts, ignore_index=True); del parts
    ct = df["ts_event"].dt.tz_convert("America/Chicago")
    df["session"] = (ct + pd.to_timedelta(7, unit="h")).dt.date.astype(str)
    vol = df.groupby(["session", "symbol"])["size"].sum().reset_index()
    front = vol.loc[vol.groupby("session")["size"].idxmax(), ["session", "symbol"]]
    df = df.merge(front, on=["session", "symbol"])
    out = pd.DataFrame({
        "ts": df["ts_event"].astype("int64"),          # exchange time, ns UTC
        "ts_recv": df["ts_recv"].astype("int64"),
        "seq": df["sequence"].astype("int64"),
        "price": df["price"].astype("float64"),
        "size": df["size"].astype("int32"),
        "side": df["side"].map(SIDE).astype("int8"),
        "symbol": df["symbol"].astype("category"),
        "session": df["session"].astype("category"),
    }).sort_values(["ts", "seq"], kind="stable")
    out["degraded"] = out["session"].astype(str).isin(degraded)
    month = os.path.basename(path).split("glbx-mdp3-")[1][:6]
    out.to_parquet(os.path.join(OUT, f"{month[:4]}-{month[4:]}.parquet"), index=False)
    print(month, len(out), "trades", flush=True)
