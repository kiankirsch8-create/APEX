"""
Eagerly warm ``intelligence_cache.db`` for a date range (standalone + API background task).

Does not run trades — only calls cached intelligence helpers so the SQLite cache fills
before or alongside a chrono job.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from utils import DATA_DIR, load_json, log, save_json

from intelligence_fetch_cached import prefetch_macro_calendar_trend_for_day

PREFETCH_FAILURES_PATH = DATA_DIR / "prefetch_failures.json"
_PREFETCH_RATE_LOCK = threading.Lock()
_LAST_PREFETCH_MONO = 0.0


def prefetch_task_path(task_id: str) -> Path:
    return DATA_DIR / f"{task_id}.json"


def get_trading_days(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _throttle_prefetch_call() -> None:
    global _LAST_PREFETCH_MONO
    with _PREFETCH_RATE_LOCK:
        gap = 0.1 - (time.monotonic() - _LAST_PREFETCH_MONO)
        if gap > 0:
            time.sleep(gap)
        _LAST_PREFETCH_MONO = time.monotonic()


def _append_failure(entry: dict[str, Any]) -> None:
    rows = load_json(PREFETCH_FAILURES_PATH, default=[]) or []
    if not isinstance(rows, list):
        rows = []
    rows.append(entry)
    save_json(PREFETCH_FAILURES_PATH, rows)


def prefetch_single(ticker: str, scan_d: date, *, regime_job_id: str | None) -> None:
    _throttle_prefetch_call()
    prefetch_macro_calendar_trend_for_day(ticker, scan_d, regime_job_id=regime_job_id)


def prefetch_date_range(
    start_date: str,
    end_date: str,
    tickers: list[str] | None = None,
    *,
    regime_job_id: str | None = None,
    task_id: str | None = None,
    max_workers: int = 16,
) -> dict[str, Any]:
    """
    Pre-fetch intelligence for ``tickers`` (default: caller should pass CHRONO_TICKERS)
    on each weekday between ``start_date`` and ``end_date`` inclusive.
    """
    s = date.fromisoformat(start_date.strip()[:10])
    e = date.fromisoformat(end_date.strip()[:10])
    days = get_trading_days(s, e)
    syms = [x.strip().upper() for x in (tickers or []) if str(x).strip()]
    if not syms:
        return {"error": "no tickers", "completed": 0, "total": 0}

    total = len(days) * len(syms)
    tid = task_id or f"prefetch_{uuid.uuid4().hex[:10]}"
    state: dict[str, Any] = {
        "task_id": tid,
        "status": "running",
        "completed": 0,
        "total": total,
        "started_at": datetime.now().isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "tickers": len(syms),
    }
    save_json(prefetch_task_path(tid), state)

    tasks: list[tuple[str, date]] = [(tk, d) for d in days for tk in syms]
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 32))) as ex:
            futs = {ex.submit(prefetch_single, tk, d, regime_job_id=regime_job_id): (tk, d) for tk, d in tasks}
            for fut in as_completed(futs):
                tk, d = futs[fut]
                try:
                    fut.result()
                except Exception as err:  # noqa: BLE001
                    log(f"[Prefetch] {tk} {d}: {err}", level="warning")
                    _append_failure(
                        {
                            "ticker": tk,
                            "date": d.isoformat(),
                            "error": str(err),
                            "ts": datetime.now().isoformat(),
                        }
                    )
                done += 1
                if done % 50 == 0 or done == total:
                    state["completed"] = done
                    save_json(prefetch_task_path(tid), state)
    finally:
        state["completed"] = done
        state["status"] = "complete" if done >= total else "failed"
        state["finished_at"] = datetime.now().isoformat()
        save_json(prefetch_task_path(tid), state)

    return {"task_id": tid, "completed": done, "total": total}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Pre-warm intelligence_cache.db for a date range.")
    p.add_argument("start_date", help="YYYY-MM-DD")
    p.add_argument("end_date", help="YYYY-MM-DD")
    p.add_argument("--tickers", nargs="*", help="Forex symbols (default: import CHRONO_TICKERS)")
    p.add_argument("--regime-job-id", default=None, help="Chrono job_id for regime cache keys")
    args = p.parse_args()
    tks = list(args.tickers) if args.tickers else None
    if not tks:
        from continuous_backtester import CHRONO_TICKERS

        tks = list(CHRONO_TICKERS)
    out = prefetch_date_range(
        args.start_date,
        args.end_date,
        tks,
        regime_job_id=args.regime_job_id,
    )
    print(out)


def get_prefetch_progress(task_id: str) -> dict[str, Any] | None:
    p = prefetch_task_path(task_id.strip())
    if not p.is_file():
        return None
    raw = load_json(p, default=None)
    return raw if isinstance(raw, dict) else None


def estimated_minutes_for_prefetch(start_date: str, end_date: str, n_tickers: int) -> float:
    s = date.fromisoformat(start_date.strip()[:10])
    e = date.fromisoformat(end_date.strip()[:10])
    n = len(get_trading_days(s, e)) * max(1, n_tickers)
    return max(1.0, n * 0.5 / 60.0)
