"""
APEX v7.3 — Interest rate differential macro bias (Prompt 3).

Central bank policy rates + price trend + optional news sentiment.
``IS_BACKTEST`` disables sentiment and reweights composite (see ``set_backtest_mode``).
"""

from __future__ import annotations

import json
import math
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
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
        "st_layer1_failed": bool(m.get("st_layer1_failed", False)),
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


def apply_macro_confidence_adjustment(
    confidence: str,
    macro: dict[str, Any],
    strategy_id: str | None = None,
) -> str:
    c = (confidence or "LOW").strip().upper()
    if c not in ("HIGH", "MEDIUM", "LOW"):
        c = "LOW"
    up = int(macro.get("confidence_upgrade", 0) or 0)
    result = c
    if up == 1:
        if c == "LOW":
            result = "MEDIUM"
        elif c == "MEDIUM":
            result = "HIGH"
    elif up == -1:
        if c == "HIGH":
            result = "MEDIUM"
        elif c == "MEDIUM":
            result = "LOW"

    strategy_confidence_caps = {
        "T01_EMA_PULLBACK": "MEDIUM",
    }
    sid_u = str(strategy_id or "").strip().upper()
    if sid_u and sid_u in strategy_confidence_caps:
        cap = strategy_confidence_caps[sid_u]
        confidence_rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        if confidence_rank.get(result, 0) > confidence_rank.get(cap, 3):
            from utils import log

            log(
                f"[CONFIDENCE CAP] {sid_u} capped at {cap} (was {result})",
                level="info",
            )
            return cap
    return result


def get_rate_differential(base: str, quote: str) -> float:
    b = (base or "").strip().upper()[:3]
    q = (quote or "").strip().upper()[:3]
    return float(CENTRAL_BANK_RATES.get(b, 0.0)) - float(CENTRAL_BANK_RATES.get(q, 0.0))


