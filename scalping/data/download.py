"""Incremental Dukascopy download.

Default backend ``bi5`` fetches the public hourly ``.bi5`` files (bid, ask,
volume). A file that already exists is never requested again, including hours
the feed returns empty. HTTP uses ``dukascopy_python.create_session`` when that
library is installed, so retries match the library. The URL itself is the one
``duka`` builds when ``duka`` is installed.

Backend ``library`` calls ``dukascopy_python.fetch_ticks`` instead. That path
is the JSON feed (shorter ranges). Its parquet days are cached the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scalping.data.bars import ticks_to_m1
from scalping.data.bi5 import BI5_HOSTS, bi5_url, cache_relpath, decode_bi5
from scalping.data.store import bar_day_path, iter_days, tick_day_path, write_parquet
from scalping.instruments import get_instrument

log = logging.getLogger("scalping.data")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class DownloadReport:
    symbol: str
    fetched_hours: int = 0
    cached_hours: int = 0
    empty_hours: int = 0
    tick_days_written: int = 0
    bar_days_written: int = 0
    backend: str = "bi5"
    notes: list[str] = field(default_factory=list)


def iter_hours(start: datetime, end: datetime, *, skip_saturday: bool = True):
    """UTC hours in ``[start, end)``. Saturday is closed and skipped by default."""
    cur = _floor_hour(_as_utc(start))
    end = _as_utc(end)
    while cur < end:
        if not (skip_saturday and cur.weekday() == 5):
            yield cur
        cur += timedelta(hours=1)


def open_session():
    """Retrying HTTP session. Prefers ``dukascopy-python`` when it imports."""
    try:
        import dukascopy_python as dk

        return dk.create_session(retries=5), "dukascopy-python"
    except Exception:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session, "requests"


def resolve_bi5_url(symbol: str, hour: datetime) -> str:
    """Use ``duka``'s URL template when that library is installed."""
    hour = _as_utc(hour)
    try:
        from duka.core.fetch import URL  # type: ignore

        return URL.format(
            currency=symbol.upper(),
            year=hour.year,
            month=hour.month - 1,
            day=hour.day,
            hour=hour.hour,
        )
    except Exception:
        return bi5_url(symbol, hour)


def fetch_bi5_http(symbol: str, hour: datetime, session) -> bytes:
    """GET one hour. Empty body and 404 are valid (no ticks)."""
    import requests

    headers = {"User-Agent": _UA}
    urls = [resolve_bi5_url(symbol, hour)]
    for host in BI5_HOSTS:
        alt = bi5_url(symbol, hour, host=host)
        if alt not in urls:
            urls.append(alt)
    last_error: Exception | None = None
    for url in urls:
        try:
            response = session.get(url, headers=headers, timeout=45)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if response.status_code == 404:
            return b""
        if response.status_code != 200:
            last_error = requests.HTTPError(f"{response.status_code} for {url}")
            continue
        return response.content or b""
    if last_error is not None:
        raise last_error
    return b""


def sync_symbol(
    symbol: str,
    start: datetime,
    end: datetime,
    data_dir: Path,
    *,
    backend: str = "bi5",
    fetcher=None,
    skip_saturday: bool = True,
    session=None,
) -> DownloadReport:
    """Cache every hour in the window, then write tick and M1 parquet days.

    ``fetcher(symbol, hour) -> bytes`` replaces HTTP. Tests use it. Production
    leaves it ``None``.
    """
    symbol = symbol.upper()
    get_instrument(symbol)  # fail fast on an unknown pair
    data_dir = Path(data_dir)
    report = DownloadReport(symbol=symbol, backend=backend)
    if backend == "library":
        _sync_library(symbol, start, end, data_dir, report)
        return report
    if backend != "bi5":
        raise ValueError(f"Unknown backend {backend!r}. Use 'bi5' or 'library'.")

    if fetcher is None:
        if session is None:
            session, session_name = open_session()
        else:
            session_name = "provided"
        report.notes.append(f"http_session={session_name}")

        def fetcher(sym: str, hour: datetime) -> bytes:
            return fetch_bi5_http(sym, hour, session)

    hours = list(iter_hours(start, end, skip_saturday=skip_saturday))
    for hour in hours:
        path = data_dir / cache_relpath(symbol, hour)
        if path.exists():
            report.cached_hours += 1
            if path.stat().st_size == 0:
                report.empty_hours += 1
            continue
        payload = fetcher(symbol, hour)
        if payload is None:
            payload = b""
        _atomic_bytes(path, payload)
        report.fetched_hours += 1
        if len(payload) == 0:
            report.empty_hours += 1
        if report.fetched_hours and report.fetched_hours % 500 == 0:
            log.info("%s fetched %s hours", symbol, report.fetched_hours)

    _materialize_days(symbol, start, end, data_dir, report, skip_saturday=skip_saturday)
    return report


