"""
SQLite read-through cache for historical intelligence / Claude outputs (chrono backtest).

- DB path: ``{DATA_DIR}/intelligence_cache.db`` (persistent volume on Railway).
- Dates within the last 7 calendar days are never cached (always fresh).
- Thread-safe: WAL journal + per-thread connections for reads + global write lock.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from utils import DATA_DIR, log

logger = logging.getLogger(__name__)

CACHE_DB_PATH = DATA_DIR / "intelligence_cache.db"

_local = threading.local()
_write_lock = threading.Lock()
_stats_lock = threading.Lock()

# Daily counters for /api/cache/stats (best-effort)
_today_key: str | None = None
_hits_today = 0
_misses_today = 0


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def is_safe_cache_date(d: date | None) -> bool:
    """True if this calendar date may be cached permanently (older than 7 days)."""
    if d is None:
        return False
    return d <= (_utc_today() - timedelta(days=7))


def _get_thread_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(CACHE_DB_PATH),
            timeout=60.0,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return _local.conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intelligence_cache (
            cache_key   TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            cached_at   TEXT NOT NULL,
            api_source  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_stats_daily (
            day TEXT PRIMARY KEY,
            hits INTEGER NOT NULL DEFAULT 0,
            misses INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def init_intelligence_cache_db() -> None:
    """Create tables if missing (idempotent)."""
    with _write_lock:
        conn = _get_thread_connection()
        _init_schema(conn)


def _bump_stat(hit: bool) -> None:
    global _today_key, _hits_today, _misses_today
    day = _utc_today().isoformat()
    with _stats_lock:
        if _today_key != day:
            _today_key = day
            _hits_today = 0
            _misses_today = 0
        if hit:
            _hits_today += 1
        else:
            _misses_today += 1


def get_cached_or_fetch(
    cache_key: str,
    api_source: str,
    is_historical: bool,
    fetch_function: Callable[[], Any],
) -> Any:
    """
    Return cached JSON-compatible value or call ``fetch_function`` and store.

    On corrupt cache rows: delete, refetch, re-store.
    """
    if not is_historical:
        _bump_stat(False)
        return fetch_function()

    init_intelligence_cache_db()
    conn = _get_thread_connection()

    try:
        row = conn.execute(
            "SELECT result_json FROM intelligence_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    except sqlite3.Error as e:
        logger.warning("intelligence_cache read error %s: %s", cache_key, e)
        _bump_stat(False)
        return fetch_function()

    if row is not None:
        try:
            out = json.loads(row[0])
            _bump_stat(True)
            return out
        except json.JSONDecodeError as e:
            logger.warning("intelligence_cache corrupt JSON %s: %s — refetching", cache_key, e)
            try:
                with _write_lock:
                    conn.execute("DELETE FROM intelligence_cache WHERE cache_key = ?", (cache_key,))
                    conn.commit()
            except sqlite3.Error as e2:
                logger.warning("intelligence_cache delete failed: %s", e2)

    _bump_stat(False)
    result = fetch_function()
    try:
        payload = json.dumps(result, default=str)
    except (TypeError, ValueError) as e:
        logger.warning("intelligence_cache cannot serialize %s: %s — not storing", cache_key, e)
        return result

    now = datetime.now(timezone.utc).isoformat()
    try:
        with _write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO intelligence_cache (cache_key, result_json, cached_at, api_source) "
                "VALUES (?, ?, ?, ?)",
                (cache_key, payload, now, api_source),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning("intelligence_cache write error %s: %s", cache_key, e)
    return result


def cache_stats() -> dict[str, Any]:
    """Lightweight stats for GET /api/cache/stats."""
    init_intelligence_cache_db()
    conn = _get_thread_connection()
    total = 0
    oldest = None
    newest = None
    size_mb = 0.0
    try:
        row = conn.execute("SELECT COUNT(*), MIN(cached_at), MAX(cached_at) FROM intelligence_cache").fetchone()
        if row:
            total = int(row[0] or 0)
            o, n = row[1], row[2]
            oldest = str(o)[:10] if o else None
            newest = str(n)[:10] if n else None
    except sqlite3.Error as e:
        logger.warning("cache_stats: %s", e)
    try:
        if CACHE_DB_PATH.is_file():
            size_mb = round(CACHE_DB_PATH.stat().st_size / (1024 * 1024), 2)
    except OSError:
        pass

    global _hits_today, _misses_today
    with _stats_lock:
        h, m = _hits_today, _misses_today
    tot = h + m
    hit_pct = f"{100.0 * h / tot:.1f}%" if tot else "n/a"

    return {
        "total_entries": total,
        "size_mb": size_mb,
        "hit_rate_today": hit_pct,
        "api_calls_saved_today": max(0, h),
        "oldest_entry": oldest,
        "newest_entry": newest,
    }


def clear_all_cache(*, confirm: bool = False) -> int:
    """Delete all rows. ``confirm`` must be True when called from API guard."""
    if not confirm:
        return 0
    init_intelligence_cache_db()
    with _write_lock:
        conn = _get_thread_connection()
        cur = conn.execute("DELETE FROM intelligence_cache")
        conn.commit()
        try:
            n = int(cur.rowcount) if cur.rowcount is not None else 0
        except Exception:  # noqa: BLE001
            n = 0
    return n


def close_thread_connection() -> None:
    """Optional cleanup for worker threads (tests)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
