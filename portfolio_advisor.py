"""APEX WEALTH — long-term diversified portfolio advice (Claude).

Separate from the daily scanner. Caches to ``portfolio_advice.json`` under RESULTS_DIR.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from anthropic import Anthropic

import analyzer
from utils import RESULTS_DIR, env, log, load_json, save_json, utcnow_iso

CLAUDE_MODEL = "claude-sonnet-4-5-20251022"
MAX_TOKENS = 12000

CACHE_PATH = RESULTS_DIR / "portfolio_advice.json"

_CLIENT: Anthropic | None = None


def _client() -> Anthropic:
    global _CLIENT
    if _CLIENT is None:
        key = env("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _CLIENT = Anthropic(api_key=key)
    return _CLIENT


PORTFOLIO_ADVISOR_PROMPT = """
You are APEX WEALTH — the world's most sophisticated
AI portfolio construction system. You combine:

- Ray Dalio's All Weather Portfolio principles
- Warren Buffett's long term value philosophy
- Modern Portfolio Theory (MPT) optimization
- Factor investing (value, quality, momentum, low vol)
- Macro regime awareness
- Tax efficiency principles

Your job is to build the PERFECT personalized portfolio
for a retail investor based on their total budget.

You receive the total budget and must output:

PART 1 — BUDGET ALLOCATION FRAMEWORK:
Split the budget into three buckets:

BUCKET 1 — FORTRESS (40-50% of budget):
Long term stable growth stocks and ETFs
These never get touched for minimum 3-5 years
Goal: inflation-beating returns with low volatility
Target: 8-12% annual return
Examples: VOO, QQQ, MSFT, AAPL, BRK.B, JNJ, V

BUCKET 2 — GROWTH ENGINE (30-40% of budget):
Medium term growth stocks with strong fundamentals
Hold 6-24 months as thesis develops
Goal: 20-40% annual return
Examples: undervalued quality companies, sector leaders

BUCKET 3 — SPECULATION LAB (10-20% of budget):
High risk high reward plays from APEX daily scanner
These are the speculative and swing picks
Never invest more than 2-5% per position
Goal: 50-200% on winners, accept total loss on some
Examples: small caps, squeeze setups, catalyst plays

PART 2 — FORTRESS PORTFOLIO (specific picks):
Give exactly 8-12 specific stock/ETF recommendations
for the FORTRESS bucket.

For each recommendation provide:
- Ticker and full name
- Allocation % of FORTRESS bucket
- Dollar amount based on budget
- Why this belongs in a forever portfolio
- Expected 5-year return estimate
- Dividend yield if applicable
- Risk level: VERY LOW / LOW / MEDIUM

The FORTRESS must be:
- Globally diversified (US, International, Emerging)
- Sector diversified (tech, healthcare, financials,
  consumer, energy, real estate, bonds)
- Mix of growth and dividend stocks
- At least 2 ETFs for broad market exposure
- At least 1 international position
- At least 1 dividend aristocrat (25+ years of increases)

Use ONLY companies that meet ALL of these criteria:
- Market cap above $10 billion
- Profitable for at least 5 consecutive years
- Revenue growing at least 5% annually
- Debt manageable (debt/equity below 2.0)
- Competitive moat (brand, network effect, patents,
  switching costs, or cost advantage)
- Management with proven track record

PART 3 — GROWTH ENGINE (specific picks):
Give exactly 5-7 specific stock recommendations
for the GROWTH ENGINE bucket.

These are quality companies currently undervalued
that have a clear catalyst for re-rating in 6-24 months.

For each provide:
- Ticker and full name
- Allocation % of GROWTH bucket
- Dollar amount
- The specific re-rating catalyst
- Target price and expected return %
- Expected timeframe: X to Y months
- Stop loss level

PART 4 — SPECULATION LAB RULES:
Do not pick specific stocks here —
that is what the daily APEX scanner does.
Instead provide:
- Maximum % of SPECULATION budget per single trade
- Maximum total open positions at once
- When to take profits (specific % targets)
- When to cut losses (specific % stop)
- How to size based on conviction tier
- Monthly speculation budget review rules

PART 5 — REBALANCING SCHEDULE:
When and how to rebalance:
- FORTRESS: rebalance quarterly if any position
  drifts more than 5% from target allocation
- GROWTH: review monthly, exit if thesis broken
- SPECULATION: review weekly, strict stop losses

PART 6 — RISK ASSESSMENT:
Based on the budget size classify the investor:
Under €5,000: BEGINNER — heavier ETF weighting
€5,000-€25,000: INTERMEDIATE — balanced approach
€25,000-€100,000: ADVANCED — more individual stocks
Above €100,000: SOPHISTICATED — full diversification

