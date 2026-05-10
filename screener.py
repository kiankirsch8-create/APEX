"""APEX market screener.

Scans the S&P 500 and Nasdaq 100 universes every morning and returns the top
five highest-conviction opportunities (long or short) using the Polygon.io REST
API. The screener is *signal driven*: each candidate accumulates points for the
bullish and bearish patterns described in the APEX spec, and the top five raw
scores are surfaced for downstream Claude analysis.

The module is safe to import — it never performs network IO at import time.
Call :func:`run_screener` to execute a scan.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("apex.screener")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE = "https://api.polygon.io"

# A reasonably current snapshot of S&P 500 + Nasdaq 100 tickers. We deliberately
# hard-code the universe so the screener is deterministic, hermetic, and does
# not depend on a third-party constituents endpoint that may rate-limit us.
SP500_TICKERS: List[str] = [
    "MMM", "AOS", "ABT", "ABBV", "ACN", "ADBE", "AMD", "AES", "AFL", "A",
    "APD", "ABNB", "AKAM", "ALB", "ARE", "ALGN", "ALLE", "LNT", "ALL", "GOOGL",
    "GOOG", "MO", "AMZN", "AMCR", "AEE", "AEP", "AXP", "AIG", "AMT", "AWK",
    "AMP", "AME", "AMGN", "APH", "ADI", "ANSS", "AON", "APA", "AAPL", "AMAT",
    "APTV", "ACGL", "ADM", "ANET", "AJG", "AIZ", "T", "ATO", "ADSK", "ADP",
    "AZO", "AVB", "AVY", "AXON", "BKR", "BALL", "BAC", "BAX", "BDX", "BRK.B",
    "BBY", "BIO", "TECH", "BIIB", "BLK", "BX", "BK", "BA", "BKNG", "BWA",
    "BSX", "BMY", "AVGO", "BR", "BRO", "BF.B", "BLDR", "BG", "CDNS", "CZR",
    "CPT", "CPB", "COF", "CAH", "KMX", "CCL", "CARR", "CTLT", "CAT", "CBOE",
    "CBRE", "CDW", "CE", "COR", "CNC", "CNP", "CF", "CHRW", "CRL", "SCHW",
    "CHTR", "CVX", "CMG", "CB", "CHD", "CI", "CINF", "CTAS", "CSCO", "C",
    "CFG", "CLX", "CME", "CMS", "KO", "CTSH", "CL", "CMCSA", "CMA", "CAG",
    "COP", "ED", "STZ", "CEG", "COO", "CPRT", "GLW", "CTVA", "CSGP", "COST",
    "CTRA", "CCI", "CSX", "CMI", "CVS", "DHR", "DRI", "DVA", "DAY", "DECK",
    "DE", "DAL", "XRAY", "DVN", "DXCM", "FANG", "DLR", "DFS", "DG", "DLTR",
    "D", "DPZ", "DOV", "DOW", "DHI", "DTE", "DUK", "DD", "EMN", "ETN",
    "EBAY", "ECL", "EIX", "EW", "EA", "ELV", "LLY", "EMR", "ENPH", "ETR",
    "EOG", "EPAM", "EQT", "EFX", "EQIX", "EQR", "ESS", "EL", "ETSY", "EG",
    "EVRG", "ES", "EXC", "EXPE", "EXPD", "EXR", "XOM", "FFIV", "FDS", "FICO",
    "FAST", "FRT", "FDX", "FIS", "FITB", "FSLR", "FE", "FI", "FMC", "F",
    "FTNT", "FTV", "FOXA", "FOX", "BEN", "FCX", "GRMN", "IT", "GE", "GEHC",
    "GEN", "GD", "GIS", "GM", "GPC", "GILD", "GPN", "GL", "GS", "HAL",
    "HIG", "HAS", "HCA", "DOC", "HSIC", "HSY", "HES", "HPE", "HLT", "HOLX",
    "HD", "HON", "HRL", "HST", "HWM", "HPQ", "HUBB", "HUM", "HBAN", "HII",
    "IBM", "IEX", "IDXX", "ITW", "ILMN", "INCY", "IR", "PODD", "INTC", "ICE",
    "IFF", "IP", "IPG", "INTU", "ISRG", "IVZ", "INVH", "IQV", "IRM", "JBHT",
    "JBL", "JKHY", "J", "JNJ", "JCI", "JPM", "JNPR", "K", "KVUE", "KDP",
    "KEY", "KEYS", "KMB", "KIM", "KMI", "KLAC", "KHC", "KR", "LHX", "LH",
    "LRCX", "LW", "LVS", "LDOS", "LEN", "LIN", "LYV", "LKQ", "LMT", "L",
    "LOW", "LULU", "LYB", "MTB", "MRO", "MPC", "MKTX", "MAR", "MMC", "MLM",
    "MAS", "MA", "MTCH", "MKC", "MCD", "MCK", "MDT", "MRK", "META", "MET",
    "MTD", "MGM", "MCHP", "MU", "MSFT", "MAA", "MRNA", "MHK", "MOH", "TAP",
    "MDLZ", "MPWR", "MNST", "MCO", "MS", "MOS", "MSI", "MSCI", "NDAQ", "NTAP",
    "NFLX", "NEM", "NWSA", "NWS", "NEE", "NKE", "NI", "NDSN", "NSC", "NTRS",
    "NOC", "NCLH", "NRG", "NUE", "NVDA", "NVR", "NXPI", "ORLY", "OXY", "ODFL",
    "OMC", "ON", "OKE", "ORCL", "OTIS", "PCAR", "PKG", "PANW", "PARA", "PH",
    "PAYX", "PAYC", "PYPL", "PNR", "PEP", "PFE", "PCG", "PM", "PSX", "PNW",
    "PXD", "PNC", "POOL", "PPG", "PPL", "PFG", "PG", "PGR", "PLD", "PRU",
    "PEG", "PTC", "PSA", "PHM", "QRVO", "PWR", "QCOM", "DGX", "RL", "RJF",
    "RTX", "O", "REG", "REGN", "RF", "RSG", "RMD", "RVTY", "ROK", "ROL",
    "ROP", "ROST", "RCL", "SPGI", "CRM", "SBAC", "SLB", "STX", "SRE", "NOW",
    "SHW", "SPG", "SWKS", "SJM", "SNA", "SO", "LUV", "SWK", "SBUX", "STT",
    "STLD", "STE", "SYK", "SMCI", "SYF", "SNPS", "SYY", "TMUS", "TROW", "TTWO",
    "TPR", "TRGP", "TGT", "TEL", "TDY", "TFX", "TER", "TSLA", "TXN", "TXT",
    "TMO", "TJX", "TSCO", "TT", "TDG", "TRV", "TRMB", "TFC", "TYL", "TSN",
    "USB", "UBER", "UDR", "ULTA", "UNP", "UAL", "UPS", "URI", "UNH", "UHS",
    "VLO", "VTR", "VRSN", "VRSK", "VZ", "VRTX", "VFC", "VTRS", "V", "VICI",
    "VMC", "WAB", "WBA", "WMT", "DIS", "WBD", "WM", "WAT", "WEC", "WFC",
    "WELL", "WST", "WDC", "WRK", "WY", "WHR", "WMB", "WTW", "GWW", "WYNN",
    "XEL", "XYL", "YUM", "ZBRA", "ZBH", "ZTS",
]

NASDAQ100_TICKERS: List[str] = [
    "ADBE", "ADP", "ABNB", "GOOGL", "GOOG", "AMZN", "AMD", "AEP", "AMGN",
    "ADI", "ANSS", "AAPL", "AMAT", "ASML", "AZN", "TEAM", "ADSK", "BKR",
    "BIIB", "BKNG", "AVGO", "CDNS", "CDW", "CHTR", "CTAS", "CSCO", "CCEP",
    "CMCSA", "CEG", "CPRT", "COST", "CRWD", "CSX", "DDOG", "DXCM", "FANG",
    "DLTR", "DASH", "EA", "EXC", "FAST", "FTNT", "GEHC", "GILD", "HON",
    "IDXX", "ILMN", "INTC", "INTU", "ISRG", "KDP", "KLAC", "KHC", "LRCX",
    "LIN", "LULU", "MAR", "MRVL", "MELI", "META", "MCHP", "MU", "MSFT",
    "MRNA", "MDLZ", "MNST", "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY",
    "PCAR", "PANW", "PAYX", "PYPL", "PEP", "QCOM", "REGN", "ROP", "ROST",
    "SBUX", "SNPS", "TTWO", "TMUS", "TSLA", "TXN", "VRSK", "VRTX", "WBA",
    "WBD", "WDAY", "XEL", "ZS", "ARM",
]


@dataclass
class Signal:
    """A single bullish or bearish trigger detected on a ticker."""

    name: str
    direction: str  # "BULLISH" or "BEARISH"
    weight: float  # how strongly the signal contributes to the raw score
    detail: str = ""


@dataclass
class ScreenerCandidate:
    """A scored candidate emitted by the screener."""

    ticker: str
    score: float
    direction: str  # net direction: UP or DOWN
    signals: List[Signal] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "score": round(self.score, 2),
            "direction": self.direction,
            "signals": [asdict(s) for s in self.signals],
            "snapshot": self.snapshot,
        }


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------

def _polygon_get(path: str, params: Optional[Dict[str, Any]] = None,
                 timeout: int = 15) -> Optional[Dict[str, Any]]:
    """GET helper that swallows errors and returns None on failure."""
    if not POLYGON_API_KEY:
        logger.warning("POLYGON_API_KEY not configured; skipping %s", path)
        return None
    params = dict(params or {})
    params["apiKey"] = POLYGON_API_KEY
    url = f"{POLYGON_BASE}{path}"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            logger.warning("Polygon 429 rate limit on %s — backing off 12s", path)
            time.sleep(12)
            resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code >= 400:
            logger.error("Polygon %s -> %s: %s", path, resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Polygon request to %s failed: %s", path, exc)
        return None


def _fetch_daily_bars(ticker: str, days: int = 260) -> List[Dict[str, Any]]:
    """Fetch ~1 year of daily OHLCV bars."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days + 30)
    data = _polygon_get(
        f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
        {"adjusted": "true", "sort": "asc", "limit": 5000},
    )
    if not data or "results" not in data:
        return []
    return data["results"] or []


