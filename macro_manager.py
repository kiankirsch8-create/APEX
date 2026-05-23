"""
APEX v7.3 — Interest rate differential macro bias (Prompt 3).

Central bank policy rates + price trend + optional news sentiment.
``IS_BACKTEST`` disables sentiment and reweights composite (see ``set_backtest_mode``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
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

_POS_KW = (
    "rise",
    "gain",
    "surge",
    "rally",
    "strong",
    "bullish",
    "hawkish",
    "hike",
    "rate increase",
    "beat",
    "growth",
    "recovery",
    "upside",
    "advance",
)
_NEG_KW = (
    "fall",
    "drop",
    "decline",
    "weak",
    "bearish",
    "dovish",
    "cut",
    "miss",
    "recession",
    "downside",
    "slowdown",
    "weaken",
    "tumble",
    "concern",
)


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


def _score_articles(
    articles: Any,
    base: str,
    quote: str,
    headline_key: str,
    body_key: str,
) -> float | None:
    """Score article list toward base currency vs quote (FIX 30)."""
    b = (base or "").strip().upper()[:3]
    q = (quote or "").strip().upper()[:3]
    if not b or not q:
        return None
    keywords = (
        b,
        q,
        f"{b}/{q}",
        f"{b}{q}",
        f"{b} {q}",
    )
    scores: list[float] = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        text = (
            str(a.get(headline_key, "") or "") + " " + str(a.get(body_key, "") or "")
        ).lower()
        if not any(k.lower() in text for k in keywords):
            continue
        p = sum(1 for w in _POS_KW if w in text)
        n = sum(1 for w in _NEG_KW if w in text)
        if p + n > 0:
            scores.append((p - n) / float(p + n))
    if not scores:
        return None
    return round(float(np.mean(scores)), 3)


def get_news_sentiment(base: str, quote: str) -> float:
    """
    Sentiment -1..+1 for base vs quote from Finnhub + NewsAPI + Benzinga (FIX 30).
    Returns 0.0 in backtest or when no usable scores.
    """
    if IS_BACKTEST:
        return 0.0
    b = (base or "").strip().upper()[:3]
    q = (quote or "").strip().upper()[:3]
    if not b or not q:
        return 0.0
    cache_key = f"{b}/{q}"
    now = datetime.now(timezone.utc)
    with _sent_lock:
        hit = _sent_cache.get(cache_key)
        if hit is not None and (now - hit[1]) < _SENT_TTL:
            return float(hit[0])

    scores: list[float] = []

    # Finnhub (forex category)
    try:
        fh_key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
        if fh_key:
            r = requests.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "forex", "token": fh_key},
                timeout=5,
            )
            if r.status_code == 200:
                s = _score_articles(r.json()[:20], b, q, "headline", "summary")
                if s is not None:
                    scores.append(float(s))
    except Exception as e:  # noqa: BLE001
        logger.warning("Finnhub sentiment: %s", e)

    # NewsAPI
    try:
        na_key = (os.environ.get("NEWSAPI_KEY") or "").strip()
        if na_key:
            r = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f"{b} OR {q} forex",
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 20,
                    "apiKey": na_key,
                },
                timeout=5,
            )
            if r.status_code == 200:
                s = _score_articles(r.json().get("articles", []), b, q, "title", "description")
                if s is not None:
                    scores.append(float(s))
    except Exception as e:  # noqa: BLE001
        logger.warning("NewsAPI sentiment: %s", e)

    # Benzinga
    try:
        bz_key = (os.environ.get("BENZINGA_API_KEY") or "").strip()
        if bz_key:
            r = requests.get(
                "https://api.benzinga.com/api/v2/news",
                params={"token": bz_key, "topics": "forex", "pageSize": 20},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                arts = data if isinstance(data, list) else []
                s = _score_articles(arts, b, q, "title", "body")
                if s is not None:
                    scores.append(float(s))
    except Exception as e:  # noqa: BLE001
        logger.warning("Benzinga sentiment: %s", e)

    out = round(float(np.mean(scores)), 3) if scores else 0.0
    out = max(-1.0, min(1.0, out))
    with _sent_lock:
        _sent_cache[cache_key] = (out, now)
    return out


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

    pair_sent = get_news_sentiment(base, quote)
    if dire == "LONG":
        sentiment_score = pair_sent
    else:
        sentiment_score = -pair_sent
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
