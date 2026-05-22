"""
APEX v7.3 — Interest rate differential macro bias (Prompt 3).

Central bank policy rates + price trend + optional news sentiment.
``IS_BACKTEST`` disables sentiment and reweights composite (see ``set_backtest_mode``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Last updated: May 2026 — update when CBs move policy.
CENTRAL_BANK_RATES: dict[str, float] = {
    "USD": 5.25,
    "EUR": 4.50,
    "GBP": 5.25,
    "JPY": 0.10,
    "AUD": 4.35,
    "CAD": 5.00,
    "NZD": 5.50,
    "CHF": 1.75,
    "NOK": 4.50,
    "SEK": 4.00,
    "MXN": 11.25,
    "ZAR": 8.25,
}

IS_BACKTEST: bool = False

_trend_cache: dict[str, str] = {}
_trend_cache_time: dict[str, datetime] = {}
_trend_lock = threading.Lock()
_TREND_TTL = timedelta(hours=24)

_sent_cache: dict[str, tuple[float, datetime]] = {}
_sent_lock = threading.Lock()
_SENT_TTL = timedelta(hours=2)

_BULLISH_KW = (
    "rate hike",
    "hawkish",
    "strong",
    "surges",
    "beats",
    "above expectations",
    "growth",
    "rally",
    "rise",
    "positive",
    "upbeat",
    "recovery",
    "gains",
)
_BEARISH_KW = (
    "rate cut",
    "dovish",
    "weak",
    "falls",
    "misses",
    "below expectations",
    "recession",
    "decline",
    "drop",
    "negative",
    "crisis",
    "slowdown",
    "concerns",
    "risk",
)

_YAHOO_RSS_PAIR: dict[str, str] = {
    "EUR": "EURUSD=X",
    "GBP": "GBPUSD=X",
    "AUD": "AUDUSD=X",
    "NZD": "NZDUSD=X",
    "CAD": "USDCAD=X",
    "CHF": "USDCHF=X",
    "JPY": "USDJPY=X",
    "NOK": "USDNOK=X",
    "SEK": "USDSEK=X",
    "MXN": "USDMXN=X",
    "ZAR": "USDZAR=X",
    "USD": "EURUSD=X",
}


def set_backtest_mode(active: bool) -> None:
    """Railway chrono sets True; live apex leaves False."""
    global IS_BACKTEST
    IS_BACKTEST = bool(active)


def _neutral_bias(ticker: str) -> dict[str, Any]:
    return {
        "bias": "NEUTRAL",
        "composite_score": 0.0,
        "rate_differential": 0.0,
        "price_trend": "RANGING",
        "sentiment_score": 0.0,
        "confidence_upgrade": 0,
        "size_multiplier": 1.0,
        "note": f"Non-forex or insufficient data — neutral bias ({ticker})",
    }


def macro_result_fields(m: dict[str, Any]) -> dict[str, Any]:
    """Persisted trade row keys (Prompt 3)."""
    return {
        "macro_bias": str(m.get("bias", "NEUTRAL")),
        "macro_score": float(m.get("composite_score", 0.0) or 0.0),
        "macro_rate_diff": float(m.get("rate_differential", 0.0) or 0.0),
        "macro_trend": str(m.get("price_trend", "RANGING")),
        "macro_sentiment": float(m.get("sentiment_score", 0.0) or 0.0),
        "macro_size_multiplier": float(m.get("size_multiplier", 1.0) or 1.0),
        "macro_confidence_upgrade": int(m.get("confidence_upgrade", 0) or 0),
    }


def neutral_macro_result_fields() -> dict[str, Any]:
    """Default macro columns when no bias row is attached."""
    return macro_result_fields(_neutral_bias(""))


def merged_macro_result_fields(ai: dict[str, Any] | None) -> dict[str, Any]:
    """Neutral defaults overridden by any macro_* keys already on ``ai``."""
    out = neutral_macro_result_fields()
    if not ai:
        return out
    for k in out:
        if k in ai and ai[k] is not None:
            out[k] = ai[k]
    return out


def apply_macro_confidence_adjustment(confidence: str, macro: dict[str, Any]) -> str:
    c = (confidence or "LOW").strip().upper()
    if c not in ("HIGH", "MEDIUM", "LOW"):
        c = "LOW"
    up = int(macro.get("confidence_upgrade", 0) or 0)
    if up == 1:
        if c == "LOW":
            return "MEDIUM"
        if c == "MEDIUM":
            return "HIGH"
    if up == -1:
        if c == "HIGH":
            return "MEDIUM"
        if c == "MEDIUM":
            return "LOW"
    return c


def get_rate_differential(base: str, quote: str) -> float:
    b = (base or "").strip().upper()[:3]
    q = (quote or "").strip().upper()[:3]
    return float(CENTRAL_BANK_RATES.get(b, 0.0)) - float(CENTRAL_BANK_RATES.get(q, 0.0))


def _yf_symbol(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return f"{t}=X"
    return t


def get_price_trend(
    ticker: str,
    lookback_days: int = 60,
    *,
    as_of_date: date | None = None,
) -> str:
    """
    Daily closes vs 60d ago + EMA20 vs EMA50. Cached 24h per ticker (+ as_of key).
    """
    tku = (ticker or "").strip().upper()
    ck = f"{tku}::{as_of_date.isoformat() if as_of_date else 'LIVE'}"
    now = datetime.now(timezone.utc)
    with _trend_lock:
        ts = _trend_cache_time.get(ck)
        if ts is not None and (now - ts) < _TREND_TTL and ck in _trend_cache:
            return _trend_cache[ck]

    end_d = as_of_date or now.date()
    start_d = end_d - timedelta(days=max(lookback_days, 120))
    start_s = start_d.isoformat()
    end_s = (end_d + timedelta(days=1)).isoformat()
    yf_sym = _yf_symbol(tku)
    try:
        from continuous_backtester import safe_yf_fetch

        df = safe_yf_fetch(yf_sym, start_s, end_s, "1d")
    except Exception as e:  # noqa: BLE001
        logger.warning("get_price_trend import/fetch %s: %s", tku, e)
        df = None
    trend = "RANGING"
    if df is None or getattr(df, "empty", True):
        with _trend_lock:
            _trend_cache[ck] = trend
            _trend_cache_time[ck] = now
        return trend
    try:
        if "Close" not in df.columns:
            raise ValueError("no Close")
        s = df["Close"].astype(float).dropna()
        if len(s) < 55:
            raise ValueError("short series")
        s = s.loc[: str(end_d)]
        if s.empty:
            raise ValueError("empty slice")
        cur = float(s.iloc[-1])
        ago = float(s.iloc[0]) if len(s) > 0 else cur
        if len(s) >= 60:
            ago = float(s.iloc[-60])
        ema20 = s.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = s.ewm(span=50, adjust=False).mean().iloc[-1]
        if cur > ago * 1.02 and ema20 > ema50:
            trend = "UPTREND"
        elif cur < ago * 0.98 and ema20 < ema50:
            trend = "DOWNTREND"
        else:
            trend = "RANGING"
    except Exception as e:  # noqa: BLE001
        logger.debug("get_price_trend calc %s: %s", tku, e)
        trend = "RANGING"
    with _trend_lock:
        _trend_cache[ck] = trend
        _trend_cache_time[ck] = now
    return trend


def _score_headline(text: str) -> float:
    tl = (text or "").lower()
    sc = 0.0
    n_b = n_br = 0
    for kw in _BULLISH_KW:
        if n_b >= 3:
            break
        if kw in tl:
            sc += 0.2
            n_b += 1
    for kw in _BEARISH_KW:
        if n_br >= 3:
            break
        if kw in tl:
            sc -= 0.2
            n_br += 1
    return sc


def _finnhub_forex_news() -> list[dict[str, Any]]:
    import os

    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not key:
        return []
    url = f"https://finnhub.io/api/v1/news?category=forex&token={key}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        logger.warning("Finnhub news: %s", e)
        return []


def _yahoo_headlines(symbol: str) -> list[str]:
    try:
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={requests.utils.quote(symbol, safe='')}&region=US&lang=en-US"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out: list[str] = []
        for title in root.findall(".//title"):
            if title.text:
                out.append(title.text)
        return out[1:]  # skip channel title
    except (requests.RequestException, ET.ParseError) as e:
        logger.debug("Yahoo RSS %s: %s", symbol, e)
        return []


def get_news_sentiment(currency: str) -> float:
    if IS_BACKTEST:
        return 0.0
    ccy = (currency or "").strip().upper()[:3]
    if not ccy:
        return 0.0
    now = datetime.now(timezone.utc)
    with _sent_lock:
        hit = _sent_cache.get(ccy)
        if hit is not None and (now - hit[1]) < _SENT_TTL:
            return float(hit[0])

    headlines: list[str] = []
    cutoff = now - timedelta(hours=6)
    for art in _finnhub_forex_news()[:40]:
        if not isinstance(art, dict):
            continue
        head = str(art.get("headline") or art.get("title") or "")
        if ccy not in head.upper() and ccy not in str(art.get("related", "")).upper():
            continue
        try:
            ts = int(art.get("datetime", 0) or 0)
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if dt < cutoff:
                    continue
        except (TypeError, ValueError, OSError):
            pass
        if head:
            headlines.append(head)

    if len(headlines) < 3:
        sym = _YAHOO_RSS_PAIR.get(ccy)
        if sym:
            headlines.extend(_yahoo_headlines(sym)[:15])

    if len(headlines) < 3:
        with _sent_lock:
            _sent_cache[ccy] = (0.0, now)
        return 0.0

    scores = [_score_headline(h) for h in headlines[:25]]
    avg = sum(scores) / float(len(scores)) if scores else 0.0
    avg = max(-1.0, min(1.0, avg))
    with _sent_lock:
        _sent_cache[ccy] = (avg, now)
    return avg


def get_macro_bias(
    ticker: str,
    direction: str,
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    t = (ticker or "").strip().upper()
    dire = (direction or "").strip().upper()
    if dire not in ("LONG", "SHORT"):
        return _neutral_bias(t)

    if not (len(t) == 6 and t.isalpha()):
        return _neutral_bias(t)

    base, quote = t[:3], t[3:]
    rate_diff = get_rate_differential(base, quote)
    if dire == "LONG":
        rate_signal = rate_diff
    else:
        rate_signal = -rate_diff
    rate_score = max(-1.0, min(1.0, rate_signal / 5.0))

    trend = get_price_trend(t, lookback_days=60, as_of_date=as_of_date)
    if dire == "LONG":
        trend_score = 1.0 if trend == "UPTREND" else (-0.5 if trend == "DOWNTREND" else 0.0)
    else:
        trend_score = 1.0 if trend == "DOWNTREND" else (-0.5 if trend == "UPTREND" else 0.0)

    base_sent = get_news_sentiment(base)
    quote_sent = get_news_sentiment(quote)
    if dire == "LONG":
        sentiment_score = base_sent - quote_sent
    else:
        sentiment_score = quote_sent - base_sent
    sentiment_score = max(-1.0, min(1.0, sentiment_score))

    if IS_BACKTEST:
        composite = rate_score * 0.625 + trend_score * 0.375
    else:
        composite = rate_score * 0.50 + trend_score * 0.30 + sentiment_score * 0.20
    composite = max(-1.0, min(1.0, composite))

    if composite >= 0.50:
        return {
            "bias": "STRONG_TAILWIND",
            "composite_score": round(composite, 3),
            "rate_differential": rate_diff,
            "price_trend": trend,
            "sentiment_score": round(sentiment_score, 3),
            "confidence_upgrade": 1,
            "size_multiplier": 1.20,
            "note": (
                f"Strong macro tailwind — rate diff {rate_diff:+.2f}%, {trend}, "
                f"sentiment {sentiment_score:+.2f}"
            ),
        }
    if composite >= 0.20:
        return {
            "bias": "TAILWIND",
            "composite_score": round(composite, 3),
            "rate_differential": rate_diff,
            "price_trend": trend,
            "sentiment_score": round(sentiment_score, 3),
            "confidence_upgrade": 0,
            "size_multiplier": 1.10,
            "note": f"Macro tailwind — rate diff {rate_diff:+.2f}%, {trend}",
        }
    if composite >= -0.20:
        return {
            "bias": "NEUTRAL",
            "composite_score": round(composite, 3),
            "rate_differential": rate_diff,
            "price_trend": trend,
            "sentiment_score": round(sentiment_score, 3),
            "confidence_upgrade": 0,
            "size_multiplier": 1.0,
            "note": f"Macro neutral — rate diff {rate_diff:+.2f}%",
        }
    if composite >= -0.50:
        return {
            "bias": "HEADWIND",
            "composite_score": round(composite, 3),
            "rate_differential": rate_diff,
            "price_trend": trend,
            "sentiment_score": round(sentiment_score, 3),
            "confidence_upgrade": 0,
            "size_multiplier": 0.85,
            "note": f"Macro headwind — rate diff {rate_diff:+.2f}%, {trend}",
        }
    return {
        "bias": "STRONG_HEADWIND",
        "composite_score": round(composite, 3),
        "rate_differential": rate_diff,
        "price_trend": trend,
        "sentiment_score": round(sentiment_score, 3),
        "confidence_upgrade": -1,
        "size_multiplier": 0.70,
        "note": (
            f"Strong macro headwind — rate diff {rate_diff:+.2f}%, {trend}, "
            f"sentiment {sentiment_score:+.2f}"
        ),
    }


def weekly_macro_summary_lines(tickers: tuple[str, ...]) -> list[str]:
    """One log line per ticker (LONG macro view) for Monday briefing."""
    lines: list[str] = []
    for sym in tickers:
        m = get_macro_bias(sym, "LONG", as_of_date=None)
        lines.append(
            f"{sym}: rate diff {m['rate_differential']:+.2f}% | {m['price_trend']} | {m['bias']} "
            f"→ size {m['size_multiplier']:.2f}x"
        )
    return lines
