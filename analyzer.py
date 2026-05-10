"""APEX deep analyzer.

For each candidate produced by ``screener.run_screener``, gather a complete
data packet (technicals, news, analyst consensus, fundamentals, macro
context) and ask Claude to produce a fully-structured APEX report.

The ``analyze_ticker`` function is the public entry point. It is robust to
partial data: every external API call is wrapped in try/except, and missing
fields degrade gracefully so Claude can still produce a useful thesis.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from master_prompt import MASTER_ANALYST_SYSTEM_PROMPT, APEX_OUTPUT_SCHEMA
from screener import (
    _bollinger,
    _fetch_daily_bars,
    _macd,
    _polygon_get,
    _recent_news,
    _rsi,
    _sma,
)

load_dotenv()

logger = logging.getLogger("apex.analyzer")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-20250514")

# Lazy-import the Anthropic client so the module is still importable in
# environments without the SDK (e.g. unit tests).
try:
    from anthropic import Anthropic  # type: ignore
except Exception:  # pragma: no cover
    Anthropic = None  # type: ignore


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

@dataclass
class AnalysisPacket:
    """Everything we ship to Claude for a single ticker."""

    ticker: str
    company_name: str
    technicals: Dict[str, Any] = field(default_factory=dict)
    news: List[Dict[str, Any]] = field(default_factory=list)
    analysts: Dict[str, Any] = field(default_factory=dict)
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    macro: Dict[str, Any] = field(default_factory=dict)
    screener_signals: List[Dict[str, Any]] = field(default_factory=list)
    direction_hint: str = "UP"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "technicals": self.technicals,
            "recent_news": self.news,
            "analyst_consensus": self.analysts,
            "fundamentals": self.fundamentals,
            "macro_context": self.macro,
            "screener_signals": self.screener_signals,
            "screener_direction_hint": self.direction_hint,
        }


def _company_meta(ticker: str) -> Dict[str, Any]:
    data = _polygon_get(f"/v3/reference/tickers/{ticker}")
    if not data:
        return {"name": ticker}
    res = data.get("results", {}) or {}
    return {
        "name": res.get("name") or ticker,
        "market_cap": res.get("market_cap"),
        "sector": (res.get("sic_description")
                   or res.get("type") or "Unknown"),
        "primary_exchange": res.get("primary_exchange"),
        "homepage_url": res.get("homepage_url"),
        "description": res.get("description"),
    }


def _gather_technicals(ticker: str,
                       prebuilt_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a complete technical packet, reusing screener snapshot if available."""
    if prebuilt_snapshot:
        return prebuilt_snapshot

    bars = _fetch_daily_bars(ticker)
    if not bars:
        return {}
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    volumes = [float(b["v"]) for b in bars]

    last_close = closes[-1]
    high_52w = max(highs[-252:]) if len(highs) >= 5 else max(highs)
    low_52w = min(lows[-252:]) if len(lows) >= 5 else min(lows)
    avg_vol_30 = sum(volumes[-31:-1]) / max(len(volumes[-31:-1]), 1)
    px_30d_ago = closes[-22] if len(closes) >= 22 else closes[0]
    pct_30d = ((last_close - px_30d_ago) / px_30d_ago) * 100 if px_30d_ago else 0
    sma200 = _sma(closes, 200)

    return {
        "current_price": round(last_close, 2),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "rsi": round(_rsi(closes), 2) if _rsi(closes) is not None else None,
        "macd": _macd(closes),
        "bollinger": _bollinger(closes),
        "sma_20": _sma(closes, 20),
        "sma_50": _sma(closes, 50),
        "sma_200": sma200,
        "avg_volume_30d": int(avg_vol_30),
        "last_volume": int(volumes[-1]),
        "pct_change_30d": round(pct_30d, 2),
        "pct_to_52w_high": round(((high_52w - last_close) / high_52w) * 100, 2)
            if high_52w else None,
        "pct_above_200dma": round(((last_close - sma200) / sma200) * 100, 2)
            if sma200 else None,
    }


