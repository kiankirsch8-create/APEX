"""APEX scorer — computes the final display metrics that the frontend renders.

Validates / recomputes:
  - upside_percentage           (with cap & flag)
  - probability_percentage       (weighted score model)
  - reasoning                   (auto-generated punchy sentence)
  - position_sizing             (budget allocation enforcing all caps)
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Probability weights — must sum to 1.0
# ---------------------------------------------------------------------------
WEIGHTS = {
    "macro_score": 0.15,
    "technical_score": 0.25,
    "fundamental_score": 0.20,
    "sentiment_score": 0.15,
    "analyst_score": 0.15,
    "historical_score": 0.10,
}

CONFIDENCE_MULT = {"HIGH": 1.0, "MEDIUM": 0.85, "SPECULATIVE": 0.70}
SECTION_MULT = {"SMALL_CAP": 0.95, "BIG_PLAYER": 1.05}

PROB_FLOOR = 25
PROB_CEIL = 91

UPSIDE_CAP = 500.0  # display cap


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def calculate(report: dict[str, Any]) -> dict[str, Any]:
    """Return the report with final_* fields and validated position_sizing.

    The function never mutates the caller's dict — it returns a copy.
    """
    r = dict(report)

    # ---- upside ----
    upside = _calc_upside(r)
    r["final_upside_percentage"] = upside
    r["upside_flag"] = _upside_flag(upside)

    # ---- probability ----
    prob = _calc_probability(r)
    r["final_probability_percentage"] = prob

    # Mirror final_* into legacy fields for clients that read either
    if not r.get("upside_percentage"):
        r["upside_percentage"] = upside
    if not r.get("probability_percentage"):
        r["probability_percentage"] = prob

    # ---- reasoning ----
    r["final_reasoning"] = _auto_reasoning(r)
    if not r.get("probability_reasoning"):
        r["probability_reasoning"] = r["final_reasoning"]

    # ---- position sizing ----
    r["position_sizing"] = _calc_position_sizing(r)

    return r


# ---------------------------------------------------------------------------
# Upside
# ---------------------------------------------------------------------------
def _calc_upside(r: dict) -> float:
    direction = (r.get("direction") or "UP").upper()
    price = float(r.get("current_price") or 0)
    if price <= 0:
        return 0.0
    if direction == "UP":
        target = float(r.get("target_12m") or price)
        upside = (target - price) / price * 100
    else:
        stop = float(r.get("stop_loss") or price)
        upside = (price - stop) / price * 100
    upside = max(0.0, min(round(upside, 1), UPSIDE_CAP))
    return upside


def _upside_flag(upside: float) -> str:
    if upside > 350:
        return "EXTREME"
    if upside > 200:
        return "HIGH CONVICTION SPECULATIVE"
    return "STANDARD"


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------
def _calc_probability(r: dict) -> int:
    weighted = 0.0
    for key, w in WEIGHTS.items():
        v = r.get(key)
        try:
            v = float(v) if v is not None else 5.0
        except (TypeError, ValueError):
            v = 5.0
        v = max(0.0, min(v, 10.0))
        weighted += v * w

    pct = weighted * 10  # to %
    pct *= CONFIDENCE_MULT.get((r.get("confidence_level") or "MEDIUM").upper(), 0.85)
    pct *= SECTION_MULT.get((r.get("section") or "SMALL_CAP").upper(), 0.95)
    pct = max(PROB_FLOOR, min(round(pct), PROB_CEIL))
    return int(pct)


# ---------------------------------------------------------------------------
# Auto reasoning
# ---------------------------------------------------------------------------
def _auto_reasoning(r: dict) -> str:
    """Pick the top 2 triggered signals by weight and combine into a sentence."""
    raw = r.get("_raw_signals") or []
    if raw and isinstance(raw[0], dict):
        sorted_raw = sorted(raw, key=lambda s: s.get("weight", 0), reverse=True)
    else:
        # only have names — synthesize order
        names = r.get("triggered_signals") or []
        sorted_raw = [{"name": n, "detail": _humanize(n), "weight": 0} for n in names]

    if not sorted_raw:
        return r.get("verdict", "Multi-factor APEX setup.")[:120]

    parts = []
    for s in sorted_raw[:2]:
        parts.append(s.get("detail") or _humanize(s.get("name", "")))

    sentence = " + ".join(p for p in parts if p)
    section = r.get("section") or ""
    rating = r.get("apex_rating") or ""
    if section == "SMALL_CAP" and "speculative" not in sentence.lower():
        sentence = sentence + " (oversold micro-cap)" if "oversold" not in sentence.lower() else sentence
    if rating in {"SHORT", "STRONG SHORT"} and "short" not in sentence.lower():
        sentence = sentence + " — short setup"

    # Hard 15-word cap.
    words = sentence.split()
    if len(words) > 15:
        sentence = " ".join(words[:15])
    return sentence


def _humanize(name: str) -> str:
    return name.replace("_", " ").lower()


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def _calc_position_sizing(r: dict) -> dict:
    """Compute a *deterministic* sizing recommendation that obeys all caps.

    Uses any sizing object from the LLM as a starting hint but enforces caps
    so the system can never be talked into oversizing. All caps are local —
    portfolio-level 35% cap is enforced in scheduler.py across picks.
    """
    budget = float(r.get("_total_budget_usd") or 10_000)
    rating = (r.get("apex_rating") or "").upper()
    section = (r.get("section") or "").upper()
    prob = float(r.get("final_probability_percentage") or 50)
    upside = float(r.get("final_upside_percentage") or 0)
    price = float(r.get("current_price") or 0)
    stop = float(r.get("stop_loss") or 0)

    # 1) Determine target % of budget
    pct, category, reasoning = _allocation_for_rating(rating, section, prob)

    # 2) Hard ceiling: never more than 15% in a single position
    pct = min(pct, 15.0)
    invest_amount = round(budget * pct / 100.0, 2)

    # 3) Risk math
    if price > 0 and stop > 0:
        risk_to_stop_pct = abs(price - stop) / price * 100
    else:
        risk_to_stop_pct = 10.0
    potential_return = round(invest_amount * (upside / 100.0), 2)
    potential_loss = round(invest_amount * (risk_to_stop_pct / 100.0), 2)
    rr_ratio = "1:0"
    if potential_loss > 0:
        rr_ratio = f"1:{round(potential_return / potential_loss, 1)}"

    return {
        "recommended_invest_amount": invest_amount,
        "recommended_invest_percentage": round(pct, 2),
        "potential_return_dollars": potential_return,
        "potential_loss_dollars": potential_loss,
        "risk_reward_ratio": rr_ratio,
        "sizing_reasoning": reasoning,
        "risk_category": category,
        "total_budget_usd": budget,
    }


def _allocation_for_rating(rating: str, section: str, prob: float) -> tuple[float, str, str]:
    """Return (percent_of_budget, risk_category, sizing_reasoning)."""
    if rating == "AVOID":
        return 0.0, "CONSERVATIVE", "AVOID rating — no allocation recommended."

    if rating in {"SHORT", "STRONG SHORT"}:
        # Short plays: max 3%
        if prob >= 70:
            pct = 3.0
        elif prob >= 55:
            pct = 2.0
        else:
            pct = 1.0
        return pct, "AGGRESSIVE", f"Short play — capped at {pct:.0f}% to limit short-side risk."

    if rating == "SPECULATIVE BUY" or section == "SMALL_CAP":
        # Speculative caps from the spec
        if prob < 55:
            pct = 2.0
            reason = "Speculative play — capped at 2% due to sub-55% probability."
        elif prob >= 75:
            pct = 5.0
            reason = "Speculative high-conviction — max 5% allocation."
        else:
            # linear between 55 and 75 → 2-5%
            pct = round(2 + (prob - 55) / 20 * 3, 1)
            reason = f"Speculative — {pct:.1f}% allocation scaled with {prob:.0f}% probability."
        return pct, "SPECULATIVE", reason

    if rating in {"BUY", "STRONG BUY"}:
        # 8–15% scaled with probability between 60 and 85
        if prob <= 60:
            pct = 8.0
        elif prob >= 85:
            pct = 15.0
        else:
            pct = round(8 + (prob - 60) / 25 * 7, 1)
        reason = f"High conviction {rating} — {pct:.1f}% allocation justified by {prob:.0f}% probability."
        category = "MODERATE" if rating == "BUY" else "AGGRESSIVE"
        return pct, category, reason

    # Default safe fallback
    return 3.0, "MODERATE", "Default cautious allocation."


# ---------------------------------------------------------------------------
# Portfolio-level cap enforcement
# ---------------------------------------------------------------------------
def enforce_portfolio_cap(reports: list[dict], max_total_pct: float = 35.0) -> list[dict]:
    """Scale sizes proportionally if combined allocation exceeds the cap."""
    if not reports:
        return reports
    long_reports = [r for r in reports if (r.get("apex_rating") or "").upper() not in {"AVOID"}]
    total_pct = sum((r.get("position_sizing") or {}).get("recommended_invest_percentage", 0) for r in long_reports)
    if total_pct <= max_total_pct or total_pct <= 0:
        return reports
    scale = max_total_pct / total_pct
    for r in long_reports:
        ps = r.get("position_sizing") or {}
        old_pct = ps.get("recommended_invest_percentage", 0)
        new_pct = round(old_pct * scale, 2)
        budget = ps.get("total_budget_usd") or r.get("_total_budget_usd") or 10_000
        invest = round(budget * new_pct / 100, 2)
        upside = float(r.get("final_upside_percentage") or 0)
        price = float(r.get("current_price") or 0)
        stop = float(r.get("stop_loss") or 0)
        risk_to_stop = abs(price - stop) / price * 100 if price > 0 else 10.0
        ps["recommended_invest_percentage"] = new_pct
        ps["recommended_invest_amount"] = invest
        ps["potential_return_dollars"] = round(invest * upside / 100, 2)
        ps["potential_loss_dollars"] = round(invest * risk_to_stop / 100, 2)
        if ps["potential_loss_dollars"] > 0:
            ps["risk_reward_ratio"] = f"1:{round(ps['potential_return_dollars'] / ps['potential_loss_dollars'], 1)}"
        ps["sizing_reasoning"] = (ps.get("sizing_reasoning") or "") + f" (scaled to fit 35% portfolio cap)"
        r["position_sizing"] = ps
    return reports
