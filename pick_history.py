"""Rolling exclusion of tickers recently recommended by APEX.

Any symbol that appeared in a daily scan within the last N calendar days is
skipped on the next pipeline unless an override condition clears it. All
exclusions and overrides are logged to logs/apex.log via utils.log.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from market_data import YFClient
from utils import RESULTS_DIR, load_json, log


RECENT_DAYS_DEFAULT = 14
MAJOR_NEWS_KEYWORDS = (
    "earnings",
    " eps ",
    "fda",
    "merger",
    "acquisition",
    "buyout",
    "tender offer",
    "going private",
    "takeover",
    "bankruptcy",
    "chapter 11",
    "clinical trial",
    "phase 3",
    "phase 2",
    " pdufa",
    "approved",
    "complete response letter",
    "crl",
    "reverse split",
    "stock split",
)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _recent_scan_dates(days: int) -> list[str]:
    today = datetime.utcnow().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def load_recent_recommendations(days: int = RECENT_DAYS_DEFAULT) -> dict[str, dict[str, Any]]:
    """Build per-ticker context from daily_picks_*.json within the window.

    For each ticker, ``ref_price`` / ``ref_short_interest_pct`` come from the
    most recent day the ticker was picked (we walk newest → oldest dates).
    """
    by_ticker: dict[str, dict[str, Any]] = {}
    for date_str in _recent_scan_dates(days):
        path = RESULTS_DIR / f"daily_picks_{date_str}.json"
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            continue
        picks = data.get("all_picks") or []
        if not picks:
            picks = list(data.get("small_cap_picks") or []) + list(data.get("big_player_picks") or [])
        for p in picks:
            t = (p.get("ticker") or "").strip().upper()
            if not t:
                continue
            if t in by_ticker:
                by_ticker[t]["pick_dates"].append(date_str)
                continue
            by_ticker[t] = {
                "most_recent_pick_date": date_str,
                "ref_price": _safe_float(p.get("current_price")),
                "ref_short_interest_pct": _safe_float(p.get("short_interest_pct_at_scan")),
                "pick_dates": [date_str],
            }
    return by_ticker


def _parse_article_time(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(ts))
        except (TypeError, ValueError, OSError):
            return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if "T" in s:
            s2 = s.replace("Z", "").split("+")[0].split(".")[0]
            return datetime.fromisoformat(s2[:19])
    except (TypeError, ValueError):
        pass
    try:
        return parsedate_to_datetime(s).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _news_major_event_recent(items: list[dict], hours: int = 24) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for n in items or []:
        title = ((n.get("title") or "") + " " + (n.get("description") or "")).lower()
        if not any(k in title for k in MAJOR_NEWS_KEYWORDS):
            continue
        ts = n.get("published_utc") or n.get("pubDate")
        dt = _parse_article_time(ts)
        if dt is None:
            continue
        if dt >= cutoff:
            return True
    return False


async def _override_price_moved(yfc: YFClient, ticker: str, ref_price: float | None) -> tuple[bool, str]:
    if not ref_price or ref_price <= 0:
        return False, ""
    snap = await yfc.snapshot(ticker)
    cur = _safe_float((snap.get("day") or {}).get("c")) if snap else None
    if cur is None or cur <= 0:
        return False, ""
    move_pct = abs(cur - ref_price) / ref_price * 100
    if move_pct > 20.0:
        return True, f"price moved {move_pct:.1f}% (>20%) since last pick @ {ref_price:.4f}"
    return False, ""


async def _override_short_interest(yfc: YFClient, ticker: str, ref_si: float | None) -> tuple[bool, str]:
    if ref_si is None:
        return False, ""
    snap = await yfc.snapshot(ticker)
    si = (snap.get("shortInterest") or {}).get("percentOfFloat") if snap else None
    cur_si = _safe_float(si)
    if cur_si is None:
        return False, ""
    if abs(cur_si - ref_si) >= 5.0:
        return True, f"short interest changed {cur_si:.2f}% vs {ref_si:.2f}% (>=5pt swing)"
    return False, ""


async def _override_major_news(yfc: YFClient, ticker: str) -> tuple[bool, str]:
    items = await yfc.news(ticker, limit=20)
    if _news_major_event_recent(items, hours=24):
        return True, "major news keyword in last 24h (earnings / FDA / M&A / etc.)"
    return False, ""


def _override_volume_spike(candidate: dict) -> tuple[bool, str]:
    ind = candidate.get("indicators") or {}
    vr = _safe_float(ind.get("volume_ratio"))
    if vr is not None and vr >= 10.0:
        return True, f"volume {vr:.1f}x 30d average (>=10x exceptional)"
    return False, ""


async def _allow_despite_recent_pick(
    ticker: str,
    candidate: dict,
    ctx: dict[str, Any],
    yfc: YFClient,
) -> tuple[bool, str]:
    """Return (allowed, reason_if_allowed). If not allowed, reason describes failed overrides."""
    ok, msg = _override_volume_spike(candidate)
    if ok:
        return True, msg
    ok, msg = await _override_price_moved(yfc, ticker, ctx.get("ref_price"))
    if ok:
        return True, msg
    ok, msg = await _override_major_news(yfc, ticker)
    if ok:
        return True, msg
    ok, msg = await _override_short_interest(yfc, ticker, ctx.get("ref_short_interest_pct"))
    if ok:
        return True, msg
    return False, "no override: still within 14d cooling window"


async def filter_recent_exclusions(
    candidates: list[dict],
    days: int = RECENT_DAYS_DEFAULT,
) -> list[dict]:
    """Drop candidates that duplicate a recent pick unless an override fires."""
    hist = load_recent_recommendations(days)
    if not hist:
        return list(candidates)

    kept: list[dict] = []
    async with YFClient() as yfc:
        for c in candidates:
            t = (c.get("ticker") or "").strip().upper()
            if not t or t not in hist:
                kept.append(c)
                continue
            allow, detail = await _allow_despite_recent_pick(t, c, hist[t], yfc)
            if allow:
                log(f"[PickHistory] OVERRIDE {t}: recent pick within {days}d — {detail}")
                kept.append(c)
            else:
                log(
                    f"[PickHistory] EXCLUDE {t}: recommended on {hist[t].get('most_recent_pick_date')} "
                    f"within last {days}d — {detail}"
                )
    return kept