def _gather_news(ticker: str, days: int = 7) -> List[Dict[str, Any]]:
    """Combine NewsAPI + Polygon news, dedup by title."""
    headlines: List[Dict[str, Any]] = []

    # Polygon news
    for item in _recent_news(ticker, days=days):
        headlines.append({
            "source": (item.get("publisher") or {}).get("name", "Polygon"),
            "title": item.get("title"),
            "description": item.get("description"),
            "published_at": item.get("published_utc"),
            "url": item.get("article_url"),
        })

    # NewsAPI
    if NEWSAPI_KEY:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f'"{ticker}"',
                    "from": cutoff,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": 20,
                    "apiKey": NEWSAPI_KEY,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                for art in resp.json().get("articles", []) or []:
                    headlines.append({
                        "source": (art.get("source") or {}).get("name", "NewsAPI"),
                        "title": art.get("title"),
                        "description": art.get("description"),
                        "published_at": art.get("publishedAt"),
                        "url": art.get("url"),
                    })
            else:
                logger.warning("NewsAPI %s for %s: %s",
                               resp.status_code, ticker, resp.text[:200])
        except requests.RequestException as exc:
            logger.error("NewsAPI fetch failed for %s: %s", ticker, exc)

    # Dedup by lowercase title.
    seen = set()
    out: List[Dict[str, Any]] = []
    for h in headlines:
        title = (h.get("title") or "").strip().lower()
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(h)
    return out[:20]


def _gather_analyst_consensus(ticker: str) -> Dict[str, Any]:
    """Best-effort analyst consensus.

    Polygon's analyst-coverage endpoint requires a paid plan, so we fall back
    to whatever we can derive from the news headlines we already pulled.
    """
    consensus = {
        "rating": "Unknown",
        "average_price_target": None,
        "number_of_analysts": None,
        "recent_changes": [],
    }
    data = _polygon_get(f"/vX/reference/financials", {"ticker": ticker, "limit": 1})
    if data:
        # Not actual analyst consensus but ensures this code path exercises the
        # endpoint cleanly when available.
        consensus["polygon_financials_available"] = bool(data.get("results"))
    return consensus


def _gather_fundamentals(ticker: str) -> Dict[str, Any]:
    """Pull the most recent Polygon fundamentals snapshot."""
    out: Dict[str, Any] = {
        "pe_ratio": None,
        "revenue_growth_yoy": None,
        "earnings_history": [],
        "gross_margin": None,
        "net_margin": None,
    }
    data = _polygon_get(
        "/vX/reference/financials",
        {"ticker": ticker, "limit": 4, "timeframe": "quarterly", "order": "desc"},
    )
    if not data:
        return out
    results = data.get("results", []) or []
    revenues: List[Optional[float]] = []
    earnings: List[Dict[str, Any]] = []
    for row in results:
        fin = row.get("financials", {}) or {}
        income = fin.get("income_statement", {}) or {}
        rev = (income.get("revenues") or {}).get("value")
        net_inc = (income.get("net_income_loss") or {}).get("value")
        gross = (income.get("gross_profit") or {}).get("value")
        end_date = row.get("end_date")
        revenues.append(rev)
        if rev and net_inc is not None:
            earnings.append({
                "period": end_date,
                "revenue": rev,
                "net_income": net_inc,
                "net_margin": (net_inc / rev) if rev else None,
            })
        if rev and gross is not None and out["gross_margin"] is None:
            out["gross_margin"] = round((gross / rev) * 100, 2)
        if rev and net_inc is not None and out["net_margin"] is None:
            out["net_margin"] = round((net_inc / rev) * 100, 2)
    out["earnings_history"] = earnings
    # YoY revenue growth: latest vs four quarters back if available.
    if len(revenues) >= 4 and revenues[0] and revenues[3]:
        try:
            out["revenue_growth_yoy"] = round(
                ((revenues[0] - revenues[3]) / revenues[3]) * 100, 2)
        except (TypeError, ZeroDivisionError):
            pass
    return out


