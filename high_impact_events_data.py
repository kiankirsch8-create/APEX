"""
Static high-impact economic event calendar for chrono backtests (2021-2026).

Meeting dates reuse ``calendar_manager.CB_MEETING_CALENDAR`` where available.
CPI / flash CPI use scheduled release-day approximations (day precision only).
NFP is the first Friday of each month (exact).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from calendar_manager import CB_MEETING_CALENDAR

EventTuple = tuple[str, str, str]  # (iso_date, currency, event_type)

_RANGE_START = date(2021, 1, 1)
_RANGE_END = date(2026, 12, 31)

_CB_CURRENCY_TYPE: dict[str, tuple[str, str]] = {
    "FED": ("USD", "FOMC"),
    "ECB": ("EUR", "ECB"),
    "BOE": ("GBP", "BOE"),
    "BOJ": ("JPY", "BOJ"),
    "RBA": ("AUD", "RBA"),
    "RBNZ": ("NZD", "RBNZ"),
    "BOC": ("CAD", "BOC"),
}

# SNB quarterly policy assessments (approximate scheduled dates).
_SNB_DECISIONS: tuple[tuple[int, int, int], ...] = (
    # 2021
    (2021, 3, 25),
    (2021, 6, 17),
    (2021, 9, 23),
    (2021, 12, 16),
    # 2022
    (2022, 3, 24),
    (2022, 6, 16),
    (2022, 9, 22),
    (2022, 12, 15),
    # 2023
    (2023, 3, 23),
    (2023, 6, 22),
    (2023, 9, 21),
    (2023, 12, 14),
    # 2024
    (2024, 3, 21),
    (2024, 6, 20),
    (2024, 9, 26),
    (2024, 12, 12),
    # 2025
    (2025, 3, 20),
    (2025, 6, 19),
    (2025, 9, 25),
    (2025, 12, 11),
    # 2026 (approximate — SNB publishes quarterly)
    (2026, 3, 19),
    (2026, 6, 18),
    (2026, 9, 24),
    (2026, 12, 10),
)

# 2026 central-bank meetings not yet in calendar_manager (approximate schedules).
_CB_2026_EXTRA: dict[str, tuple[tuple[int, int, int], ...]] = {
    "ECB": (
        (2026, 1, 22),
        (2026, 3, 12),
        (2026, 4, 16),
        (2026, 6, 11),
        (2026, 7, 23),
        (2026, 9, 10),
        (2026, 10, 29),
        (2026, 12, 17),
    ),
    "BOE": (
        (2026, 2, 5),
        (2026, 3, 19),
        (2026, 5, 7),
        (2026, 6, 18),
        (2026, 8, 6),
        (2026, 9, 17),
        (2026, 11, 5),
        (2026, 12, 17),
    ),
    "BOJ": (
        (2026, 1, 23),
        (2026, 3, 19),
        (2026, 4, 28),
        (2026, 6, 16),
        (2026, 7, 31),
        (2026, 9, 22),
        (2026, 10, 30),
        (2026, 12, 18),
    ),
    "RBA": (
        (2026, 2, 3),
        (2026, 3, 17),
        (2026, 5, 5),
        (2026, 6, 16),
        (2026, 8, 4),
        (2026, 9, 29),
        (2026, 11, 3),
        (2026, 12, 8),
    ),
    "RBNZ": (
        (2026, 2, 18),
        (2026, 5, 27),
        (2026, 8, 19),
        (2026, 11, 25),
    ),
    "BOC": (
        (2026, 1, 28),
        (2026, 3, 11),
        (2026, 4, 15),
        (2026, 6, 3),
        (2026, 7, 29),
        (2026, 9, 16),
        (2026, 10, 28),
        (2026, 12, 9),
    ),
}


def _in_range(d: date) -> bool:
    return _RANGE_START <= d <= _RANGE_END


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _adjust_to_weekday(d: date, *, direction: int = 1) -> date:
    """Shift off weekends (direction +1 forward, -1 backward)."""
    while d.weekday() >= 5:
        d += timedelta(days=direction)
    return d


def _approx_us_cpi(year: int, month: int) -> date:
    """US CPI release day (~13th, approximate)."""
    return _adjust_to_weekday(date(year, month, 13), direction=1)


def _approx_uk_cpi(year: int, month: int) -> date:
    """UK CPI release day (~18th, approximate)."""
    return _adjust_to_weekday(date(year, month, 18), direction=-1)


def _approx_eu_flash_cpi(year: int, month: int) -> date:
    """Euro-area flash CPI (~last business day of month, approximate)."""
    last_day = monthrange(year, month)[1]
    d = date(year, month, last_day)
    return _adjust_to_weekday(d, direction=-1)


def _build_high_impact_events() -> list[EventTuple]:
    events: list[EventTuple] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(d: date, currency: str, event_type: str) -> None:
        if not _in_range(d):
            return
        key = (d.isoformat(), currency, event_type)
        if key in seen:
            return
        seen.add(key)
        events.append(key)

    for cb_key, tuples in CB_MEETING_CALENDAR.items():
        cur, etype = _CB_CURRENCY_TYPE.get(cb_key, ("", ""))
        if not cur:
            continue
        for y, m, day in tuples:
            _add(date(y, m, day), cur, etype)

    for cb_key, tuples in _CB_2026_EXTRA.items():
        cur, etype = _CB_CURRENCY_TYPE[cb_key]
        for y, m, day in tuples:
            _add(date(y, m, day), cur, etype)

    for y, m, day in _SNB_DECISIONS:
        _add(date(y, m, day), "CHF", "SNB")

    for year in range(2021, 2027):
        for month in range(1, 13):
            _add(_first_friday(year, month), "USD", "NFP")
            _add(_approx_us_cpi(year, month), "USD", "CPI")
            _add(_approx_uk_cpi(year, month), "GBP", "CPI")
            _add(_approx_eu_flash_cpi(year, month), "EUR", "FLASH_CPI")

    events.sort(key=lambda x: (x[0], x[1], x[2]))
    return events


# Module-level table consumed by continuous_backtester.py
HIGH_IMPACT_EVENTS: list[EventTuple] = _build_high_impact_events()
