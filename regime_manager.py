"""
APEX v7.3 — Rolling win-rate regime adjuster (Prompt 2).

Reads recent completed trades via ``load_all_results()`` (lazy import to avoid
cycles). Optional ``as_of_date`` scopes history for chronological backtests.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_regime_cache: dict[str, dict[str, Any]] = {}
_regime_cache_time: dict[str, datetime] = {}
_regime_lock = Lock()
_CACHE_TTL = timedelta(minutes=30)


def _load_results_rows() -> list[dict[str, Any]]:
    try:
        from continuous_backtester import load_all_results

        raw = load_all_results()
    except ImportError:
        logger.warning("regime_manager: continuous_backtester not importable — no trade history")
        return []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _job_matches(row: dict[str, Any], job_id: str) -> bool:
    jid = str(row.get("job_id") or "").strip()
    want = str(job_id or "").strip()
    if jid == want:
        return True
    if want in ("rolling", "backtest", "default") and jid == "":
        return True
    return False


def get_recent_trades(n: int, job_id: str, *, as_of_date: date | None = None) -> list[dict[str, Any]]:
    """
    Last ``n`` completed WIN/LOSS rows for ``job_id``, most recent first.
    When ``as_of_date`` is set, excludes rows dated after that calendar day (chrono-safe).
    """
    rows = _load_results_rows()
    out: list[dict[str, Any]] = []
    as_s = as_of_date.isoformat() if as_of_date is not None else None
    for r in reversed(rows):
        oc = str(r.get("outcome", "") or "").strip().upper()
        if oc not in ("WIN", "LOSS"):
            continue
        if r.get("skipped") or r.get("skip_trade"):
            continue
        if not _job_matches(r, job_id):
            continue
        ds = str(r.get("date", "") or "")[:10]
        if as_s is not None and ds and ds > as_s:
            continue
        out.append(r)
        if len(out) >= n:
            break
    return out


def calculate_rolling_winrate(trades: list[dict[str, Any]]) -> float:
    if not trades or len(trades) < 5:
        return 0.5
    wins = sum(1 for t in trades if str(t.get("outcome", "")).strip().upper() == "WIN")
    return max(0.0, min(1.0, wins / float(len(trades))))


def calculate_rolling_pnl(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    s = 0.0
    for t in trades:
        try:
            s += float(t.get("pnl_dollars", 0) or 0)
        except (TypeError, ValueError):
            continue
    return s


def _neutral_regime(note: str) -> dict[str, Any]:
    return {
        "regime": "NORMAL",
        "size_multiplier": 1.0,
        "wr_10": 0.5,
        "wr_20": 0.5,
        "consecutive_losses": 0,
        "pnl_10": 0.0,
        "note": note,
    }


def get_regime(job_id: str, *, as_of_date: date | None = None) -> dict[str, Any]:
    """Classify rolling performance into CRISIS … PEAK tiers (most severe wins first on drawdowns)."""
    all_completed: list[dict[str, Any]] = []
    rows = _load_results_rows()
    as_s = as_of_date.isoformat() if as_of_date is not None else None
    for r in rows:
        oc = str(r.get("outcome", "") or "").strip().upper()
        if oc not in ("WIN", "LOSS"):
            continue
        if r.get("skipped") or r.get("skip_trade"):
            continue
        if not _job_matches(r, job_id):
            continue
        ds = str(r.get("date", "") or "")[:10]
        if as_s is not None and ds and ds > as_s:
            continue
        all_completed.append(r)

    if len(all_completed) < 5:
        return _neutral_regime("NORMAL: insufficient trade history (<5 completed) — neutral sizing")

    recent_10 = get_recent_trades(10, job_id, as_of_date=as_of_date)
    recent_20 = get_recent_trades(20, job_id, as_of_date=as_of_date)

    wr_10 = calculate_rolling_winrate(recent_10)
    wr_20 = calculate_rolling_winrate(recent_20)
    pnl_10 = calculate_rolling_pnl(recent_10)

    consecutive_losses = 0
    for trade in recent_10:
        if str(trade.get("outcome", "")).strip().upper() == "LOSS":
            consecutive_losses += 1
        else:
            break

    base = {
        "wr_10": wr_10,
        "wr_20": wr_20,
        "consecutive_losses": consecutive_losses,
        "pnl_10": pnl_10,
    }

    if consecutive_losses >= 6 or (wr_10 < 0.25 and pnl_10 < -400.0):
        return {
            "regime": "CRISIS",
            "size_multiplier": 0.25,
            **base,
            "note": "CRISIS: consecutive losses or severe drawdown — 25% size",
        }
    if (
        consecutive_losses >= 4
        or (wr_10 < 0.35 and wr_20 < 0.40)
        or pnl_10 < -250.0
    ):
        return {
            "regime": "DEFENSIVE",
            "size_multiplier": 0.50,
            **base,
            "note": "DEFENSIVE: losing streak detected — 50% size",
        }
    if (
        consecutive_losses >= 2
        or (wr_10 < 0.45 and wr_20 < 0.45)
        or pnl_10 < -100.0
    ):
        return {
            "regime": "CAUTION",
            "size_multiplier": 0.75,
            **base,
            "note": "CAUTION: slightly below average — 75% size",
        }

    # Upside tiers: evaluate strongest first so hot streaks do not get stuck at NORMAL.
    if (
        wr_10 >= 0.70
        and wr_20 >= 0.65
        and consecutive_losses == 0
        and pnl_10 > 500.0
    ):
        return {
            "regime": "PEAK",
            "size_multiplier": 1.50,
            **base,
            "note": "PEAK: exceptional performance — 150% size",
        }
    if (
        wr_10 >= 0.60
        and wr_20 >= 0.55
        and consecutive_losses == 0
        and pnl_10 > 200.0
    ):
        return {
            "regime": "AGGRESSIVE",
            "size_multiplier": 1.25,
            **base,
            "note": "AGGRESSIVE: system on hot streak — 125% size",
        }
    if (
        wr_10 >= 0.45
        and wr_20 >= 0.40
        and consecutive_losses < 2
        and pnl_10 >= -100.0
    ):
        return {
            "regime": "NORMAL",
            "size_multiplier": 1.0,
            **base,
            "note": "NORMAL: system performing as expected — 100% size",
        }

    return {
        "regime": "NORMAL",
        "size_multiplier": 1.0,
        **base,
        "note": "NORMAL: default bucket — 100% size",
    }


def get_regime_cached(job_id: str, *, as_of_date: date | None = None) -> dict[str, Any]:
    """
    Cached ``get_regime`` (30 min) for live / repeated scans.
    Historical chrono passes ``as_of_date`` — each date has its own cache entry.
    """
    key = f"{job_id}::{as_of_date.isoformat() if as_of_date else '__live__'}"
    now = datetime.now(timezone.utc)
    with _regime_lock:
        ts = _regime_cache_time.get(key)
        if ts is not None and (now - ts) < _CACHE_TTL and key in _regime_cache:
            return copy.deepcopy(_regime_cache[key])
        reg = get_regime(job_id, as_of_date=as_of_date)
        _regime_cache[key] = copy.deepcopy(reg)
        _regime_cache_time[key] = now
        return copy.deepcopy(reg)
