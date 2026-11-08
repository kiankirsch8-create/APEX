"""Incremental Dukascopy download.

Default backend ``bi5`` fetches the public hourly ``.bi5`` files (bid, ask,
volume). A file that already exists is never requested again, including hours
the feed returns empty. HTTP uses ``dukascopy_python.create_session`` when that
library is installed. The URL itself is the one ``duka`` builds when ``duka``
is installed.

Dukascopy answers a busy client with 503s and read timeouts. Those are not
fatal: each hour is retried with exponential backoff and jitter, and an hour
that still fails is recorded and skipped. The next run requests only the hours
that are not already in the cache, so the gaps fill in.

Backend ``library`` calls ``dukascopy_python.fetch_ticks`` instead. That path
is the JSON feed (shorter ranges). Its parquet days are cached the same way.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, replace
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

# Statuses Dukascopy uses when it is throttling or briefly unhealthy.
# 404 is not in this set: an empty hour is a successful response.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

_RETRYABLE_NAMES = frozenset(
    {
        "MaxRetryError",
        "ReadTimeout",
        "ReadTimeoutError",
        "ConnectTimeout",
        "ConnectTimeoutError",
        "Timeout",
        "TimeoutError",
        "RetryError",
        "ConnectionError",
        "ProtocolError",
        "ChunkedEncodingError",
        "TransientFetchError",
    }
)


@dataclass(frozen=True)
class FetchPolicy:
    """How politely, and how patiently, to ask Dukascopy for each hour.

    ``request_delay`` is the pause between hourly requests. ``max_retries`` is
    how many extra attempts follow the first one after a 503 or a read
    timeout. The wait before retry ``n`` (0-based) is ``min(max_backoff,
    2**n)`` seconds, multiplied by a jitter factor in ``[0.5, 1]``.
    """

    request_delay: float = 0.3
    max_retries: int = 6
    max_backoff: float = 60.0
    initial_backoff: float = 1.0

    def __post_init__(self) -> None:
        if self.request_delay < 0:
            raise ValueError("request_delay must be >= 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.max_backoff <= 0:
            raise ValueError("max_backoff must be > 0")
        if self.initial_backoff <= 0:
            raise ValueError("initial_backoff must be > 0")


class TransientFetchError(Exception):
    """Dukascopy throttled this hour or the socket timed out. Safe to retry."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class HourFailed(Exception):
    """The hour was given up after backoff. The run continues with the next hour."""

    def __init__(self, error: BaseException):
        super().__init__(str(error))
        self.error = error


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
    missed_hours: list[str] = field(default_factory=list)


def iter_hours(start: datetime, end: datetime, *, skip_saturday: bool = True):
    """UTC hours in ``[start, end)``. Saturday is closed and skipped by default."""
    cur = _floor_hour(_as_utc(start))
    end = _as_utc(end)
    while cur < end:
        if not (skip_saturday and cur.weekday() == 5):
            yield cur
        cur += timedelta(hours=1)


def open_session():
    """HTTP session with no hidden retries.

    Prefers ``dukascopy-python`` when it imports. Status retries stay off:
    urllib3's own retry raises ``MaxRetryError`` and aborts the hour before
    this module can back off. 503s and read timeouts are retried in
    :func:`fetch_with_backoff`.
    """
    try:
        import dukascopy_python as dk

        return dk.create_session(retries=0), "dukascopy-python"
    except Exception:
        import requests

        return requests.Session(), "requests"


