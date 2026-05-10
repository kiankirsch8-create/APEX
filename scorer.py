"""APEX final-score adapter.

Takes the raw analyzer output and computes the three user-facing display
metrics defined in the APEX spec:

* ``upside_percentage`` — capped, rounded percentage move to target/stop
* ``probability_percentage`` — weighted composite scaled by confidence
* ``brief_reasoning`` — one-sentence explanation of the top two signals

The function is pure: it never mutates the original dict in-place beyond
returning a new merged dict.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("apex.scorer")

# Weights for the probability formula (must sum to 1.0).
SCORE_WEIGHTS = {
    "technical_score": 0.25,
    "fundamental_score": 0.20,
    "sentiment_score": 0.20,
    "analyst_score": 0.20,
    "historical_score": 0.15,
}

CONFIDENCE_MULT = {"HIGH": 1.0, "MEDIUM": 0.82, "SPECULATIVE": 0.65}

PROBABILITY_FLOOR = 30
PROBABILITY_CEILING = 92
UPSIDE_DISPLAY_CAP = 300.0


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Upside
# ---------------------------------------------------------------------------

def compute_upside(report: Dict[str, Any]) -> float:
    """Compute the display upside percentage per APEX spec."""
    direction = (report.get("direction") or "UP").upper()
    current = _safe_float(report.get("current_price"))
    if current <= 0:
        return 0.0

    if direction == "UP":
        target = _safe_float(report.get("target_12m"))
        if target <= 0:
            return 0.0
        pct = ((target - current) / current) * 100.0
    else:  # DOWN — short setup
        stop = _safe_float(report.get("stop_loss"))
        if stop <= 0:
            return 0.0
        pct = ((current - stop) / current) * 100.0

    if pct > UPSIDE_DISPLAY_CAP:
        pct = UPSIDE_DISPLAY_CAP
    return round(pct, 1)


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------

def _composite(report: Dict[str, Any]) -> float:
    return sum(
        _safe_float(report.get(field)) * weight
        for field, weight in SCORE_WEIGHTS.items()
    )


def compute_probability(report: Dict[str, Any]) -> int:
    """Compute the display probability percentage per APEX spec."""
    composite = _composite(report)
    base = composite * 10.0  # convert 0-10 -> 0-100
    confidence = (report.get("confidence_level") or "MEDIUM").upper()
    mult = CONFIDENCE_MULT.get(confidence, CONFIDENCE_MULT["MEDIUM"])
    raw = base * mult
    if raw < PROBABILITY_FLOOR:
        raw = PROBABILITY_FLOOR
    if raw > PROBABILITY_CEILING:
        raw = PROBABILITY_CEILING
    return int(round(raw))


# ---------------------------------------------------------------------------
# Brief reasoning
# ---------------------------------------------------------------------------

# Friendly phrases for the top contributing dimensions. The scorer combines the
# top two and adds a macro tail clause when relevant.
_DIMENSION_PHRASES = {
    "technical_score": "Strong technical setup",
    "fundamental_score": "Solid fundamentals",
    "sentiment_score": "Positive sentiment momentum",
    "analyst_score": "Analyst upgrade tailwind",
    "historical_score": "Strong historical analog",
}

_BEARISH_DIMENSION_PHRASES = {
    "technical_score": "Extreme overbought conditions",
    "fundamental_score": "Deteriorating fundamentals",
    "sentiment_score": "Negative sentiment shift",
    "analyst_score": "Analyst downgrades",
    "historical_score": "Weak historical analog",
}


def _top_dimensions(report: Dict[str, Any], n: int = 2) -> List[Tuple[str, float]]:
    scored = [(field, _safe_float(report.get(field))) for field in SCORE_WEIGHTS]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:n]


def _bottom_dimensions(report: Dict[str, Any], n: int = 2) -> List[Tuple[str, float]]:
    scored = [(field, _safe_float(report.get(field))) for field in SCORE_WEIGHTS]
    scored.sort(key=lambda kv: kv[1])
    return scored[:n]


def _signal_phrases(report: Dict[str, Any]) -> List[str]:
    """Build short phrases from concrete signals when available."""
    phrases: List[str] = []
    technicals = report.get("technicals") or {}
    rsi = _safe_float(technicals.get("rsi"))
    pct_to_high = _safe_float(technicals.get("pct_to_52w_high"))
    pct_above_200 = _safe_float(technicals.get("pct_above_200dma"))

    direction = (report.get("direction") or "UP").upper()
    if direction == "UP":
        if 28 <= rsi <= 38:
            phrases.append("Oversold RSI")
        if 0 <= pct_to_high <= 3:
            phrases.append("52-week high breakout")
    else:
        if rsi >= 72:
            phrases.append("Extreme overbought conditions")
        if pct_above_200 >= 25:
            phrases.append("Stock extended >25% above 200-DMA")

    catalysts = report.get("catalysts") or []
    if catalysts:
        first = str(catalysts[0])
        lower = first.lower()
        if "upgrade" in lower:
            phrases.append("Analyst upgrade")
        elif "earnings" in lower:
            phrases.append("Earnings catalyst")
        elif "insider" in lower and "buy" in lower:
            phrases.append("Insider buying")
        elif "insider" in lower and ("sell" in lower or "selling" in lower):
            phrases.append("Insider selling")

    return phrases


def _macro_tail(report: Dict[str, Any]) -> str:
    macro = (report.get("macro_signal") or "NEUTRAL").upper()
    if macro == "BULLISH":
        return "in a bullish macro environment"
    if macro == "BEARISH":
        return "in a bearish macro environment"
    return "in a neutral macro environment"


def compute_brief_reasoning(report: Dict[str, Any]) -> str:
    """Return a one-sentence (<15 words) reasoning string."""
    direction = (report.get("direction") or "UP").upper()

    parts: List[str] = []

    signal_phrases = _signal_phrases(report)
    parts.extend(signal_phrases[:2])

    if len(parts) < 2:
        if direction == "UP":
            for field, _ in _top_dimensions(report, n=4):
                phrase = _DIMENSION_PHRASES.get(field)
                if phrase and phrase not in parts:
                    parts.append(phrase)
                if len(parts) >= 2:
                    break
        else:
            for field, _ in _bottom_dimensions(report, n=4):
                phrase = _BEARISH_DIMENSION_PHRASES.get(field)
                if phrase and phrase not in parts:
                    parts.append(phrase)
                if len(parts) >= 2:
                    break

    if not parts:
        parts = ["Mixed signals"]

    head = " + ".join(parts[:2])
    tail = _macro_tail(report)
    sentence = f"{head} {tail}".strip()

    # Enforce <15 word ceiling.
    words = sentence.split()
    if len(words) > 15:
        sentence = " ".join(words[:15])
    return sentence


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict with display metrics merged into the analyzer output."""
    if not isinstance(report, dict):
        raise TypeError("score_report expected a dict")

    upside = compute_upside(report)
    probability = compute_probability(report)
    reasoning = compute_brief_reasoning(report)

    out = dict(report)
    out["upside_percentage"] = upside
    out["probability_percentage"] = probability
    out["probability_reasoning"] = reasoning
    out["brief_reasoning"] = reasoning
    out["display"] = {
        "upside_pct": upside,
        "probability_pct": probability,
        "reasoning": reasoning,
        "direction": (report.get("direction") or "UP").upper(),
        "rating": report.get("apex_rating", "AVOID"),
    }
    return out


def score_reports(reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [score_report(r) for r in reports]


if __name__ == "__main__":
    import json
    sample = {
        "ticker": "NVDA",
        "current_price": 100.0,
        "target_12m": 145.0,
        "stop_loss": 82.0,
        "direction": "UP",
        "apex_rating": "BUY",
        "confidence_level": "HIGH",
        "macro_signal": "BULLISH",
        "technical_score": 8.5,
        "fundamental_score": 9.0,
        "sentiment_score": 7.8,
        "analyst_score": 8.0,
        "historical_score": 7.0,
        "catalysts": ["Analyst upgrade from Goldman", "Q4 earnings on Feb 21"],
        "technicals": {"rsi": 32, "pct_to_52w_high": 2.1, "pct_above_200dma": 8.0},
    }
    print(json.dumps(score_report(sample), indent=2))