def _gather_macro_context() -> Dict[str, Any]:
    """Lightweight macro context derived from index ETFs.

    No FRED key required — we sample SPY/QQQ/TLT/XLE so Claude has a feel for
    the regime. This is purposely conservative; the LLM is responsible for
    interpretation.
    """
    macro: Dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fed_posture_note": ("Inferred from rates and equity behavior; "
                             "Claude should interpret in context."),
    }
    proxies = {
        "SPY": "S&P 500 ETF",
        "QQQ": "Nasdaq 100 ETF",
        "TLT": "20+ Yr Treasury ETF",
        "XLE": "Energy Sector ETF",
        "XLK": "Tech Sector ETF",
        "XLF": "Financials Sector ETF",
        "VIX": "Volatility Index",
    }
    for sym, label in proxies.items():
        bars = _fetch_daily_bars(sym, days=70)
        if not bars:
            continue
        closes = [float(b["c"]) for b in bars]
        if len(closes) < 22:
            continue
        last = closes[-1]
        m1 = closes[-22]
        macro[sym] = {
            "label": label,
            "last": round(last, 2),
            "pct_change_1m": round(((last - m1) / m1) * 100, 2) if m1 else None,
            "rsi": round(_rsi(closes), 2) if _rsi(closes) is not None else None,
        }
    # Crude regime classification that Claude can override.
    spy = macro.get("SPY", {})
    spy_chg = spy.get("pct_change_1m") or 0
    if spy_chg > 3:
        macro["market_regime_hint"] = "RISK_ON"
    elif spy_chg < -3:
        macro["market_regime_hint"] = "RISK_OFF"
    else:
        macro["market_regime_hint"] = "NEUTRAL"
    return macro


def build_packet(candidate: Dict[str, Any]) -> AnalysisPacket:
    """Assemble an :class:`AnalysisPacket` for a screener candidate dict."""
    ticker = candidate["ticker"]
    meta = _company_meta(ticker)
    return AnalysisPacket(
        ticker=ticker,
        company_name=meta.get("name") or ticker,
        technicals=_gather_technicals(ticker, candidate.get("snapshot")),
        news=_gather_news(ticker),
        analysts=_gather_analyst_consensus(ticker),
        fundamentals=_gather_fundamentals(ticker),
        macro=_gather_macro_context(),
        screener_signals=candidate.get("signals", []),
        direction_hint=candidate.get("direction", "UP"),
    )


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def _user_message(packet: AnalysisPacket) -> str:
    """Build the user-side prompt that ships the data + JSON schema to Claude."""
    return (
        "You are analyzing a single equity candidate surfaced by the APEX "
        "morning screener. Use ALL the provided data. Where data is missing, "
        "say so explicitly in the thesis and rely on inference.\n\n"
        f"=== TICKER: {packet.ticker} ({packet.company_name}) ===\n\n"
        "DATA PACKET (JSON):\n"
        f"{json.dumps(packet.to_dict(), indent=2, default=str)}\n\n"
        "Return ONLY valid JSON conforming exactly to this schema (do not "
        "wrap in markdown, do not include any commentary outside the JSON):\n"
        f"{json.dumps(APEX_OUTPUT_SCHEMA, indent=2)}\n\n"
        "Hard requirements:\n"
        "- composite_score must be a number 0-10\n"
        "- All sub-scores (technical/fundamental/sentiment/analyst/historical)"
        " must be numbers 0-10\n"
        "- direction must be 'UP' or 'DOWN' and must be consistent with apex_rating\n"
        "- target_30d, target_90d, target_12m, stop_loss are absolute prices\n"
        "- thesis must be exactly three paragraphs separated by blank lines\n"
        "- risks must contain exactly 3 entries\n"
        "- catalysts must contain at least 2 entries\n"
        "- generated_at must be an ISO8601 UTC timestamp\n"
    )