Adjust all recommendations based on this classification.

CURRENT MARKET CONTEXT:
Factor in the current macro regime when building
the FORTRESS and GROWTH picks:
- If FEAR MODE: increase bond/defensive allocation
- If NORMAL MODE: standard allocation
- If COMPLACENCY MODE: reduce equity allocation 5-10%
- If RATE SHOCK MODE: avoid long duration bonds

OUTPUT FORMAT — return as valid JSON ONLY (no markdown, no code fences):
{
  "total_budget": number,
  "investor_classification": string,
  "budget_allocation": {
    "fortress_amount": number,
    "fortress_percentage": number,
    "growth_amount": number,
    "growth_percentage": number,
    "speculation_amount": number,
    "speculation_percentage": number,
    "cash_reserve_amount": number,
    "cash_reserve_percentage": number
  },
  "fortress_portfolio": [
    {
      "ticker": string,
      "name": string,
      "allocation_percentage": number,
      "dollar_amount": number,
      "thesis": string,
      "expected_5yr_return": string,
      "dividend_yield": number,
      "risk_level": string,
      "sector": string,
      "is_etf": boolean
    }
  ],
  "growth_engine": [
    {
      "ticker": string,
      "name": string,
      "allocation_percentage": number,
      "dollar_amount": number,
      "catalyst": string,
      "target_price": number,
      "expected_return_percentage": number,
      "timeframe_months": string,
      "stop_loss": number
    }
  ],
  "speculation_rules": {
    "max_per_trade_percentage": number,
    "max_open_positions": number,
    "take_profit_targets": [string],
    "stop_loss_rule": string,
    "monthly_review_rule": string
  },
  "rebalancing_schedule": string,
  "key_risks": [string],
  "portfolio_summary": string
}
""".strip()


def _parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _read_cache_envelope() -> dict[str, Any] | None:
    raw = load_json(CACHE_PATH, default=None)
    if not isinstance(raw, dict):
        return None
    return raw


def cache_stale(envelope: dict[str, Any] | None, budget: float) -> bool:
    """True if we should regenerate advice."""
    if not envelope or "payload" not in envelope:
        return True
    try:
        cached_b = float(envelope.get("cached_budget", -1))
    except (TypeError, ValueError):
        return True
    if abs(cached_b - budget) > 0.01:
        return True
    ts = envelope.get("cached_at")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")[:19])
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
    except (TypeError, ValueError):
        return True
    age = datetime.utcnow() - dt
    if age > timedelta(days=7):
        return True
    now = datetime.utcnow()
    if now.weekday() == 0:  # Monday UTC — refresh if cache predates today
        if dt.date() < now.date():
            return True
    return False


def _call_claude_sync(user_json: str) -> str:
    client = _client()
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=PORTFOLIO_ADVISOR_PROMPT,
        messages=[{"role": "user", "content": user_json}],
    )
    if not msg.content:
        return ""
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


async def generate_portfolio_advice(budget: float) -> dict[str, Any]:
    """Call Claude, parse JSON portfolio plan, write cache, return payload dict."""
    macro = analyzer._macro_snapshot()
    body = {
        "total_budget_usd": budget,
        "macro_market_indicators": macro,
        "instructions": (
            "Return ONLY raw JSON matching the OUTPUT FORMAT in your instructions. "
            "No markdown fences. All numeric fields must be JSON numbers."
        ),
    }
    user_msg = json.dumps(body, indent=2, default=str)

    try:
        raw = await asyncio.to_thread(_call_claude_sync, user_msg)
    except Exception as e:  # noqa: BLE001
        log(f"[PortfolioAdvisor] Claude failed: {e}", "error")
        raise RuntimeError("Portfolio advisor model request failed") from e

    parsed = _parse_json(raw)
    if parsed is None:
        raw2 = await asyncio.to_thread(
            _call_claude_sync,
            user_msg + "\n\nReturn ONLY raw JSON. No markdown. No commentary.",
        )
        parsed = _parse_json(raw2)
    if parsed is None:
        raise RuntimeError("Portfolio advisor did not return parseable JSON")

    parsed.setdefault("total_budget", float(budget))

    envelope = {
        "cached_budget": float(budget),
        "cached_at": utcnow_iso(),
        "payload": parsed,
    }
    save_json(CACHE_PATH, envelope)
    log(f"[PortfolioAdvisor] cached advice for budget=${budget:,.0f}")
    return parsed


async def get_cached_or_regenerate(budget: float) -> dict[str, Any]:
    """Return cached payload if fresh; otherwise regenerate."""
    env = _read_cache_envelope()
    if not cache_stale(env, budget):
        return env["payload"]  # type: ignore[return-value]
    return await generate_portfolio_advice(budget)
