"""Import HistData.com monthly tick zips into the scalping parquet cache.

HistData ships one zip per pair per month (``DAT_ASCII_EURUSD_T_YYYYMM.zip``)
with ``YYYYMMDD HHMMSSmmm,bid,ask,volume`` rows in fixed EST (UTC-5, no
daylight saving). A month file therefore covers UTC
``[YYYY-MM-01 05:00, next-month-01 05:00)``, so the first five UTC hours of
each month live in the previous month's file. Days are merged on write.

Every UTC day a file touches is written, empty when it has no ticks
(weekends), so ``load_market`` finds a file for each day of a block. Volume
is always zero in HistData; it is written as ``bid_volume``/``ask_volume`` 0.

    python -m scalping.data.histdata path/to/DAT_ASCII_EURUSD_T_*.zip
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scalping.data.store import tick_day_path, write_parquet

NAME = re.compile(r"DAT_ASCII_([A-Z]{6})_T_(\d{4})(\d{2})", re.I)
TICK_COLUMNS = ["timestamp", "bid", "ask", "bid_volume", "ask_volume"]


def identify(path: Path) -> tuple[str, int, int]:
    match = NAME.search(path.name)
    if not match:
        raise ValueError(f"not a HistData tick zip name: {path.name}")
    return match.group(1).upper(), int(match.group(2)), int(match.group(3))


def parse_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        raw = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv")))
    sep = ";" if raw[:200].count(b";") > raw[:200].count(b",") else ","
    df = pd.read_csv(
        io.BytesIO(raw), sep=sep, header=None, usecols=[0, 1, 2],
        names=["dt", "bid", "ask"], dtype={"dt": str, "bid": "float64", "ask": "float64"},
    )
    est = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S%f")
    out = pd.DataFrame({
        "timestamp": (est + pd.Timedelta(hours=5)).dt.tz_localize("UTC"),
        "bid": df["bid"],
        "ask": df["ask"],
        "bid_volume": 0.0,
        "ask_volume": 0.0,
    })
    return out.sort_values("timestamp", kind="stable").reset_index(drop=True)


def month_days(year: int, month: int) -> list[date]:
    """UTC days touched by one month file: the 1st through the 1st of next month."""
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return [first + timedelta(days=i) for i in range((nxt - first).days + 1)]


def import_zip(path: Path, data_dir: Path) -> dict:
    symbol, year, month = identify(path)
    ticks = parse_zip(path)
    by_day = {d: g for d, g in ticks.groupby(ticks["timestamp"].dt.date)}
    crossed = int((ticks["ask"] < ticks["bid"]).sum())
    for day in month_days(year, month):
        fresh = by_day.get(day, ticks.iloc[0:0])
        target = tick_day_path(data_dir, symbol, day)
        if target.exists():
            old = pd.read_parquet(target)
            if not old.empty:
                fresh = pd.concat([old, fresh], ignore_index=True)
            fresh = fresh.drop_duplicates().sort_values("timestamp", kind="stable")
        write_parquet(fresh.loc[:, TICK_COLUMNS].reset_index(drop=True), target)
        # a stale bar file would hide newly merged ticks; load_market rebuilds it
        bar = Path(str(target).replace(f"{Path(data_dir)}/ticks/", f"{Path(data_dir)}/bars/", 1))
        if bar.exists():
            bar.unlink()
    return {"symbol": symbol, "month": f"{year}-{month:02d}", "ticks": len(ticks), "crossed": crossed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scalping.data.histdata")
    parser.add_argument("zips", nargs="+", type=Path)
    parser.add_argument("--data-dir", default="scalping_data", type=Path)
    args = parser.parse_args(argv)
    for path in sorted(args.zips, key=lambda p: identify(p)[1:]):
        info = import_zip(path, args.data_dir)
        print(f"{info['symbol']} {info['month']}  {info['ticks']:>11,} ticks"
              + (f"  crossed={info['crossed']}" if info["crossed"] else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
