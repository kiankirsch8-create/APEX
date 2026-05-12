"""Data-driven chart / SMC analysis using yfinance + pandas-ta + Claude (no screenshots)."""
from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401  — registers ``df.ta`` accessor on pandas

from utils import env

CLAUDE_MODEL = "claude-opus-4-5"
MAX_TOKENS = 3000

_TF_MAP: dict[str, tuple[str, str]] = {
    "1h": ("60d", "1h"),
    "4h": ("730d", "4h"),
    "1d": ("730d", "1d"),
    "daily": ("730d", "1d"),
    "1w": ("1825d", "1wk"),
    "weekly": ("1825d", "1wk"),
}


def _client() -> Anthropic:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return float(obj) if isinstance(obj, np.floating) else int(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (pd.Timestamp,)):
        return str(obj)
    return obj


def _period_interval(timeframe: str, years: int) -> tuple[str, str]:
    tf = timeframe.lower().strip()
    default_p, interval = _TF_MAP.get(tf, ("730d", "1d"))
    default_days = int(default_p.rstrip("d"))
    want = max(30, int(years) * 365)
    cap = 730 if interval in ("1h", "4h") else 7300
    days = min(max(want, 30), cap, default_days if interval == "1h" else min(want, cap))
    if interval == "1h" and days > 730:
        days = 730
    return f"{days}d", interval


def _pick_bb_cols(df: pd.DataFrame) -> tuple[str, str, str]:
    lower = mid = upper = ""
    for c in df.columns:
        if str(c).startswith("BBL_"):
            lower = c
        elif str(c).startswith("BBM_"):
            mid = c
        elif str(c).startswith("BBU_"):
            upper = c
    return lower, mid, upper


