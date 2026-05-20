"""
External market intelligence for backtesting prompts.
Caches aggressively to limit API cost (macro changes slowly).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
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
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            data = r.json()
            score = float(data.get("fear_and_greed", {}).get("score", 50) or 50)
            rating = str(data.get("fear_and_greed", {}).get("rating", "Neutral"))
            out: dict[str, Any] = {"score": score, "rating": rating}
            _cache_set(key, out, CACHE_HOURS_FEAR_GREED)
            return out
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] Fear&Greed CNN: {e}")
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        if r.status_code == 200:
            data = r.json()
            score = int(data["data"][0]["value"])
            out = {
                "score": float(score),
                "rating": str(data["data"][0]["value_classification"]),
            }
            _cache_set(key, out, CACHE_HOURS_FEAR_GREED)
            return out
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] Fear&Greed alt: {e}")
    out = {"score": 50.0, "rating": "Neutral"}
    _cache_set(key, out, CACHE_HOURS_FEAR_GREED)
    return out


def get_fear_greed() -> dict[str, Any]:
    """Public alias — CNN Fear & Greed with Alternative.me fallback."""
    return _get_fear_greed()


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


def get_cftc_cot(ticker: str) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    key = f"cot:{sym}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    cftc_codes = {
        "EURUSD": "099741",
        "GBPUSD": "096742",
        "USDJPY": "097741",
        "AUDUSD": "232741",
        "USDCAD": "090741",
        "NZDUSD": "112741",
    }
    out: dict[str, Any] = {"bias": "UNKNOWN"}
    try:
        code = cftc_codes.get(sym, "")
        if not code:
            _cache_set(key, out, CACHE_HOURS_COT)
            return out
        url = (
            "https://publicreporting.cftc.gov/api/views/6dca-aqww/rows.json"
            f"?$where=cftc_contract_market_code='{code}'"
            "&$order=report_date_as_yyyy_mm_dd DESC"
            "&$limit=2"
        )
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            rows = data.get("data", [])
            if rows:
                out = {"bias": "BULLISH", "raw": rows[0]}
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] CFTC COT: {e}")
    _cache_set(key, out, CACHE_HOURS_COT)
    return out


def get_economic_events_today() -> list[dict[str, Any]]:
    key = f"econ:{date.today().isoformat()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    if not finnhub_key:
        _cache_set(key, [], 0.25)
        return []
    try:
        today = date.today().isoformat()
        url = f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={finnhub_key}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            events = r.json().get("economicCalendar", [])
            out = [e for e in events if e.get("impact") == "high"]
            _cache_set(key, out, 0.25)
            return out
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] Finnhub calendar: {e}")
    _cache_set(key, [], 0.25)
    return []


def is_news_blackout(minutes_buffer: int = 30) -> bool:
    events = get_economic_events_today()
    now = datetime.now(timezone.utc)
    for event in events:
        try:
            event_time = datetime.fromisoformat(event.get("time", "").replace("Z", "+00:00"))
            diff_mins = abs((event_time - now).total_seconds() / 60.0)
            if diff_mins <= minutes_buffer:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def get_alpha_vantage_sentiment(ticker: str) -> float:
    av_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
    if not av_key:
        return 0.0
    sym = (ticker or "").strip().upper()
    if len(sym) < 6:
        return 0.0
    base = sym[:3]
    try:
        url = (
            "https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
            f"&tickers=FOREX:{base}&apikey={av_key}&limit=10"
        )
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            articles = data.get("feed", [])
            if articles:
                scores = [float(a.get("overall_sentiment_score", 0)) for a in articles[:5]]
                return round(sum(scores) / len(scores), 3)
    except Exception as e:  # noqa: BLE001
        log(f"[Intel] AlphaVantage sentiment: {e}")
    return 0.0


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
        merged["cot_base"] = get_cftc_cot(ticker)
        return merged

    result: dict[str, Any] = {
        "fear_greed": get_fear_greed(),
        "proxies": _get_proxies(),
        "cot_base": get_cftc_cot(ticker),
        "macro": _get_macro_stub(),
        "news": _benzinga_news(ticker, ds),
        "sources_available": {
            "fear_greed": True,
            "proxies": True,
            "cot": True,
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
