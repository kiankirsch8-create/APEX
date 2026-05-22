"""
APEX v7.3 — Economic calendar intelligence (live feed + historical simulation).

Used by ``apex_trader.py`` (live) and ``continuous_backtester.py`` (chrono / backtest).
Live fetch is fail-open: network errors never block trading.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

_FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# Note: ``ff_calendar_nextweek.json`` returns 404 on the public host; ``thisweek`` JSON
# already spans a rolling multi-day window sufficient for the ±48h filter.

_calendar_cache: list[dict[str, Any]] = []
_calendar_fetched_at: datetime | None = None

_CACHE_TTL = timedelta(hours=1)
_REQUEST_TIMEOUT = 10

# ── Historical simulation (hardcoded central-bank / NFP dates) ──
_FOMC_2024: frozenset[date] = frozenset(
    {
        date(2024, 1, 31),
        date(2024, 3, 20),
        date(2024, 5, 1),
        date(2024, 6, 12),
        date(2024, 7, 31),
        date(2024, 9, 18),
        date(2024, 11, 7),
        date(2024, 12, 18),
    }
)
_FOMC_2025: frozenset[date] = frozenset(
    {
        date(2025, 1, 29),
        date(2025, 3, 19),
        date(2025, 5, 7),
        date(2025, 6, 18),
        date(2025, 7, 30),
        date(2025, 9, 17),
        date(2025, 11, 5),
        date(2025, 12, 17),
    }
)
_ECB_2024: frozenset[date] = frozenset(
    {
        date(2024, 1, 25),
        date(2024, 3, 7),
        date(2024, 4, 11),
        date(2024, 6, 6),
        date(2024, 7, 18),
        date(2024, 9, 12),
        date(2024, 10, 17),
        date(2024, 12, 12),
    }
)
_ECB_2025: frozenset[date] = frozenset(
    {
        date(2025, 1, 30),
        date(2025, 3, 6),
        date(2025, 4, 17),
        date(2025, 6, 5),
        date(2025, 7, 24),
        date(2025, 9, 11),
        date(2025, 10, 30),
        date(2025, 12, 18),
    }
)
_BOE_2024: frozenset[date] = frozenset(
    {
        date(2024, 2, 1),
        date(2024, 3, 21),
        date(2024, 5, 9),
        date(2024, 6, 20),
        date(2024, 8, 1),
        date(2024, 9, 19),
        date(2024, 11, 7),
        date(2024, 12, 19),
    }
)
_BOE_2025: frozenset[date] = frozenset(
    {
        date(2025, 2, 6),
        date(2025, 3, 20),
        date(2025, 5, 8),
        date(2025, 6, 19),
        date(2025, 8, 7),
        date(2025, 9, 18),
        date(2025, 11, 6),
        date(2025, 12, 18),
    }
)
_BOJ_2024: frozenset[date] = frozenset(
    {
        date(2024, 1, 23),
        date(2024, 3, 19),
        date(2024, 4, 26),
        date(2024, 6, 14),
        date(2024, 7, 31),
        date(2024, 9, 20),
        date(2024, 10, 31),
        date(2024, 12, 19),
    }
)
_BOJ_2025: frozenset[date] = frozenset(
    {
        date(2025, 1, 24),
        date(2025, 3, 19),
        date(2025, 4, 30),
        date(2025, 6, 17),
        date(2025, 7, 31),
        date(2025, 9, 22),
        date(2025, 10, 29),
        date(2025, 12, 18),
    }
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pair_currencies(ticker: str) -> list[str]:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return [t[:3], t[3:]]
    return ["USD"]


def _is_first_friday(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def parse_event_datetime(event: dict[str, Any]) -> datetime:
    """
    Parse event time from ForexFactory-style JSON.
    Prefer full ISO ``date``; otherwise combine date + ``time``; tentative / all-day → 12:00 UTC.
    """
    raw_d = event.get("date")
    raw_t = event.get("time")
    d_str = str(raw_d or "").strip()
    t_str = str(raw_t or "").strip()

    if not d_str:
        return datetime.now(timezone.utc)

    if "T" in d_str:
        try:
            dt = datetime.fromisoformat(d_str.replace("Z", "+00:00"))
            return _utc(dt)
        except ValueError:
            pass

    try:
        day = date.fromisoformat(d_str[:10])
    except ValueError:
        return datetime.now(timezone.utc)

    tl = t_str.lower()
    if not t_str or "tentative" in tl or "all day" in tl or tl == "tbd":
        return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)

    hm = _parse_time_hm(t_str)
    if hm is None:
        return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)
    h, m = hm
    return datetime.combine(day, time(h, m), tzinfo=timezone.utc)


def _parse_time_hm(t: str) -> tuple[int, int] | None:
    s = t.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)?$", s)
    if not m:
        m2 = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if not m2:
            return None
        hh, mm = int(m2.group(1)), int(m2.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    ap = m.group(3) or ""
    if ap == "pm" and hh != 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def fetch_calendar() -> list[dict[str, Any]]:
    """
    Fetches this week and next week calendar events from ForexFactory.
    Returns a list of event dicts. Caches result for 1 hour.
    On failure, returns last cached data if any, else [] (fail-open).
    """
    global _calendar_cache, _calendar_fetched_at
    now = datetime.now(timezone.utc)
    if _calendar_fetched_at is not None and (now - _calendar_fetched_at) < _CACHE_TTL:
        return list(_calendar_cache)

    merged: list[dict[str, Any]] = []
    err: str | None = None
    for url in (_FF_THIS_WEEK,):
        try:
            r = requests.get(url, timeout=_REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                merged.extend(data)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            err = str(e)
            logger.warning("calendar fetch failed %s: %s", url, e)

    if err:
        logger.warning("fetch_calendar partial/total failure: %s", err)

    if merged:
        _calendar_cache = merged
        _calendar_fetched_at = now
        return list(_calendar_cache)

    if _calendar_cache:
        logger.warning("fetch_calendar: using stale cache after failure")
        return list(_calendar_cache)

    return []


def get_high_impact_events(currency: str, scan_datetime: datetime) -> list[dict[str, Any]]:
    """
    Returns all HIGH impact events for the given currency within ±48 hours of ``scan_datetime``.
    """
    ccy = (currency or "").strip().upper()
    if not ccy:
        return []

    events = fetch_calendar()
    scan_utc = _utc(scan_datetime)
    result: list[dict[str, Any]] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        imp = str(event.get("impact") or "").strip().lower()
        if imp != "high":
            continue
        country = str(event.get("country") or "").strip().upper()
        if country == "ALL":
            continue
        if country != ccy and not (country == "EU" and ccy == "EUR"):
            continue
        try:
            event_dt = parse_event_datetime(event)
        except Exception:  # noqa: BLE001
            continue
        ev_utc = _utc(event_dt)
        hours_diff = abs((ev_utc - scan_utc).total_seconds() / 3600.0)
        if hours_diff <= 48.0:
            hours_away = (ev_utc - scan_utc).total_seconds() / 3600.0
            result.append(
                {
                    "title": str(event.get("title") or "High impact event"),
                    "currency": ccy,
                    "event_dt": ev_utc,
                    "hours_away": hours_away,
                }
            )
    return result


def check_calendar_risk(ticker: str, scan_datetime: datetime) -> dict[str, Any]:
    """
    Live calendar risk before opening a position. Fail-open when no data.
    """
    currencies = _pair_currencies(ticker)
    all_ev: list[dict[str, Any]] = []
    for ccy in currencies:
        all_ev.extend(get_high_impact_events(ccy, scan_datetime))

    if not all_ev:
        return {
            "action": "CLEAR",
            "reason": "No high-impact events within 48h",
            "size_multiplier": 1.0,
        }

    closest = min(all_ev, key=lambda e: abs(float(e.get("hours_away", 0.0))))
    h = float(closest["hours_away"])
    ah = abs(h)
    title = str(closest.get("title") or "Event")
    currency = str(closest.get("currency") or "")

    def _fmt_hours() -> str:
        if h >= 0:
            return f"in {h:.1f}h"
        return f"{abs(h):.1f}h ago"

    suffix = _fmt_hours()

    if ah <= 4.0:
        return {
            "action": "BLOCK",
            "reason": f"HIGH IMPACT {title} for {currency} {suffix}",
            "size_multiplier": 0.0,
        }
    if ah <= 12.0:
        return {
            "action": "REDUCE",
            "reason": f"HIGH IMPACT {title} for {currency} {suffix} — size 50%",
            "size_multiplier": 0.5,
        }
    if ah <= 24.0:
        return {
            "action": "REDUCE",
            "reason": f"HIGH IMPACT {title} for {currency} {suffix} — size 75%",
            "size_multiplier": 0.75,
        }
    if ah <= 48.0:
        return {
            "action": "WATCH",
            "reason": f"HIGH IMPACT {title} for {currency} {suffix} — monitoring",
            "size_multiplier": 1.0,
        }

    return {
        "action": "CLEAR",
        "reason": "No high-impact events within 48h",
        "size_multiplier": 1.0,
    }


def check_calendar_risk_historical(ticker: str, scan_date: date) -> dict[str, Any]:
    """
    Simulated calendar block for backtests (no live HTTP). NFP + major CB dates only.
    """
    sym = (ticker or "").strip().upper()
    pairs = _pair_currencies(sym)

    reasons: list[str] = []

    if "USD" in pairs and _is_first_friday(scan_date):
        reasons.append("NFP (USD, first Friday — simulated)")

    if "USD" in pairs and (scan_date in _FOMC_2024 or scan_date in _FOMC_2025):
        reasons.append("FOMC decision day (USD — simulated)")

    if "EUR" in pairs and (scan_date in _ECB_2024 or scan_date in _ECB_2025):
        reasons.append("ECB decision day (EUR — simulated)")

    if "GBP" in pairs and (scan_date in _BOE_2024 or scan_date in _BOE_2025):
        reasons.append("BOE decision day (GBP — simulated)")

    if "JPY" in pairs and (scan_date in _BOJ_2024 or scan_date in _BOJ_2025):
        reasons.append("BOJ decision day (JPY — simulated)")

    if not reasons:
        return {
            "action": "CLEAR",
            "reason": "No high-impact events within 48h",
            "size_multiplier": 1.0,
        }

    return {
        "action": "BLOCK",
        "reason": "; ".join(reasons),
        "size_multiplier": 0.0,
    }