def _last_float(series: pd.Series, default: float = 0.0) -> float:
    s = series.dropna()
    if s.empty:
        return default
    v = float(s.iloc[-1])
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _parse_json_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    clean = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s*```\s*$", "", clean).strip()
    try:
        out = json.loads(clean)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            out = json.loads(m.group(0))
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def find_swing_points(df: pd.DataFrame, lookback: int = 5) -> tuple[list[tuple[Any, float]], list[tuple[Any, float]]]:
    highs: list[tuple[Any, float]] = []
    lows: list[tuple[Any, float]] = []
    n = len(df)
    if n < lookback * 2 + 1:
        return highs, lows
    high_s = df["High"]
    low_s = df["Low"]
    for i in range(lookback, n - lookback):
        window_h = high_s.iloc[i - lookback : i + lookback + 1]
        window_l = low_s.iloc[i - lookback : i + lookback + 1]
        if high_s.iloc[i] == window_h.max():
            highs.append((df.index[i], float(high_s.iloc[i])))
        if low_s.iloc[i] == window_l.min():
            lows.append((df.index[i], float(low_s.iloc[i])))
    return highs[-10:], lows[-10:]


def _market_structure(swing_highs: list, swing_lows: list) -> str:
    last_5_highs = [h[1] for h in swing_highs[-5:]]
    last_5_lows = [l[1] for l in swing_lows[-5:]]
    if len(last_5_highs) >= 2 and len(last_5_lows) >= 2:
        hh = last_5_highs[-1] > last_5_highs[-2]
        hl = last_5_lows[-1] > last_5_lows[-2]
        lh = last_5_highs[-1] < last_5_highs[-2]
        ll = last_5_lows[-1] < last_5_lows[-2]
        if hh and hl:
            return "BULLISH (HH + HL)"
        if lh and ll:
            return "BEARISH (LH + LL)"
        return "RANGING"
    return "RANGING"


def find_order_blocks(df: pd.DataFrame, n: int = 3) -> list[dict[str, Any]]:
    obs: list[dict[str, Any]] = []
    if len(df) < n + 2:
        return obs
    for i in range(n, len(df) - 1):
        candle = df.iloc[i]
        next_candle = df.iloc[i + 1]
        if (
            candle["Close"] > candle["Open"]
            and next_candle["Close"] < next_candle["Open"]
            and abs(next_candle["Close"] - next_candle["Open"]) > abs(candle["Close"] - candle["Open"]) * 1.5
        ):
            obs.append(
                {
                    "type": "BEARISH",
                    "high": float(candle["High"]),
                    "low": float(candle["Low"]),
                    "date": str(df.index[i].date()),
                }
            )
        if (
            candle["Close"] < candle["Open"]
            and next_candle["Close"] > next_candle["Open"]
            and abs(next_candle["Close"] - next_candle["Open"]) > abs(candle["Close"] - candle["Open"]) * 1.5
        ):
            obs.append(
                {
                    "type": "BULLISH",
                    "high": float(candle["High"]),
                    "low": float(candle["Low"]),
                    "date": str(df.index[i].date()),
                }
            )
    return obs[-6:]


def _bar_index_for_date(df: pd.DataFrame, date_str: str) -> int:
    d = pd.Timestamp(date_str).date()
    for i, ts in enumerate(df.index):
        if hasattr(ts, "date") and ts.date() == d:
            return i
    return 0


def find_fvgs(df: pd.DataFrame) -> list[dict[str, Any]]:
    fvgs: list[dict[str, Any]] = []
    if len(df) < 3:
        return fvgs
    for i in range(1, len(df) - 1):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        nxt = df.iloc[i + 1]
        if nxt["Low"] > prev["High"]:
            fvgs.append(
                {
                    "type": "BULLISH",
                    "top": float(nxt["Low"]),
                    "bottom": float(prev["High"]),
                    "date": str(df.index[i].date()),
                    "filled": False,
                }
            )
        if nxt["High"] < prev["Low"]:
            fvgs.append(
                {
                    "type": "BEARISH",
                    "top": float(prev["Low"]),
                    "bottom": float(nxt["High"]),
                    "date": str(df.index[i].date()),
                    "filled": False,
                }
            )
    recent_fvgs: list[dict[str, Any]] = []
    for fvg in fvgs[-20:]:
        filled = False
        start_i = _bar_index_for_date(df, fvg["date"])
        for j in range(start_i, len(df)):
            if fvg["type"] == "BULLISH":
                if float(df["Low"].iloc[j]) <= fvg["bottom"]:
                    filled = True
                    break
            else:
                if float(df["High"].iloc[j]) >= fvg["top"]:
                    filled = True
                    break
        if not filled:
            recent_fvgs.append(fvg)
    return recent_fvgs[-5:]


def analyze_ticker_full(ticker: str, timeframe: str = "4h", years: int = 2) -> dict[str, Any]:
    """
    Fetch OHLCV via yfinance, compute indicators with pandas-ta, derive SMC-style
    context, then ask Claude for a structured trade thesis (JSON).
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise ValueError("ticker is required")

    if len(sym) == 6 and sym.isalpha():
        yf_ticker = sym + "=X"
    else:
        yf_ticker = sym

    period, interval = _period_interval(timeframe, years)
    df = yf.Ticker(yf_ticker).history(period=period, interval=interval)
    if df.empty:
        raise ValueError(f"No data for {ticker}")

    df = df.sort_index()
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    # Indicators
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.stoch(append=True)
    df.ta.macd(append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.obv(append=True)
    try:
        df.ta.vwap(append=True)
    except Exception:
        pass
    df.ta.adx(length=14, append=True)

    swing_highs, swing_lows = find_swing_points(df)
    structure = _market_structure(swing_highs, swing_lows)
    order_blocks = find_order_blocks(df)
    fvgs = find_fvgs(df)

    prev_day = df.tail(2).iloc[0] if len(df) >= 2 else df.iloc[-1]
    prev_week_data = df.tail(min(10, len(df)))
    liquidity = {
        "prev_day_high": round(float(prev_day["High"]), 5),
        "prev_day_low": round(float(prev_day["Low"]), 5),
        "weekly_high": round(float(prev_week_data["High"].max()), 5),
        "weekly_low": round(float(prev_week_data["Low"].min()), 5),
        "swing_highs": [round(h[1], 5) for h in swing_highs[-3:]],
        "swing_lows": [round(l[1], 5) for l in swing_lows[-3:]],
    }

    latest = df.iloc[-1]
    current_price = round(float(latest["Close"]), 5)

    bbl, bbm, bbu = _pick_bb_cols(df)
    rsi_col = "RSI_14" if "RSI_14" in df.columns else None
    rsi_prev = 50.0
    rsi_now = 50.0
    if rsi_col:
        rsi_now = _last_float(df[rsi_col], 50.0)
        rsi_prev = _last_float(df[rsi_col].iloc[:-1], rsi_now)

    macd_hist = _last_float(df["MACDh_12_26_9"]) if "MACDh_12_26_9" in df.columns else 0.0
    macd_line = _last_float(df["MACD_12_26_9"]) if "MACD_12_26_9" in df.columns else 0.0
    macd_sig_line = _last_float(df["MACDs_12_26_9"]) if "MACDs_12_26_9" in df.columns else 0.0

    ema20 = _last_float(df["EMA_20"]) if "EMA_20" in df.columns else current_price
    ema50 = _last_float(df["EMA_50"]) if "EMA_50" in df.columns else current_price
    ema200 = _last_float(df["EMA_200"]) if "EMA_200" in df.columns else current_price

    bb_u = _last_float(df[bbu]) if bbu else current_price
    bb_l = _last_float(df[bbl]) if bbl else current_price
    bb_m = _last_float(df[bbm]) if bbm else current_price

    atr_v = _last_float(df["ATRr_14"]) if "ATRr_14" in df.columns else 0.0
    adx_v = _last_float(df["ADX_14"]) if "ADX_14" in df.columns else 0.0

    vol_ma = _last_float(df["Volume"].rolling(20).mean(), 0.0)
    vol_cur = _last_float(latest["Volume"], 0.0)

    stoch_k = None
    for c in df.columns:
        if str(c).startswith("STOCHk_"):
            stoch_k = _last_float(df[c])
            break

    indicators: dict[str, Any] = {
        "rsi": round(rsi_now, 2),
        "rsi_prev": round(rsi_prev, 2),
        "macd": round(macd_line, 5),
        "macd_signal_line": round(macd_sig_line, 5),
        "macd_hist": round(macd_hist, 5),
        "ema20": round(ema20, 5),
        "ema50": round(ema50, 5),
        "ema200": round(ema200, 5),
        "bb_upper": round(bb_u, 5),
        "bb_lower": round(bb_l, 5),
        "bb_mid": round(bb_m, 5),
        "atr": round(atr_v, 5),
        "adx": round(adx_v, 2),
        "volume_avg": round(vol_ma, 0),
        "volume_current": round(vol_cur, 0),
    }
    if stoch_k is not None:
        indicators["stoch_k"] = round(stoch_k, 2)

    indicators["rsi_signal"] = (
        "OVERSOLD" if indicators["rsi"] < 30 else "OVERBOUGHT" if indicators["rsi"] > 70 else "NEUTRAL"
    )
    prev_close = _last_float(df["Close"].iloc[:-1], current_price) if len(df) > 1 else current_price
    indicators["rsi_divergence"] = bool(indicators["rsi"] > indicators["rsi_prev"] and current_price < prev_close)
    indicators["macd_signal"] = "BULLISH" if indicators["macd_hist"] > 0 else "BEARISH"
    indicators["price_vs_ema20"] = "ABOVE" if current_price > indicators["ema20"] else "BELOW"
    indicators["price_vs_ema50"] = "ABOVE" if current_price > indicators["ema50"] else "BELOW"
    indicators["price_vs_ema200"] = "ABOVE" if current_price > indicators["ema200"] else "BELOW"
    va = indicators["volume_avg"] or 1.0
    indicators["volume_signal"] = (
        "HIGH" if indicators["volume_current"] > va * 1.5 else "LOW" if indicators["volume_current"] < va * 0.7 else "NORMAL"
    )
    indicators["trend_strength"] = (
        "STRONG" if indicators["adx"] > 25 else "MODERATE" if indicators["adx"] > 20 else "WEAK"
    )

    vwap_col = next((c for c in df.columns if "VWAP" in str(c).upper()), None)
    if vwap_col:
        indicators["vwap"] = round(_last_float(df[vwap_col], current_price), 5)

    context: dict[str, Any] = {
        "ticker": sym,
        "timeframe": timeframe,
        "current_price": current_price,
        "market_structure": structure,
        "indicators": indicators,
        "order_blocks": _json_safe(order_blocks),
        "fair_value_gaps": _json_safe(fvgs),
        "liquidity": liquidity,
        "swing_highs": [round(h[1], 5) for h in swing_highs[-5:]],
        "swing_lows": [round(l[1], 5) for l in swing_lows[-5:]],
        "52w_high": round(float(df["High"].max()), 5),
        "52w_low": round(float(df["Low"].min()), 5),
        "data_points": len(df),
        "date_range": f"{df.index[0].date()} to {df.index[-1].date()}",
    }

    ctx_json = json.dumps(_json_safe(context), indent=2)
    macd_word = indicators["macd_signal"]
    prompt = f"""
You are an elite ICT/SMC trader.
Analyze this asset using the provided data.
No estimation needed — all numbers are exact.

ASSET DATA:
{ctx_json}

Based on this precise data provide:

1. OVERALL VERDICT: STRONG BUY/BUY/WAIT/SELL/STRONG SELL
   Justify with specific numbers from the data.

2. MARKET STRUCTURE ANALYSIS:
   Current structure: {structure}
   Is this valid? What does it mean?

3. SMC ANALYSIS:
   Order blocks: which ones are most relevant now?
   FVGs: which ones price is likely to fill?
   Liquidity: where is smart money targeting?
   Premium/discount: is price cheap or expensive?

4. INDICATOR CONFLUENCE:
   RSI {indicators['rsi']}: what does this tell us?
   MACD {macd_word}: confirming trend?
   Price vs EMAs: trend direction?
   ADX {indicators['adx']}: trend strength?

5. TRADE PLAN:
   Direction: LONG or SHORT
   Entry: exact price and why
   Stop Loss: exact price (use ATR of {indicators['atr']} for sizing)
   TP1: exact price at key level
   TP2: exact price at next key level
   TP3: runner target
   Risk/Reward: calculate precisely
   Max TP1 distance: 2.5% from entry

6. KEY RISKS TO THIS TRADE:
   What would invalidate this setup?

Return ONLY valid JSON matching this schema:
{{
  "verdict": string,
  "confidence": string,
  "confluence_score": number,
  "do_not_trade": boolean,
  "reasoning": string,
  "structure": string,
  "smart_money_summary": string,
  "trade_plan": {{
    "direction": string,
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
    "tp3": number,
    "rr_ratio": string
  }},
  "key_levels": {{
    "resistance": [number, number, number],
    "support": [number, number, number]
  }},
  "indicators_summary": {{
    "rsi": number,
    "rsi_signal": string,
    "macd": string,
    "trend_strength": string,
    "volume": string
  }},
  "order_blocks_relevant": [string],
  "fvgs_relevant": [string],
  "liquidity_targets": [string],
  "risks": [string],
  "confluences": [string],
  "conflicts": [string]
}}
"""

    client = _client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = ""
    for block in response.content or []:
        if getattr(block, "type", None) == "text":
            raw = block.text
            break

    parsed = _parse_json_response(raw)
    if not parsed:
        return {
            "error": "Failed to parse model JSON",
            "raw_response_excerpt": (raw or "")[:2000],
            "current_price": current_price,
            "ticker": sym,
            "timeframe": timeframe,
            "data_driven": True,
            "indicators": indicators,
            "context": context,
        }

    parsed["current_price"] = current_price
    parsed["ticker"] = sym
    parsed["timeframe"] = timeframe
    parsed["data_driven"] = True
    parsed["indicators"] = indicators
    parsed["context"] = context
    return parsed