def is_retryable(exc: BaseException) -> bool:
    """True for 503s, read timeouts, and the MaxRetryError those used to raise."""
    if isinstance(exc, TransientFetchError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return True
    if type(exc).__name__ in _RETRYABLE_NAMES:
        return True
    text = str(exc).lower()
    if "read timed out" in text or "read timeout" in text or "max retries" in text:
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc and is_retryable(cause):
        return True
    return False


def backoff_seconds(
    attempt: int,
    *,
    initial: float = 1.0,
    max_backoff: float = 60.0,
    uniform=None,
) -> float:
    """Wait before retry ``attempt`` (0 → ~1s, 1 → ~2s, 2 → ~4s, capped).

    Jitter draws a factor in ``[0.5, 1]`` so the wait never exceeds the
    schedule. Pass ``uniform`` (same signature as ``random.uniform``) to pin it.
    """
    raw = min(float(max_backoff), float(initial) * (2 ** int(attempt)))
    if uniform is None:
        factor = random.uniform(0.5, 1.0)
    else:
        factor = float(uniform(0.5, 1.0))
    return raw * factor


def fetch_with_backoff(call, policy: FetchPolicy, *, sleep=time.sleep, uniform=None):
    """Call ``call`` until it returns or the retry budget is spent.

    A non-retryable error, or a retryable error after ``max_retries``, raises
    :class:`HourFailed` instead of escaping as a raw HTTP error.
    """
    last: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return call()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            last = exc
            if not is_retryable(exc) or attempt >= policy.max_retries:
                raise HourFailed(exc) from exc
            wait = backoff_seconds(
                attempt,
                initial=policy.initial_backoff,
                max_backoff=policy.max_backoff,
                uniform=uniform,
            )
            log.warning(
                "Dukascopy throttled this hour (%s); retry %s/%s in %.1fs",
                exc,
                attempt + 1,
                policy.max_retries,
                wait,
            )
            if wait > 0:
                sleep(wait)
    raise HourFailed(last or TransientFetchError("hour failed"))


def run_exit_code(reports: list[DownloadReport]) -> int:
    """Non-zero only when the run downloaded nothing and hours were missed.

    A partial download (some hours saved, some missed) exits 0 so a caller can
    keep the bytes and re-run for the gaps. A cache-only run with no misses
    also exits 0.
    """
    fetched = sum(report.fetched_hours for report in reports)
    missed = sum(len(report.missed_hours) for report in reports)
    if missed and fetched == 0:
        return 1
    return 0


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
    """GET one hour once across the known hosts.

    Empty body and 404 are valid (no ticks) and return ``b""``. A 503 or a
    read timeout raises :class:`TransientFetchError` so the caller can back
    off and try the same hour again. Nothing is written here.
    """
    headers = {"User-Agent": _UA}
    urls = [resolve_bi5_url(symbol, hour)]
    for host in BI5_HOSTS:
        alt = bi5_url(symbol, hour, host=host)
        if alt not in urls:
            urls.append(alt)
    last_error: BaseException | None = None
    for url in urls:
        try:
            response = session.get(url, headers=headers, timeout=45)
        except Exception as exc:
            if is_retryable(exc):
                last_error = TransientFetchError(str(exc))
                continue
            raise
        if response.status_code == 404:
            return b""
        if response.status_code == 200:
            return response.content or b""
        if response.status_code in TRANSIENT_STATUS:
            last_error = TransientFetchError(
                f"{response.status_code} for {url}",
                status=response.status_code,
            )
            continue
        last_error = TransientFetchError(
            f"{response.status_code} for {url}",
            status=response.status_code,
        )
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
    policy: FetchPolicy | None = None,
    request_delay: float | None = None,
    max_retries: int | None = None,
    max_backoff: float | None = None,
    sleep=time.sleep,
    uniform=None,
) -> DownloadReport:
    """Cache every hour in the window, then write tick and M1 parquet days.

    ``fetcher(symbol, hour) -> bytes`` replaces HTTP. Tests use it. Production
    leaves it ``None``. An hour that still fails after backoff is listed on
    ``report.missed_hours`` and is not cached, so the next run tries it again.
    Hours already on disk are not requested.
    """
    symbol = symbol.upper()
    get_instrument(symbol)  # fail fast on an unknown pair
    data_dir = Path(data_dir)
    policy = _policy(policy, request_delay, max_retries, max_backoff)
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
    requested = False
    for hour in hours:
        path = data_dir / cache_relpath(symbol, hour)
        if path.exists():
            report.cached_hours += 1
            if path.stat().st_size == 0:
                report.empty_hours += 1
            continue
        if requested and policy.request_delay > 0:
            sleep(policy.request_delay)
        requested = True
        try:
            payload = fetch_with_backoff(
                lambda hour=hour: fetcher(symbol, hour),
                policy,
                sleep=sleep,
                uniform=uniform,
            )
        except HourFailed as exc:
            key = _hour_key(hour)
            report.missed_hours.append(key)
            log.error("%s %s failed after backoff: %s", symbol, key, exc.error)
            continue
        if payload is None:
            payload = b""
        _atomic_bytes(path, payload)
        report.fetched_hours += 1
        if len(payload) == 0:
            report.empty_hours += 1
        if report.fetched_hours and report.fetched_hours % 500 == 0:
            log.info("%s fetched %s hours", symbol, report.fetched_hours)

    if report.missed_hours:
        report.notes.append(f"missed {len(report.missed_hours)} hour(s)")
    _materialize_days(symbol, start, end, data_dir, report, skip_saturday=skip_saturday)
    return report


def _policy(policy, request_delay, max_retries, max_backoff) -> FetchPolicy:
    current = policy or FetchPolicy()
    updates = {}
    if request_delay is not None:
        updates["request_delay"] = request_delay
    if max_retries is not None:
        updates["max_retries"] = max_retries
    if max_backoff is not None:
        updates["max_backoff"] = max_backoff
    if not updates:
        return current
    return replace(current, **updates)


def _hour_key(hour: datetime) -> str:
    return _as_utc(hour).strftime("%Y-%m-%dT%H:%M:%SZ")


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
