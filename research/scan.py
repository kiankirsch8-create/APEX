"""Fast signal scanner: "is there any edge here at all?" in seconds, before writing a strategy.

Write a hypothesis file, e.g. research/scans/b001_london_sweep.py:

    FAMILY = "B001"            # ledger idea id this scan belongs to
    TF = 5                     # bar size in minutes (1, 5, 15, 60 ...)
    HORIZONS = [3, 6, 12, 24]  # forward horizons in bars of TF
    COOLDOWN = 12              # bars to skip after an event (no overlapping, clustered events)

    def signal_sweep_fade(df):          # any name starting with "signal_"
        # df: one row per completed TF bar, columns described below. Use only rows <= t
        # (rolling/shift(+k) are fine; shift(-k) is lookahead and is detected).
        # Return a Series of -1 / 0 / +1 aligned with df: the direction you expect AFTER bar t closes.
        ...

    python -m research.scan research/scans/b001_london_sweep.py

Columns: open high low close (mid), spread_close, spread_high_raw (raw, pre-broker-floor), tick_count,
upticks, downticks, london_minute, ny_minute (DST-aware minute of day at bar open), weekday (UTC, Mon=0),
news_next_min / news_last_min (minutes to next / since last high-impact event: NFP, CPI, FOMC, ECB),
year, date (UTC date).

Entry = next bar's open mid; result = signed mid move in pips, BEFORE costs. Compare with the cost line:
Capital.com EURUSD is about 1.05-1.1 pips round trip, so a scan is only worth turning into a strategy when the
best horizon clears about 1.4 pips with t >= 3 AND holds in both halves (2018-19 and 2020-21).
Scans are exploration, not evidence. They are logged (kind "scan") so the number of looks is visible.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))

from research import ledger  # noqa: E402

PIP = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "AUDUSD": 1e-4, "USDJPY": 1e-2, "XAUUSD": 0.1}
BUILD_START, BUILD_END = date(2018, 1, 1), date(2022, 1, 1)
COST_LINE = 1.1
FEATURES = ["open", "high", "low", "close", "spread_close", "spread_high_raw", "tick_count", "upticks",
            "downticks", "london_minute", "ny_minute", "weekday", "news_next_min", "news_last_min"]


def _feature_path(data_dir: Path, pair: str, day: date) -> Path:
    return data_dir / "features" / f"pair={pair}" / f"year={day.year}" / f"{day.isoformat()}.parquet"


def build_features(pair: str = "EURUSD", start: date = BUILD_START, end: date = BUILD_END,
                   data_dir: Path = REPO / "scalping_data") -> pd.DataFrame:
    """M1 feature frame for [start, end), cached per day under scalping_data/features/."""
    from scalping.data.bars import ticks_to_m1
    from scalping.data.store import tick_day_path
    from scalping.market import build_market

    frames = []
    day = start
    while day < end:
        cache = _feature_path(data_dir, pair, day)
        if cache.exists():
            frames.append(pd.read_parquet(cache))
        else:
            tick_file = tick_day_path(data_dir, pair, day)
            if tick_file.exists():
                ticks = pd.read_parquet(tick_file)
                if not ticks.empty:
                    ticks["timestamp"] = pd.to_datetime(ticks["timestamp"], utc=True)
                    m = build_market(ticks, ticks_to_m1(ticks))
                    f = pd.DataFrame({k: getattr(m, k) for k in FEATURES})
                    f.insert(0, "time", pd.DatetimeIndex(m.bar_time.astype("datetime64[ns]")).tz_localize("UTC"))
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    f.to_parquet(cache, index=False)
                    frames.append(f)
        day += timedelta(days=1)
    if not frames:
        raise SystemExit(f"no tick data for {pair} {start}..{end}: run research/bootstrap.sh first")
    df = pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    return df


def to_tf(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate M1 features to TF bars, clock-aligned like strategies.timeframe (decision at bar close)."""
    if minutes == 1:
        out = m1.copy()
    else:
        key = m1["time"].dt.floor(f"{minutes}min")
        g = m1.groupby(key, sort=True)
        out = pd.DataFrame({
            "time": g["time"].first(), "open": g["open"].first(), "high": g["high"].max(),
            "low": g["low"].min(), "close": g["close"].last(), "spread_close": g["spread_close"].last(),
            "spread_high_raw": g["spread_high_raw"].max(), "tick_count": g["tick_count"].sum(),
            "upticks": g["upticks"].sum(), "downticks": g["downticks"].sum(),
            "london_minute": g["london_minute"].first(), "ny_minute": g["ny_minute"].first(),
            "weekday": g["weekday"].first(), "news_next_min": g["news_next_min"].last(),
            "news_last_min": g["news_last_min"].last(),
        }).reset_index(drop=True)
    out["year"] = out["time"].dt.year
    out["date"] = out["time"].dt.date
    return out


