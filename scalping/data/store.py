"""Parquet layout, partitioned by pair and year.

Ticks and M1 bars are written one UTC day per file so a download can stop and
resume without rewriting a whole year:

    {data_dir}/ticks/pair=EURUSD/year=2018/2018-01-03.parquet
    {data_dir}/bars/pair=EURUSD/year=2018/2018-01-03.parquet

The year directory is the partition. Days inside it are the incremental unit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scalping.data.bars import empty_bars, ticks_to_m1
from scalping.market import MarketData, build_market


class DataNotReady(FileNotFoundError):
    """Raised when a block has not been downloaded and materialized yet."""


def tick_day_path(data_dir: Path, symbol: str, day: date) -> Path:
    return (
        Path(data_dir)
        / "ticks"
        / f"pair={symbol.upper()}"
        / f"year={day.year}"
        / f"{day.isoformat()}.parquet"
    )


def bar_day_path(data_dir: Path, symbol: str, day: date) -> Path:
    return (
        Path(data_dir)
        / "bars"
        / f"pair={symbol.upper()}"
        / f"year={day.year}"
        / f"{day.isoformat()}.parquet"
    )


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


def read_parquet(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def iter_days(start: datetime, end: datetime):
    """UTC dates that overlap ``[start, end)``."""
    day = start.astimezone(timezone.utc).date()
    end = end.astimezone(timezone.utc)
    while datetime(day.year, day.month, day.day, tzinfo=timezone.utc) < end:
        yield day
        day += timedelta(days=1)


def missing_days(data_dir: Path, symbol: str, start: datetime, end: datetime, kind: str) -> list[date]:
    path_of = tick_day_path if kind == "ticks" else bar_day_path
    return [day for day in iter_days(start, end) if not path_of(data_dir, symbol, day).exists()]


def load_ticks(data_dir: Path, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    return _load_range(data_dir, symbol, start, end, "ticks")


def load_bars(data_dir: Path, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    return _load_range(data_dir, symbol, start, end, "bars")


def load_market(data_dir: Path, symbol: str, start: datetime, end: datetime) -> MarketData:
    """Load ticks and M1 bars for ``[start, end)``.

    Bars are rebuilt from ticks when the tick day exists and the bar day does
    not, so a tick download is enough to backtest.
    """
    missing = missing_days(data_dir, symbol, start, end, "ticks")
    if missing:
        sample = ", ".join(d.isoformat() for d in missing[:5])
        raise DataNotReady(
            f"No tick parquet for {symbol} ({len(missing)} day(s) missing, e.g. {sample}). "
            "Download on a machine that can reach Dukascopy:\n"
            "  python -m scalping.data.pipeline download"
        )
    ticks = load_ticks(data_dir, symbol, start, end)
    bar_missing = missing_days(data_dir, symbol, start, end, "bars")
    if bar_missing:
        for day in bar_missing:
            day_ticks = _slice_day(ticks, day)
            write_parquet(ticks_to_m1(day_ticks), bar_day_path(data_dir, symbol, day))
    bars = load_bars(data_dir, symbol, start, end)
    if ticks.empty or bars.empty:
        raise DataNotReady(
            f"{symbol} has no ticks between {start.date().isoformat()} and {end.date().isoformat()}"
        )
    return build_market(ticks, bars)


def _load_range(
    data_dir: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    kind: str,
) -> pd.DataFrame:
    path_of = tick_day_path if kind == "ticks" else bar_day_path
    frames: list[pd.DataFrame] = []
    for day in iter_days(start, end):
        path = path_of(data_dir, symbol, day)
        if not path.exists():
            raise DataNotReady(f"Missing {kind} file {path}")
        frame = read_parquet(path)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame() if kind == "ticks" else empty_bars()
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    mask = (out["timestamp"] >= start_ts) & (out["timestamp"] < end_ts)
    out = out.loc[mask].sort_values("timestamp").reset_index(drop=True)
    return out


def _slice_day(ticks: pd.DataFrame, day: date) -> pd.DataFrame:
    if ticks.empty:
        return ticks
    start = pd.Timestamp(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
    end = start + pd.Timedelta(days=1)
    mask = (ticks["timestamp"] >= start) & (ticks["timestamp"] < end)
    return ticks.loc[mask].copy()