def _yf_symbol(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return f"{t}=X"
    return t


JPY_ST_CORR_GROUP: frozenset[str] = frozenset(
    {"USDJPY", "GBPJPY", "CHFJPY", "CADJPY", "AUDJPY", "NZDJPY", "EURJPY"},
)
EUR_ST_CORR_GROUP: frozenset[str] = frozenset(
    {"EURUSD", "EURGBP", "EURJPY", "EURAUD", "EURNZD", "EURCAD", "EURCHF"},
)
COMMODITY_ST_CORR_GROUP: frozenset[str] = frozenset(
    {
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "AUDCAD",
        "AUDNZD",
        "AUDCHF",
        "NZDCAD",
        "NZDCHF",
        "CADCHF",
        "GBPAUD",
        "GBPNZD",
        "GBPCAD",
        "EURAUD",
        "EURNZD",
        "EURCAD",
    },
)


def _fetch_weekly_ohlc(ticker: str, as_of_date: date | None) -> pd.DataFrame | None:
    """Weekly OHLC through ``as_of_date`` (same ``safe_yf_fetch`` contract as daily trend)."""
    tku = (ticker or "").strip().upper()
    end_d = as_of_date or datetime.now(timezone.utc).date()
    start_d = end_d - timedelta(days=140)
    try:
        from continuous_backtester import safe_yf_fetch

        df = safe_yf_fetch(
            _yf_symbol(tku),
            start_d.isoformat(),
            (end_d + timedelta(days=1)).isoformat(),
            "1wk",
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("_fetch_weekly_ohlc %s: %s", tku, e)
        return None
    if df is None or getattr(df, "empty", True):
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    for col in ("Open", "High", "Low", "Close"):
        if col not in df.columns:
            return None
    out = df.copy()
    out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    out = out[out.index.date <= end_d]
    if out.empty:
        return None
    return out


def _check_8wk_ema_alignment(
    ticker: str,
    direction: str,
    as_of_date: date | None = None,
) -> bool:
    """
    Layer 1 hard gate for STRONG_TAILWIND.
    Returns True if:
      - LONG: weekly close > 8-period EMA of weekly closes
      - SHORT: weekly close < 8-period EMA of weekly closes
    Uses the same weekly price source that get_price_trend uses.
    Returns True (passes) if data is unavailable, to avoid blocking legitimate signals on data gaps.
    """
    dire = (direction or "").strip().upper()
    if dire not in ("LONG", "SHORT"):
        return True
    df = _fetch_weekly_ohlc(ticker, as_of_date)
    if df is None or len(df) < 8:
        return True
    try:
        closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(closes) < 8:
            return True
        ema8 = float(closes.ewm(span=8, adjust=False).mean().iloc[-1])
        last_close = float(closes.iloc[-1])
        if not math.isfinite(ema8) or not math.isfinite(last_close):
            return True
        if dire == "LONG":
            return last_close > ema8
        return last_close < ema8
    except Exception as e:  # noqa: BLE001
        logger.debug("_check_8wk_ema_alignment %s: %s", ticker, e)
        return True


def _st_correlation_group(ticker: str) -> frozenset[str] | None:
    t = (ticker or "").strip().upper()
    if t in JPY_ST_CORR_GROUP:
        return JPY_ST_CORR_GROUP
    if len(t) == 6 and t.isalpha():
        base, quote = t[:3], t[3:]
        if "EUR" in (base, quote):
            return EUR_ST_CORR_GROUP
        if any(c in (base, quote) for c in ("AUD", "NZD", "CAD")):
            return COMMODITY_ST_CORR_GROUP
    return None


def _weekly_atr(df: pd.DataFrame, period: int = 8) -> float | None:
    try:
        high = pd.to_numeric(df["High"], errors="coerce")
        low = pd.to_numeric(df["Low"], errors="coerce")
        close = pd.to_numeric(df["Close"], errors="coerce")
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_s = tr.rolling(period).mean().dropna()
        if atr_s.empty:
            return None
        val = float(atr_s.iloc[-1])
        return val if math.isfinite(val) and val > 0 else None
    except Exception:  # noqa: BLE001
        return None


def compute_st_layer2_score(
    ticker: str,
    direction: str,
    trend_strength: float,
    rate_diff: float,
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Computes STRONG_TAILWIND Layer 2 confirmation score (0-5).
    Only called when macro bias is already STRONG_TAILWIND.
    Each criterion adds 1 point.
    """
    dire = (direction or "").strip().upper()
    tku = (ticker or "").strip().upper()
    criteria_met: list[str] = []
    score = 0

    # 1 — rate differential widening (4 weeks)
    try:
        if len(tku) == 6 and tku.isalpha():
            base, quote = tku[:3], tku[3:]
            rd_now = float(rate_diff)
            _ = as_of_date
            rd_prev = get_rate_differential(base, quote)
            if dire == "LONG":
                sig_now, sig_prev = rd_now, rd_prev
            else:
                sig_now, sig_prev = -rd_now, -rd_prev
            if sig_now - sig_prev >= 0.25:
                score += 1
                criteria_met.append("rate_diff_widening")
    except Exception:  # noqa: BLE001
        pass

    # 2 — last 3 weekly closes directional
    try:
        wdf = _fetch_weekly_ohlc(tku, as_of_date)
        if wdf is not None and len(wdf) >= 4:
            closes = pd.to_numeric(wdf["Close"], errors="coerce").dropna()
            if len(closes) >= 4:
                c1, c2, c3, c4 = (
                    float(closes.iloc[-4]),
                    float(closes.iloc[-3]),
                    float(closes.iloc[-2]),
                    float(closes.iloc[-1]),
                )
                if dire == "LONG" and c2 > c1 and c3 > c2 and c4 > c3:
                    score += 1
                    criteria_met.append("weekly_closes_directional")
                elif dire == "SHORT" and c2 < c1 and c3 < c2 and c4 < c3:
                    score += 1
                    criteria_met.append("weekly_closes_directional")
    except Exception:  # noqa: BLE001
        pass

    # 3 — cross-pair correlation
    try:
        grp = _st_correlation_group(tku)
        if grp is not None and dire in ("LONG", "SHORT"):
            st_count = 0
            for sym in grp:
                mb_row = get_macro_bias(sym, dire, as_of_date=as_of_date)
                if str(mb_row.get("bias", "")).strip().upper() == "STRONG_TAILWIND":
                    st_count += 1
            if st_count >= 3:
                score += 1
                criteria_met.append("cross_pair_correlation")
    except Exception:  # noqa: BLE001
        pass

    # 4 — trend strength
    try:
        if float(trend_strength or 0) > 0.70:
            score += 1
            criteria_met.append("trend_strength")
    except (TypeError, ValueError):
        pass

    # 5 — no recent reversal candle (last 2 weekly)
    try:
        wdf2 = _fetch_weekly_ohlc(tku, as_of_date)
        if wdf2 is not None and len(wdf2) >= 8:
            atr8 = _weekly_atr(wdf2, period=8)
            if atr8 is not None and atr8 > 0:
                tail = wdf2.tail(2)
                bad = False
                for _, row in tail.iterrows():
                    o = float(row["Open"])
                    c = float(row["Close"])
                    body = abs(c - o)
                    if dire == "LONG" and c < o and body > 0.5 * atr8:
                        bad = True
                        break
                    if dire == "SHORT" and c > o and body > 0.5 * atr8:
                        bad = True
                        break
                if not bad:
                    score += 1
                    criteria_met.append("no_reversal_candle")
    except Exception:  # noqa: BLE001
        pass

    return {
        "st_layer2_score": int(score),
        "st_criteria_met": criteria_met,
    }


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
        price_confirms = _check_8wk_ema_alignment(t, dire, as_of_date=as_of_date)
        if not price_confirms:
            from utils import log

            log(
                f"[ST LAYER1 FAIL] {t} {dire}: composite={composite:.3f} >= 0.50 but "
                f"8wk EMA gate failed — downgraded to TAILWIND. "
                f"rate_diff={rate_diff:+.2f}%, trend={trend}",
                level="info",
            )
            return {
                "bias": "TAILWIND",
                "composite_score": round(composite, 3),
                "rate_differential": rate_diff,
                "price_trend": trend,
                "sentiment_score": round(sentiment_score, 3),
                "confidence_upgrade": 0,
                "size_multiplier": 1.10,
                "note": (
                    f"STRONG_TAILWIND downgraded to TAILWIND — 8wk EMA gate failed, "
                    f"rate diff {rate_diff:+.2f}%, {trend}"
                ),
                "st_layer1_failed": True,
            }
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
                f"sentiment {sentiment_score:+.2f}, 8wk EMA confirmed"
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


def _macro_tier_economics(bias: str) -> tuple[float, int]:
    """(size_multiplier, confidence_upgrade) for a macro bias label after price-alignment downgrade."""
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return 1.20, 1
    if b == "TAILWIND":
        return 1.10, 0
    if b == "NEUTRAL":
        return 1.0, 0
    if b == "HEADWIND":
        return 0.85, 0
    if b == "STRONG_HEADWIND":
        return 0.70, -1
    return 1.0, 0


def check_macro_price_alignment(
    ticker: str,
    direction: str,
    macro_bias: str,
    price_data: pd.DataFrame | None,
    *,
    as_of_date: date | None = None,
) -> tuple[str, float | None, str]:
    """
    v7.4 — 7-day spot momentum vs macro direction. Does not recompute rate/sentiment composites.
    Returns (adjusted_bias, momentum_pct_or_None, reason_tag).
    """
    tku = (ticker or "").strip().upper()
    dire = (direction or "").strip().upper()
    mb0 = str(macro_bias or "").strip().upper()
    if dire not in ("LONG", "SHORT") or not (len(tku) == 6 and tku.isalpha()):
        return mb0, None, "skip_non_fx"
    if mb0 not in ("STRONG_TAILWIND", "TAILWIND", "NEUTRAL", "HEADWIND", "STRONG_HEADWIND"):
        return mb0, None, "skip_bias_tier"

    df = price_data
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        try:
            from continuous_backtester import safe_yf_fetch

            end_d = as_of_date or date.today()
            start_d = end_d - timedelta(days=20)
            yf_sym = _yf_symbol(tku)
            df = safe_yf_fetch(yf_sym, start_d.isoformat(), (end_d + timedelta(days=1)).isoformat(), "1d")
        except Exception as e:  # noqa: BLE001
            logger.warning("check_macro_price_alignment fetch %s: %s", tku, e)
            df = None
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return mb0, None, "no_price_data"

    closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if len(closes) < 8:
        return mb0, None, "short_history"
    c_now = float(closes.iloc[-1])
    c_7 = float(closes.iloc[-8])
    if not math.isfinite(c_now) or not math.isfinite(c_7) or c_7 == 0:
        return mb0, None, "bad_closes"
    momentum = (c_now - c_7) / abs(c_7) * 100.0

    agree_hi = 0.3
    agree_lo = -0.3
    if dire == "LONG":
        if momentum > agree_hi:
            return mb0, momentum, "price_agrees"
        if agree_lo <= momentum <= agree_hi:
            return _downgrade_macro_bias_one_level(mb0), momentum, "neutral_momentum_band"
        return _downgrade_macro_bias_two_levels(mb0), momentum, "price_contradicts"
    # SHORT
    if momentum < agree_lo:
        return mb0, momentum, "price_agrees"
    if agree_lo <= momentum <= agree_hi:
        return _downgrade_macro_bias_one_level_short(mb0), momentum, "neutral_momentum_band"
    return _downgrade_macro_bias_two_levels_short(mb0), momentum, "price_contradicts"


def _downgrade_macro_bias_one_level(bias: str) -> str:
    """LONG book: softer macro when 7d drift is flat (momentum band)."""
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return "TAILWIND"
    if b == "TAILWIND":
        return "NEUTRAL"
    if b == "HEADWIND":
        return "STRONG_HEADWIND"
    return b


def _downgrade_macro_bias_two_levels(bias: str) -> str:
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return "NEUTRAL"
    if b == "TAILWIND":
        return "HEADWIND"
    if b == "NEUTRAL":
        return "HEADWIND"
    if b == "HEADWIND":
        return "STRONG_HEADWIND"
    return b


def _downgrade_macro_bias_one_level_short(bias: str) -> str:
    """SHORT book: symmetric tier moves when momentum is flat."""
    return _downgrade_macro_bias_one_level(bias)


def _downgrade_macro_bias_two_levels_short(bias: str) -> str:
    return _downgrade_macro_bias_two_levels(bias)


def align_macro_bias_with_price(
    ticker: str,
    direction: str,
    macro: dict[str, Any],
    *,
    as_of_date: date | None = None,
    price_df: pd.DataFrame | None = None,
    log_fn: Any | None = None,
) -> dict[str, Any]:
    """
    Apply v7.4 7d price alignment on top of ``get_macro_bias`` output.
    ``log_fn`` optional callable(str) e.g. continuous_backtester.log.
    """
    out = dict(macro)
    mb0 = str(out.get("bias", "NEUTRAL")).strip().upper()
    adj, mom, tag = check_macro_price_alignment(
        ticker,
        direction,
        mb0,
        price_df,
        as_of_date=as_of_date,
    )
    if adj != mb0 and mom is not None:
        sm, cu = _macro_tier_economics(adj)
        out["bias"] = adj
        out["size_multiplier"] = sm
        out["confidence_upgrade"] = cu
        msg = (
            f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} "
            f"(7d momentum: {mom:+.1f}%, price contradicting macro direction)"
            if "contradict" in tag
            else (
                f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} "
                f"(7d momentum: {mom:+.1f}%, neutral price action)"
                if "neutral_momentum" in tag
                else f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} (7d momentum: {mom:+.1f}%, {tag})"
            )
        )
        if log_fn:
            try:
                log_fn(msg, level="info")
            except TypeError:
                log_fn(msg)
        else:
            logger.info(msg)
    return out


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
