"""
External market intelligence for backtesting prompts.
Caches aggressively to limit API cost (macro changes slowly).
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests
import yfinance as yf

from utils import log

# TTL hours (cost optimization)
CACHE_HOURS_FEAR_GREED = 6.0
CACHE_HOURS_PROXIES = 12.0
CACHE_HOURS_COT = 48.0
CACHE_HOURS_FRED = 24.0
CACHE_HOURS_NEWS = 2.0

_session_intel: dict[str, dict[str, Any]] = {}
_session_intel_time: dict[str, float] = {}

_memo: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    row = _memo.get(key)
    if not row:
        return None
    exp, val = row
    if time.time() > exp:
        del _memo[key]
        return None
    return val


def _cache_set(key: str, value: Any, hours: float) -> None:
    _memo[key] = (time.time() + hours * 3600.0, value)


def get_session_intel(date_str: str) -> dict[str, Any]:
    """Macro/global briefing blob for one analysis date (no per-ticker news)."""
    if date_str in _session_intel:
        return _session_intel[date_str]
    return {}


def set_session_intel(date_str: str, data: dict[str, Any]) -> None:
    _session_intel[date_str] = data
    _session_intel_time[date_str] = time.time()


def _get_fear_greed() -> dict[str, Any]:
    key = "fg"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    score = 50.0
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=8,
        )
        if r.ok:
            j = r.json()
            score = float(j.get("fear_and_greed", {}).get("score", 50) or 50)
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] Fear&Greed: {e}")
    out = {"score": score}
    _cache_set(key, out, CACHE_HOURS_FEAR_GREED)
    return out


def _get_proxies() -> dict[str, Any]:
    key = "proxies_v1"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    vix_val = 20.0
    try:
        v = yf.Ticker("^VIX")
        h = v.history(period="5d")
        if not h.empty and "Close" in h.columns:
            vix_val = float(h["Close"].iloc[-1])
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] VIX: {e}")
    out = {"proxies": {"vix": {"value": vix_val}}}
    _cache_set(key, out, CACHE_HOURS_PROXIES)
    return out


def _get_cot_stub() -> dict[str, Any]:
    key = "cot_stub"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out = {"bias": "UNKNOWN"}
    _cache_set(key, out, CACHE_HOURS_COT)
    return out


def _get_macro_stub() -> dict[str, Any]:
    key = "fred_stub"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out = {"regime": "UNKNOWN"}
    _cache_set(key, out, CACHE_HOURS_FRED)
    return out


def _benzinga_news(ticker: str, date_str: str) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    key = f"news:{sym}:{date_str}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out: dict[str, Any] = {"net_sentiment": 0.0, "headlines": []}
    _cache_set(key, out, CACHE_HOURS_NEWS)
    return out


def get_complete_briefing(
    ticker: str,
    date_str: str,
    past_df: pd.DataFrame | None = None,  # noqa: ARG001
) -> dict[str, Any]:
    """
    Session cache: macro/global slice is keyed by date only.
    News is refreshed per (ticker, date) with its own TTL.
    """
    ds = str(date_str).strip()
    cached = get_session_intel(ds)
    if cached:
        log(f"[Intel] Session cache hit: {ds}")
        merged = dict(cached)
        merged["news"] = _benzinga_news(ticker, ds)
        return merged

    result: dict[str, Any] = {
        "fear_greed": _get_fear_greed(),
        "proxies": _get_proxies(),
        "cot_base": _get_cot_stub(),
        "macro": _get_macro_stub(),
        "news": _benzinga_news(ticker, ds),
        "sources_available": {
            "fear_greed": True,
            "proxies": True,
            "cot": False,
            "fred": False,
            "news": False,
        },
    }
    set_session_intel(ds, result)
    return result


def format_for_prompt(briefing: dict[str, Any]) -> str:
    if not isinstance(briefing, dict):
        return ""
    fg = briefing.get("fear_greed") or {}
    px = briefing.get("proxies") or {}
    vix = 20.0
    try:
        vix = float(px.get("proxies", {}).get("vix", {}).get("value", 20) or 20)
    except (TypeError, ValueError):
        pass
    news = briefing.get("news") or {}
    try:
        sent = float(news.get("net_sentiment", 0) or 0)
    except (TypeError, ValueError):
        sent = 0.0
    cot = str((briefing.get("cot_base") or {}).get("bias", "UNKNOWN"))
    lines = [
        "── Briefing ──",
        f"Fear&Greed: {float(fg.get('score', 50) or 50):.0f}/100 | VIX: {vix:.1f}",
        f"News sentiment: {sent:+.2f} | COT: {cot}",
    ]
    return "\n".join(lines) + "\n"
