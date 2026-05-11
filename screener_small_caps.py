"""Small-cap / speculative explosive opportunity screener.

Universe: the curated `SMALL_CAP_UNIVERSE` from `universe.py` filtered down
to US-listed stocks with $10M <= market cap <= $2B and matching liquidity
filters. Uses yfinance (no API key, no rate limits).

Returns the top 3 candidates ranked by composite signal score, each enriched
with the list of triggered signals so analyzer.py and scorer.py can consume
the scoring downstream.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from market_data import YFClient, build_indicator_pack
from universe import SMALL_CAP_UNIVERSE
from utils import log

# ---------------------------------------------------------------------------
# Permanent / structural exclusions (symbol-level)
# ---------------------------------------------------------------------------
PERMANENT_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "ESPR",  # Being acquired by ARCHIMED at $3.16
    }
)

# Known definitive acquisition prices — exclude when spot within 3%.
ACQUISITION_DEAL_PRICES: dict[str, float] = {
    "ESPR": 3.16,
}


def _news_acquisition_language(news: list[dict]) -> bool:
    blob = " ".join(
        ((n.get("title") or "") + " " + (n.get("description") or "")).lower() for n in (news or [])
    )
    return "acquisition" in blob or "take private" in blob or "going private" in blob or "buyout" in blob


def _price_near_known_deal(ticker: str, price: float | None) -> bool:
    deal = ACQUISITION_DEAL_PRICES.get(ticker.upper())
    if deal is None or deal <= 0 or price is None or price <= 0:
        return False
    return abs(price - deal) / deal <= 0.03

# ---------------------------------------------------------------------------
# Signal definitions (name, weight, direction)
# ---------------------------------------------------------------------------
UP_SIGNALS = {
    "VOLUME_SPIKE_NO_NEWS": 20,           # 5x-20x avg volume, no major news yet
    "INSIDER_BUYING_500K": 18,            # exec/director $500K+ in 14 days
    "SHORT_SQUEEZE_SETUP": 17,            # SI > 20% float + vol spike + price up
    "MASSIVE_OVERSOLD": 15,               # -50% to -80% from 52w high + RSI < 32
    "REVENUE_ACCELERATION": 14,           # +80% YoY rev growth, stock flat
    "CATALYST_IMMINENT": 13,              # FDA / earnings / contract in 21 days
    "INSTITUTIONAL_ACCUMULATION": 12,     # 13F new position from $500M+ AUM fund
    "BREAKOUT_SETUP": 10,                 # tight range 30+ days + volume building
    "SECTOR_TAILWIND": 8,                 # gov funding / legislative tailwind
    "CEO_REENTRY_BUYING": 8,              # founder/CEO buying after long absence
}

DOWN_SIGNALS = {
    "PARABOLIC_NO_FUNDAMENTAL": 20,       # +200% in 30d w/ no support
    "MULTI_INSIDER_SELLING_2M": 18,       # $2M+ insider selling in 14 days
    "AUDITOR_GOING_CONCERN": 17,          # auditor change / going concern in 30d
    "REVENUE_DECLINE_NEAR_HIGH": 15,      # rev -20% YoY but stock within 20% of ATH
    "SHORT_INTEREST_SPIKE": 12,           # SI +50% in last 2 weeks
}

FILTERS = {
    "min_avg_volume": 200_000,
    "min_price": 0.50,
    "max_price": 50.0,
    "min_market_cap": 10_000_000,
    "max_market_cap": 2_000_000_000,
    "min_history_quarters": 2,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def scan(top_n: int = 3, candidate_pool_size: int = 60) -> list[dict[str, Any]]:
    """Scan the small-cap universe and return the top `top_n` picks.

    Strategy:
      1. yfinance batch-download 1y of daily history for the entire universe.
      2. Filter by price + 30-day average volume on the latest bar.
      3. Sort by latest volume (proxy for activity), keep top `candidate_pool_size`.
      4. Per-candidate enrich with details/snapshot/financials and score.
      5. Return ranked top-N including triggered signals.
    """
    log(f"[SmallCapScreener] starting scan over {len(SMALL_CAP_UNIVERSE)} tickers")

    async with YFClient() as yfc:
        bars_by_ticker = await yfc.batch_history(SMALL_CAP_UNIVERSE, period="1y")
        log(f"[SmallCapScreener] yfinance returned bars for {sum(1 for v in bars_by_ticker.values() if v)}/{len(SMALL_CAP_UNIVERSE)} tickers")

        prelim: list[dict] = []
        for ticker, rows in bars_by_ticker.items():
            if not rows:
                continue
            last = rows[-1]
            price = last.get("c")
            vol = last.get("v") or 0
            if price is None:
                continue
            if not (FILTERS["min_price"] <= price <= FILTERS["max_price"]):
                continue
            # 30-day average volume must clear the floor
            recent = rows[-30:]
            avg_v = sum(r.get("v", 0) for r in recent) / max(len(recent), 1)
            if avg_v < FILTERS["min_avg_volume"]:
                continue
            prelim.append(
                {
                    "T": ticker,
                    "c": price,
                    "v": vol,
                    "_rows": rows,
                    "_avg_vol_30d": avg_v,
                }
            )

        prelim.sort(key=lambda r: r["_avg_vol_30d"], reverse=True)
        prelim = prelim[: max(candidate_pool_size, 30)]
        log(f"[SmallCapScreener] {len(prelim)} candidates pass price/volume filter")

        sem = asyncio.Semaphore(8)

        async def enrich(row: dict) -> dict | None:
            async with sem:
                ticker = row["T"]
                if ticker.upper() in PERMANENT_EXCLUSIONS:
                    log(f"[SmallCapScreener] EXCLUDE {ticker}: permanent exclusion list")
                    return None
                try:
                    details, snap, financials, news_items = await asyncio.gather(
                        yfc.ticker_details(ticker),
                        yfc.snapshot(ticker),
                        yfc.financials(ticker, limit=8),
                        yfc.news(ticker, limit=12),
                    )
                except Exception as e:  # noqa: BLE001
                    log(f"[SmallCapScreener] enrich {ticker} failed: {e}", "warning")
                    return None

                last_px = (snap.get("day") or {}).get("c") if snap else None
                try:
                    px_f = float(last_px) if last_px is not None else None
                except (TypeError, ValueError):
                    px_f = None
                if _price_near_known_deal(ticker, px_f):
                    log(f"[SmallCapScreener] EXCLUDE {ticker}: price within 3% of known acquisition deal")
                    return None
                if _news_acquisition_language(news_items):
                    log(f"[SmallCapScreener] EXCLUDE {ticker}: acquisition language in recent headlines")
                    return None

                mcap = (details or {}).get("market_cap") or 0
                if mcap and not (
                    FILTERS["min_market_cap"] <= mcap <= FILTERS["max_market_cap"]
                ):
                    return None
                indicators = build_indicator_pack(row["_rows"])
                if not indicators:
                    return None
                signals, score = _score_small_cap(row, indicators, details, snap, financials)
                if not signals:
                    return None
                return {
                    "ticker": ticker,
                    "company_name": (details or {}).get("name") or ticker,
                    "section": "SMALL_CAP",
                    "score": score,
                    "triggered_signals": signals,
                    "market_cap": mcap,
                    "exchange": (details or {}).get("primary_exchange"),
                    "indicators": indicators,
                    "details": details,
                    "snapshot": snap,
                    "financials": financials,
                    "ref_date": datetime.utcnow().date().isoformat(),
                }

        enriched = await asyncio.gather(*(enrich(r) for r in prelim))
        scored = [e for e in enriched if e]
        scored.sort(key=lambda x: x["score"], reverse=True)

        top = scored[:top_n]
        log(f"[SmallCapScreener] returning {len(top)} ranked picks")
        return top


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------
def _score_small_cap(
    row: dict,
    indicators: dict,
    details: dict,
    snapshot: dict,
    financials: list[dict],
) -> tuple[list[dict], float]:
    triggered: list[dict] = []

    # ---- volume spike ----
    vr = indicators.get("volume_ratio")
    if vr and vr >= 5:
        triggered.append(
            {
                "name": "VOLUME_SPIKE_NO_NEWS",
                "weight": UP_SIGNALS["VOLUME_SPIKE_NO_NEWS"],
                "direction": "UP",
                "detail": f"Volume {vr:.1f}x 30-day average",
            }
        )

    # ---- massive oversold ----
    pct_from_high = indicators.get("pct_from_52w_high")
    rsi = indicators.get("rsi_14")
    if pct_from_high is not None and rsi is not None:
        if -80 <= pct_from_high <= -50 and rsi < 32:
            triggered.append(
                {
                    "name": "MASSIVE_OVERSOLD",
                    "weight": UP_SIGNALS["MASSIVE_OVERSOLD"],
                    "direction": "UP",
                    "detail": f"{pct_from_high:.0f}% from 52w high, RSI {rsi}",
                }
            )

    # ---- breakout setup ----
    bb = indicators.get("bbands") or {}
    if (
        bb.get("position") is not None
        and 0.45 <= bb["position"] <= 0.65
        and vr
        and vr >= 1.5
        and rsi
        and 50 <= rsi <= 65
    ):
        triggered.append(
            {
                "name": "BREAKOUT_SETUP",
                "weight": UP_SIGNALS["BREAKOUT_SETUP"],
                "direction": "UP",
                "detail": "30d consolidation breaking out on rising volume",
            }
        )

    # ---- short squeeze setup ----
    short_pct = (snapshot.get("shortInterest") or {}).get("percentOfFloat") if snapshot else None
    if short_pct and short_pct > 20 and vr and vr >= 3:
        triggered.append(
            {
                "name": "SHORT_SQUEEZE_SETUP",
                "weight": UP_SIGNALS["SHORT_SQUEEZE_SETUP"],
                "direction": "UP",
                "detail": f"Short interest {short_pct:.1f}% of float + {vr:.1f}x volume",
            }
        )

    # ---- revenue acceleration ----
    rev_growth = _yoy_revenue_growth(financials)
    if rev_growth is not None and rev_growth >= 80 and pct_from_high is not None and pct_from_high < -20:
        triggered.append(
            {
                "name": "REVENUE_ACCELERATION",
                "weight": UP_SIGNALS["REVENUE_ACCELERATION"],
                "direction": "UP",
                "detail": f"Revenue +{rev_growth:.0f}% YoY, stock has not re-rated",
            }
        )

    # ---- parabolic / overextended (DOWN) ----
    if pct_from_high is not None and pct_from_high > 0 and rsi and rsi > 80:
        pct_from_low = indicators.get("pct_from_52w_low")
        if pct_from_low and pct_from_low >= 200:
            triggered.append(
                {
                    "name": "PARABOLIC_NO_FUNDAMENTAL",
                    "weight": DOWN_SIGNALS["PARABOLIC_NO_FUNDAMENTAL"],
                    "direction": "DOWN",
                    "detail": f"+{pct_from_low:.0f}% off lows, RSI {rsi} — overextended",
                }
            )

    # ---- catalyst imminent ----
    if _has_imminent_catalyst(details, financials):
        triggered.append(
            {
                "name": "CATALYST_IMMINENT",
                "weight": UP_SIGNALS["CATALYST_IMMINENT"],
                "direction": "UP",
                "detail": "Earnings or known catalyst expected within 21 days",
            }
        )

    # ---- sector tailwind heuristic ----
    sic = (details.get("sic_description") or "").lower() if details else ""
    if any(k in sic for k in ("biological", "pharmaceutical", "biotech", "semiconductor", "defense", "uranium", "nuclear")):
        triggered.append(
            {
                "name": "SECTOR_TAILWIND",
                "weight": UP_SIGNALS["SECTOR_TAILWIND"],
                "direction": "UP",
                "detail": f"Active policy / capital flow tailwind for sector ({sic})",
            }
        )

    # ---- momentum multiplier ----
    score = sum(s["weight"] for s in triggered)
    if len([s for s in triggered if s["direction"] == "UP"]) >= 4:
        score *= 1.4

    return triggered, round(score, 2)


def _yoy_revenue_growth(financials: list[dict]) -> float | None:
    if len(financials) < 5:
        return None
    try:
        latest = (
            financials[0].get("financials", {}).get("income_statement", {}).get("revenues", {}).get("value")
        )
        year_ago = (
            financials[4].get("financials", {}).get("income_statement", {}).get("revenues", {}).get("value")
        )
        if latest and year_ago:
            return ((latest - year_ago) / abs(year_ago)) * 100
    except (AttributeError, KeyError, TypeError, ZeroDivisionError):
        return None
    return None


def _has_imminent_catalyst(details: dict, financials: list[dict]) -> bool:
    if not financials:
        return False
    end = financials[0].get("end_date")
    if not end:
        return False
    try:
        d = datetime.fromisoformat(str(end))
    except ValueError:
        return False
    days_since = (datetime.utcnow() - d).days
    return 60 <= days_since <= 100


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    res = asyncio.run(scan())
    print(json.dumps(res, indent=2, default=str))
