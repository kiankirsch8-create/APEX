"""Scoring of one candidate against the 5 pillars. Pure functions (unit-tested)."""
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, time
from zoneinfo import ZoneInfo
from . import config as C

NY = ZoneInfo("America/New_York")


@dataclass
class Candidate:
    symbol: str
    last: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[float] = None          # today's volume incl. pre-market (same units as avg_volume)
    avg_volume: Optional[float] = None      # average daily volume (IB tick 21, 90 days)
    float_shares: Optional[float] = None
    news: list = field(default_factory=list)  # [{"time": iso, "source": str, "title": str}]
    first_seen: Optional[str] = None
    high: Optional[float] = None

    @property
    def change_pct(self):
        if not self.last or not self.prev_close: return None
        return (self.last / self.prev_close - 1) * 100


def session_fraction(now: datetime) -> float:
    """Typical share of a day's volume that has traded by `now` (New York time).

    Small caps on a news day trade heavily from 7:00 pre-market. We use a simple profile: 10% of the day's
    volume by the 9:30 open (pre-market), then a front-loaded curve to 100% at 16:00. Floor 5% so very
    early pre-market readings don't explode.
    """
    t = now.astimezone(NY).time()
    mins = t.hour * 60 + t.minute
    pre0, open_, close = 4 * 60, 9 * 60 + 30, 16 * 60
    if mins <= pre0: return 0.05
    if mins < open_: return max(0.05, 0.10 * (mins - pre0) / (open_ - pre0))
    if mins >= close: return 1.0
    x = (mins - open_) / (close - open_)            # 0..1 through the regular session
    return 0.10 + 0.90 * (x ** 0.6)                  # front-loaded (more volume early)


def rvol(c: Candidate, now: datetime):
    """Relative volume, two flavours:
    day : today's volume so far / average full-day volume (conservative; what most free screeners show)
    tod : today's volume so far / average volume expected by this time of day (closer to the Dash's reading)
    """
    if not c.volume or not c.avg_volume: return None, None
    day = c.volume / c.avg_volume
    return day, day / session_fraction(now)


def score(c: Candidate, now: datetime) -> dict:
    ch = c.change_pct
    r_day, r_tod = rvol(c, now)
    checks = {
        "rvol": r_tod is not None and r_tod >= C.MIN_RVOL,
        "change": ch is not None and ch >= C.MIN_CHANGE_PCT,
        "news": len(c.news) > 0,
        "price": c.last is not None and C.MIN_PRICE <= c.last <= C.MAX_PRICE,
        "float": c.float_shares is not None and c.float_shares < C.MAX_FLOAT,
    }
    near_float = c.float_shares is not None and C.MAX_FLOAT <= c.float_shares < C.SOFT_MAX_FLOAT
    n = sum(checks.values())
    d = asdict(c)
    d.update(change_pct=ch, rvol_day=r_day, rvol_tod=r_tod, checks=checks, pillars=n,
             near_float=near_float, all5=n == 5)
    return d