# ---------------------------------------------------------------------------
# Indicator calculations (pure-python, no numpy dependency required)
# ---------------------------------------------------------------------------

def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sma(values: Iterable[float], period: int) -> Optional[float]:
    vals = list(values)[-period:]
    if len(vals) < period:
        return None
    return sum(vals) / period


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _macd(closes: List[float]) -> Optional[Dict[str, float]]:
    if len(closes) < 35:
        return None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None
    macd_line = ema12 - ema26
    # Build a short MACD history for the signal line.
    macd_hist: List[float] = []
    for i in range(26, len(closes) + 1):
        slice_closes = closes[:i]
        e12 = _ema(slice_closes, 12)
        e26 = _ema(slice_closes, 26)
        if e12 is not None and e26 is not None:
            macd_hist.append(e12 - e26)
    signal = _ema(macd_hist, 9) if len(macd_hist) >= 9 else None
    return {
        "macd": macd_line,
        "signal": signal if signal is not None else macd_line,
        "histogram": macd_line - (signal if signal is not None else macd_line),
    }


def _bollinger(closes: List[float], period: int = 20, k: float = 2.0) -> Optional[Dict[str, float]]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    std = math.sqrt(var)
    return {"upper": mean + k * std, "middle": mean, "lower": mean - k * std}


