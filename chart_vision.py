"""Claude vision chart analysis — ICT/SMC + classic TA (single and multi-timeframe)."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

from anthropic import Anthropic

from utils import env, log, utcnow_iso

CLAUDE_MODEL = "claude-sonnet-4-5-20251022"
MAX_TOKENS_SINGLE = 3000
MAX_TOKENS_MULTI = 4000

_CLIENT: Anthropic | None = None


def _client() -> Anthropic:
    global _CLIENT
    if _CLIENT is None:
        key = env("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _CLIENT = Anthropic(api_key=key)
    return _CLIENT


def _strip_data_url(b64: str) -> tuple[str, str | None]:
    s = (b64 or "").strip()
    if s.startswith("data:"):
        head, _, rest = s.partition(",")
        mt = None
        if ";" in head:
            mt = head[5:].split(";")[0].strip() or None
        return rest.strip(), mt
    return s, None


def _detect_media_type(raw_b64: str, hint: str | None) -> str:
    if hint and hint in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return hint
    try:
        pad = (-len(raw_b64)) % 4
        blob = base64.b64decode(raw_b64 + "=" * pad, validate=False)[:12]
    except Exception:  # noqa: BLE001
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image/gif"
    if blob.startswith(b"RIFF") and b"WEBP" in blob[:12]:
        return "image/webp"
    return "image/png"


def _message_text(msg: Any) -> str:
    parts: list[str] = []
    for block in msg.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _parse_json_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    clean = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s*```\s*$", "", clean).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


VALID_VERDICTS: frozenset[str] = frozenset(
    {"STRONG BUY", "BUY", "WAIT", "SELL", "STRONG SELL"}
)


def _trade_plan_float(plan: dict[str, Any], key: str) -> float:
    v = plan.get(key)
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _verdict_to_bearish(result: dict[str, Any]) -> None:
    v = str(result.get("verdict", "")).upper()
    if "STRONG" in v and "BUY" in v:
        result["verdict"] = "STRONG SELL"
    elif "BUY" in v:
        result["verdict"] = "SELL"
    else:
        result["verdict"] = "SELL"


def _verdict_to_bullish(result: dict[str, Any]) -> None:
    v = str(result.get("verdict", "")).upper()
    if "STRONG" in v and "SELL" in v:
        result["verdict"] = "STRONG BUY"
    elif "SELL" in v:
        result["verdict"] = "BUY"
    else:
        result["verdict"] = "BUY"


def validate_trade_plan(result: dict[str, Any]) -> dict[str, Any]:
    """Fix direction vs TP/stop contradictions after model output."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    plan_raw = result.get("trade_plan")
    if not isinstance(plan_raw, dict):
        result["trade_plan"] = {}
        return result
    plan = dict(plan_raw)

    direction = str(plan.get("direction", "")).strip().upper()
    entry = _trade_plan_float(plan, "entry_aggressive")
    tp1 = _trade_plan_float(plan, "tp1")
    tp2 = _trade_plan_float(plan, "tp2")
    stop = _trade_plan_float(plan, "stop_loss")

    if entry == 0 or direction in ("", "NO TRADE"):
        result["trade_plan"] = plan
        return result

    if direction == "LONG":
        if tp1 > 0 and tp1 < entry:
            plan["direction"] = "SHORT"
            direction = "SHORT"
            _verdict_to_bearish(result)
    elif direction == "SHORT":
        if tp1 > 0 and tp1 > entry:
            plan["direction"] = "LONG"
            direction = "LONG"
            _verdict_to_bullish(result)

    stop = _trade_plan_float(plan, "stop_loss")
    if direction == "LONG" and stop > 0 and stop > entry:
        plan["stop_loss"] = round(entry * 0.99, 6)
    elif direction == "SHORT" and stop > 0 and stop < entry:
        plan["stop_loss"] = round(entry * 1.01, 6)

    result["trade_plan"] = plan
    return result


