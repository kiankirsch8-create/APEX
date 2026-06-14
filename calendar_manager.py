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

# ── CB meeting calendar (Layer 2 CB-CALENDAR boost + historical sim) ──


def _cb_date_tuple(d: date) -> tuple[int, int, int]:
    return (d.year, d.month, d.day)


def _merge_cb_meeting_dates(
    *frozen_sets: frozenset[date],
    extra: tuple[tuple[int, int, int], ...] = (),
) -> tuple[tuple[int, int, int], ...]:
    seen: set[tuple[int, int, int]] = set()
    for fs in frozen_sets:
        for d in fs:
            seen.add(_cb_date_tuple(d))
    seen.update(extra)
    return tuple(sorted(seen))


# Central bank meeting schedule — extend quarterly from official CB calendars.
CB_MEETING_CALENDAR: dict[str, tuple[tuple[int, int, int], ...]] = {
    "FED": _merge_cb_meeting_dates(
        _FOMC_2024,
        _FOMC_2025,
        extra=(
            # 2020
            (2020, 1, 29),
            (2020, 3, 15),
            (2020, 4, 29),
            (2020, 6, 10),
            (2020, 7, 29),
            (2020, 9, 16),
            (2020, 11, 5),
            (2020, 12, 16),
            # 2021
            (2021, 1, 27),
            (2021, 3, 17),
            (2021, 4, 28),
            (2021, 6, 16),
            (2021, 7, 28),
            (2021, 9, 22),
            (2021, 11, 3),
            (2021, 12, 15),
            # 2022
            (2022, 2, 2),
            (2022, 3, 16),
            (2022, 5, 4),
            (2022, 6, 15),
            (2022, 7, 27),
            (2022, 9, 21),
            (2022, 11, 2),
            (2022, 12, 14),
            # 2023
            (2023, 2, 1),
            (2023, 3, 22),
            (2023, 5, 3),
            (2023, 6, 14),
            (2023, 7, 26),
            (2023, 9, 20),
            (2023, 11, 1),
            (2023, 12, 13),
            # 2026 (Fed published schedule)
            (2026, 1, 28),
            (2026, 3, 18),
            (2026, 5, 6),
            (2026, 6, 17),
            (2026, 7, 29),
            (2026, 9, 16),
            (2026, 11, 4),
            (2026, 12, 16),
        ),
    ),
    "BOJ": _merge_cb_meeting_dates(
        _BOJ_2024,
        _BOJ_2025,
        extra=(
            (2020, 1, 21),
            (2020, 3, 16),
            (2020, 4, 27),
            (2020, 6, 16),
            (2020, 7, 15),
            (2020, 9, 17),
            (2020, 10, 29),
            (2020, 12, 18),
            (2021, 1, 19),
            (2021, 3, 18),
            (2021, 4, 27),
            (2021, 6, 17),
            (2021, 7, 15),
            (2021, 9, 21),
            (2021, 10, 27),
            (2021, 12, 16),
            (2022, 1, 18),
            (2022, 3, 17),
            (2022, 4, 27),
            (2022, 6, 16),
            (2022, 7, 20),
            (2022, 9, 21),
            (2022, 10, 27),
            (2022, 12, 19),
            (2023, 1, 17),
            (2023, 3, 9),
            (2023, 4, 27),
            (2023, 6, 15),
            (2023, 7, 30),
            (2023, 9, 21),
            (2023, 10, 30),
            (2023, 12, 18),
        ),
    ),
    "ECB": _merge_cb_meeting_dates(
        _ECB_2024,
        _ECB_2025,
        extra=(
            (2020, 1, 23),
            (2020, 3, 12),
            (2020, 4, 30),
            (2020, 6, 4),
            (2020, 7, 16),
            (2020, 9, 10),
            (2020, 10, 29),
            (2020, 12, 10),
            (2021, 1, 21),
            (2021, 3, 11),
            (2021, 4, 22),
            (2021, 6, 10),
            (2021, 7, 22),
            (2021, 9, 9),
            (2021, 10, 28),
            (2021, 12, 16),
            (2022, 2, 3),
            (2022, 3, 10),
            (2022, 4, 14),
            (2022, 6, 9),
            (2022, 7, 21),
            (2022, 9, 8),
            (2022, 10, 27),
            (2022, 12, 15),
            (2023, 2, 2),
            (2023, 3, 16),
            (2023, 5, 4),
            (2023, 6, 15),
            (2023, 7, 27),
            (2023, 9, 14),
            (2023, 10, 26),
            (2023, 12, 14),
        ),
    ),
    "BOE": _merge_cb_meeting_dates(
        _BOE_2024,
        _BOE_2025,
        extra=(
            (2020, 1, 30),
            (2020, 3, 11),
            (2020, 5, 7),
            (2020, 6, 18),
            (2020, 8, 6),
            (2020, 9, 17),
            (2020, 11, 5),
            (2020, 12, 17),
            (2021, 2, 4),
            (2021, 3, 18),
            (2021, 5, 6),
            (2021, 6, 24),
            (2021, 8, 5),
            (2021, 9, 23),
            (2021, 11, 4),
            (2021, 12, 16),
            (2022, 2, 3),
            (2022, 3, 17),
            (2022, 5, 5),
            (2022, 6, 16),
            (2022, 8, 4),
            (2022, 9, 22),
            (2022, 11, 3),
            (2022, 12, 15),
            (2023, 2, 2),
            (2023, 3, 23),
            (2023, 5, 11),
            (2023, 6, 22),
            (2023, 8, 3),
            (2023, 9, 21),
            (2023, 11, 2),
            (2023, 12, 14),
        ),
    ),
    "RBA": (
        (2020, 2, 4),
        (2020, 3, 3),
        (2020, 4, 7),
        (2020, 5, 5),
        (2020, 6, 2),
        (2020, 7, 7),
        (2020, 8, 4),
        (2020, 9, 1),
        (2020, 10, 6),
        (2020, 11, 3),
        (2020, 12, 1),
        (2021, 2, 2),
        (2021, 3, 2),
        (2021, 4, 6),
        (2021, 5, 4),
        (2021, 6, 1),
        (2021, 7, 6),
        (2021, 8, 3),
        (2021, 9, 7),
        (2021, 10, 5),
        (2021, 11, 2),
        (2021, 12, 7),
        (2022, 2, 1),
        (2022, 3, 1),
        (2022, 4, 5),
        (2022, 5, 3),
        (2022, 6, 7),
        (2022, 7, 5),
        (2022, 8, 2),
        (2022, 9, 6),
        (2022, 10, 4),
        (2022, 11, 1),
        (2022, 12, 6),
        (2023, 2, 7),
        (2023, 3, 7),
        (2023, 4, 4),
        (2023, 5, 2),
        (2023, 6, 6),
        (2023, 7, 4),
        (2023, 8, 1),
        (2023, 9, 5),
        (2023, 10, 3),
        (2023, 11, 7),
        (2023, 12, 5),
        (2024, 2, 6),
        (2024, 3, 19),
        (2024, 5, 7),
        (2024, 6, 18),
        (2024, 8, 6),
        (2024, 9, 24),
        (2024, 11, 5),
        (2024, 12, 10),
        (2025, 2, 18),
        (2025, 4, 1),
        (2025, 5, 20),
        (2025, 7, 8),
        (2025, 8, 12),
        (2025, 9, 30),
        (2025, 11, 4),
        (2025, 12, 9),
    ),
    "RBNZ": (
        (2020, 2, 12),
        (2020, 5, 13),
        (2020, 8, 12),
        (2020, 11, 11),
        (2021, 2, 24),
        (2021, 5, 26),
        (2021, 8, 18),
        (2021, 11, 24),
        (2022, 2, 23),
        (2022, 5, 25),
        (2022, 8, 17),
        (2022, 11, 23),
        (2023, 2, 22),
        (2023, 5, 24),
        (2023, 7, 12),
        (2023, 11, 29),
        (2024, 2, 28),
        (2024, 5, 22),
        (2024, 8, 14),
        (2024, 11, 27),
        (2025, 2, 19),
        (2025, 5, 28),
        (2025, 8, 20),
        (2025, 11, 26),
    ),
    "BOC": (
        (2020, 1, 22),
        (2020, 3, 4),
        (2020, 4, 15),
        (2020, 6, 3),
        (2020, 7, 15),
        (2020, 9, 9),
        (2020, 10, 28),
        (2020, 12, 9),
        (2021, 1, 20),
        (2021, 3, 10),
        (2021, 4, 21),
        (2021, 6, 9),
        (2021, 7, 14),
        (2021, 9, 8),
        (2021, 10, 27),
        (2021, 12, 8),
        (2022, 1, 26),
        (2022, 3, 2),
        (2022, 4, 13),
        (2022, 6, 1),
        (2022, 7, 13),
        (2022, 9, 7),
        (2022, 10, 26),
        (2022, 12, 7),
        (2023, 1, 25),
        (2023, 3, 8),
        (2023, 4, 12),
        (2023, 6, 7),
        (2023, 7, 12),
        (2023, 9, 6),
        (2023, 10, 25),
        (2023, 12, 6),
        (2024, 1, 24),
        (2024, 3, 6),
        (2024, 4, 10),
        (2024, 6, 5),
        (2024, 7, 24),
        (2024, 9, 4),
        (2024, 10, 23),
        (2024, 12, 11),
        (2025, 1, 29),
        (2025, 3, 12),
        (2025, 4, 16),
        (2025, 6, 4),
        (2025, 7, 30),
        (2025, 9, 17),
        (2025, 10, 29),
        (2025, 12, 10),
    ),
}

