"""High-impact economic calendar (US NFP, US CPI, FOMC, ECB), 2018-2024.

Source data: ``econ_calendar.csv`` next to this file. Times are stored in the
publisher's local time zone (New York / Frankfurt) and converted to UTC here,
so daylight saving is handled. Dates: BLS release archives (NFP, CPI), Federal
Reserve FOMC calendars, ECB press releases (decision time 13:45 CET until
2022-06-09, 14:15 CET from 2022-07-21). Unscheduled 2020 emergency actions included.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

CSV = Path(__file__).with_name("econ_calendar.csv")


@lru_cache(maxsize=1)
def events() -> pd.DataFrame:
    raw = pd.read_csv(CSV, dtype=str)
    utc = [pd.Timestamp(f"{d} {t}").tz_localize(tz).tz_convert("UTC")
           for d, t, tz in zip(raw["date"], raw["local_time"], raw["tz"])]
    out = raw.assign(utc=pd.DatetimeIndex(utc)).sort_values("utc").reset_index(drop=True)
    return out


@lru_cache(maxsize=8)
def _times(kinds: tuple[str, ...] | None) -> np.ndarray:
    ev = events()
    if kinds:
        ev = ev[ev["event"].isin(kinds)]
    return ev["utc"].astype("int64").to_numpy()


def news_distances(t_ns: np.ndarray, kinds: tuple[str, ...] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Minutes to the next event at or after ``t_ns`` and since the last event before it (inf if none)."""
    ev = _times(kinds)
    t = np.asarray(t_ns, dtype=np.int64)
    idx = np.searchsorted(ev, t, side="left")
    nxt = np.full(len(t), np.inf)
    last = np.full(len(t), np.inf)
    has_next = idx < len(ev)
    nxt[has_next] = (ev[idx[has_next]] - t[has_next]) / 60e9
    has_last = idx > 0
    last[has_last] = (t[has_last] - ev[idx[has_last] - 1]) / 60e9
    return nxt, last