# ---------------------------------------------------------------------------
# News / corporate-action helpers
# ---------------------------------------------------------------------------

def _recent_news(ticker: str, days: int = 7) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data = _polygon_get(
        "/v2/reference/news",
        {"ticker": ticker, "limit": 25, "order": "desc",
         "published_utc.gte": cutoff},
    )
    if not data:
        return []
    return data.get("results", []) or []


def _detect_analyst_upgrade(news: List[Dict[str, Any]]) -> Optional[str]:
    keywords = ("upgrade", "raises price target", "raised price target",
                "price target raised", "buy rating", "outperform", "overweight")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    for item in news:
        title = (item.get("title") or "").lower()
        published = item.get("published_utc", "")
        try:
            ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        if any(kw in title for kw in keywords):
            return item.get("title")
    return None


def _detect_earnings_surprise(news: List[Dict[str, Any]]) -> Optional[str]:
    positive = ("beats earnings", "tops estimates", "earnings beat",
                "raises guidance", "raised guidance", "record revenue")
    for item in news[:25]:
        title = (item.get("title") or "").lower()
        if any(kw in title for kw in positive):
            return item.get("title")
    return None


def _detect_negative_earnings(news: List[Dict[str, Any]]) -> Optional[str]:
    negatives = ("misses earnings", "earnings miss", "guidance cut",
                 "lowers guidance", "cuts guidance", "revenue miss")
    for item in news[:25]:
        title = (item.get("title") or "").lower()
        if any(kw in title for kw in negatives):
            return item.get("title")
    return None