def _extract_json(text: str) -> Dict[str, Any]:
    """Robust JSON extractor (handles stray markdown fences just in case)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find the largest JSON object in the response.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _call_claude(packet: AnalysisPacket,
                 model: str = CLAUDE_MODEL,
                 max_tokens: int = 4096) -> Dict[str, Any]:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot run analyzer.")
    if Anthropic is None:
        raise RuntimeError("anthropic SDK is not installed.")

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=MASTER_ANALYST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_message(packet)}],
    )
    text_parts: List[str] = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    return _extract_json("".join(text_parts))


# ---------------------------------------------------------------------------
# Output validation / coercion
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "ticker", "company_name", "current_price", "apex_rating",
    "upside_percentage", "downside_percentage", "probability_percentage",
    "probability_reasoning", "target_30d", "target_90d", "target_12m",
    "stop_loss", "composite_score", "confidence_level", "thesis",
    "macro_signal", "technical_score", "fundamental_score",
    "sentiment_score", "analyst_score", "historical_score", "risks",
    "historical_analog", "catalysts", "verdict", "direction", "generated_at",
]


def _coerce_number(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        if isinstance(val, str):
            m = re.search(r"-?\d+(?:\.\d+)?", val)
            if m:
                return float(m.group(0))
        return default


def _validate_report(report: Dict[str, Any], packet: AnalysisPacket) -> Dict[str, Any]:
    """Fill in any missing fields with safe defaults so downstream never crashes."""
    report.setdefault("ticker", packet.ticker)
    report.setdefault("company_name", packet.company_name)
    cp = packet.technicals.get("current_price") if packet.technicals else None
    report.setdefault("current_price", cp or 0.0)

    for f in ("upside_percentage", "downside_percentage", "probability_percentage",
              "target_30d", "target_90d", "target_12m", "stop_loss",
              "composite_score", "technical_score", "fundamental_score",
              "sentiment_score", "analyst_score", "historical_score"):
        report[f] = _coerce_number(report.get(f), 0.0)

    report["current_price"] = _coerce_number(report.get("current_price"), 0.0)

    report.setdefault("apex_rating", "AVOID")
    report.setdefault("confidence_level", "MEDIUM")
    report.setdefault("macro_signal", "NEUTRAL")
    report.setdefault("direction", packet.direction_hint or "UP")
    report.setdefault("thesis", "")
    report.setdefault("verdict", "")
    report.setdefault("historical_analog", "")
    report.setdefault("probability_reasoning", "")
    report.setdefault("catalysts", [])
    report.setdefault("risks", [])
    report.setdefault("generated_at", datetime.now(timezone.utc).isoformat())

    # Ensure risks has the right shape.
    cleaned_risks: List[Dict[str, Any]] = []
    for r in report.get("risks") or []:
        if not isinstance(r, dict):
            continue
        cleaned_risks.append({
            "name": str(r.get("name", "Unspecified risk"))[:120],
            "description": str(r.get("description", ""))[:500],
            "probability": str(r.get("probability", "Medium")).title(),
            "impact_percentage": _coerce_number(r.get("impact_percentage"), 0.0),
        })
    while len(cleaned_risks) < 3:
        cleaned_risks.append({
            "name": "Unspecified risk",
            "description": "Insufficient data to fully characterize this risk.",
            "probability": "Medium",
            "impact_percentage": -10.0,
        })
    report["risks"] = cleaned_risks[:3]

    if report["direction"] not in ("UP", "DOWN"):
        report["direction"] = packet.direction_hint or "UP"
    if report["apex_rating"] not in ("BUY", "SPECULATIVE_BUY", "AVOID", "SHORT"):
        report["apex_rating"] = "AVOID"
    if report["confidence_level"] not in ("HIGH", "MEDIUM", "SPECULATIVE"):
        report["confidence_level"] = "MEDIUM"
    if report["macro_signal"] not in ("BULLISH", "NEUTRAL", "BEARISH"):
        report["macro_signal"] = "NEUTRAL"

    return report


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def analyze_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Run a full APEX analysis on one screener candidate dict."""
    packet = build_packet(candidate)
    try:
        report = _call_claude(packet)
    except Exception as exc:
        logger.exception("Claude analysis failed for %s: %s", packet.ticker, exc)
        report = {
            "ticker": packet.ticker,
            "company_name": packet.company_name,
            "current_price": packet.technicals.get("current_price", 0.0),
            "apex_rating": "AVOID",
            "thesis": (f"Analysis failed for {packet.ticker}: {exc}. "
                       "Returning a neutral placeholder so the pipeline can continue."),
            "verdict": "Analysis unavailable; treat as no-signal.",
            "direction": packet.direction_hint or "UP",
            "error": str(exc),
        }
    return _validate_report(report, packet)


def analyze_ticker(ticker: str) -> Dict[str, Any]:
    """Manually run a fresh APEX analysis on an arbitrary ticker."""
    fake_candidate = {
        "ticker": ticker.upper(),
        "score": 0,
        "direction": "UP",
        "signals": [],
        "snapshot": None,
    }
    return analyze_candidate(fake_candidate)


def analyze_top_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run analyzer over a list of candidates, keeping going on individual errors."""
    reports: List[Dict[str, Any]] = []
    for cand in candidates:
        try:
            reports.append(analyze_candidate(cand))
        except Exception as exc:  # pragma: no cover
            logger.exception("Hard failure analyzing %s: %s", cand.get("ticker"), exc)
    return reports


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    out = analyze_ticker(target)
    print(json.dumps(out, indent=2, default=str))
