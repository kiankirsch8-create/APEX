"""APEX analyzer — turns a screener candidate into a full APEX report.

Calls Claude (claude-sonnet-4-5-20251022) with the master analyst system prompt
and a richly-populated user message, parses the JSON response, retries once
on parse failure, and returns the structured report dict.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from anthropic import Anthropic

from market_data import YFClient, build_indicator_pack, fetch_news_headlines
from master_prompt import MASTER_ANALYST_SYSTEM_PROMPT
from utils import env, log, utcnow_iso

CLAUDE_MODEL = "claude-sonnet-4-5-20251022"
MAX_TOKENS = 5500


_CLIENT: Anthropic | None = None


def _client() -> Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    return _CLIENT


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def analyze_stock(
    ticker: str,
    section: str,
    triggered_signals: list[dict],
    market_data: dict | None = None,
    total_budget_usd: float = 10_000.0,
) -> dict[str, Any]:
    """Analyze `ticker` and return the full APEX report dict.

    Parameters
    ----------
    ticker : str
        US stock symbol.
    section : str
        Either "SMALL_CAP" or "BIG_PLAYER".
    triggered_signals : list[dict]
        Signal list from the screener — each entry has name, weight, direction.
    market_data : dict | None
        Optional pre-fetched data (from screener); otherwise fetched fresh.
    total_budget_usd : float
        User's total capital for position sizing.
    """
    log(f"[Analyzer] {ticker} ({section}) — fetching data")
    enriched = await _gather_market_data(ticker, market_data)
    user_msg = _build_user_message(ticker, section, triggered_signals, enriched, total_budget_usd)

    raw = await _call_claude(user_msg)
    parsed = _parse_json(raw)
    if parsed is None:
        log(f"[Analyzer] {ticker} JSON parse failed — retrying once", "warning")
        raw = await _call_claude(user_msg + "\n\nReturn ONLY raw JSON. No markdown fences.")
        parsed = _parse_json(raw)
        if parsed is None:
            log(f"[Analyzer] {ticker} JSON parse failed twice — falling back to local report", "error")
            return _fallback_report(ticker, section, triggered_signals, enriched, total_budget_usd)

    parsed.setdefault("ticker", ticker)
    parsed.setdefault("section", section)
    parsed.setdefault("triggered_signals", [s["name"] for s in triggered_signals])
    parsed.setdefault("generated_at", utcnow_iso())
    parsed.setdefault("conviction_tier", "TIER_3_STANDARD")
    parsed.setdefault("pattern_matched", "PATTERN_NONE")
    parsed.setdefault("pattern_win_rate", 0)
    parsed.setdefault("macro_regime", "NORMAL_MODE")
    parsed.setdefault("smart_money_score", 5.0)
    try:
        cs0 = float(parsed.get("composite_score")) if parsed.get("composite_score") is not None else 5.0
    except (TypeError, ValueError):
        cs0 = 5.0
    parsed.setdefault("risk_adjusted_score", cs0)
    parsed.setdefault("investment_timeframe", "3 to 8 weeks")
    parsed.setdefault("timeframe_basis", "Default — model omitted signal-derived window; verify SECTION 1 format")
    parsed.setdefault(
        "timeframe_catalyst",
        "Re-check: primary technical or fundamental trigger for move within stated window",
    )
    parsed.setdefault("sector_bucket", "OTHER")
    _sanitize_timeframe_for_market_cap(parsed, enriched)
    parsed["_raw_signals"] = triggered_signals
    parsed["_total_budget_usd"] = total_budget_usd
    return parsed


def _sanitize_timeframe_for_market_cap(parsed: dict[str, Any], enriched: dict[str, Any]) -> None:
    """Clamp unrealistic day/week horizons for large / mega caps (post-parse guard)."""
    details = enriched.get("details") or {}
    mcap = details.get("market_cap")
    if mcap is None:
        return
    try:
        mcap_f = float(mcap)
    except (TypeError, ValueError):
        return
    original_tf = (parsed.get("investment_timeframe") or "").lower()
    if mcap_f > 2_000_000_000 and "day" in original_tf:
        parsed["investment_timeframe"] = "3 to 6 months"
        parsed["timeframe_basis"] = "Large cap value re-rating requires months not days"
    if mcap_f > 50_000_000_000 and "week" in original_tf:
        parsed["investment_timeframe"] = "6 to 18 months"
        parsed["timeframe_basis"] = "Mega cap re-rating minimum 6 months"


# ---------------------------------------------------------------------------
async def _gather_market_data(ticker: str, market_data: dict | None) -> dict:
    """Collect everything Claude needs about a ticker."""
    md = dict(market_data or {})
    async with YFClient() as yfc:
        details = md.get("details") or await yfc.ticker_details(ticker)
        snap = md.get("snapshot") or await yfc.snapshot(ticker)
        aggs = md.get("aggs")
        indicators = md.get("indicators")
        if indicators is None:
            aggs = aggs or await yfc.aggs(ticker, days=250)
            indicators = build_indicator_pack(aggs)
        financials = md.get("financials") or await yfc.financials(ticker, limit=8)
        company_name = details.get("name") if isinstance(details, dict) else None
        news = await fetch_news_headlines(ticker, company_name)
        yfinance_news = await yfc.news(ticker, limit=8)

    return {
        "details": details,
        "snapshot": snap,
        "indicators": indicators,
        "financials": financials,
        "newsapi_headlines": news,
        "yfinance_news": [
            {
                "title": n.get("title"),
                "publisher": (n.get("publisher") or {}).get("name"),
                "url": n.get("article_url"),
                "published_utc": n.get("published_utc"),
                "description": n.get("description"),
            }
            for n in yfinance_news
        ],
    }


# ---------------------------------------------------------------------------
def _build_user_message(
    ticker: str,
    section: str,
    triggered_signals: list[dict],
    md: dict,
    total_budget_usd: float,
) -> str:
    details = md.get("details") or {}
    ind = md.get("indicators") or {}
    snap = md.get("snapshot") or {}
    fin = md.get("financials") or []

    company = details.get("name") or ticker
    market_cap = details.get("market_cap")
    sector = details.get("sic_description") or "Unknown"
    list_date = details.get("list_date") or "unknown"

    # Fundamentals digest
    fundamentals = _fundamentals_digest(fin, ind.get("current_price"))

    # Macro context (heuristic — replaced by real data when available)
    macro = _macro_snapshot()

    body = {
        "ticker": ticker,
        "company_name": company,
        "section": section,
        "user_total_budget_usd": total_budget_usd,
        "as_of": utcnow_iso(),
        "company_profile": {
            "sector_or_sic": sector,
            "market_cap_usd": market_cap,
            "list_date": list_date,
            "exchange": details.get("primary_exchange"),
            "description": (details.get("description") or "")[:600],
            "homepage_url": details.get("homepage_url"),
        },
        "price_action": {
            "current_price": ind.get("current_price"),
            "fifty_two_week_high": ind.get("fifty_two_week_high"),
            "fifty_two_week_low": ind.get("fifty_two_week_low"),
            "pct_from_52w_high": ind.get("pct_from_52w_high"),
            "pct_from_52w_low": ind.get("pct_from_52w_low"),
            "today_volume": ind.get("volume"),
            "avg_volume_30d": ind.get("avg_volume_30d"),
            "volume_ratio_vs_30d": ind.get("volume_ratio"),
        },
        "technicals": {
            "rsi_14": ind.get("rsi_14"),
            "macd": ind.get("macd"),
            "sma_20": ind.get("sma_20"),
            "sma_50": ind.get("sma_50"),
            "sma_200": ind.get("sma_200"),
            "bollinger_bands": ind.get("bbands"),
        },
        "fundamentals": fundamentals,
        "snapshot_extras": {
            "day": snap.get("day"),
            "prevDay": snap.get("prevDay"),
            "todaysChange": snap.get("todaysChange"),
            "todaysChangePerc": snap.get("todaysChangePerc"),
        },
        "screener_triggered_signals": triggered_signals,
        "recent_news_newsapi": md.get("newsapi_headlines", [])[:8],
        "recent_news_yfinance": md.get("yfinance_news", [])[:8],
        "macro_context": macro,
        "macro_market_indicators": macro,
        "instructions": (
            "Return ONLY valid raw JSON matching the exact schema below. "
            "No markdown, no commentary, no code fences. Every field is required."
        ),
        "required_json_schema": {
            "ticker": "string",
            "company_name": "string",
            "section": "SMALL_CAP | BIG_PLAYER",
            "direction": "UP | DOWN",
            "apex_rating": (
                "STRONG BUY | BUY | SPECULATIVE BUY | AVOID | SHORT | STRONG SHORT | WATCH"
            ),
            "conviction_tier": (
                "TIER_1_APEX_PRIME | TIER_2_HIGH_CONVICTION | TIER_3_STANDARD | "
                "TIER_4_WATCHLIST | TIER_5_AVOID | TIER_S1_STRONG_SHORT | TIER_S2_SHORT"
            ),
            "pattern_matched": (
                "PATTERN_A_PHOENIX | PATTERN_B_SQUEEZE_CANNON | PATTERN_C_SLEEPING_GIANT | "
                "PATTERN_D_CATALYST_SPRINT | PATTERN_E_FUNDAMENTAL_DISCONNECT | PATTERN_NONE"
            ),
            "pattern_win_rate": "integer 0-100 (historical pattern win %)",
            "macro_regime": "string — FEAR_MODE | NORMAL_MODE | COMPLACENCY_MODE (+ overlays)",
            "smart_money_score": "number 0-10",
            "risk_adjusted_score": "number 0-10 after penalties/bonuses",
            "investment_timeframe": (
                'string — format \"X to Y days|weeks|months\" per prompt SECTION 1; '
                'M&A no-edge only: \"AVOID — NO TIMEFRAME\"'
            ),
            "timeframe_basis": "1-2 sentences tying horizon to concrete momentum/fundamental signals (not cap buckets alone)",
            "timeframe_catalyst": "specific price driver in the window (breakout level, volume, dated catalyst, etc.)",
            "sector_bucket": "BIOTECH_PHARMA | CHINA_ADR | TECH_SOFTWARE_SEMI | ... | OTHER",
            "current_price": "number",
            "target_30d": "number",
            "target_90d": "number",
            "target_12m": "number",
            "stop_loss": "number",
            "upside_percentage": "number",
            "probability_percentage": "number",
            "probability_reasoning": "string (max 15 words)",
            "composite_score": "number 0-10",
            "confidence_level": "HIGH | MEDIUM | SPECULATIVE",
            "thesis": "string with 3 paragraphs separated by \\n\\n",
            "macro_signal": "BULLISH | NEUTRAL | BEARISH",
            "macro_score": "number 0-10",
            "technical_score": "number 0-10",
            "fundamental_score": "number 0-10",
            "sentiment_score": "number 0-10",
            "analyst_score": "number 0-10",
            "historical_score": "number 0-10",
            "triggered_signals": "string[]",
            "risks": "[{name, description, probability LOW|MEDIUM|HIGH, impact_percentage number}]",
            "historical_analog": "string",
            "catalysts": "string[]",
            "verdict": "string (1 punchy sentence)",
            "position_sizing": {
                "recommended_invest_amount": "number USD",
                "recommended_invest_percentage": "number % of total budget",
                "potential_return_dollars": "number",
                "potential_loss_dollars": "number",
                "risk_reward_ratio": "string '1:X.X'",
                "sizing_reasoning": "string max 20 words",
                "risk_category": "CONSERVATIVE | MODERATE | AGGRESSIVE | SPECULATIVE",
            },
            "generated_at": "ISO timestamp",
        },
    }
    return json.dumps(body, indent=2, default=str)


def _fundamentals_digest(financials: list[dict], price: float | None) -> dict:
    digest: dict[str, Any] = {
        "quarters_available": len(financials),
        "history": [],
    }
    if not financials:
        return digest

    revs, eps_list = [], []
    for q in financials[:4]:
        f = q.get("financials", {}) if isinstance(q, dict) else {}
        inc = f.get("income_statement", {}) if isinstance(f, dict) else {}
        rev = (inc.get("revenues") or {}).get("value")
        net_inc = (inc.get("net_income_loss") or {}).get("value")
        eps = (inc.get("basic_earnings_per_share") or {}).get("value")
        gross = (inc.get("gross_profit") or {}).get("value")
        gm = (gross / rev * 100) if rev and gross else None
        digest["history"].append(
            {
                "fiscal_period": q.get("fiscal_period"),
                "end_date": q.get("end_date"),
                "revenue": rev,
                "net_income": net_inc,
                "eps": eps,
                "gross_margin_pct": round(gm, 2) if gm is not None else None,
            }
        )
        if rev is not None:
            revs.append(rev)
        if eps is not None:
            eps_list.append(eps)

    if len(financials) >= 5:
        try:
            latest_rev = digest["history"][0]["revenue"]
            year_ago_rev = (
                financials[4].get("financials", {}).get("income_statement", {}).get("revenues", {}).get("value")
            )
            if latest_rev and year_ago_rev:
                digest["revenue_growth_yoy_pct"] = round(((latest_rev - year_ago_rev) / abs(year_ago_rev)) * 100, 2)
        except (AttributeError, KeyError, TypeError, ZeroDivisionError):
            pass

    # PE
    if price and eps_list and sum(eps_list[:4]) > 0:
        ttm_eps = sum(eps_list[:4])
        digest["ttm_eps"] = round(ttm_eps, 4)
        digest["trailing_pe"] = round(price / ttm_eps, 2) if ttm_eps else None

    # Balance sheet
    try:
        bs = financials[0].get("financials", {}).get("balance_sheet", {})
        liab = (bs.get("liabilities") or {}).get("value")
        eq = (bs.get("equity") or {}).get("value")
        cash = (bs.get("cash") or {}).get("value") or (bs.get("current_assets") or {}).get("value")
        if liab and eq and eq > 0:
            digest["debt_to_equity"] = round(liab / eq, 3)
        digest["cash_and_equivalents"] = cash
    except (AttributeError, KeyError, TypeError, ZeroDivisionError):
        pass

    return digest


def _macro_snapshot() -> dict[str, Any]:
    """Live VIX / 10Y snapshot for regime rules in the master prompt."""
    import yfinance as yf

    vix: float | None = None
    tnx_last: float | None = None
    tnx_week_ago: float | None = None
    try:
        vh = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if vh is not None and not getattr(vh, "empty", True):
            vix = float(vh["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        th = yf.Ticker("^TNX").history(period="15d", interval="1d")
        if th is not None and not getattr(th, "empty", True) and len(th) >= 2:
            tnx_last = float(th["Close"].iloc[-1])
            tnx_week_ago = float(th["Close"].iloc[max(-8, -len(th))])
    except Exception:  # noqa: BLE001
        pass

    regime = "NORMAL_MODE"
    if vix is not None:
        if vix > 25:
            regime = "FEAR_MODE"
        elif vix < 15:
            regime = "COMPLACENCY_MODE"
        else:
            regime = "NORMAL_MODE"
    rate_overlay = ""
    if tnx_last and tnx_week_ago and tnx_week_ago > 0 and tnx_last >= tnx_week_ago * 1.06:
        rate_overlay = " + RATE_SHOCK (10Y yield rising fast)"

    note_parts = []
    if vix is not None:
        note_parts.append(f"VIX≈{vix:.2f}")
    else:
        note_parts.append("VIX n/a")
    if tnx_last is not None:
        note_parts.append(f"10Y≈{tnx_last:.2f}")
    else:
        note_parts.append("10Y n/a")

    return {
        "fed_posture": "Data-dependent; see VIX / yield snapshot below.",
        "inflation_trend": "Track CPI/PCE from external feeds.",
        "vix_last": vix,
        "ten_year_yield_proxy_last": tnx_last,
        "ten_year_yield_proxy_week_ago": tnx_week_ago,
        "apex_macro_regime_hint": regime + rate_overlay,
        "regime_note": " | ".join(note_parts),
    }


# ---------------------------------------------------------------------------
async def _call_claude(user_message: str) -> str:
    """Call Anthropic Messages API in a thread (SDK is sync)."""
    def _do_call() -> str:
        client = _client()
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=MASTER_ANALYST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        # extract text block
        if not msg.content:
            return ""
        out = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                out.append(block.text)
        return "\n".join(out).strip()

    try:
        return await asyncio.to_thread(_do_call)
    except Exception as e:  # noqa: BLE001 — surface any provider error to retry path
        log(f"[Analyzer] Claude call failed: {e}", "error")
        return ""


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    txt = raw.strip()
    # Strip code fences if present
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    # Pull the largest JSON object substring
    match = re.search(r"\{[\s\S]*\}", txt)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
def _fallback_report(
    ticker: str,
    section: str,
    triggered_signals: list[dict],
    md: dict,
    total_budget_usd: float,
) -> dict:
    """Produce a structurally valid degraded report when Claude fails twice."""
    ind = md.get("indicators") or {}
    price = ind.get("current_price") or 0
    direction = "UP"
    if any(s.get("direction") == "DOWN" for s in triggered_signals):
        ups = sum(s["weight"] for s in triggered_signals if s["direction"] == "UP")
        downs = sum(s["weight"] for s in triggered_signals if s["direction"] == "DOWN")
        direction = "DOWN" if downs > ups else "UP"

    if direction == "UP":
        target_12m = round(price * (1.4 if section == "BIG_PLAYER" else 2.2), 2)
        stop = round(price * 0.85, 2) if price else 0
        rating = "BUY" if section == "BIG_PLAYER" else "SPECULATIVE BUY"
    else:
        target_12m = round(price * 0.7, 2)
        stop = round(price * 1.15, 2)
        rating = "SHORT"

    upside = round(abs(target_12m - price) / price * 100, 1) if price else 0.0
    macro = _macro_snapshot()
    regime = str(macro.get("apex_macro_regime_hint") or "NORMAL_MODE")
    return {
        "ticker": ticker,
        "company_name": (md.get("details") or {}).get("name") or ticker,
        "section": section,
        "direction": direction,
        "apex_rating": rating,
        "conviction_tier": "TIER_3_STANDARD",
        "pattern_matched": "PATTERN_NONE",
        "pattern_win_rate": 0,
        "macro_regime": regime,
        "smart_money_score": 5.0,
        "risk_adjusted_score": 6.0,
        "investment_timeframe": "5 to 15 days" if direction == "DOWN" else "3 to 8 weeks",
        "timeframe_basis": "Fallback — horizon from direction and screener signals only; regenerate with AI.",
        "timeframe_catalyst": "Fallback: confirm breakout or earnings catalyst manually; Claude unavailable.",
        "sector_bucket": "OTHER",
        "current_price": price,
        "target_30d": round(price * (1.05 if direction == "UP" else 0.95), 2) if price else 0,
        "target_90d": round(price * (1.18 if direction == "UP" else 0.85), 2) if price else 0,
        "target_12m": target_12m,
        "stop_loss": stop,
        "upside_percentage": upside,
        "probability_percentage": 50,
        "probability_reasoning": "Fallback report — Claude unavailable; signals only.",
        "composite_score": 6.0,
        "confidence_level": "MEDIUM" if section == "BIG_PLAYER" else "SPECULATIVE",
        "thesis": (
            "Automated fallback report generated because the AI analysis layer was unavailable.\n\n"
            "Signal weights and indicator pack are reproduced verbatim below; please regenerate the "
            "full analysis once the Claude service is reachable.\n\n"
            "Use this report only for context, not for execution."
        ),
        "macro_signal": "NEUTRAL",
        "macro_score": 5,
        "technical_score": 5,
        "fundamental_score": 5,
        "sentiment_score": 5,
        "analyst_score": 5,
        "historical_score": 5,
        "triggered_signals": [s["name"] for s in triggered_signals],
        "risks": [
            {"name": "Liquidity", "description": "Small-cap liquidity risk.",
             "probability": "MEDIUM", "impact_percentage": 10},
        ],
        "historical_analog": "n/a (fallback report)",
        "catalysts": [s.get("detail", s["name"]) for s in triggered_signals[:5]],
        "verdict": f"{rating} on {ticker} — review manually.",
        "generated_at": utcnow_iso(),
        "_raw_signals": triggered_signals,
        "_total_budget_usd": total_budget_usd,
    }


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    async def _demo():
        report = await analyze_stock(
            "AAPL",
            "BIG_PLAYER",
            [{"name": "DEEP_VALUE", "weight": 20, "direction": "UP"}],
            None,
            10_000,
        )
        print(json.dumps(report, indent=2, default=str))

    asyncio.run(_demo())
