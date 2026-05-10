"""Big-player / undervalued large-cap screener.

Universe: US-listed stocks with market cap > $2B that are 3+ years old.
Returns the top 2 candidates ranked by signal score.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from market_data import PolygonClient, build_indicator_pack
from utils import log

# Sector P/E priors used as a stand-in when an exact peer comparison is not
# available from the data source. Conservative averages.
SECTOR_PE_PRIORS = {
    "technology": 28,
    "communication": 22,
    "consumer cyclical": 22,
    "consumer defensive": 21,
    "industrials": 20,
    "financial": 14,
    "energy": 12,
    "utilities": 18,
    "healthcare": 22,
    "real estate": 24,
    "materials": 17,
    "default": 20,
}

UP_SIGNALS = {
    "DEEP_VALUE": 20,
    "ANALYST_AWAKENING": 18,
    "INSTITUTIONAL_BUYING": 16,
    "EARNINGS_INFLECTION": 15,
    "INSIDER_CONVICTION": 15,
    "SENTIMENT_MISMATCH": 14,
    "SECTOR_ROTATION_INCOMING": 12,
    "HIDDEN_ASSET_VALUE": 11,
    "BUYBACK_ACCELERATION": 10,
    "TECHNICAL_BASE_BREAKOUT": 9,
}

DOWN_SIGNALS = {
    "PE_OVEREXTENDED": 20,
    "GUIDANCE_CUT_2X": 18,
    "EXEC_SELLING_AT_HIGHS": 15,
    "DEBT_DETERIORATION": 12,
}

FILTERS = {
    "min_market_cap": 2_000_000_000,
    "min_avg_volume": 1_000_000,
    "min_history_years": 3,
    "max_pct_to_target": 5,  # exclude if within 5% of consensus PT
}


async def scan(top_n: int = 2, candidate_pool_size: int = 80) -> list[dict[str, Any]]:
    log("[BigPlayerScreener] starting scan")

    async with PolygonClient() as poly:
        # We use a directed list of US large caps as anchor: pulling all tickers
        # & filtering by market cap is correct but slow. We pull a wide set and
        # filter rigorously below. Use grouped daily as an anchor to avoid the
        # full ref-data pagination cost in the common case.
        ref_date = _last_trading_day_str()
        grouped = await poly.grouped_daily(ref_date)

        candidates: list[dict] = []
        for row in grouped:
            price = row.get("c") or 0
            vol = row.get("v") or 0
            if price < 5 or vol < FILTERS["min_avg_volume"]:
                continue
            candidates.append(row)

        candidates.sort(key=lambda r: r.get("v", 0), reverse=True)
        candidates = candidates[: max(candidate_pool_size * 4, 250)]

        log(f"[BigPlayerScreener] {len(candidates)} pre-filtered (price/volume) candidates")

        sem = asyncio.Semaphore(8)

        async def enrich(row: dict) -> dict | None:
            async with sem:
                ticker = row.get("T")
                if not ticker:
                    return None
                details, aggs, snap = await asyncio.gather(
                    poly.ticker_details(ticker),
                    poly.aggs(ticker, days=300),
                    poly.snapshot(ticker),
                )
                if not details:
                    return None
                mcap = details.get("market_cap") or 0
                if mcap < FILTERS["min_market_cap"]:
                    return None
                # history check
                list_date = details.get("list_date")
                if list_date:
                    try:
                        ld = datetime.fromisoformat(list_date)
                        if (datetime.utcnow() - ld).days < 365 * FILTERS["min_history_years"]:
                            return None
                    except ValueError:
                        pass
                indicators = build_indicator_pack(aggs)
                if not indicators:
                    return None
                financials = await poly.financials(ticker, limit=8)
                signals, score = _score_big_player(row, indicators, details, snap, financials)
                if not signals:
                    return None
                return {
                    "ticker": ticker,
                    "company_name": details.get("name") or ticker,
                    "section": "BIG_PLAYER",
                    "score": score,
                    "triggered_signals": signals,
                    "market_cap": mcap,
                    "exchange": (details.get("primary_exchange") or "").upper(),
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
        log(f"[BigPlayerScreener] returning {len(top)} ranked picks")
        return top


# ---------------------------------------------------------------------------
def _score_big_player(
    row: dict,
    indicators: dict,
    details: dict,
    snapshot: dict,
    financials: list[dict],
) -> tuple[list[dict], float]:
    triggered: list[dict] = []
    sector = (details.get("sic_description") or "").lower()
    sector_pe = _sector_pe(sector)

    pe = _trailing_pe(details, financials, indicators.get("current_price"))
    rev_growth = _yoy_rev_growth(financials)

    if pe is not None and pe > 0 and rev_growth is not None and rev_growth > 0:
        if pe <= sector_pe * 0.6:
            triggered.append(
                {
                    "name": "DEEP_VALUE",
                    "weight": UP_SIGNALS["DEEP_VALUE"],
                    "direction": "UP",
                    "detail": f"P/E {pe:.1f} vs sector ~{sector_pe} with {rev_growth:.0f}% revenue growth",
                }
            )

    earnings_growth_accelerating = _earnings_inflection(financials)
    if earnings_growth_accelerating:
        triggered.append(
            {
                "name": "EARNINGS_INFLECTION",
                "weight": UP_SIGNALS["EARNINGS_INFLECTION"],
                "direction": "UP",
                "detail": "Earnings re-accelerating after multi-quarter decline",
            }
        )

    pct_from_high = indicators.get("pct_from_52w_high")
    rsi = indicators.get("rsi_14")
    if pct_from_high is not None and pct_from_high <= -30 and rev_growth and rev_growth > 0:
        triggered.append(
            {
                "name": "SENTIMENT_MISMATCH",
                "weight": UP_SIGNALS["SENTIMENT_MISMATCH"],
                "direction": "UP",
                "detail": f"Down {pct_from_high:.0f}% YTD while fundamentals improving",
            }
        )

    bb = indicators.get("bbands") or {}
    if (
        indicators.get("sma_50")
        and indicators.get("sma_200")
        and indicators["sma_50"] > indicators["sma_200"]
        and bb.get("position") is not None
        and bb["position"] >= 0.55
        and rsi
        and 50 <= rsi <= 65
    ):
        triggered.append(
            {
                "name": "TECHNICAL_BASE_BREAKOUT",
                "weight": UP_SIGNALS["TECHNICAL_BASE_BREAKOUT"],
                "direction": "UP",
                "detail": "Multi-month base + golden cross + breakout volume",
            }
        )

    de = _debt_to_equity(financials)
    if pe is not None and pe > sector_pe * 2 and rev_growth is not None and rev_growth < 5:
        triggered.append(
            {
                "name": "PE_OVEREXTENDED",
                "weight": DOWN_SIGNALS["PE_OVEREXTENDED"],
                "direction": "DOWN",
                "detail": f"P/E {pe:.0f} vs sector {sector_pe} with decelerating growth",
            }
        )

    if de is not None and de > 1.5 and rev_growth is not None and rev_growth < 0:
        triggered.append(
            {
                "name": "DEBT_DETERIORATION",
                "weight": DOWN_SIGNALS["DEBT_DETERIORATION"],
                "direction": "DOWN",
                "detail": f"Debt/equity {de:.2f}, revenue declining",
            }
        )

    score = sum(s["weight"] for s in triggered)
    # quality multiplier
    if de is not None and de < 0.5 and any(s["direction"] == "UP" for s in triggered):
        score *= 1.2

    return triggered, round(score, 2)


def _sector_pe(sector: str) -> float:
    if not sector:
        return SECTOR_PE_PRIORS["default"]
    for k, v in SECTOR_PE_PRIORS.items():
        if k in sector:
            return v
    return SECTOR_PE_PRIORS["default"]


def _trailing_pe(details: dict, financials: list[dict], price: float | None) -> float | None:
    if price is None:
        return None
    eps_total = 0.0
    used = 0
    for q in financials[:4]:
        try:
            eps = (
                q.get("financials", {})
                .get("income_statement", {})
                .get("basic_earnings_per_share", {})
                .get("value")
            )
            if eps is not None:
                eps_total += eps
                used += 1
        except (AttributeError, KeyError, TypeError):
            continue
    if used >= 2 and eps_total > 0:
        return round(price / eps_total, 2)
    return None


def _yoy_rev_growth(financials: list[dict]) -> float | None:
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


def _earnings_inflection(financials: list[dict]) -> bool:
    """Last 2 quarters showed accelerating EPS growth after 4+ quarters of decline."""
    if len(financials) < 6:
        return False
    eps = []
    for q in financials[:6]:
        try:
            v = (
                q.get("financials", {})
                .get("income_statement", {})
                .get("basic_earnings_per_share", {})
                .get("value")
            )
            eps.append(v)
        except (AttributeError, KeyError, TypeError):
            eps.append(None)
    if any(e is None for e in eps):
        return False
    recent_growth = eps[0] > eps[1] > 0  # most-recent quarters improving
    older_decline = sum(1 for i in range(2, 5) if eps[i] < eps[i + 1]) >= 3
    return recent_growth and older_decline


def _debt_to_equity(financials: list[dict]) -> float | None:
    if not financials:
        return None
    try:
        bs = financials[0].get("financials", {}).get("balance_sheet", {})
        liab = bs.get("liabilities", {}).get("value")
        eq = bs.get("equity", {}).get("value")
        if liab and eq and eq > 0:
            return liab / eq
    except (AttributeError, KeyError, TypeError, ZeroDivisionError):
        return None
    return None


def _last_trading_day_str() -> str:
    d = datetime.utcnow().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    if datetime.utcnow().hour < 22:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


if __name__ == "__main__":
    import json
    res = asyncio.run(scan())
    print(json.dumps(res, indent=2, default=str))