def _lookahead_check(fn, df: pd.DataFrame, full: pd.Series) -> str | None:
    """Re-run the signal on truncated history; any change in past signals means it used future rows."""
    # A legitimate signal at row t uses rows <= t only, so it must be identical when everything after
    # the cut is removed, INCLUDING the last kept row (that is where shift(-k) leaks show up).
    n = len(df)
    for frac in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9):
        cut = int(n * frac)
        part = np.sign(pd.Series(fn(df.iloc[:cut].copy()), index=df.index[:cut]).fillna(0).to_numpy())
        head = np.sign(full.iloc[:cut].to_numpy())
        if not np.array_equal(part, head):
            bad = int((part != head).sum())
            return f"LOOKAHEAD: {bad} signals changed when rows after them were removed (cut at {frac:.0%})"
    return None


def _stats(x: np.ndarray) -> dict:
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}
    sd = float(np.std(x, ddof=1)) if n > 1 else 0.0
    return {"n": n, "mean": round(float(np.mean(x)), 3),
            "t": round(float(np.mean(x) / (sd / math.sqrt(n))), 2) if sd > 0 else 0.0,
            "hit": round(float(np.mean(x > 0)), 3)}


def session_of(london_minute: pd.Series) -> pd.Series:
    h = (london_minute // 60).astype(int)
    return pd.Series(np.select([(h >= 22) | (h < 7), h < 12, h < 16], ["asia", "london", "overlap"], "ny"),
                     index=london_minute.index)


def scan_signal(df: pd.DataFrame, sig: pd.Series, horizons: list[int], cooldown: int, pip: float) -> dict:
    sig = sig.fillna(0).astype(float)
    idx = np.flatnonzero(sig.to_numpy() != 0)
    keep, last = [], -10**9
    for i in idx:                      # cooldown: drop clustered events
        if i - last > cooldown and i + 1 < len(df):
            keep.append(i)
            last = i
    keep = np.array(keep, dtype=int)
    out = {"events": int(len(keep)), "horizons": {}}
    if len(keep) == 0:
        return out
    direction = np.sign(sig.to_numpy()[keep])
    entry = df["open"].to_numpy()[keep + 1]
    close, high, low = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    years = df["year"].to_numpy()[keep]
    sessions = session_of(df["london_minute"]).to_numpy()[keep]
    for h in horizons:
        end = np.minimum(keep + h, len(df) - 1)
        move = direction * (close[end] - entry) / pip
        # max favourable / adverse excursion from entry over bars (t+1 .. t+h), in pips
        mfe = np.zeros(len(keep))
        mae = np.zeros(len(keep))
        for j in range(len(keep)):
            lo, hi = keep[j] + 1, end[j] + 1
            if hi <= lo:
                continue
            up, down = high[lo:hi].max() - entry[j], entry[j] - low[lo:hi].min()
            mfe[j], mae[j] = (up, down) if direction[j] > 0 else (down, up)
        mfe, mae = mfe / pip, mae / pip
        first_half = years <= 2019
        out["horizons"][str(h)] = {
            **_stats(move),
            "mfe_mean": round(float(mfe.mean()), 2), "mae_mean": round(float(mae.mean()), 2),
            "split_2018_19": _stats(move[first_half]), "split_2020_21": _stats(move[~first_half]),
            "by_year": {str(y): _stats(move[years == y]) for y in sorted(set(years))},
            "by_session": {s: _stats(move[sessions == s]) for s in ("asia", "london", "overlap", "ny")},
            "by_side": {"long": _stats(move[direction > 0]), "short": _stats(move[direction < 0])},
        }
    return out


def verdict(res: dict) -> str:
    best = None
    for h, r in res.get("horizons", {}).items():
        if best is None or r["mean"] > best[1]["mean"]:
            best = (h, r)
    if best is None:
        return "no events"
    h, r = best
    both = r["split_2018_19"]["mean"] > 0 and r["split_2020_21"]["mean"] > 0
    if r["mean"] >= 1.4 and r["t"] >= 3 and both:
        return f"PROMOTE (h={h}: {r['mean']:+.2f} pips, t {r['t']}, both halves positive)"
    if r["mean"] > 0.5 and r["t"] >= 2:
        return f"LEAD? (h={h}: {r['mean']:+.2f} pips, t {r['t']}; below 1.4 pip / t 3 bar or split fails)"
    return f"DROP (best h={h}: {r['mean']:+.2f} pips, t {r['t']})"


def run(path: Path, pair: str = "EURUSD") -> int:
    spec = importlib.util.spec_from_file_location(f"scan_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    family = getattr(mod, "FAMILY", None)
    if family and family not in ledger.ideas():
        raise SystemExit(f"FAMILY {family} is not a registered idea: run `python -m research.ledger idea ...` first")
    tf, horizons, cooldown = int(getattr(mod, "TF", 5)), list(getattr(mod, "HORIZONS", [3, 6, 12, 24])), \
        int(getattr(mod, "COOLDOWN", 12))
    pair = getattr(mod, "PAIR", pair)
    df = to_tf(build_features(pair), tf)
    results = {}
    for name in sorted(n for n in vars(mod) if n.startswith("signal_")):
        fn = getattr(mod, name)
        sig = pd.Series(fn(df.copy()), index=df.index).fillna(0)
        problem = _lookahead_check(fn, df, sig)
        res = scan_signal(df, sig, horizons, cooldown, PIP[pair]) if problem is None else {"events": 0}
        res["lookahead"] = problem
        res["verdict"] = problem or verdict(res)
        results[name] = res
        print(f"\n== {name}  [{pair} TF={tf}m cooldown={cooldown}]  events={res['events']}  ->  {res['verdict']}")
        if problem:
            continue
        print(f"   {'h':>4} {'n':>6} {'mean':>7} {'t':>6} {'hit':>5} {'MFE':>6} {'MAE':>6} {'18-19':>7} {'20-21':>7}  by year")
        for h, r in res["horizons"].items():
            yrs = " ".join(f"{v['mean']:+.2f}" for v in r["by_year"].values())
            print(f"   {h:>4} {r['n']:>6} {r['mean']:+7.2f} {r['t']:6.2f} {r['hit']:5.2f} {r['mfe_mean']:6.2f} "
                  f"{r['mae_mean']:6.2f} {r['split_2018_19']['mean']:+7.2f} {r['split_2020_21']['mean']:+7.2f}  {yrs}")
    out = ROOT / "scans" / "results" / f"{path.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"file": str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path),
                               "pair": pair, "tf": tf, "horizons": horizons, "cooldown": cooldown,
                               "results": results}, indent=1, default=str) + "\n")
    for name, res in results.items():
        ledger.append({"kind": "scan", "idea": family, "signal": name, "pair": pair, "tf": tf,
                       "events": res["events"], "verdict": res["verdict"]})
    print(f"\nwrote {out}  (cost line about {COST_LINE} pips round trip; promote only at >= 1.4 pips, t >= 3, both halves)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(64)
    sys.exit(run(Path(sys.argv[1]).resolve(), *(sys.argv[2:3])))
