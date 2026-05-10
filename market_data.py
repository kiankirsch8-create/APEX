"""Market data client — wraps Polygon.io and a few derived metrics.

All functions are async and resilient: any failure returns a degraded but
well-formed payload so the rest of the pipeline keeps running.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from utils import env, log

POLYGON_BASE = "https://api.polygon.io"
NEWS_BASE = "https://newsapi.org/v2"


class PolygonClient:
    """Minimal async Polygon.io client with the few endpoints APEX needs."""

    def __init__(self, api_key: str | None = None, session: aiohttp.ClientSession | None = None):
        self.api_key = api_key or env("POLYGON_API_KEY") or ""
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "PolygonClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{POLYGON_BASE}{path}"
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    log(f"Polygon {resp.status} on {path}: {text[:200]}", "warning")
                    return {}
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log(f"Polygon error on {path}: {e}", "warning")
            return {}

    # ------------------------------------------------------------------
    # Universe scans
    # ------------------------------------------------------------------
    async def list_active_tickers(self, market: str = "stocks", limit: int = 1000) -> list[dict]:
        """List all active US-listed tickers. Paginates until cursor exhausts."""
        results: list[dict] = []
        params = {"market": market, "active": "true", "limit": limit}
        path = "/v3/reference/tickers"
        for _ in range(20):  # cap pages
            data = await self._get(path, params=params)
            results.extend(data.get("results", []) or [])
            next_url = data.get("next_url")
            if not next_url:
                break
            tail = next_url.split(POLYGON_BASE, 1)[-1]
            if "?" in tail:
                path, qs = tail.split("?", 1)
                params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            else:
                path, params = tail, {}
        return results

    async def grouped_daily(self, date_str: str) -> list[dict]:
        """Daily bar for *every* US ticker on `date_str` (YYYY-MM-DD)."""
        data = await self._get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}", params={"adjusted": "true"})
        return data.get("results", []) or []

    # ------------------------------------------------------------------
    # Per-ticker
    # ------------------------------------------------------------------
    async def ticker_details(self, ticker: str) -> dict:
        data = await self._get(f"/v3/reference/tickers/{ticker}")
        return data.get("results", {}) or {}

    async def aggs(self, ticker: str, days: int = 250) -> list[dict]:
        end = datetime.utcnow().date()
        start = end - timedelta(days=int(days * 1.6) + 10)  # buffer for non-trading days
        data = await self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 5000},
        )
        return data.get("results", []) or []

    async def previous_close(self, ticker: str) -> dict:
        data = await self._get(f"/v2/aggs/ticker/{ticker}/prev", params={"adjusted": "true"})
        rows = data.get("results", []) or []
        return rows[0] if rows else {}

    async def snapshot(self, ticker: str) -> dict:
        data = await self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        return data.get("ticker", {}) or {}

    async def financials(self, ticker: str, limit: int = 4) -> list[dict]:
        data = await self._get(
            "/vX/reference/financials",
            params={"ticker": ticker, "limit": limit, "timeframe": "quarterly"},
        )
        return data.get("results", []) or []

    async def news(self, ticker: str, limit: int = 10) -> list[dict]:
        data = await self._get("/v2/reference/news", params={"ticker": ticker, "limit": limit})
        return data.get("results", []) or []


# ----------------------------------------------------------------------
# Technical indicator helpers — pure python, no numpy dependency
# ----------------------------------------------------------------------
def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = max(d, 0)
        loss = max(-d, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_macd(closes: list[float]) -> dict:
    if len(closes) < 35:
        return {"macd": None, "signal": None, "histogram": None, "state": "INSUFFICIENT"}
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal_line = _ema(macd_line, 9)
    macd = macd_line[-1]
    signal = signal_line[-1]
    hist = macd - signal
    state = "BULLISH" if macd > signal else "BEARISH"
    return {"macd": round(macd, 4), "signal": round(signal, 4), "histogram": round(hist, 4), "state": state}


def compute_sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


def compute_bbands(closes: list[float], period: int = 20, stds: float = 2.0) -> dict:
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None, "position": None}
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    sd = var ** 0.5
    upper = mean + stds * sd
    lower = mean - stds * sd
    last = closes[-1]
    pos = (last - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(upper, 4),
        "middle": round(mean, 4),
        "lower": round(lower, 4),
        "position": round(pos, 3),
    }


def fifty_two_week(rows: list[dict]) -> tuple[float | None, float | None]:
    if not rows:
        return None, None
    recent = rows[-252:] if len(rows) >= 252 else rows
    highs = [r.get("h") for r in recent if r.get("h") is not None]
    lows = [r.get("l") for r in recent if r.get("l") is not None]
    if not highs or not lows:
        return None, None
    return max(highs), min(lows)


def avg_volume(rows: list[dict], days: int = 30) -> float | None:
    if not rows:
        return None
    recent = rows[-days:]
    vols = [r.get("v") for r in recent if r.get("v") is not None]
    if not vols:
        return None
    return sum(vols) / len(vols)


def build_indicator_pack(rows: list[dict]) -> dict:
    """From a list of daily aggregate bars produce the indicator dict
    consumed by analyzer.py + screeners."""
    if not rows:
        return {}
    closes = [r.get("c") for r in rows if r.get("c") is not None]
    last = rows[-1]
    fwh, fwl = fifty_two_week(rows)
    av30 = avg_volume(rows, 30)
    cur_vol = last.get("v") or 0
    vol_ratio = (cur_vol / av30) if av30 else None
    return {
        "current_price": last.get("c"),
        "open": last.get("o"),
        "high": last.get("h"),
        "low": last.get("l"),
        "volume": cur_vol,
        "avg_volume_30d": av30,
        "volume_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "fifty_two_week_high": fwh,
        "fifty_two_week_low": fwl,
        "rsi_14": compute_rsi(closes, 14),
        "macd": compute_macd(closes),
        "sma_20": compute_sma(closes, 20),
        "sma_50": compute_sma(closes, 50),
        "sma_200": compute_sma(closes, 200),
        "bbands": compute_bbands(closes, 20, 2.0),
        "pct_from_52w_high": (
            round(((last.get("c") - fwh) / fwh) * 100, 2) if fwh and last.get("c") else None
        ),
        "pct_from_52w_low": (
            round(((last.get("c") - fwl) / fwl) * 100, 2) if fwl and last.get("c") else None
        ),
    }


# ----------------------------------------------------------------------
# News
# ----------------------------------------------------------------------
async def fetch_news_headlines(ticker: str, company_name: str | None = None, days: int = 7) -> list[dict]:
    api_key = env("NEWSAPI_KEY")
    if not api_key:
        return []
    query = company_name or ticker
    params = {
        "q": f'"{query}"',
        "from": (datetime.utcnow() - timedelta(days=days)).date().isoformat(),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 15,
        "apiKey": api_key,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"{NEWS_BASE}/everything", params=params) as r:
                if r.status >= 400:
                    return []
                data = await r.json()
                return [
                    {
                        "title": a.get("title"),
                        "source": (a.get("source") or {}).get("name"),
                        "url": a.get("url"),
                        "published_at": a.get("publishedAt"),
                        "description": a.get("description"),
                    }
                    for a in (data.get("articles") or [])[:10]
                ]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log(f"NewsAPI error for {ticker}: {e}", "warning")
        return []
