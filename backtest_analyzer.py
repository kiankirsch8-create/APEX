"""Historical chart backtests: point-in-time context + Claude call + forward outcome."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401

from utils import RESULTS_DIR, env, load_json, log, save_json, utcnow_iso

CLAUDE_MODEL = "claude-sonnet-4-5-20251022"
MAX_TOKENS_SINGLE = 2000

BACKTEST_HISTORY_PATH = RESULTS_DIR / "backtest_history.json"

_TF_MAP: dict[str, str] = {
    "1h": "1h",
    "4h": "1h",
    "daily": "1d",
    "1d": "1d",
    "weekly": "1wk",
    "1w": "1wk",
}


def _client() -> Anthropic:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


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


def _message_text(response: Any) -> str:
    parts: list[str] = []
    for block in response.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    idx = out.index
    if getattr(idx, "tz", None) is not None:
        out.index = idx.tz_convert("UTC").tz_localize(None)
    return out


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


def _future_fetch_buffer(interval: str, forward_candles: int) -> timedelta:
    if interval == "1h":
        return timedelta(days=max(14, forward_candles // 4 + 7))
    if interval == "1wk":
        return timedelta(weeks=max(4, forward_candles + 2))
    return timedelta(days=max(35, forward_candles * 3 + 10))


def append_backtest_history(record: dict[str, Any]) -> None:
    data = load_json(BACKTEST_HISTORY_PATH, default={"entries": []})
    if not isinstance(data, dict):
        data = {"entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    row = {**record, "saved_at": utcnow_iso()}
    entries.append(row)
    save_json(BACKTEST_HISTORY_PATH, {"entries": entries})


def get_backtest_history() -> dict[str, Any]:
    return load_json(BACKTEST_HISTORY_PATH, default={"entries": []})


def analyze_at_date(
    ticker: str,
    timeframe: str,
    analysis_date: str,
    forward_candles: int = 20,
    *,
    save_history: bool = True,
) -> dict[str, Any]:
    """
    Analyze ticker as if it were ``analysis_date``, using only data through that date,
    then compare to the next ``forward_candles`` bars.

    ``analysis_date`` format: ``YYYY-MM-DD``.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise ValueError("ticker is required")

    interval = _TF_MAP.get(timeframe.lower().strip(), "1d")
    if len(sym) == 6 and sym.isalpha():
        yf_ticker = sym + "=X"
    else:
        yf_ticker = sym

    target_date = datetime.strptime(analysis_date.strip(), "%Y-%m-%d")
    start_date = target_date - timedelta(days=730)
    buf = _future_fetch_buffer(interval, forward_candles)
    fetch_end = target_date + buf + timedelta(days=1)

    full_df = yf.Ticker(yf_ticker).history(
        start=start_date.strftime("%Y-%m-%d"),
        end=fetch_end.strftime("%Y-%m-%d"),
        interval=interval,
    )
    if full_df.empty:
        raise ValueError(f"No data for {ticker}")

    full_df = _strip_tz(full_df.sort_index())

    day_end = pd.Timestamp(analysis_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    past_df = full_df[full_df.index <= day_end].copy()
    future_df = full_df[full_df.index > day_end].copy()

    if len(past_df) < 50:
        raise ValueError(f"Not enough historical data before {analysis_date}")
    if future_df.empty:
        raise ValueError(f"No future data after {analysis_date}")

    past_df.ta.ema(length=20, append=True)
    past_df.ta.ema(length=50, append=True)
    past_df.ta.ema(length=200, append=True)
    past_df.ta.rsi(length=14, append=True)
    past_df.ta.macd(append=True)
    past_df.ta.bbands(length=20, append=True)
    past_df.ta.atr(length=14, append=True)
    past_df.ta.adx(length=14, append=True)

    latest = past_df.iloc[-1]
    current_price = round(float(latest["Close"]), 5)
    if current_price <= 0:
        raise ValueError("Invalid close price at analysis date")

    bbl, bbm, bbu = _pick_bb_cols(past_df)
    indicators: dict[str, Any] = {}
    try:
        indicators = {
            "rsi": round(_last_float(past_df["RSI_14"], 50.0), 2),
            "macd_hist": round(_last_float(past_df["MACDh_12_26_9"]), 5),
            "macd_signal_line": round(_last_float(past_df["MACDs_12_26_9"]), 5),
            "ema20": round(_last_float(past_df["EMA_20"], current_price), 5),
            "ema50": round(_last_float(past_df["EMA_50"], current_price), 5),
            "ema200": round(_last_float(past_df["EMA_200"], current_price), 5),
            "atr": round(_last_float(past_df["ATRr_14"]), 5),
            "adx": round(_last_float(past_df["ADX_14"]), 2),
            "bb_upper": round(_last_float(past_df[bbu], current_price), 5) if bbu else current_price,
            "bb_lower": round(_last_float(past_df[bbl], current_price), 5) if bbl else current_price,
        }
    except Exception as e:  # noqa: BLE001
        log(f"[Backtest] Indicator error: {e}", level="warning")

    recent_past = past_df.tail(50)
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(5, len(recent_past) - 5):
        w_h = recent_past["High"].iloc[i - 5 : i + 6]
        w_l = recent_past["Low"].iloc[i - 5 : i + 6]
        if recent_past["High"].iloc[i] == w_h.max():
            swing_highs.append(round(float(recent_past["High"].iloc[i]), 5))
        if recent_past["Low"].iloc[i] == w_l.min():
            swing_lows.append(round(float(recent_past["Low"].iloc[i]), 5))

    ema20 = float(indicators.get("ema20", current_price))
    ema50 = float(indicators.get("ema50", current_price))
    ema200 = float(indicators.get("ema200", current_price))

    if current_price > ema20 > ema50 > ema200:
        trend = "STRONG UPTREND"
    elif current_price < ema20 < ema50 < ema200:
        trend = "STRONG DOWNTREND"
    elif current_price > ema50:
        trend = "UPTREND"
    elif current_price < ema50:
        trend = "DOWNTREND"
    else:
        trend = "RANGING"

    n = min(int(forward_candles), len(future_df))
    future_slice = future_df.iloc[:n]
    future_highs = future_slice["High"].astype(float).values
    future_lows = future_slice["Low"].astype(float).values
    future_closes = future_slice["Close"].astype(float).values

    max_gain = round((float(np.max(future_highs)) - current_price) / current_price * 100, 2)
    max_loss = round((float(np.min(future_lows)) - current_price) / current_price * 100, 2)
    final_price = round(float(future_closes[-1]), 5)
    final_return = round((final_price - current_price) / current_price * 100, 2)

    context_obj = {
        "ticker": sym,
        "analysis_date": analysis_date,
        "timeframe": timeframe,
        "current_price_at_date": current_price,
        "trend": trend,
        "indicators": indicators,
        "recent_swing_highs": swing_highs[-5:],
        "recent_swing_lows": swing_lows[-5:],
        "52w_high": round(float(past_df["High"].max()), 5),
        "52w_low": round(float(past_df["Low"].min()), 5),
    }
    context_str = json.dumps(context_obj, indent=2, default=str)

    prompt = f"""
You are analyzing {sym} on {analysis_date}
as if you are a trader on that exact date.
You have NO knowledge of what happened after this date.
Use ONLY the data provided.

MARKET DATA ON {analysis_date}:
{context_str}

Provide your complete trading analysis as if
it is {analysis_date}. Give:
1. Verdict: STRONG BUY/BUY/WAIT/SELL/STRONG SELL
2. Direction: LONG or SHORT
3. Entry price (exact)
4. Stop loss (exact, max 1.5% from entry for forex)
5. TP1 (max 1.5% from entry for 4H)
6. TP2 (max 2.5% from entry for 4H)
7. Key confluences
8. Main risks

Return ONLY valid JSON:
{{
  "verdict": string,
  "confidence": string,
  "direction": string,
  "entry": number,
  "stop_loss": number,
  "tp1": number,
  "tp2": number,
  "rr_ratio": string,
  "confluences": [string],
  "risks": [string],
  "reasoning": string,
  "key_levels": {{
    "resistance": [number],
    "support": [number]
  }}
}}
"""

    client = _client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS_SINGLE,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _message_text(response)
    ai_analysis = _parse_json_response(raw) or {}

    if not ai_analysis:
        ai_analysis = {
            "verdict": "WAIT",
            "confidence": "low",
            "direction": "LONG",
            "entry": current_price,
            "stop_loss": current_price,
            "tp1": current_price,
            "tp2": current_price,
            "rr_ratio": "1:0",
            "confluences": [],
            "risks": ["Model output was not valid JSON"],
            "reasoning": (raw or "")[:500],
            "key_levels": {"resistance": [], "support": []},
            "parse_error": True,
        }

    predicted_direction = str(ai_analysis.get("direction", "")).strip().upper()
    tp1 = float(ai_analysis.get("tp1") or 0)
    tp2 = float(ai_analysis.get("tp2") or 0)
    stop = float(ai_analysis.get("stop_loss") or 0)
    entry = float(ai_analysis.get("entry") or current_price)

    first_tp1: int | None = None
    first_tp2: int | None = None
    first_stop: int | None = None

    if predicted_direction == "LONG" and tp1 and stop:
        for i in range(len(future_highs)):
            h, l = float(future_highs[i]), float(future_lows[i])
            if first_tp1 is None and h >= tp1:
                first_tp1 = i + 1
            if first_tp2 is None and tp2 and h >= tp2:
                first_tp2 = i + 1
            if first_stop is None and l <= stop:
                first_stop = i + 1
    elif predicted_direction == "SHORT" and tp1 and stop:
        for i in range(len(future_highs)):
            h, l = float(future_highs[i]), float(future_lows[i])
            if first_tp1 is None and l <= tp1:
                first_tp1 = i + 1
            if first_tp2 is None and tp2 and l <= tp2:
                first_tp2 = i + 1
            if first_stop is None and h >= stop:
                first_stop = i + 1

    hit_tp1 = first_tp1 is not None
    hit_tp2 = first_tp2 is not None
    hit_stop = first_stop is not None
    candles_to_tp1 = first_tp1
    candles_to_tp2 = first_tp2
    candles_to_stop = first_stop

    if predicted_direction not in ("LONG", "SHORT"):
        direction_correct = abs(final_return) < 0.01
        outcome = "OPEN — non-directional signal"
        correct = direction_correct
    elif hit_stop and not hit_tp1:
        outcome = "LOSS — Stop hit"
        correct = False
    elif hit_tp1 and not hit_stop:
        outcome = "WIN — TP1 hit"
        correct = True
    elif hit_tp1 and hit_stop and candles_to_tp1 is not None and candles_to_stop is not None:
        if candles_to_tp1 <= candles_to_stop:
            outcome = "WIN — TP1 hit before stop"
            correct = True
        else:
            outcome = "LOSS — Stop hit before TP1"
            correct = False
    else:
        direction_correct = (predicted_direction == "LONG" and final_return > 0) or (
            predicted_direction == "SHORT" and final_return < 0
        )
        outcome = f"OPEN — Direction {'correct' if direction_correct else 'wrong'} (no TP1/stop in window)"
        correct = direction_correct

    result: dict[str, Any] = {
        "backtest": True,
        "ticker": sym,
        "timeframe": timeframe,
        "analysis_date": analysis_date,
        "price_at_date": current_price,
        "ai_prediction": ai_analysis,
        "actual_outcome": {
            "final_price": final_price,
            "final_return_pct": final_return,
            "max_gain_pct": max_gain,
            "max_loss_pct": max_loss,
            "hit_tp1": hit_tp1,
            "hit_tp2": hit_tp2,
            "hit_stop": hit_stop,
            "candles_to_tp1": candles_to_tp1,
            "candles_to_tp2": candles_to_tp2,
            "candles_to_stop": candles_to_stop,
            "outcome": outcome,
            "prediction_correct": correct,
            "forward_candles_checked": int(n),
        },
    }

    if save_history:
        append_backtest_history({"kind": "single", "result": result})

    return result


def run_backtest_series(
    ticker: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    step_days: int = 7,
    forward_candles: int = 20,
    *,
    save_history: bool = True,
) -> dict[str, Any]:
    """
    Run ``analyze_at_date`` on a grid of dates between ``start_date`` and ``end_date``.

    Each successful run is logged when ``save_history`` is true (via ``analyze_at_date``).
    """
    results: list[dict[str, Any]] = []
    current = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end = datetime.strptime(end_date.strip(), "%Y-%m-%d")

    while current <= end:
        d = current.strftime("%Y-%m-%d")
        try:
            result = analyze_at_date(
                ticker,
                timeframe,
                d,
                forward_candles=forward_candles,
                save_history=save_history,
            )
            results.append(result)
            log(f"[Backtest] {ticker} {d}: {result['actual_outcome']['outcome']}")
        except Exception as e:  # noqa: BLE001
            log(f"[Backtest] Error on {d}: {e}", level="warning")

        current += timedelta(days=int(step_days))

    if not results:
        return {"error": "No backtest results generated"}

    correct = sum(1 for r in results if r["actual_outcome"]["prediction_correct"])
    total = len(results)
    win_rate = round(correct / total * 100, 1) if total else 0.0

    wins = [r["actual_outcome"]["final_return_pct"] for r in results if r["actual_outcome"]["prediction_correct"]]
    losses = [r["actual_outcome"]["final_return_pct"] for r in results if not r["actual_outcome"]["prediction_correct"]]

    summary: dict[str, Any] = {
        "ticker": (ticker or "").strip().upper(),
        "timeframe": timeframe,
        "period": f"{start_date} to {end_date}",
        "total_signals": total,
        "correct_predictions": correct,
        "win_rate_pct": win_rate,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best_trade_pct": round(max(wins), 2) if wins else 0.0,
        "worst_trade_pct": round(min(losses), 2) if losses else 0.0,
        "individual_results": results,
    }

    return summary
