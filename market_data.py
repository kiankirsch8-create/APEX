"""Market data client — yfinance backed.

yfinance is free, key-less, and rate-limit free. We expose the same shape
the rest of the project already consumes (Polygon-style dicts) so analyzer
and screener helpers continue to work unchanged.

The blocking yfinance calls are dispatched to threads via `asyncio.to_thread`
so we keep the async interface used elsewhere.

The class `YFClient` is the new primary client. `PolygonClient` is kept as a
backward-compatible alias so analyzer.py / api.py continue to import the same
name without modification.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import pandas as pd
import yfinance as yf

from utils import env, log

NEWS_BASE = "https://newsapi.org/v2"


# ---------------------------------------------------------------------------
# yfinance-backed client
# ---------------------------------------------------------------------------
class YFClient:
    """Async-friendly wrapper around yfinance.

    Each public method returns the same dict shape the legacy Polygon client
    returned, so downstream code keeps working without changes.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None  # used for NewsAPI

    async def __aenter__(self) -> "YFClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Per-ticker reference / fundamentals
    # ------------------------------------------------------------------
    async def ticker_details(self, ticker: str) -> dict:
        return await asyncio.to_thread(self._sync_ticker_details, ticker)

    @staticmethod
    def _sync_ticker_details(ticker: str) -> dict:
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as e:  # noqa: BLE001 — yfinance can throw a wide set of errors
            log(f"yfinance ticker_details {ticker}: {e}", "warning")
            return {}
        if not info:
            return {}
        return {
            "name": info.get("longName") or info.get("shortName") or ticker,
            "ticker": ticker,
            "market_cap": info.get("marketCap"),
            "primary_exchange": info.get("exchange") or info.get("fullExchangeName"),
            "description": info.get("longBusinessSummary"),
            "homepage_url": info.get("website"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "sic_description": info.get("industry") or info.get("sector") or "",
            "list_date": _format_first_trade(info),
            "currency": info.get("currency") or "USD",
            "country": info.get("country"),
            "_raw": info,
        }

    async def aggs(self, ticker: str, days: int = 250) -> list[dict]:
        return await asyncio.to_thread(self._sync_aggs, ticker, days)

    @staticmethod
    def _sync_aggs(ticker: str, days: int) -> list[dict]:
        try:
            period = "2y" if days > 250 else "1y"
            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        except Exception as e:  # noqa: BLE001
            log(f"yfinance aggs {ticker}: {e}", "warning")
            return []
        if df is None or df.empty:
            return []
        return _df_to_rows(df.tail(days))

    async def previous_close(self, ticker: str) -> dict:
        rows = await self.aggs(ticker, days=2)
        return rows[-1] if rows else {}

    async def snapshot(self, ticker: str) -> dict:
        return await asyncio.to_thread(self._sync_snapshot, ticker)

    @staticmethod
    def _sync_snapshot(ticker: str) -> dict:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
        except Exception as e:  # noqa: BLE001
            log(f"yfinance snapshot {ticker}: {e}", "warning")
            return {}

        last = info.get("regularMarketPrice") or info.get("currentPrice")
        prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
        change = (last - prev) if (last is not None and prev is not None) else None
        change_pct = info.get("regularMarketChangePercent")
        if change_pct is None and last and prev:
            change_pct = (last - prev) / prev * 100

        short_pct_float = info.get("shortPercentOfFloat")  # 0..1 in yfinance
        return {
            "ticker": ticker,
            "day": {"c": last, "v": info.get("regularMarketVolume")},
            "prevDay": {"c": prev, "v": info.get("regularMarketPreviousClose")},
            "todaysChange": change,
            "todaysChangePerc": change_pct,
            "shortInterest": {
                "percentOfFloat": (short_pct_float * 100) if short_pct_float else None,
                "shares": info.get("sharesShort"),
                "shortRatio": info.get("shortRatio"),
            },
            "_info": info,
        }

    async def financials(self, ticker: str, limit: int = 4) -> list[dict]:
        return await asyncio.to_thread(self._sync_financials, ticker, limit)

    @staticmethod
    def _sync_financials(ticker: str, limit: int) -> list[dict]:
        try:
            t = yf.Ticker(ticker)
            inc = t.quarterly_financials  # rows = line items, cols = period end dates
            bs = t.quarterly_balance_sheet
        except Exception as e:  # noqa: BLE001
            log(f"yfinance financials {ticker}: {e}", "warning")
            return []
        if inc is None or getattr(inc, "empty", True):
            return []
        cols = list(inc.columns)  # newest first usually
        results: list[dict] = []
        for col in cols[:limit]:
            rev = _df_at(inc, col, ["Total Revenue", "TotalRevenue", "Revenue"])
            ni = _df_at(
                inc, col,
                ["Net Income", "NetIncome", "Net Income Common Stockholders", "NetIncomeCommonStockholders"],
            )
            eps_basic = _df_at(inc, col, ["Basic EPS", "BasicEPS"])
            gross = _df_at(inc, col, ["Gross Profit", "GrossProfit"])

            liab = _df_at(
                bs, col,
                [
                    "Total Liabilities Net Minority Interest",
                    "TotalLiabilitiesNetMinorityInterest",
                    "Total Liab",
                    "TotalLiabilities",
                ],
            )
            eq = _df_at(
                bs, col,
                [
                    "Stockholders Equity",
                    "StockholdersEquity",
                    "Total Stockholder Equity",
                    "TotalStockholderEquity",
                ],
            )
            cash = _df_at(
                bs, col,
                [
                    "Cash And Cash Equivalents",
                    "CashAndCashEquivalents",
                    "Cash",
                    "Cash Cash Equivalents And Short Term Investments",
                ],
            )
            current_assets = _df_at(bs, col, ["Current Assets", "CurrentAssets", "Total Current Assets"])

            end_date = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
            results.append(
                {
                    "fiscal_period": "Q",
                    "end_date": end_date,
                    "financials": {
                        "income_statement": {
                            "revenues": {"value": rev},
                            "net_income_loss": {"value": ni},
                            "basic_earnings_per_share": {"value": eps_basic},
                            "gross_profit": {"value": gross},
                        },
                        "balance_sheet": {
                            "liabilities": {"value": liab},
                            "equity": {"value": eq},
                            "cash": {"value": cash},
                            "current_assets": {"value": current_assets},
                        },
                    },
                }
            )
        return results

    async def news(self, ticker: str, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._sync_news, ticker, limit)

    @staticmethod
    def _sync_news(ticker: str, limit: int) -> list[dict]:
        try:
            items = yf.Ticker(ticker).news or []
        except Exception as e:  # noqa: BLE001
            log(f"yfinance news {ticker}: {e}", "warning")
            return []
        out: list[dict] = []
        for n in items[:limit]:
            if not isinstance(n, dict):
                continue
            content = n.get("content") if "content" in n else None
            if isinstance(content, dict):
                provider = (content.get("provider") or {}).get("displayName")
                url = (content.get("canonicalUrl") or content.get("clickThroughUrl") or {}).get("url")
                out.append(
                    {
                        "title": content.get("title"),
                        "publisher": {"name": provider},
                        "article_url": url,
                        "published_utc": content.get("pubDate"),
                        "description": content.get("summary"),
                    }
                )
            else:
                pub_ts = n.get("providerPublishTime")
                pub_iso = (
                    datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                    if pub_ts
                    else None
                )
                out.append(
                    {
                        "title": n.get("title"),
                        "publisher": {"name": n.get("publisher")},
                        "article_url": n.get("link"),
                        "published_utc": pub_iso,
                        "description": n.get("summary"),
                    }
                )
        return out

    # ------------------------------------------------------------------
    # Universe scans — batch download
    # ------------------------------------------------------------------
    async def batch_history(
        self,
        tickers: list[str],
        period: str = "1y",
        chunk_size: int = 60,
    ) -> dict[str, list[dict]]:
        """Download daily OHLCV history for many tickers in chunks.

        Yahoo limits very large multi-ticker queries, so we split into
        ``chunk_size`` ticker chunks and run them sequentially. yfinance
        already parallelizes inside each chunk via threads.
        """
        out: dict[str, list[dict]] = {}
        chunks = [tickers[i : i + chunk_size] for i in range(0, len(tickers), chunk_size)]
        for chunk in chunks:
            partial = await asyncio.to_thread(self._sync_batch_history, chunk, period)
            out.update(partial)
        return out

    @staticmethod
    def _sync_batch_history(tickers: list[str], period: str) -> dict[str, list[dict]]:
        try:
            df = yf.download(
                tickers=tickers,
                period=period,
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001
            log(f"yfinance batch_history error ({len(tickers)} tickers): {e}", "warning")
            return {}
        if df is None or getattr(df, "empty", True):
            return {}
        out: dict[str, list[dict]] = {}
        if len(tickers) == 1:
            out[tickers[0]] = _df_to_rows(df)
            return out
        for t in tickers:
            try:
                sub = df[t]
                out[t] = _df_to_rows(sub)
            except (KeyError, AttributeError, ValueError):
                out[t] = []
        return out


# ---------------------------------------------------------------------------
# Backwards-compat alias — analyzer.py / api.py keep importing PolygonClient
# under this name. The implementation is now yfinance.
# ---------------------------------------------------------------------------
PolygonClient = YFClient


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------
def _df_to_rows(df) -> list[dict]:
    rows: list[dict] = []
    if df is None or getattr(df, "empty", True):
        return rows
    for idx, row in df.iterrows():
        try:
            close = row.get("Close")
            if close is None or pd.isna(close):
                continue
            o = row.get("Open")
            h = row.get("High")
            l = row.get("Low")  # noqa: E741
            v = row.get("Volume")
            rows.append(
                {
                    "t": int(idx.timestamp() * 1000) if hasattr(idx, "timestamp") else None,
                    "o": float(o) if o is not None and not pd.isna(o) else None,
                    "h": float(h) if h is not None and not pd.isna(h) else None,
                    "l": float(l) if l is not None and not pd.isna(l) else None,
                    "c": float(close),
                    "v": int(v) if v is not None and not pd.isna(v) else 0,
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _df_at(df, col, candidates: list[str]):
    """Look up a single value in a yfinance DataFrame, returning None on miss."""
    if df is None or getattr(df, "empty", True):
        return None
    if col not in df.columns:
        return None
    for name in candidates:
        try:
            if name in df.index:
                v = df.loc[name, col]
                if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
                    return None
                return float(v)
        except (KeyError, ValueError, TypeError):
            continue
    return None


def _format_first_trade(info: dict) -> str | None:
    epoch = (
        info.get("firstTradeDateEpochUtc")
        or info.get("firstTradeDateMilliseconds")
        or info.get("firstTradeDate")
    )
    if not epoch:
        return None
    try:
        if epoch > 10**12:
            epoch = epoch / 1000
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Technical indicator helpers (pure python, identical signatures to before)
# ---------------------------------------------------------------------------
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
    if not closes:
        return {}
    last = rows[-1]
    fwh, fwl = fifty_two_week(rows)
    av30 = avg_volume(rows, 30)
    cur_vol = last.get("v") or 0
    vol_ratio = (cur_vol / av30) if av30 else None
    cur = last.get("c")
    return {
        "current_price": cur,
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
            round(((cur - fwh) / fwh) * 100, 2) if fwh and cur else None
        ),
        "pct_from_52w_low": (
            round(((cur - fwl) / fwl) * 100, 2) if fwl and cur else None
        ),
    }


# ---------------------------------------------------------------------------
# News (NewsAPI — separate from yfinance)
# ---------------------------------------------------------------------------
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