def _materialize_days(
    symbol: str,
    start: datetime,
    end: datetime,
    data_dir: Path,
    report: DownloadReport,
    *,
    skip_saturday: bool,
) -> None:
    for day in iter_days(start, end):
        tick_path = tick_day_path(data_dir, symbol, day)
        bar_path = bar_day_path(data_dir, symbol, day)
        if tick_path.exists() and bar_path.exists():
            continue
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        if day_start.weekday() == 5 and skip_saturday:
            frames = []
        else:
            frames = []
            complete = True
            for hour in iter_hours(day_start, day_start + timedelta(days=1), skip_saturday=False):
                if skip_saturday and hour.weekday() == 5:
                    continue
                path = data_dir / cache_relpath(symbol, hour)
                if not path.exists():
                    complete = False
                    break
                payload = path.read_bytes()
                if payload:
                    frames.append(decode_bi5(payload, hour, symbol))
            if not complete:
                report.notes.append(f"incomplete {day.isoformat()}")
                continue
        if frames:
            ticks = pd.concat(frames, ignore_index=True)
            ticks = ticks.sort_values("timestamp").reset_index(drop=True)
        else:
            ticks = decode_bi5(b"", day_start, symbol)
        if not tick_path.exists():
            write_parquet(ticks, tick_path)
            report.tick_days_written += 1
        if not bar_path.exists():
            write_parquet(ticks_to_m1(ticks), bar_path)
            report.bar_days_written += 1


def _sync_library(symbol: str, start: datetime, end: datetime, data_dir: Path, report: DownloadReport) -> None:
    """Fetch via ``dukascopy_python.fetch_ticks`` and cache parquet days."""
    import dukascopy_python as dk

    start = _as_utc(start)
    end = _as_utc(end)
    missing = []
    for day in iter_days(start, end):
        if not tick_day_path(data_dir, symbol, day).exists():
            missing.append(day)
    if not missing:
        report.notes.append("library cache hit")
        report.cached_hours = len(list(iter_hours(start, end)))
        _materialize_bars_only(symbol, start, end, data_dir, report)
        return
    code = _library_code(symbol)
    ticks = list(dk.fetch_ticks(code, start, end, version=1))
    frame = _ticks_to_frame(ticks)
    report.fetched_hours = len(list(iter_hours(start, end)))
    for day in iter_days(start, end):
        day_start = pd.Timestamp(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
        day_end = day_start + pd.Timedelta(days=1)
        if frame.empty:
            chunk = frame
        else:
            mask = (frame["timestamp"] >= day_start) & (frame["timestamp"] < day_end)
            chunk = frame.loc[mask].reset_index(drop=True)
        path = tick_day_path(data_dir, symbol, day)
        if not path.exists():
            write_parquet(chunk, path)
            report.tick_days_written += 1
        bar_path = bar_day_path(data_dir, symbol, day)
        if not bar_path.exists():
            write_parquet(ticks_to_m1(chunk), bar_path)
            report.bar_days_written += 1


def _materialize_bars_only(symbol, start, end, data_dir, report) -> None:
    from scalping.data.store import read_parquet

    for day in iter_days(start, end):
        bar_path = bar_day_path(data_dir, symbol, day)
        if bar_path.exists():
            continue
        ticks = read_parquet(tick_day_path(data_dir, symbol, day))
        write_parquet(ticks_to_m1(ticks), bar_path)
        report.bar_days_written += 1


def _library_code(symbol: str) -> str:
    from dukascopy_python.instruments import I_EUR_USD, I_GBP_USD, I_USD_JPY

    return {"EURUSD": I_EUR_USD, "GBPUSD": I_GBP_USD, "USDJPY": I_USD_JPY}[symbol]


def _ticks_to_frame(ticks) -> pd.DataFrame:
    if not ticks:
        return decode_bi5(b"", datetime(1970, 1, 1, tzinfo=timezone.utc), "EURUSD")
    rows = []
    for tick in ticks:
        rows.append(
            {
                "timestamp": pd.Timestamp(tick.timestamp).tz_convert("UTC")
                if pd.Timestamp(tick.timestamp).tzinfo
                else pd.Timestamp(tick.timestamp).tz_localize("UTC"),
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "bid_volume": float(getattr(tick, "bid_volume", 0.0) or 0.0),
                "ask_volume": float(getattr(tick, "ask_volume", 0.0) or 0.0),
            }
        )
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp").reset_index(drop=True)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_bytes(payload)
    tmp.replace(path)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)