def enforce_verdict_format(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize verdict to VALID_VERDICTS; never LONG/SHORT as verdict."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    raw = str(result.get("verdict", "")).strip()
    vu = " ".join(raw.upper().replace("/", " ").replace("_", " ").split())

    if vu in ("NEUTRAL", "NEUTRAL WAIT", "NO TRADE", "NOTRADE", "HOLD"):
        result["verdict"] = "WAIT"
        return result
    if raw in VALID_VERDICTS:
        result["verdict"] = raw
        return result
    vn = vu.replace(" ", "")
    for valid in VALID_VERDICTS:
        if vn == valid.upper().replace(" ", ""):
            result["verdict"] = valid
            return result

    if vu == "LONG":
        result["verdict"] = "BUY"
        return result
    if vu == "SHORT":
        result["verdict"] = "SELL"
        return result

    plan = result.get("trade_plan")
    if isinstance(plan, dict):
        d = str(plan.get("direction", "")).strip().upper()
        if d == "LONG":
            result["verdict"] = "BUY"
        elif d == "SHORT":
            result["verdict"] = "SELL"
        else:
            result["verdict"] = "WAIT"
    else:
        result["verdict"] = "WAIT"
    return result


def cap_tp_distance(result: dict[str, Any]) -> dict[str, Any]:
    """Cap TP and stop distance from entry (forex vs stock/crypto limits)."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    plan_raw = result.get("trade_plan")
    if not isinstance(plan_raw, dict):
        return result
    plan = dict(plan_raw)

    entry = _trade_plan_float(plan, "entry_aggressive")
    direction = str(plan.get("direction", "")).strip().upper()
    if entry == 0 or direction not in ("LONG", "SHORT"):
        result["trade_plan"] = plan
        return result

    asset = str(result.get("asset_type", "")).upper()
    if asset == "FOREX":
        max_tp1_pct, max_tp2_pct, max_stop_pct = 0.025, 0.045, 0.015
    else:
        max_tp1_pct, max_tp2_pct, max_stop_pct = 0.05, 0.09, 0.03

    tp1 = _trade_plan_float(plan, "tp1")
    tp2 = _trade_plan_float(plan, "tp2")
    stop = _trade_plan_float(plan, "stop_loss")

    if direction == "SHORT":
        cap_tp1 = entry * (1 - max_tp1_pct)
        cap_tp2 = entry * (1 - max_tp2_pct)
        min_stop = entry * (1 + max_stop_pct)
        if tp1 > 0 and tp1 < entry * 0.94:
            plan["tp1"] = round(cap_tp1, 5)
        if tp2 > 0 and tp2 < entry * 0.90:
            plan["tp2"] = round(cap_tp2, 5)
        if stop > 0 and stop > entry * 1.02:
            plan["stop_loss"] = round(min_stop, 5)
        if stop > 0 and stop <= entry:
            plan["stop_loss"] = round(min_stop, 5)
    elif direction == "LONG":
        cap_tp1 = entry * (1 + max_tp1_pct)
        cap_tp2 = entry * (1 + max_tp2_pct)
        max_stop_below = entry * (1 - max_stop_pct)
        if tp1 > 0 and tp1 > entry * 1.06:
            plan["tp1"] = round(cap_tp1, 5)
        if tp2 > 0 and tp2 > entry * 1.10:
            plan["tp2"] = round(cap_tp2, 5)
        if stop > 0 and stop < max_stop_below:
            plan["stop_loss"] = round(max_stop_below, 5)
        if stop > 0 and stop >= entry:
            plan["stop_loss"] = round(max_stop_below, 5)

    direction = str(plan.get("direction", "")).strip().upper()
    entry = _trade_plan_float(plan, "entry_aggressive")
    stop = _trade_plan_float(plan, "stop_loss")
    tp1 = _trade_plan_float(plan, "tp1")
    if direction == "LONG" and entry > 0 and stop > 0 and tp1 > 0:
        risk = entry - stop
        reward = tp1 - entry
        if risk > 0 and reward > 0:
            plan["rr_ratio"] = f"1:{round(reward / risk, 1)}"
    elif direction == "SHORT" and entry > 0 and stop > 0 and tp1 > 0:
        risk = stop - entry
        reward = entry - tp1
        if risk > 0 and reward > 0:
            plan["rr_ratio"] = f"1:{round(reward / risk, 1)}"

    result["trade_plan"] = plan
    return result


def _safe_float_top(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _prices_from_summary(summary: str) -> list[float]:
    """Pull plausible price literals from summary (forex 4dp first, then broader)."""
    if not summary:
        return []
    seen: set[float] = set()
    ordered: list[float] = []
    for pat in (r"\d+\.\d{4}", r"\d+\.\d{2,6}", r"\d+\.\d+"):
        for x in re.findall(pat, summary):
            try:
                f = float(x)
            except ValueError:
                continue
            if f not in seen:
                seen.add(f)
                ordered.append(f)
        if ordered:
            break
    return ordered


def fix_entry_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Backfill entry_aggressive / entry_conservative and risk fields when the model omits them."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    plan_raw = result.get("trade_plan")
    if not isinstance(plan_raw, dict):
        result["trade_plan"] = {}
        return result
    plan = dict(plan_raw)

    direction_raw = str(plan.get("direction", "")).strip().upper()
    if direction_raw in ("NO TRADE", "NO_TRADE"):
        cur = _safe_float_top(result.get("current_price"), 0.0)
        if cur <= 0:
            cur = _trade_plan_float(plan, "tp1") or _trade_plan_float(plan, "stop_loss") or 0.0
        if cur > 0 and (not plan.get("entry_aggressive") or _trade_plan_float(plan, "entry_aggressive") == 0):
            plan["entry_aggressive"] = round(cur, 6)
        if cur > 0 and (not plan.get("entry_conservative") or _trade_plan_float(plan, "entry_conservative") == 0):
            plan["entry_conservative"] = round(cur, 6)
        result["trade_plan"] = plan
        return result

    summary = str(result.get("summary") or "")
    direction = direction_raw if direction_raw in ("LONG", "SHORT") else "LONG"

    current = _safe_float_top(result.get("current_price"), 0.0)
    if current <= 0:
        current = _trade_plan_float(plan, "tp1") or _trade_plan_float(plan, "stop_loss") or 0.0

    ea = plan.get("entry_aggressive")
    try:
        ea_f = float(ea) if ea is not None and ea != "" else 0.0
    except (TypeError, ValueError):
        ea_f = 0.0

    if not ea or ea_f == 0:
        prices = _prices_from_summary(summary)
        if prices and current > 0:
            if direction == "SHORT":
                above_current = [p for p in prices if p >= current * 0.999]
                if above_current:
                    plan["entry_aggressive"] = round(min(above_current), 6)
                else:
                    plan["entry_aggressive"] = round(current, 6)
            else:
                below_current = [p for p in prices if p <= current * 1.001]
                if below_current:
                    plan["entry_aggressive"] = round(max(below_current), 6)
                else:
                    plan["entry_aggressive"] = round(current, 6)
        else:
            plan["entry_aggressive"] = round(current, 6) if current > 0 else 0.0

    ec = plan.get("entry_conservative")
    try:
        ec_f = float(ec) if ec is not None and ec != "" else 0.0
    except (TypeError, ValueError):
        ec_f = 0.0
    if not ec or ec_f == 0:
        entry = _trade_plan_float(plan, "entry_aggressive") or current
        if direction == "SHORT":
            plan["entry_conservative"] = round(entry * 1.005, 5)
        else:
            plan["entry_conservative"] = round(entry * 0.995, 5)

    entry = _trade_plan_float(plan, "entry_aggressive")
    stop = _trade_plan_float(plan, "stop_loss")
    tp1 = _trade_plan_float(plan, "tp1")
    direction = str(plan.get("direction", "")).strip().upper()

    if entry > 0 and stop > 0 and tp1 > 0 and direction in ("LONG", "SHORT"):
        if direction == "SHORT":
            risk = abs(stop - entry)
            reward = abs(entry - tp1)
        else:
            risk = abs(entry - stop)
            reward = abs(tp1 - entry)
        if risk > 0:
            plan["rr_ratio"] = f"1:{round(reward / risk, 1)}"
            plan["risk_pct"] = round(risk / entry * 100, 2)

    result["trade_plan"] = plan
    return result


def _finalize_chart_analysis(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("error"):
        return result
    result = validate_trade_plan(result)
    result = enforce_verdict_format(result)
    result = cap_tp_distance(result)
    result = fix_entry_fields(result)
    return result


CHART_PROMPT_SINGLE = """
You are an elite professional trader combining:
- ICT (Inner Circle Trader) methodology
- Smart Money Concepts (SMC)
- Classic technical analysis
- Price action mastery

Analyze this chart with extreme precision.
Never give vague answers. Every number must be specific.

ANALYSIS PROTOCOL:

1. IDENTIFY:
Asset type (FOREX/STOCK/CRYPTO)
Current approximate price
Visible timeframe

2. OVERALL VERDICT — choose exactly one (this field is NOT trade direction):
VERDICT MUST BE EXACTLY ONE OF:
STRONG BUY = high confidence long, 7+ bullish signals
BUY = moderate confidence long, 5-6 bullish signals
WAIT = no clear edge, conflicting signals, stay out
SELL = moderate confidence short, 5-6 bearish signals
STRONG SELL = high confidence short, 7+ bearish signals

NEVER use LONG or SHORT as the verdict.
LONG and SHORT only appear in trade_plan.direction.

CRITICAL RULE:
If trade_plan.direction is SHORT then TP1 and TP2 must be BELOW the entry price (lower numbers than entry).
If trade_plan.direction is LONG then TP1 and TP2 must be ABOVE the entry price (higher numbers than entry).
NEVER give a long setup with targets below entry.
NEVER give a short setup with targets above entry.

For forex pairs target maximum 2.5% for TP1 from entry.
For stocks target maximum 5% for TP1 from entry.
Stop loss maximum 1.5% from entry for forex, 3% for stocks.
These limits reflect realistic swing trade targets.

3. MARKET STRUCTURE:
- Trend direction: BULLISH/BEARISH/RANGING
- Last significant high and low prices
- Break of Structure (BOS): yes/no, price, direction
- Change of Character (CHOCH): yes/no, price
- Higher highs/lows or lower highs/lows pattern

4. SMART MONEY CONCEPTS:
LIQUIDITY POOLS:
- Buy side liquidity sitting above: exact price
- Sell side liquidity sitting below: exact price
- Equal highs (liquidity magnet): price
- Equal lows (liquidity magnet): price

ORDER BLOCKS:
- Most recent bullish order block zone: price range
- Most recent bearish order block zone: price range
- Is price at an order block right now? yes/no

FAIR VALUE GAPS:
- Bullish FVG (imbalance): price range
- Bearish FVG (imbalance): price range
- Is price trading inside a FVG? yes/no

PREMIUM/DISCOUNT:
- Range high: price
- Range low: price
- 50% level (equilibrium): price
- Current position: PREMIUM/DISCOUNT/EQUILIBRIUM
- Optimal Trade Entry (OTE) zone: 0.618-0.786 of range

5. CLASSIC PATTERNS:
Chart pattern if any: name, completion %, target price
Candlestick pattern if any: name, location, implication
Key trendlines: broken/tested/holding

6. INDICATORS (if visible on chart):
RSI level and signal (divergence if present)
MACD signal and histogram direction
Moving averages: price relationship to MAs
Volume: accumulation or distribution
Bollinger Bands: position and squeeze

7. KEY PRICE LEVELS:
Resistance 1: price (why important)
Resistance 2: price (why important)
Resistance 3: price (why important)
Support 1: price (why important)
Support 2: price (why important)
Support 3: price (why important)

8. TRADE PLAN:
Direction: LONG or SHORT or NO TRADE
If NO TRADE explain why (low confluence)

Entry Zone:
- Aggressive: enter at current price/order block
- Conservative: wait for confirmation at price
- Trigger condition: specific price action required

Stop Loss: exact price
- Placed below/above: specific level name
- Risk from aggressive entry: X%

Take Profit targets:
- TP1: exact price — at what level and why
- TP2: exact price — at what level and why
- TP3: exact price — runner target

Risk/Reward ratio: 1:X

9. CONFLUENCES:
List every signal supporting the verdict
List every signal conflicting with verdict
Confluence score: X out of 10

10. CONFIDENCE:
VERY HIGH: 8+ signals aligned — trade it
HIGH: 6-7 signals — good setup
MEDIUM: 4-5 signals — smaller size
LOW: under 4 — DO NOT TRADE

Return ONLY valid JSON, no markdown, no explanation:
{
  "asset_type": string,
  "ticker": string,
  "timeframe": string,
  "current_price": number,
  "verdict": "STRONG BUY|BUY|WAIT|SELL|STRONG SELL",
  "confidence": "VERY HIGH|HIGH|MEDIUM|LOW",
  "confluence_score": number,
  "do_not_trade": boolean,
  "do_not_trade_reason": string or null,
  "trend": {
    "direction": "BULLISH|BEARISH|RANGING",
    "last_high": number,
    "last_low": number,
    "structure": string
  },
  "smart_money": {
    "bsl": number or null,
    "ssl": number or null,
    "equal_highs": number or null,
    "equal_lows": number or null,
    "bullish_ob": string or null,
    "bearish_ob": string or null,
    "at_order_block": boolean,
    "bullish_fvg": string or null,
    "bearish_fvg": string or null,
    "in_fvg": boolean,
    "position": "PREMIUM|DISCOUNT|EQUILIBRIUM",
    "ote_zone": string or null,
    "choch": boolean,
    "choch_price": number or null,
    "bos": boolean,
    "bos_direction": string or null,
    "bos_price": number or null
  },
  "patterns": {
    "chart_pattern": string or null,
    "chart_pattern_completion": number or null,
    "chart_pattern_target": number or null,
    "candlestick": string or null,
    "candlestick_signal": string or null
  },
  "indicators": {
    "rsi": number or null,
    "rsi_signal": string,
    "rsi_divergence": boolean,
    "macd": "BULLISH|BEARISH|NEUTRAL",
    "volume": "ACCUMULATION|DISTRIBUTION|NEUTRAL",
    "bb_position": string
  },
  "levels": {
    "resistance": [
      {"price": number, "reason": string},
      {"price": number, "reason": string},
      {"price": number, "reason": string}
    ],
    "support": [
      {"price": number, "reason": string},
      {"price": number, "reason": string},
      {"price": number, "reason": string}
    ]
  },
  "trade_plan": {
    "direction": "LONG|SHORT|NO TRADE",
    "entry_aggressive": number,
    "entry_conservative": number,
    "entry_trigger": string,
    "stop_loss": number,
    "stop_reason": string,
    "risk_pct": number,
    "tp1": number,
    "tp1_reason": string,
    "tp2": number,
    "tp2_reason": string,
    "tp3": number or null,
    "tp3_reason": string or null,
    "rr_ratio": string
  },
  "confluences": [string],
  "conflicts": [string],
  "summary": string
}
"""


CHART_PROMPT_MULTI = """
You are an elite professional trader analyzing
MULTIPLE TIMEFRAMES of the same asset.

Images are labeled in order:
Image 1 = WEEKLY chart (highest timeframe bias)
Image 2 = DAILY chart
Image 3 = 4H chart
Image 4 = 1H chart (entry timing)

MULTI-TIMEFRAME PROTOCOL:

1. Start with WEEKLY for overall bias
2. Confirm with DAILY
3. Find setup on 4H
4. Time entry on 1H

For each timeframe identify:
- Trend direction
- Key levels
- Any SMC concepts
- Verdict for that timeframe

Then synthesize:
- How many timeframes align?
- What is the dominant bias?
- Where is the optimal entry on the lowest TF?
- What is the final verdict?

""" + CHART_PROMPT_SINGLE + """

Add these additional fields to JSON output:
"timeframe_analysis": {
  "weekly": {"trend": string, "verdict": string,
             "key_level": number},
  "daily": {"trend": string, "verdict": string,
            "key_level": number},
  "4h": {"trend": string, "verdict": string,
         "key_level": number},
  "1h": {"trend": string, "verdict": string,
         "key_level": number}
},
"tf_confluence": "X/4 timeframes bullish/bearish",
"entry_timeframe": "1H|4H"
"""


def _decode_image_b64(image_data: str) -> tuple[str, str]:
    raw_b64, hint = _strip_data_url(image_data)
    if not raw_b64:
        raise ValueError("image_base64 is empty")
    try:
        decoded = base64.b64decode(raw_b64 + "=" * ((-len(raw_b64)) % 4), validate=False)
    except Exception as e:  # noqa: BLE001
        raise ValueError("invalid base64 image data") from e
    if len(decoded) > 15 * 1024 * 1024:
        raise ValueError("image exceeds 15MB decoded limit")
    media_type = _detect_media_type(raw_b64, hint)
    return raw_b64, media_type


def analyze_chart(image_data: str, ticker: str, timeframe: str) -> dict[str, Any]:
    """Synchronous single-chart analysis (ICT/SMC + TA)."""
    raw_b64, media_type = _decode_image_b64(image_data)
    t = (ticker or "").strip() or "UNKNOWN"
    tf = (timeframe or "").strip() or "unspecified"

    def _call() -> dict[str, Any]:
        client = _client()
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS_SINGLE,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": raw_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Ticker: {t}\nTimeframe: {tf}\n\n{CHART_PROMPT_SINGLE}",
                        },
                    ],
                }
            ],
        )
        raw = _message_text(msg)
        result = _parse_json_response(raw)
        if result is None:
            log("[ChartVision] JSON parse failed, retrying", "warning")
            msg2 = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS_SINGLE,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": raw_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    f"Ticker: {t}\nTimeframe: {tf}\n\n{CHART_PROMPT_SINGLE}\n\n"
                                    "Return ONLY raw JSON. No markdown fences."
                                ),
                            },
                        ],
                    }
                ],
            )
            raw2 = _message_text(msg2)
            result = _parse_json_response(raw2)
        if result is None:
            return {
                "error": "Analysis failed",
                "ticker": t.upper(),
                "timeframe": tf,
                "verdict": "WAIT",
                "do_not_trade": True,
                "do_not_trade_reason": "Analysis could not complete",
            }
        result["ticker"] = (result.get("ticker") or t).strip().upper()
        result["timeframe"] = result.get("timeframe") or tf
        result["analyzed_at"] = utcnow_iso()
        result["multi_timeframe"] = False
        return _finalize_chart_analysis(result)

    try:
        return _call()
    except Exception as e:  # noqa: BLE001
        log(f"[ChartVision] analyze_chart failed: {e}", "error")
        return {
            "error": str(e),
            "ticker": t.upper(),
            "timeframe": tf,
            "verdict": "WAIT",
            "do_not_trade": True,
            "do_not_trade_reason": "Analysis could not complete",
        }


def analyze_multi_chart(images: list[str], ticker: str, timeframes: list[str]) -> dict[str, Any]:
    """Synchronous multi-timeframe chart analysis."""
    if not images:
        return {
            "error": "No images provided",
            "verdict": "WAIT",
            "do_not_trade": True,
        }
    t = (ticker or "").strip() or "UNKNOWN"
    tfs = timeframes if timeframes else ["1W", "1D", "4H", "1H"]
    tf_labels = ["WEEKLY", "DAILY", "4H", "1H"]
    decoded: list[tuple[str, str]] = [_decode_image_b64(img) for img in images]

    def _call() -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for i, (raw_b64, media_type) in enumerate(decoded):
            label = tf_labels[i] if i < len(tf_labels) else f"TF{i + 1}"
            content.append({"type": "text", "text": f"=== IMAGE {i + 1}: {label} CHART ==="})
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": raw_b64},
                }
            )
        content.append(
            {
                "type": "text",
                "text": (
                    f"Ticker: {t}\nTimeframes: {', '.join(tfs)}\n\n{CHART_PROMPT_MULTI}"
                ),
            }
        )

        client = _client()
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS_MULTI,
            messages=[{"role": "user", "content": content}],
        )
        raw = _message_text(msg)
        result = _parse_json_response(raw)
        if result is None:
            log("[ChartVision] Multi JSON parse failed", "warning")
            return {
                "error": "Analysis failed",
                "ticker": t.upper(),
                "verdict": "WAIT",
                "do_not_trade": True,
                "do_not_trade_reason": "Analysis could not complete",
            }
        result["ticker"] = (result.get("ticker") or t).strip().upper()
        result["multi_timeframe"] = True
        result["analyzed_at"] = utcnow_iso()
        return _finalize_chart_analysis(result)

    try:
        return _call()
    except Exception as e:  # noqa: BLE001
        log(f"[ChartVision] Multi analysis failed: {e}", "error")
        return {"error": str(e), "verdict": "WAIT", "do_not_trade": True, "ticker": t.upper()}


async def analyze_chart_image(image_base64: str, ticker: str, timeframe: str) -> dict[str, Any]:
    """Async wrapper used by legacy callers."""
    return await asyncio.to_thread(analyze_chart, image_base64, ticker, timeframe)