def _insider_activity(ticker: str) -> Dict[str, float]:
    """Return aggregate insider buy/sell dollar volume for the last 14 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = _polygon_get(
        f"/vX/reference/insider-transactions",
        {"ticker": ticker, "limit": 100,
         "transaction_date.gte": cutoff},
    )
    buys = 0.0
    sells = 0.0
    if not data:
        return {"buys": 0.0, "sells": 0.0}
    for row in data.get("results", []) or []:
        try:
            shares = float(row.get("shares") or 0)
            price = float(row.get("price") or 0)
            value = abs(shares * price)
        except (TypeError, ValueError):
            continue
        code = (row.get("transaction_code") or "").upper()
        if code in ("P", "A"):
            buys += value
        elif code in ("S", "D"):
            sells += value
    return {"buys": buys, "sells": sells}


# ---------------------------------------------------------------------------
# Per-ticker scoring
# ---------------------------------------------------------------------------

def _score_ticker(ticker: str) -> Optional[ScreenerCandidate]:
    bars = _fetch_daily_bars(ticker)
    if len(bars) < 60:
        logger.debug("Skipping %s: insufficient bars (%d)", ticker, len(bars))
        return None

    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    volumes = [float(b["v"]) for b in bars]

    last_close = closes[-1]
    high_52w = max(highs[-252:]) if len(highs) >= 5 else max(highs)
    low_52w = min(lows[-252:]) if len(lows) >= 5 else min(lows)
    avg_vol_30 = sum(volumes[-31:-1]) / max(len(volumes[-31:-1]), 1)
    last_vol = volumes[-1]

    rsi = _rsi(closes)
    macd = _macd(closes)
    bb = _bollinger(closes)
    sma200 = _sma(closes, 200)
    sma50 = _sma(closes, 50)
    sma20 = _sma(closes, 20)

    # 30-day price change
    px_30d_ago = closes[-22] if len(closes) >= 22 else closes[0]
    pct_change_30d = ((last_close - px_30d_ago) / px_30d_ago) * 100 if px_30d_ago else 0.0

    pct_to_high = ((high_52w - last_close) / high_52w) * 100 if high_52w else 100
    pct_above_sma200 = ((last_close - sma200) / sma200) * 100 if sma200 else 0.0

    news = _recent_news(ticker, days=7)
    upgrade = _detect_analyst_upgrade(news)
    earnings_pos = _detect_earnings_surprise(news)
    earnings_neg = _detect_negative_earnings(news)
    insiders = _insider_activity(ticker)

    signals: List[Signal] = []

    # ---- Bullish signals -------------------------------------------------
    if avg_vol_30 > 0 and last_vol >= 3 * avg_vol_30:
        signals.append(Signal("Volume 3x+ 30d avg", "BULLISH", 18,
                              f"vol={last_vol:.0f} vs avg={avg_vol_30:.0f}"))
    if rsi is not None and 28 <= rsi <= 38:
        signals.append(Signal("Oversold RSI 28-38", "BULLISH", 14, f"RSI={rsi:.1f}"))
    if pct_to_high <= 3 and pct_to_high >= 0:
        signals.append(Signal("Within 3% of 52w high", "BULLISH", 16,
                              f"price={last_close:.2f} high={high_52w:.2f}"))
    if upgrade:
        signals.append(Signal("Analyst upgrade <48h", "BULLISH", 20, upgrade[:140]))
    if earnings_pos:
        signals.append(Signal("Positive earnings surprise <7d", "BULLISH", 18,
                              earnings_pos[:140]))
    if insiders["buys"] > 0 and insiders["buys"] > insiders["sells"]:
        signals.append(Signal("Insider buying <14d", "BULLISH", 12,
                              f"${insiders['buys']:,.0f}"))
    if -40 <= pct_change_30d <= -15 and rsi is not None and rsi > 30:
        signals.append(Signal("Overreaction drawdown", "BULLISH", 15,
                              f"{pct_change_30d:.1f}% in 30d, RSI rebounding"))
    if macd and macd["histogram"] > 0 and macd["macd"] > macd["signal"]:
        signals.append(Signal("MACD bullish cross", "BULLISH", 6,
                              f"hist={macd['histogram']:.3f}"))
    if bb and last_close <= bb["lower"] * 1.01:
        signals.append(Signal("Touch of lower Bollinger", "BULLISH", 4,
                              f"close={last_close:.2f} bb_lo={bb['lower']:.2f}"))

    # ---- Bearish signals -------------------------------------------------
    if rsi is not None and rsi > 72:
        signals.append(Signal("Overbought RSI >72", "BEARISH", 16, f"RSI={rsi:.1f}"))
    if insiders["sells"] >= 5_000_000:
        signals.append(Signal("Insider selling >$5M", "BEARISH", 18,
                              f"${insiders['sells']:,.0f}"))
    if earnings_neg:
        signals.append(Signal("Earnings miss + guidance cut <7d", "BEARISH", 22,
                              earnings_neg[:140]))
    if pct_above_sma200 >= 25:
        signals.append(Signal("Extended >25% above 200-DMA", "BEARISH", 12,
                              f"+{pct_above_sma200:.1f}%"))
    if macd and macd["histogram"] < 0 and macd["macd"] < macd["signal"]:
        signals.append(Signal("MACD bearish cross", "BEARISH", 5,
                              f"hist={macd['histogram']:.3f}"))

    if not signals:
        return None

    bull = sum(s.weight for s in signals if s.direction == "BULLISH")
    bear = sum(s.weight for s in signals if s.direction == "BEARISH")
    net = bull - bear
    raw_score = max(bull, bear)
    # Reward conviction: scale by absolute imbalance.
    raw_score += min(abs(net) * 0.3, 15)
    raw_score = min(raw_score, 100.0)

    direction = "UP" if bull >= bear else "DOWN"

    snapshot = {
        "current_price": round(last_close, 2),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "rsi": round(rsi, 2) if rsi is not None else None,
        "macd": {k: round(v, 4) for k, v in macd.items()} if macd else None,
        "bollinger": {k: round(v, 2) for k, v in bb.items()} if bb else None,
        "sma_20": round(sma20, 2) if sma20 else None,
        "sma_50": round(sma50, 2) if sma50 else None,
        "sma_200": round(sma200, 2) if sma200 else None,
        "avg_volume_30d": int(avg_vol_30),
        "last_volume": int(last_vol),
        "pct_change_30d": round(pct_change_30d, 2),
        "pct_to_52w_high": round(pct_to_high, 2),
        "pct_above_200dma": round(pct_above_sma200, 2),
        "insider_buys_14d": insiders["buys"],
        "insider_sells_14d": insiders["sells"],
        "recent_headlines": [n.get("title") for n in news[:5] if n.get("title")],
        "bullish_weight": bull,
        "bearish_weight": bear,
    }

    return ScreenerCandidate(
        ticker=ticker,
        score=raw_score,
        direction=direction,
        signals=signals,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def universe() -> List[str]:
    """Return the deduplicated S&P 500 ∪ Nasdaq 100 universe."""
    seen = set()
    out: List[str] = []
    for t in SP500_TICKERS + NASDAQ100_TICKERS:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def run_screener(top_n: int = 5,
                 tickers: Optional[List[str]] = None,
                 throttle: float = 0.12) -> List[Dict[str, Any]]:
    """Scan the universe and return the ``top_n`` candidates as plain dicts.

    Parameters
    ----------
    top_n:
        How many candidates to surface to the analyzer.
    tickers:
        Optional explicit list (used by tests); falls back to the full universe.
    throttle:
        Sleep between Polygon calls to stay under free-tier rate limits.
    """
    pool = tickers if tickers is not None else universe()
    logger.info("APEX screener starting on %d tickers", len(pool))

    candidates: List[ScreenerCandidate] = []
    for i, ticker in enumerate(pool, 1):
        try:
            cand = _score_ticker(ticker)
            if cand:
                candidates.append(cand)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Error scoring %s: %s", ticker, exc)
        if throttle:
            time.sleep(throttle)
        if i % 25 == 0:
            logger.info("...screened %d/%d (kept %d)", i, len(pool), len(candidates))

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[:top_n]
    logger.info("APEX screener complete: %d candidates -> top %d",
                len(candidates), len(top))
    return [c.to_dict() for c in top]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    import json
    picks = run_screener()
    print(json.dumps(picks, indent=2, default=str))
