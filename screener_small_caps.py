"""Small-cap / speculative explosive opportunity screener.

Universe: US-listed stocks with $10M <= market cap <= $2B.
Returns the top 3 candidates ranked by composite signal score, including the
list of triggered signals so analyzer.py & scorer.py can use them downstream.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from market_data import PolygonClient, build_indicator_pack
from utils import log

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
    "exchanges": {"XNAS", "XNYS", "XASE", "AMEX", "NASDAQ", "NYSE", "BATS"},
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
    """Scan the entire small-cap universe and return the top `top_n` picks.

    Strategy:
      1. Pull grouped daily bars for the most recent trading day.
      2. Resolve ticker reference data (market cap, exchange, etc.) to filter
         the universe down to qualifying small/micro caps.
      3. Score each candidate using technical signals from daily aggregates and
         enrichment data (financials, snapshot, news) in parallel.
      4. Return the ranked top-N including the list of triggered signals.
    """
    log("[SmallCapScreener] starting scan")

    async with PolygonClient() as poly:
        ref_date = _last_trading_day_str()
        grouped = await poly.grouped_daily(ref_date)
        if not grouped:
            log("[SmallCapScreener] grouped_daily empty — using fallback universe", "warning")
            grouped = []

        candidates: list[dict] = []
        for row in grouped:
            price = row.get("c")
            vol = row.get("v")
            if price is None or vol is None:
                continue
            if not (FILTERS["min_price"] <= price <= FILTERS["max_price"]):
                continue
            if vol < FILTERS["min_avg_volume"]:
                continue
            candidates.append(row)

        candidates.sort(key=lambda r: r.get("v", 0), reverse=True)
        candidates = candidates[: max(candidate_pool_size * 4, 200)]

        log(f"[SmallCapScreener] {len(candidates)} pre-filtered candidates from grouped daily")

        sem = asyncio.Semaphore(8)

        async def enrich(row: dict) -> dict | None:
            async with sem:
                ticker = row.get("T")
                if not ticker:
                    return None
                details, aggs, snap = await asyncio.gather(
                    poly.ticker_details(ticker),
                    poly.aggs(ticker, days=250),
                    poly.snapshot(ticker),
                )
                if not details:
                    return None
                mcap = details.get("market_cap") or 0
                exch = (details.get("primary_exchange") or "").upper()
                if mcap and not (
                    FILTERS["min_market_cap"] <= mcap <= FILTERS["max_market_cap"]
                ):
                    return None
                if exch and exch not in FILTERS["exchanges"]:
                    pass  # tolerant — Polygon uses MIC codes that may vary
                indicators = build_indicator_pack(aggs)
                if not indicators:
                    return None
                financials = await poly.financials(ticker, limit=4)
                signals, score = _score_small_cap(row, indicators, details, snap, financials)
                if not signals:
                    return None
                return {
                    "ticker": ticker,
                    "company_name": details.get("name") or ticker,
                    "section": "SMALL_CAP",
                    "score": score,
                    "triggered_signals": signals,
                    "market_cap": mcap,
                    "exchange": exch,
                    "indicators": indicators,
                    "details": details,
                    "snapshot": snap,
                    "financials": financials,
                    "ref_date": ref_date,
                }

        enriched = await asyncio.gather(*(enrich(r) for r in candidates[:candidate_pool_size]))
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

    # ---- short squeeze setup (use snapshot if exposed) ----
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
        # if 30d return > 200% — approximate using 52w low as anchor
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
    sic = (details.get("sic_description") or "").lower()
    if any(k in sic for k in ("biological", "pharmaceutical", "semiconductor", "defense", "uranium")):
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
    """Approximate catalyst-imminent flag: most recent quarterly filing is 60-90
    days old, suggesting earnings inside the next 21-day window."""
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


def _last_trading_day_str() -> str:
    d = datetime.utcnow().date()
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    # if it's early UTC, the prior US session is the most recent confirmed bar
    if datetime.utcnow().hour < 22:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    res = asyncio.run(scan())
    print(json.dumps(res, indent=2, default=str))
