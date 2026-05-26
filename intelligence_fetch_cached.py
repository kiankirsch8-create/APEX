"""
Read-through SQLite caching for intelligence stack (chrono / rolling backtest).

Delegates to ``intelligence_cache.get_cached_or_fetch`` so historical dates (>7d old)
skip redundant API work. Live / recent dates always fetch fresh.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from anthropic import Anthropic

from calendar_manager import check_calendar_risk_historical
from intelligence_cache import get_cached_or_fetch, is_safe_cache_date
from macro_manager import get_macro_bias
from market_intelligence import get_complete_briefing
from regime_manager import get_regime_cached
from trend_manager import apply_trend_filter


def _anthropic_message_text(response: Any) -> str:
    parts: list[str] = []
    for block in response.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def cached_macro_bias(ticker: str, direction: str, scan_d: date | None) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    dire = (direction or "").strip().upper()
    if scan_d is None:
        return get_macro_bias(sym, dire, as_of_date=None)
    key = f"macro_bias:{sym}:{dire}:{scan_d.isoformat()}"
    return get_cached_or_fetch(
        key,
        "macro_bias",
        is_safe_cache_date(scan_d),
        lambda: get_macro_bias(sym, dire, as_of_date=scan_d),
    )


def cached_calendar_historical(ticker: str, scan_d: date) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    key = f"calendar_risk:{sym}:{scan_d.isoformat()}"
    return get_cached_or_fetch(
        key,
        "calendar_risk",
        is_safe_cache_date(scan_d),
        lambda: check_calendar_risk_historical(sym, scan_d),
    )


def cached_apply_trend_filter(
    ticker: str,
    direction: str,
    strategy_id: str,
    as_of_date: str | None,
) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    dire = (direction or "").strip().upper()
    sid = (strategy_id or "").strip().upper()
    if not as_of_date:
        return apply_trend_filter(sym, dire, sid, as_of_date=None)
    ds = str(as_of_date).strip()[:10]
    try:
        d = date.fromisoformat(ds)
    except ValueError:
        return apply_trend_filter(sym, dire, sid, as_of_date=as_of_date)
    key = f"trend:{sym}:{dire}:{sid}:{ds}"
    return get_cached_or_fetch(
        key,
        "trend_filter",
        is_safe_cache_date(d),
        lambda: apply_trend_filter(sym, dire, sid, as_of_date=as_of_date),
    )


def cached_regime(job_id: str, scan_d: date | None) -> dict[str, Any]:
    jid = (job_id or "").strip() or "rolling"
    if scan_d is None:
        return get_regime_cached(jid, as_of_date=None)
    key = f"regime:{jid}:{scan_d.isoformat()}"
    return get_cached_or_fetch(
        key,
        "regime",
        is_safe_cache_date(scan_d),
        lambda: get_regime_cached(jid, as_of_date=scan_d),
    )


def cached_complete_briefing(ticker: str, date_str: str) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    ds = str(date_str).strip()[:10]
    try:
        d = date.fromisoformat(ds)
    except ValueError:
        return get_complete_briefing(sym, str(date_str).strip(), None)
    key = f"briefing:{sym}:{ds}"
    return get_cached_or_fetch(
        key,
        "market_intelligence_briefing",
        is_safe_cache_date(d),
        lambda: get_complete_briefing(sym, str(date_str).strip(), None),
    )


def cached_claude_master_text(
    client: Anthropic,
    *,
    model: str,
    max_tokens: int,
    prompt: str,
    ticker: str,
    analysis_date: str,
    scan_d: date | None,
) -> str:
    """Return assistant plain text for the master prompt path (Layer 1 Claude)."""
    sym = (ticker or "").strip().upper()
    ds = str(analysis_date).strip()[:10]
    ph = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]
    key = f"anthropic:{sym}:{ds}:{ph}"

    def fetch() -> dict[str, str]:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"text": _anthropic_message_text(resp)}

    if scan_d is None or not is_safe_cache_date(scan_d):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _anthropic_message_text(resp)

    row = get_cached_or_fetch(key, "anthropic", True, fetch)
    if isinstance(row, dict):
        return str(row.get("text", ""))
    return str(row or "")


def prefetch_macro_calendar_trend_for_day(
    ticker: str,
    scan_d: date,
    *,
    regime_job_id: str | None = None,
) -> None:
    """Populate cache for one ticker + trading day (used by prefetch script / API)."""
    sym = (ticker or "").strip().upper()
    for dire in ("LONG", "SHORT"):
        cached_macro_bias(sym, dire, scan_d)
        cached_apply_trend_filter(sym, dire, "T01_EMA_PULLBACK", scan_d.isoformat())
        cached_apply_trend_filter(sym, dire, "R01_EXTREME_ZONE_REVERSION", scan_d.isoformat())
    cached_calendar_historical(sym, scan_d)
    cached_complete_briefing(sym, scan_d.isoformat())
    if regime_job_id:
        cached_regime(regime_job_id.strip(), scan_d)