# Which central bank is relevant for each pair (high-rate / policy-driver side).
TICKER_CB_MAP: dict[str, list[str]] = {
    "USDJPY": ["FED"],
    "GBPUSD": ["BOE"],
    "EURUSD": ["ECB"],
    "AUDUSD": ["RBA"],
    "NZDUSD": ["RBNZ"],
    "USDCHF": ["FED"],
    "USDNOK": ["FED"],
    "USDSEK": ["FED"],
    "GBPJPY": ["BOE", "FED"],
    "AUDJPY": ["RBA"],
    "CADJPY": ["BOC"],
    "NZDJPY": ["RBNZ"],
    "CHFJPY": ["BOJ"],
    "NZDCAD": ["RBNZ", "BOC"],
    "EURCHF": ["ECB"],
}


def get_cb_calendar_boost(ticker: str, current_date: date | str, macro_bias: str) -> int:
    """
    Returns +1 if a relevant central bank meeting is within the pre-meeting window
    (1 day before through 3 days ahead) and STRONG_TAILWIND is already active.
    """
    if str(macro_bias or "").strip().upper() != "STRONG_TAILWIND":
        return 0

    sym = (ticker or "").strip().upper()
    relevant_cbs = TICKER_CB_MAP.get(sym, [])
    if not relevant_cbs:
        return 0

    if isinstance(current_date, date):
        ref = current_date
    else:
        try:
            ref = date.fromisoformat(str(current_date).strip()[:10])
        except (TypeError, ValueError):
            return 0

    window_start = ref - timedelta(days=1)
    window_end = ref + timedelta(days=3)

    for cb in relevant_cbs:
        for y, m, d in CB_MEETING_CALENDAR.get(cb, ()):
            meeting_date = date(y, m, d)
            if window_start <= meeting_date <= window_end:
                return 1

    return 0


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
