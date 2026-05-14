"""
Continuous autonomous backtesting loop: random historical setups, forward outcomes,
rolling stats, and periodic self-improvement. All state is simple JSON on ``DATA_DIR``
(default ``/data`` on Railway with a volume; falls back to ``./results`` locally).
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401

from utils import DATA_DIR, env, load_json, log, save_json, utcnow_iso

CLAUDE_MODEL = "claude-opus-4-5"

RESULTS_FILE = DATA_DIR / "backtest_results.json"
STATS_FILE = DATA_DIR / "backtest_stats.json"
LEARNED_FILE = DATA_DIR / "learned_weights.json"
STATE_FILE = DATA_DIR / "backtest_state.json"
ENABLED_FILE = DATA_DIR / "backtest_enabled.json"
IMPROVING_FILE = DATA_DIR / "improving.json"
IMPROVE_DEBUG_FILE = DATA_DIR / "improve_debug.json"

STARTING_CAPITAL = 10000.0
POSITION_SIZE_PCT = 0.05  # 5% of capital
LEVERAGE = 50  # 50x on notional exposure
IMPROVE_EVERY = 100

# --- Hard filters: CHF crosses only (see ``apply_hard_filters``) ---
CHF_PAIRS = [
    "GBPCHF",
    "AUDCHF",
    "EURCHF",
    "USDCHF",
    "NZDCHF",
    "CADCHF",
]

BANNED_SIGNALS: list[str] = []

# --- Multi-timeframe backtest universe ---
TF_WEIGHTS = {
    "1h": 0.20,
    "4h": 0.40,
    "1d": 0.30,
    "1w": 0.10,
}

TIMEFRAMES: list[str] = ["1h", "4h", "1d", "1w"]

TF_FORWARD_CANDLES: dict[str, int] = {
    "1h": 48,
    "4h": 30,
    "1d": 20,
    "1w": 8,
}

TF_MAX_STOP_PCT: dict[str, float] = {
    "1h": 0.008,
    "4h": 0.012,
    "1d": 0.018,
    "1w": 0.030,
}

TF_MAX_TP_PCT: dict[str, float] = {
    "1h": 0.012,
    "4h": 0.020,
    "1d": 0.030,
    "1w": 0.060,
}

TF_DESCRIPTIONS: dict[str, str] = {
    "1h": "1H intraday — hold 2-48 hours, tight stops",
    "4h": "4H day trade — hold 4-48 hours, best confluence",
    "1d": "Daily swing — hold 3-20 days; prefer ADX 25+ for conviction",
    "1w": "Weekly position — highest conviction only",
}

# Legacy default horizon (prefer TF_FORWARD_CANDLES per timeframe).
FORWARD_CANDLES = TF_FORWARD_CANDLES.get("4h", 30)

BACKTEST_TICKERS = [
    # Major pairs
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    # Euro crosses
    "EURGBP",
    "EURJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "EURNZD",
    # GBP crosses
    "GBPJPY",
    "GBPAUD",
    "GBPCAD",
    "GBPCHF",
    "GBPNZD",
    # JPY crosses
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "NZDJPY",
    # AUD crosses
    "AUDCAD",
    "AUDCHF",
    "AUDNZD",
    # Other crosses
    "CADCHF",
    "NZDCAD",
    "NZDCHF",
    # Exotic pairs
    "USDMXN",
    "USDZAR",
    "USDNOK",
    "USDSEK",
    "USDDKK",
    "USDSGD",
    "USDHKD",
    "USDTRY",
    "USDPLN",
    "USDHUF",
    "USDCZK",
    # Gold and Silver (trade like forex)
    "XAUUSD",
    "XAGUSD",
    # Oil (commodity forex)
    "USOIL",
    "UKOIL",
]

_backtest_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_results_lock = threading.Lock()


def log_learned_startup_preview() -> None:
    """Log first 500 chars of the current learned report for Railway / ops visibility."""
    raw = load_json(LEARNED_FILE, default={}) or {}
    try:
        snippet = json.dumps(raw, indent=2, default=str)[:500]
    except (TypeError, ValueError):
        snippet = str(raw)[:500]
    log(f"[Learned] Current report: {snippet}", level="info")


def reset_learned_rules() -> None:
    """Overwrite ``learned_weights.json`` with empty defaults (clears overly strict improvement rules)."""
    default_learned: dict[str, Any] = {
        "updated_at": datetime.now().isoformat(),
        "total_trades_analyzed": 0,
        "analysis_summary": "",
        "new_rules": [],
        "reliable_signals": [],
        "unreliable_signals": [],
        "main_loss_reasons": [],
        "recommendation": "",
        "expected_improvement": "",
        "signal_win_rates": {},
        "signal_adjustments": {},
    }
    save_json(LEARNED_FILE, default_learned)
    log("[Reset] Learned rules reset to defaults", level="info")


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


def is_enabled() -> bool:
    data = load_json(ENABLED_FILE, default=None)
    if not isinstance(data, dict):
        return False
    return bool(data.get("enabled", False))


def is_improving() -> bool:
    data = load_json(IMPROVING_FILE, default=None)
    if not isinstance(data, dict):
        return False
    return bool(data.get("running", False))


def get_state() -> dict[str, Any]:
    st = load_json(STATE_FILE, default=None)
    if isinstance(st, dict):
        return st
    return {
        "status": "idle",
        "current_ticker": None,
        "current_date": None,
        "current_timeframe": None,
        "sessions_completed": 0,
        "total_tests_run": 0,
        "last_session_at": None,
        "next_session_at": None,
        "session_results": [],
    }


def update_state(updates: dict[str, Any]) -> None:
    state = get_state()
    state.update(updates)
    state["updated_at"] = datetime.now().isoformat()
    save_json(STATE_FILE, state)


def get_random_date(days_back_max: int = 365, days_back_min: int = 10, *, skip_weekends: bool = True) -> str:
    days = random.randint(days_back_min, days_back_max)
    date = datetime.now() - timedelta(days=days)
    if skip_weekends:
        while date.weekday() >= 5:
            date -= timedelta(days=1)
    return date.strftime("%Y-%m-%d")


def _result_dedup_key(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return f"{row.get('ticker')}_{row.get('date')}_{row.get('timeframe')}"


def _read_backtest_results_file() -> list[dict[str, Any]]:
    """Read ``RESULTS_FILE`` into a list of dicts. Callers must hold ``_results_lock`` when used with writes."""
    if not RESULTS_FILE.exists():
        return []
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001
        log(f"[IO] read backtest_results.json: {e}", level="warning")
    return []


def _load_results_list() -> list[dict[str, Any]]:
    with _results_lock:
        return _read_backtest_results_file()


def append_result(result: dict[str, Any]) -> int:
    """Append one backtest row if (ticker, date, timeframe) is new. Sole writer to ``RESULTS_FILE``. Returns new length or prior length if duplicate; 0 on hard failure."""
    try:
        with _results_lock:
            existing = _read_backtest_results_file()

            key = f"{result.get('ticker')}_{result.get('date')}_{result.get('timeframe')}"
            existing_keys: set[str] = set()
            for r in existing:
                k = f"{r.get('ticker')}_{r.get('date')}_{r.get('timeframe')}"
                existing_keys.add(k)

            if key in existing_keys:
                return len(existing)

            existing.append(result)

            tmp = str(RESULTS_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, default=str)
            os.replace(tmp, str(RESULTS_FILE))

            n = len(existing)
            log(
                f"[IO] Appended result #{n}: {result.get('ticker')} "
                f"{result.get('outcome', 'SKIP')}",
                level="info",
            )
            return n

    except Exception as e:  # noqa: BLE001
        log(f"[IO] append_result error: {e}", level="error")
        log(traceback.format_exc(), level="error")
        return 0


def get_ohlcv(
    yf_ticker: str,
    timeframe: str,
    analysis_date: str,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Fetch OHLCV for backtest window; resample 1h→4h when needed."""
    tf_key = timeframe.lower().strip()
    tf_cfg: dict[str, dict[str, Any]] = {
        "1h": {"interval": "1h", "days_back": 180, "days_fwd": 14},
        "4h": {"interval": "1h", "resample": "4h", "days_back": 365, "days_fwd": 30},
        "1d": {"interval": "1d", "days_back": 730, "days_fwd": 60},
        "1w": {"interval": "1wk", "days_back": 1825, "days_fwd": 120},
    }
    cfg = tf_cfg.get(tf_key, tf_cfg["4h"])
    target = datetime.strptime(analysis_date.strip(), "%Y-%m-%d")
    start = target - timedelta(days=int(cfg["days_back"]))
    end = target + timedelta(days=int(cfg["days_fwd"]))

    df = yf.Ticker(yf_ticker).history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval=str(cfg["interval"]),
    )
    if df.empty:
        return None, None

    df = df.sort_index()
    if cfg.get("resample"):
        agg: dict[str, str] = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        if "Volume" in df.columns:
            agg["Volume"] = "sum"
        df = df.resample(str(cfg["resample"])).agg(agg).dropna()

    df = _strip_tz(df)
    day_end = pd.Timestamp(analysis_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    past = df[df.index <= day_end].copy()
    future = df[df.index > day_end].copy()
    return past, future


def apply_hard_filters(
    ticker: str,
    indicators: dict[str, Any],  # noqa: ARG001
    timeframe: str,  # noqa: ARG001
) -> dict[str, Any]:
    """CHF crosses only — ADX and SMC are decided in the Claude prompt."""
    chf_only = ["GBPCHF", "AUDCHF", "EURCHF", "USDCHF", "NZDCHF", "CADCHF"]
    sym = (ticker or "").strip().upper()
    if sym in chf_only:
        return {"pass": False, "reason": "CHF excluded"}
    return {"pass": True, "reason": None}


def validate_rr(plan: dict[str, Any]) -> dict[str, Any]:
    """Ensure reward at TP1 vs stop risk meets minimum R/R (1.3); downgrade to NO TRADE if not."""
    if not isinstance(plan, dict):
        return {}
    direction = str(plan.get("direction", "")).strip().upper()
    if direction not in ("LONG", "SHORT"):
        return plan

    try:
        entry = float(plan.get("entry", 0) or 0)
        stop = float(plan.get("stop_loss", 0) or 0)
        tp1 = float(plan.get("tp1", 0) or 0)
    except (TypeError, ValueError):
        return plan

    if entry == 0 or stop == 0 or tp1 == 0:
        return plan

    if direction == "LONG":
        risk = abs(entry - stop)
        reward = abs(tp1 - entry)
    else:
        risk = abs(stop - entry)
        reward = abs(entry - tp1)

    if risk == 0:
        return plan

    rr = reward / risk

    if rr < 1.3:
        log(f"[Backtest] RR {rr:.2f} below 1.3 minimum", level="info")
        plan["direction"] = "NO TRADE"
        plan["skip_reason"] = f"R/R {rr:.2f} below 1.3 minimum"

    plan["rr_ratio"] = f"1:{rr:.2f}"
    return plan


def run_one_backtest(ticker: str, timeframe: str, analysis_date: str) -> dict[str, Any] | None:
    try:
        sym = (ticker or "").strip().upper()
        tf_key = timeframe.lower().strip()
        is_forex = len(sym) == 6 and sym.isalpha()
        yf_ticker = sym + "=X" if is_forex else sym

        past, future = get_ohlcv(yf_ticker, tf_key, analysis_date.strip())
        if past is None or future is None or past.empty or future.empty:
            return None
        min_past = 30 if tf_key == "1w" else 50
        if len(past) < min_past:
            return None
        fwd_n = int(TF_FORWARD_CANDLES.get(tf_key, FORWARD_CANDLES))
        if len(future) < 5:
            log(f"[Backtest] Not enough future data for {sym} {analysis_date} {tf_key}", level="info")
            return None

        past.ta.rsi(length=14, append=True)
        past.ta.macd(append=True)
        past.ta.ema(length=20, append=True)
        past.ta.ema(length=50, append=True)
        past.ta.ema(length=200, append=True)
        past.ta.bbands(length=20, append=True)
        past.ta.atr(length=14, append=True)
        past.ta.adx(length=14, append=True)

        latest = past.iloc[-1]
        price = round(float(latest["Close"]), 5)
        if price <= 0:
            return None

        bbl, bbm, bbu = _pick_bb_cols(past)

        def safe(col: str, default: float = 0.0) -> float:
            try:
                if col not in past.columns:
                    return default
                v = past[col].iloc[-1]
                if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                    return default
                return round(float(v), 5)
            except Exception:  # noqa: BLE001
                return default

        ind: dict[str, Any] = {
            "rsi": safe("RSI_14"),
            "macd_hist": safe("MACDh_12_26_9"),
            "ema20": safe("EMA_20", price),
            "ema50": safe("EMA_50", price),
            "ema200": safe("EMA_200", price),
            "atr": safe("ATRr_14"),
            "adx": safe("ADX_14"),
            "bb_upper": safe(bbu, price) if bbu else price,
            "bb_lower": safe(bbl, price) if bbl else price,
        }

        try:
            if "MACD_12_26_9" in past.columns and "MACDs_12_26_9" in past.columns:
                ind["macd_line"] = round(float(past["MACD_12_26_9"].iloc[-1]), 5)
                ind["macd_signal"] = round(float(past["MACDs_12_26_9"].iloc[-1]), 5)
            else:
                ind["macd_line"] = 0.0
                ind["macd_signal"] = 0.0
        except Exception:  # noqa: BLE001
            ind["macd_line"] = 0.0
            ind["macd_signal"] = 0.0

        try:
            if bbm and bbm in past.columns:
                ind["bb_mid"] = round(float(past[bbm].iloc[-1]), 5)
            else:
                ind["bb_mid"] = round((ind["bb_upper"] + ind["bb_lower"]) / 2, 5)
        except Exception:  # noqa: BLE001
            ind["bb_mid"] = round((ind["bb_upper"] + ind["bb_lower"]) / 2, 5)

        try:
            ind["high_52w"] = round(float(past["High"].max()), 5)
            ind["low_52w"] = round(float(past["Low"].min()), 5)
        except Exception:  # noqa: BLE001
            ind["high_52w"] = ind["low_52w"] = round(float(price), 5)

        swing_highs: list[float] = []
        swing_lows: list[float] = []
        recent = past.tail(50)
        if len(recent) > 11:
            for i in range(5, len(recent) - 5):
                try:
                    hi = float(recent["High"].iloc[i])
                    if hi == float(recent["High"].iloc[i - 5 : i + 6].max()):
                        swing_highs.append(round(hi, 5))
                    lo = float(recent["Low"].iloc[i])
                    if lo == float(recent["Low"].iloc[i - 5 : i + 6].min()):
                        swing_lows.append(round(lo, 5))
                except Exception:  # noqa: BLE001
                    continue
        ind["swing_highs"] = swing_highs[-5:]
        ind["swing_lows"] = swing_lows[-5:]

        filters = apply_hard_filters(sym, {}, tf_key)
        if not filters.get("pass", True):
            log(f"[Backtest] FILTERED: {sym} — {filters.get('reason')}", level="info")
            return {
                "date": analysis_date,
                "ticker": sym,
                "timeframe": timeframe,
                "verdict": "FILTERED",
                "direction": "NO TRADE",
                "skipped": True,
                "skip_reason": str(filters.get("reason") or ""),
                "reasoning": str(filters.get("reason") or ""),
                "entry_price": price,
            }

        tf_desc = TF_DESCRIPTIONS.get(tf_key, tf_key)
        sh = json.dumps(ind.get("swing_highs") or [])
        sl = json.dumps(ind.get("swing_lows") or [])

        prompt = f"""
You are APEX — an elite autonomous trading system
trained on institutional order flow, smart money
concepts, and price action methodology.

ASSET: {sym}
TIMEFRAME: {tf_desc}
DATE: {analysis_date}
CURRENT PRICE: {price}

MARKET DATA:
RSI(14): {ind["rsi"]:.2f}
MACD Line: {ind.get("macd_line", 0):.5f}
MACD Signal: {ind.get("macd_signal", 0):.5f}
MACD Histogram: {ind["macd_hist"]:.5f}
EMA20: {ind["ema20"]:.5f}
EMA50: {ind["ema50"]:.5f}
EMA200: {ind["ema200"]:.5f}
ATR(14): {ind["atr"]:.5f}
ADX(14): {ind["adx"]:.2f}
BB Upper: {ind["bb_upper"]:.5f}
BB Middle: {ind["bb_mid"]:.5f}
BB Lower: {ind["bb_lower"]:.5f}
52w High: {ind.get("high_52w", 0):.5f}
52w Low: {ind.get("low_52w", 0):.5f}
Recent Swing Highs: {sh}
Recent Swing Lows: {sl}

ANALYSIS FRAMEWORK — USE ALL OF THESE:

STEP 1 — HIGHER TIMEFRAME BIAS:
Before anything else determine the HTF bias.
Is price above or below the 200 EMA?
Is the 50 EMA above or below the 200 EMA?
This determines the ONLY direction you trade.
ONLY trade in the direction of HTF bias.
Never trade counter-trend.

STEP 2 — SMART MONEY CONCEPTS:
Look for these institutional footprints:

LIQUIDITY POOLS:
Equal highs above price = buy-side liquidity (BSL)
Smart money will hunt this before reversing down
Equal lows below price = sell-side liquidity (SSL)
Smart money will hunt this before reversing up
Previous day/week high or low = liquidity targets

ORDER BLOCKS:
Last bearish candle before strong bullish move = bullish OB
Last bullish candle before strong bearish move = bearish OB
Price returning to OB = high probability entry zone
Use swing_highs and swing_lows to identify these zones

FAIR VALUE GAPS (FVG/Imbalance):
Three candle pattern where middle candle leaves gap
Price often returns to fill imbalance before continuing
Use as entry confirmation

MARKET STRUCTURE:
Higher Highs + Higher Lows = uptrend (only take longs)
Lower Highs + Lower Lows = downtrend (only take shorts)
Break of Structure (BOS) = trend confirmed
Change of Character (CHOCH) = potential reversal

PREMIUM/DISCOUNT:
Identify the range between last major high and low
Above 50% of range = PREMIUM (favorable context for shorts — use in Tier scoring)
Below 50% of range = DISCOUNT (favorable context for longs — use in Tier scoring)
These are guides, not absolute bans: if your Step 3 score clears 3+ points you may still trade with clear risk/reward.

STEP 3 — ENTRY CONFIRMATION:
You need MINIMUM 3 of these to align.
You do NOT need all of them.
Find whatever combination is strongest:

TIER 1 SIGNALS (each counts as 2):
- Price in DISCOUNT for longs / PREMIUM for shorts
- Liquidity sweep just occurred (faked a level then reversed)
- Clear order block with price returning to it
- ADX above 30 with strong trend direction

TIER 2 SIGNALS (each counts as 1):
- HTF bias confirmed (price vs 200 EMA direction)
- Market structure break in trade direction
- Fair Value Gap present near entry
- RSI between 40-60 with momentum in direction
- MACD aligned with trade direction
- EMA stack perfectly aligned (20/50/200)
- Price at key level (previous high/low/EMA200)

MINIMUM TO TRADE: 3 points total
(e.g. 1 Tier1 + 1 Tier2 = 3 points = trade)
(e.g. 3 Tier2 signals = 3 points = trade)

CRITICAL: If you have ADX above 25 AND
clear trend direction AND price not at extreme:
That alone is enough for MEDIUM confidence trade.

Do NOT require perfect SMC setup for every trade.
SMC concepts IMPROVE the setup when present.
Standard trend following works when SMC absent.

TARGET: Take a trade on 35-40% of setups.
If you find yourself saying NO TRADE more than
60% of the time you are being too selective.

R/R RULES — RELAXED:
Minimum R/R: 1.5
Preferred: 2.0 to 3.0
If setup is excellent (Tier1 + Tier1) accept 1.3+
Never accept below 1.3 regardless

STEP 4 — QUICK CHECKLIST before entry:
1. Which direction does 200 EMA favor?
2. Is ADX above 20? (below 15 = NO TRADE)
3. Is RSI neutral or confirming direction?
4. What is my specific stop level?
5. What is my R/R? Above 1.3?
If all 5 answered positively = TAKE THE TRADE

REMEMBER: You are backtesting to LEARN.
Taking imperfect trades and seeing results
teaches us more than taking zero trades.
A MEDIUM confidence trade with 1.5 R/R
is better than NO TRADE.
Be decisive. Make a call.

STEP 5 — WHAT BOOSTS EDGE (optional):
These often improve win rate when several align; they are not all mandatory if Step 3 scoring and R/R already justify the trade:
- HTF bias is clear (strong trend on 200 EMA)
- Price has swept liquidity (faked below/above a level)
- Price is now in discount/premium zone
- Order block or FVG nearby as entry
- ADX above 25 with momentum
- RSI has room to run (roughly 40-60)

STEP 6 — WHAT TO AVOID:
Never trade:
- Into obvious support/resistance without a sweep first
- When price is at BB extreme (wait for bounce confirmation)
- When RSI is above 70 (long) or below 30 (short)
- When MACD and price disagree (divergence)
- When ADX is below 15 (no trend)
- Hard counter to 200 EMA unless your Step 3 tier score clearly supports a tactical mean-reversion plan
- When the move has already happened (chasing)
- At random entry points — always need a specific reason

STEP 7 — PAIR SPECIFIC RULES:
Based on backtested performance apply these:
Avoid GBPAUD, EURCAD — historically unreliable
Avoid CHF pairs completely
USDJPY, EURUSD, GBPUSD — most reliable pairs
AUD pairs — good for trend following
JPY crosses — strong trends but volatile stops

STEP 8 — RETURN YOUR ANALYSIS:
State clearly:
1. HTF bias: BULLISH/BEARISH and why
2. Market structure: last BOS/CHOCH observed
3. Zone: is price in PREMIUM or DISCOUNT
4. SMC concept present: OB/FVG/Liquidity/None
5. Entry logic: specific reason to enter NOW (include tier points used)
6. Stop placement: specific level with reason
7. TP levels: with specific target reason
8. R/R ratio: default target ≥1.5; may use 1.3+ only when exceptional Tier1 overlap per rules above

Use NO TRADE when the Step 4 checklist fails or achievable R/R would be below 1.3.

Return ONLY valid JSON:
{{
  "verdict": "STRONG BUY|BUY|WAIT|SELL|STRONG SELL",
  "direction": "LONG|SHORT|NO TRADE",
  "confidence": "HIGH|MEDIUM|LOW",
  "htf_bias": "BULLISH|BEARISH|NEUTRAL",
  "market_structure": "UPTREND|DOWNTREND|RANGING",
  "price_zone": "PREMIUM|DISCOUNT|EQUILIBRIUM",
  "smc_concept": "ORDER_BLOCK|FVG|LIQUIDITY_SWEEP|NONE",
  "entry": {price},
  "stop_loss": 0.0,
  "tp1": 0.0,
  "tp2": 0.0,
  "tp3": 0.0,
  "rr_ratio": "string",
  "signals_used": ["string"],
  "confluences": ["string"],
  "conflicts": ["string"],
  "reasoning": "string",
  "no_trade_reason": null
}}
"""

        client = _client()
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _message_text(resp)
        ai = _parse_json_response(raw) or {}
        if isinstance(ai, dict):
            ai = validate_rr(ai)
        else:
            ai = {}

        direction = str(ai.get("direction", "NO TRADE")).strip().upper()
        if direction in ("NO TRADE", "WAIT", ""):
            rs = str(ai.get("skip_reason", "") or "").strip()
            ntr = ai.get("no_trade_reason")
            if not rs and ntr is not None and str(ntr).strip():
                rs = str(ntr).strip()
            base_reason = str(ai.get("reasoning", ""))
            reasoning = f"{base_reason} [{rs}]" if rs else base_reason
            return {
                "date": analysis_date,
                "ticker": sym,
                "timeframe": timeframe,
                "verdict": ai.get("verdict", "WAIT"),
                "direction": "NO TRADE",
                "skipped": True,
                "entry_price": price,
                "skip_reason": rs,
                "reasoning": reasoning,
                "rr_ratio": str(ai.get("rr_ratio", "")),
                "htf_bias": str(ai.get("htf_bias") or ""),
                "market_structure": str(ai.get("market_structure") or ""),
                "price_zone": str(ai.get("price_zone") or ""),
                "smc_concept": str(ai.get("smc_concept") or ""),
                "no_trade_reason": ntr if ntr is not None else None,
            }

        entry = float(ai.get("entry", price) or price)
        stop = float(ai.get("stop_loss", 0) or 0)
        tp1 = float(ai.get("tp1", 0) or 0)
        tp2 = float(ai.get("tp2", 0) or 0)
        tp3 = float(ai.get("tp3", 0) or 0)

        fut = future.head(fwd_n)
        highs = fut["High"].astype(float).values
        lows = fut["Low"].astype(float).values
        closes = fut["Close"].astype(float).values
        if len(closes) == 0:
            return None

        hit_tp1 = hit_tp2 = hit_stop = False
        c_tp1: int | None = None
        c_tp2: int | None = None
        c_stop: int | None = None

        for i, (h, l) in enumerate(zip(highs, lows)):
            if direction == "LONG":
                if not hit_tp1 and tp1 > 0 and h >= tp1:
                    hit_tp1 = True
                    c_tp1 = i + 1
                if not hit_tp2 and tp2 > 0 and h >= tp2:
                    hit_tp2 = True
                    c_tp2 = i + 1
                if not hit_stop and stop > 0 and l <= stop:
                    hit_stop = True
                    c_stop = i + 1
            elif direction == "SHORT":
                if not hit_tp1 and tp1 > 0 and l <= tp1:
                    hit_tp1 = True
                    c_tp1 = i + 1
                if not hit_tp2 and tp2 > 0 and l <= tp2:
                    hit_tp2 = True
                    c_tp2 = i + 1
                if not hit_stop and stop > 0 and h >= stop:
                    hit_stop = True
                    c_stop = i + 1
            else:
                return {
                    "date": analysis_date,
                    "ticker": sym,
                    "timeframe": timeframe,
                    "verdict": ai.get("verdict", "WAIT"),
                    "direction": direction,
                    "skipped": True,
                    "entry_price": price,
                    "reasoning": "Unknown direction from model",
                    "htf_bias": str(ai.get("htf_bias") or ""),
                    "market_structure": str(ai.get("market_structure") or ""),
                    "price_zone": str(ai.get("price_zone") or ""),
                    "smc_concept": str(ai.get("smc_concept") or ""),
                }

        if entry == 0:
            entry = price
        entry = float(entry)
        if not math.isfinite(entry) or abs(entry) < 1e-12:
            pv = float(price)
            entry = pv if math.isfinite(pv) and abs(pv) > 1e-12 else 1e-8

        if hit_stop and (not hit_tp1 or (c_stop is not None and c_tp1 is not None and c_stop < c_tp1)):
            outcome = "LOSS"
            correct = False
            exit_p = round(float(stop), 5)
            exit_r = "Stop loss hit"
            if direction == "LONG":
                raw_pct = (stop - entry) / entry
            else:
                raw_pct = (entry - stop) / entry

        elif hit_tp1:
            outcome = "WIN"
            correct = True
            exit_p = round(float(tp1), 5)
            exit_r = "TP1 hit"
            if direction == "LONG":
                raw_pct = (tp1 - entry) / entry
            else:
                raw_pct = (entry - tp1) / entry

        else:
            final = closes[-1]
            if pd.isna(final) or final == 0:
                return None
            final = round(float(final), 5)
            exit_p = final
            exit_r = "Window ended"
            if direction == "LONG":
                raw_pct = (final - entry) / entry
            else:
                raw_pct = (entry - final) / entry
            correct = raw_pct > 0
            outcome = "WIN" if correct else "LOSS"

        base_position = STARTING_CAPITAL * POSITION_SIZE_PCT
        leveraged_exposure = base_position * LEVERAGE
        pnl_dollars = round(leveraged_exposure * raw_pct, 2)
        pnl_pct_display = round(raw_pct * 100, 2)

        log(
            f"[Backtest] PnL calc: entry={entry} exit={exit_p} direction={direction} "
            f"raw_pct={raw_pct:.4f} exposure={leveraged_exposure:.2f} pnl={pnl_dollars:.2f}",
            level="info",
        )

        candles_to_exit = c_tp1 or c_stop
        if candles_to_exit is None:
            candles_to_exit = len(closes)

        return {
            "date": analysis_date,
            "ticker": sym,
            "timeframe": timeframe,
            "verdict": ai.get("verdict"),
            "direction": direction,
            "confidence": ai.get("confidence"),
            "htf_bias": str(ai.get("htf_bias") or ""),
            "market_structure": str(ai.get("market_structure") or ""),
            "price_zone": str(ai.get("price_zone") or ""),
            "smc_concept": str(ai.get("smc_concept") or ""),
            "entry_price": round(entry, 5),
            "stop_loss": round(stop, 5),
            "tp1": round(tp1, 5),
            "tp2": round(tp2, 5),
            "tp3": round(tp3, 5),
            "exit_price": exit_p,
            "exit_reason": exit_r,
            "outcome": outcome,
            "correct": correct,
            "pnl_pct": pnl_pct_display,
            "pnl_dollars": pnl_dollars,
            "leverage": LEVERAGE,
            "position_size": base_position,
            "leveraged_exposure": leveraged_exposure,
            "hit_tp1": hit_tp1,
            "hit_tp2": hit_tp2,
            "hit_stop": hit_stop,
            "candles_to_exit": candles_to_exit,
            "signals_used": ai.get("signals_used") if isinstance(ai.get("signals_used"), list) else [],
            "confluences": ai.get("confluences") if isinstance(ai.get("confluences"), list) else [],
            "conflicts": ai.get("conflicts") if isinstance(ai.get("conflicts"), list) else [],
            "reasoning": str(ai.get("reasoning", "")),
            "rr_ratio": str(ai.get("rr_ratio", "")),
            "skipped": False,
        }

    except Exception as e:  # noqa: BLE001
        log(f"[ContinuousBacktest] Error {ticker} {analysis_date}: {e}", level="warning")
        return None


def _parse_rr_ratio_numeric(rr: Any) -> float | None:
    """Parse reward side from ``rr_ratio`` strings like ``1:2.35``."""
    if rr is None:
        return None
    s = str(rr).strip()
    if not s:
        return None
    if ":" in s:
        tail = s.split(":", 1)[1].strip()
        try:
            return float(tail)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _smc_stats_from_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """HTF bias alignment, SMC concept, price zone, and R/R distribution for completed trades."""
    htf_rows: list[tuple[bool, bool]] = []
    for t in trades:
        d = str(t.get("direction", "")).upper()
        hb = str(t.get("htf_bias", "")).upper()
        if d not in ("LONG", "SHORT") or hb not in ("BULLISH", "BEARISH"):
            continue
        aligned = (d == "LONG" and hb == "BULLISH") or (d == "SHORT" and hb == "BEARISH")
        win = t.get("outcome") == "WIN"
        htf_rows.append((aligned, win))

    aligned = [w for a, w in htf_rows if a]
    against = [w for a, w in htf_rows if not a]
    htf_bias_stats = {
        "aligned_with_bias_trades": len(aligned),
        "aligned_win_rate_pct": round(100 * sum(1 for w in aligned if w) / len(aligned), 1) if aligned else None,
        "against_bias_trades": len(against),
        "against_bias_win_rate_pct": round(100 * sum(1 for w in against if w) / len(against), 1) if against else None,
    }

    smc_break: dict[str, dict[str, Any]] = {}
    for t in trades:
        k = str(t.get("smc_concept") or "UNKNOWN").strip().upper() or "UNKNOWN"
        if k not in smc_break:
            smc_break[k] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        smc_break[k]["total"] += 1
        if t.get("outcome") == "WIN":
            smc_break[k]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            smc_break[k]["losses"] += 1
        smc_break[k]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    zone_buckets: dict[str, dict[str, Any]] = {}
    for t in trades:
        d = str(t.get("direction", "")).upper()
        z = str(t.get("price_zone", "")).upper()
        if d not in ("LONG", "SHORT") or z not in ("PREMIUM", "DISCOUNT", "EQUILIBRIUM"):
            continue
        key = f"{d}_{z}"
        if key not in zone_buckets:
            zone_buckets[key] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        zone_buckets[key]["total"] += 1
        if t.get("outcome") == "WIN":
            zone_buckets[key]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            zone_buckets[key]["losses"] += 1
        zone_buckets[key]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    rr_bins = {"lt_1_3": 0, "1_3_to_2": 0, "gt_2": 0, "unknown": 0}
    rr_vals: list[float] = []
    for t in trades:
        v = _parse_rr_ratio_numeric(t.get("rr_ratio"))
        if v is None:
            rr_bins["unknown"] += 1
            continue
        rr_vals.append(v)
        if v < 1.3:
            rr_bins["lt_1_3"] += 1
        elif v <= 2.0:
            rr_bins["1_3_to_2"] += 1
        else:
            rr_bins["gt_2"] += 1

    rr_ratio_distribution = {
        "buckets": rr_bins,
        "mean_rr_reward_side": round(sum(rr_vals) / len(rr_vals), 3) if rr_vals else None,
    }

    return {
        "htf_bias_stats": htf_bias_stats,
        "smc_concept_performance": smc_break,
        "price_zone_accuracy": zone_buckets,
        "rr_ratio_distribution": rr_ratio_distribution,
    }


def calculate_kelly(trades: list[dict[str, Any]]) -> float:
    completed = [t for t in trades if not t.get("skipped") and t.get("outcome") in ("WIN", "LOSS")]

    if len(completed) < 20:
        return POSITION_SIZE_PCT

    wins = [t for t in completed if t.get("outcome") == "WIN"]
    losses = [t for t in completed if t.get("outcome") == "LOSS"]

    if not wins or not losses:
        return POSITION_SIZE_PCT

    win_rate = len(wins) / len(completed)
    loss_rate = 1 - win_rate

    avg_win_pct = sum(abs(float(t.get("pnl_pct", 0) or 0)) for t in wins) / len(wins)
    avg_loss_pct = sum(abs(float(t.get("pnl_pct", 0) or 0)) for t in losses) / len(losses)

    if avg_loss_pct == 0:
        return POSITION_SIZE_PCT

    win_loss_ratio = avg_win_pct / avg_loss_pct
    kelly = win_rate - (loss_rate / win_loss_ratio)
    kelly = max(0.01, min(0.10, kelly))

    log(
        f"[Kelly] Win rate: {win_rate:.1%} W/L ratio: {win_loss_ratio:.2f} Kelly: {kelly:.1%}",
        level="info",
    )

    return round(kelly, 3)


def calculate_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [r for r in results if r and not r.get("skipped")]
    if not trades:
        k0 = calculate_kelly([])
        smc_empty = _smc_stats_from_trades([])
        return {
            "generated_at": datetime.now().isoformat(),
            "total_trades": 0,
            "win_rate_pct": 0.0,
            "total_pnl_dollars": 0.0,
            "final_capital": STARTING_CAPITAL,
            "starting_capital": STARTING_CAPITAL,
            "leverage": LEVERAGE,
            "position_size_pct": POSITION_SIZE_PCT * 100,
            "position_size_dollars": STARTING_CAPITAL * POSITION_SIZE_PCT,
            "leveraged_exposure": STARTING_CAPITAL * POSITION_SIZE_PCT * LEVERAGE,
            "max_loss_per_trade": STARTING_CAPITAL * POSITION_SIZE_PCT * LEVERAGE * 0.015,
            "kelly_pct": round(k0 * 100, 2),
            "kelly_dollars": round(STARTING_CAPITAL * k0, 2),
            "timeframe_performance": {},
            "excluded_tickers": list(CHF_PAIRS),
            "banned_signals": list(BANNED_SIGNALS),
            **smc_empty,
        }

    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]

    total_pnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in trades)
    win_pnls = [float(t.get("pnl_dollars", 0) or 0) for t in wins]
    loss_pnls = [float(t.get("pnl_dollars", 0) or 0) for t in losses]

    capital = STARTING_CAPITAL
    curve: list[dict[str, Any]] = []
    peak = capital
    max_dd = 0.0

    for t in sorted(trades, key=lambda x: str(x.get("date", ""))):
        pnl = t.get("pnl_dollars", 0)
        try:
            pnl_f = float(pnl) if pnl is not None else 0.0
        except (TypeError, ValueError):
            pnl_f = 0.0
        if pnl_f != pnl_f:  # NaN
            pnl_f = 0.0
        capital += pnl_f
        curve.append(
            {
                "date": t.get("date"),
                "capital": round(capital, 2),
                "pnl": round(pnl_f, 2),
                "outcome": t.get("outcome", "?"),
            }
        )
        if capital > peak:
            peak = capital
        if peak > 0:
            dd = (peak - capital) / peak * 100
            if dd > max_dd:
                max_dd = dd

    sig_perf: dict[str, dict[str, Any]] = {}
    for t in trades:
        for s in t.get("signals_used") or []:
            if not isinstance(s, str):
                continue
            if s not in sig_perf:
                sig_perf[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if t.get("outcome") == "WIN":
                sig_perf[s]["wins"] += 1
            else:
                sig_perf[s]["losses"] += 1
            sig_perf[s]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    best = max(trades, key=lambda t: float(t.get("pnl_dollars", 0) or 0))
    worst = min(trades, key=lambda t: float(t.get("pnl_dollars", 0) or 0))

    loss_sum = sum(loss_pnls)
    win_sum = sum(win_pnls)
    if loss_sum != 0:
        pf = abs(win_sum / loss_sum)
    else:
        pf = 0.0

    kelly_f = calculate_kelly(trades)

    tf_perf: dict[str, Any] = {}
    for tf in TIMEFRAMES:
        tf_trades = [
            t
            for t in trades
            if str(t.get("timeframe", "") or "").lower() == tf
            and not t.get("skipped")
            and t.get("outcome") in ("WIN", "LOSS")
        ]
        if tf_trades:
            tf_wins = [t for t in tf_trades if t.get("outcome") == "WIN"]
            tf_perf[tf] = {
                "total": len(tf_trades),
                "wins": len(tf_wins),
                "losses": len(tf_trades) - len(tf_wins),
                "win_rate": round(len(tf_wins) / len(tf_trades) * 100, 1),
                "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in tf_trades), 2),
            }

    smc_agg = _smc_stats_from_trades(trades)

    return {
        "generated_at": datetime.now().isoformat(),
        "starting_capital": STARTING_CAPITAL,
        "final_capital": round(capital, 2),
        "total_pnl_dollars": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win_dollars": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0,
        "avg_loss_dollars": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "leverage": LEVERAGE,
        "position_size_pct": POSITION_SIZE_PCT * 100,
        "position_size_dollars": STARTING_CAPITAL * POSITION_SIZE_PCT,
        "leveraged_exposure": STARTING_CAPITAL * POSITION_SIZE_PCT * LEVERAGE,
        "max_loss_per_trade": STARTING_CAPITAL * POSITION_SIZE_PCT * LEVERAGE * 0.015,
        "kelly_pct": round(kelly_f * 100, 2),
        "kelly_dollars": round(STARTING_CAPITAL * kelly_f, 2),
        "best_trade": {
            "ticker": best.get("ticker"),
            "date": best.get("date"),
            "pnl": best.get("pnl_dollars", 0),
            "direction": best.get("direction"),
            "entry": best.get("entry_price"),
            "exit": best.get("exit_price"),
            "reason": best.get("exit_reason"),
        },
        "worst_trade": {
            "ticker": worst.get("ticker"),
            "date": worst.get("date"),
            "pnl": worst.get("pnl_dollars", 0),
            "direction": worst.get("direction"),
            "entry": worst.get("entry_price"),
            "exit": worst.get("exit_price"),
            "reason": worst.get("exit_reason"),
        },
        "capital_curve": curve,
        "signal_performance": sig_perf,
        "recent_trades": sorted(trades, key=lambda x: str(x.get("date", "")), reverse=True)[:50],
        "timeframe_performance": tf_perf,
        "excluded_tickers": list(CHF_PAIRS),
        "banned_signals": list(BANNED_SIGNALS),
        **smc_agg,
    }


def _normalize_signal_adjustments(raw: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        u = str(v).upper()
        if "REMOVE" in u:
            out[k] = "REMOVE_WEIGHT"
        elif "DECREASE" in u:
            out[k] = "DECREASE_WEIGHT"
        elif "INCREASE" in u:
            out[k] = "INCREASE_WEIGHT"
        else:
            out[k] = "KEEP_WEIGHT"
    return out


def run_improvement_cycle(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Self-improvement over backtest rows (full snapshot list). Filters to completed WIN/LOSS rows."""
    snap = trades if isinstance(trades, list) else []
    log(f"[Improve] Starting with {len(snap)} snapshot rows", level="info")

    completed = [
        t
        for t in snap
        if isinstance(t, dict) and not t.get("skipped") and t.get("outcome") in ("WIN", "LOSS")
    ]
    log(f"[Improve] Completed trades: {len(completed)}", level="info")

    if len(completed) < 10:
        log("[Improve] Not enough completed trades", level="info")
        return None

    save_json(
        IMPROVING_FILE,
        {
            "running": True,
            "started_at": datetime.now().isoformat(),
            "trades_analyzed": len(completed),
        },
    )

    sig_stats: dict[str, dict[str, float]] = {}
    for t in completed:
        for s in t.get("signals_used") or []:
            if not isinstance(s, str):
                continue
            if s not in sig_stats:
                sig_stats[s] = {"wins": 0.0, "losses": 0.0, "pnl": 0.0}
            if t.get("outcome") == "WIN":
                sig_stats[s]["wins"] += 1
            else:
                sig_stats[s]["losses"] += 1
            sig_stats[s]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    sig_win_rates: dict[str, Any] = {}
    for s, st in sig_stats.items():
        total = int(st["wins"] + st["losses"])
        if total >= 2:
            wr = round(st["wins"] / total * 100, 1)
            sig_win_rates[s] = {
                "win_rate": wr,
                "total": total,
                "pnl": round(float(st["pnl"]), 2),
                "adjustment": (
                    "INCREASE_WEIGHT" if wr > 60 else "DECREASE_WEIGHT" if wr < 40 else "KEEP_WEIGHT"
                ),
            }

    top8 = dict(
        sorted(
            sig_win_rates.items(),
            key=lambda x: int(x[1].get("total", 0)) if isinstance(x[1], dict) else 0,
            reverse=True,
        )[:8]
    )

    wins = [t for t in completed if t.get("outcome") == "WIN"]
    losses = [t for t in completed if t.get("outcome") == "LOSS"]
    win_rate = round(len(wins) / len(completed) * 100, 1) if completed else 0.0
    total_pnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in completed)

    ticker_perf: dict[str, dict[str, Any]] = {}
    for t in completed:
        tk = str(t.get("ticker", "") or "")
        if not tk:
            continue
        if tk not in ticker_perf:
            ticker_perf[tk] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t.get("outcome") == "WIN":
            ticker_perf[tk]["wins"] += 1
        else:
            ticker_perf[tk]["losses"] += 1
        ticker_perf[tk]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    loss_details: list[dict[str, Any]] = []
    for t in losses[-5:]:
        loss_details.append(
            {
                "ticker": t.get("ticker"),
                "date": t.get("date"),
                "signals": (t.get("signals_used") or [])[:5],
                "pnl": t.get("pnl_dollars", 0),
                "reasoning": str(t.get("reasoning", ""))[:150],
            }
        )

    win_details: list[dict[str, Any]] = []
    for t in wins[-5:]:
        win_details.append(
            {
                "ticker": t.get("ticker"),
                "date": t.get("date"),
                "signals": (t.get("signals_used") or [])[:5],
                "pnl": t.get("pnl_dollars", 0),
            }
        )

    prompt = f"""You are the APEX trading AI.
Analyze your own backtesting results and write
a detailed improvement report.

PERFORMANCE:
Total trades: {len(completed)}
Wins: {len(wins)} | Losses: {len(losses)}
Win rate: {win_rate}%
Total P&L: ${total_pnl:.2f}

TOP SIGNALS BY VOLUME:
{json.dumps(top8, indent=2, default=str)}

TICKER PERFORMANCE:
{json.dumps(ticker_perf, indent=2, default=str)}

RECENT LOSSES:
{json.dumps(loss_details, indent=2, default=str)}

RECENT WINS:
{json.dumps(win_details, indent=2, default=str)}

Write a comprehensive trading system review.
Be specific with numbers. Reference actual signal names.

Return ONLY this exact JSON structure, nothing else:
{{
  "analysis_summary": "Write 2-3 detailed paragraphs analyzing what patterns separate wins from losses. Reference specific signal names and percentages from the data above. Be analytical and specific.",
  "new_rules": [
    "RULE_1_NAME: Specific actionable rule with numbers",
    "RULE_2_NAME: Specific actionable rule with numbers",
    "RULE_3_NAME: Specific actionable rule with numbers",
    "RULE_4_NAME: Specific actionable rule with numbers",
    "RULE_5_NAME: Specific actionable rule with numbers"
  ],
  "reliable_signals": [
    "Signal Name - X% win rate, $Y PnL - reason why reliable"
  ],
  "unreliable_signals": [
    "Signal Name - X% win rate, $Y PnL (REMOVE/REDUCE) - reason"
  ],
  "main_loss_reasons": [
    "Specific reason 1 with numbers",
    "Specific reason 2 with numbers",
    "Specific reason 3 with numbers"
  ],
  "recommendation": "The single most important change to make right now based on the data.",
  "expected_improvement": "From {win_rate}% to approximately X% because of specific reasons",
  "signal_adjustments": {{"SIGNAL_NAME": "INCREASE|DECREASE|REMOVE"}}
}}"""

    log("[Improve] Calling Claude claude-opus-4-5...", level="info")
    log(f"[Improve] Prompt length: {len(prompt)} chars", level="info")

    raw = ""
    try:
        client = _client()
        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.content and getattr(resp.content[0], "text", None) is not None:
            raw = str(resp.content[0].text)
        else:
            raw = _message_text(resp)

        log(f"[Improve] Got response: {len(raw)} chars", level="info")
        log(f"[Improve] Preview: {raw[:300]!r}", level="info")

        clean = re.sub(r"```json|```", "", raw, flags=re.IGNORECASE).strip()
        start = clean.find("{")
        end = clean.rfind("}") + 1

        if start == -1 or end == 0:
            log("[Improve] No JSON in response!", level="warning")
            log(f"[Improve] Full response (truncated for log): {raw[:4000]!r}", level="warning")
            save_json(IMPROVE_DEBUG_FILE, {"raw": raw, "error": "no json found"})
            return None

        result = json.loads(clean[start:end])
        if not isinstance(result, dict):
            log(f"[Improve] Parsed JSON is not an object: {type(result).__name__}", level="warning")
            save_json(IMPROVE_DEBUG_FILE, {"raw": raw, "error": "not a dict", "parsed": result})
            return None

        log("[Improve] Parsed JSON successfully", level="info")
        log(f"[Improve] Summary: {len(str(result.get('analysis_summary', '') or ''))} chars", level="info")
        log(f"[Improve] Rules: {len(result.get('new_rules', []) or [])}", level="info")

        raw_adj = result.get("signal_adjustments", {})
        adj_norm = _normalize_signal_adjustments(raw_adj) if isinstance(raw_adj, dict) else {}

        nr = result.get("new_rules")
        if not isinstance(nr, list):
            nr = []

        learned: dict[str, Any] = {
            "updated_at": datetime.now().isoformat(),
            "total_trades_analyzed": len(completed),
            "analysis_summary": str(result.get("analysis_summary", "") or ""),
            "new_rules": nr,
            "reliable_signals": result.get("reliable_signals", [])
            if isinstance(result.get("reliable_signals"), list)
            else [],
            "unreliable_signals": result.get("unreliable_signals", [])
            if isinstance(result.get("unreliable_signals"), list)
            else [],
            "main_loss_reasons": result.get("main_loss_reasons", [])
            if isinstance(result.get("main_loss_reasons"), list)
            else [],
            "recommendation": str(result.get("recommendation", "") or ""),
            "expected_improvement": str(result.get("expected_improvement", "") or ""),
            "signal_win_rates": sig_win_rates,
            "signal_adjustments": adj_norm,
            "source": "continuous_backtester",
        }

        save_json(LEARNED_FILE, learned)

        log("[Improve] Saved to learned_weights.json", level="info")
        log(f"[Improve] New rules count: {len(learned['new_rules'])}", level="info")
        return learned

    except json.JSONDecodeError as e:
        log(f"[Improve] JSON error: {e}", level="error")
        save_json(IMPROVE_DEBUG_FILE, {"raw": raw, "error": str(e)})
        return None
    except Exception as e:  # noqa: BLE001
        log(f"[Improve] Error: {e}", level="error")
        log(f"[Improve] {traceback.format_exc()}", level="error")
        save_json(IMPROVE_DEBUG_FILE, {"raw": raw, "error": str(e), "traceback": traceback.format_exc()})
        return None
    finally:
        save_json(
            IMPROVING_FILE,
            {"running": False, "completed_at": datetime.now().isoformat()},
        )


def continuous_backtest_loop() -> None:
    log("[Loop] Starting continuous backtest loop", level="info")
    tests_since_improve = 0
    loop_completed_tests = 0

    while not _stop_flag.is_set():
        try:
            if not is_enabled():
                time.sleep(10)
                continue

            if is_improving():
                log("[Loop] Improvement running, waiting...", level="info")
                time.sleep(30)
                continue

            if not env("ANTHROPIC_API_KEY"):
                log("[Loop] ANTHROPIC_API_KEY missing — sleeping", level="warning")
                time.sleep(60)
                continue

            ticker = random.choice(BACKTEST_TICKERS)
            timeframe = random.choices(
                list(TF_WEIGHTS.keys()),
                weights=list(TF_WEIGHTS.values()),
                k=1,
            )[0]
            is_fx = len(ticker) == 6 and ticker.isalpha()
            date = get_random_date(days_back_max=365, days_back_min=10, skip_weekends=not is_fx)

            existing = _load_results_list()
            key = _result_dedup_key({"ticker": ticker, "date": date, "timeframe": timeframe})
            existing_keys = {_result_dedup_key(r) for r in existing}

            if key in existing_keys:
                continue

            update_state(
                {
                    "status": "testing",
                    "current_ticker": ticker,
                    "current_date": date,
                    "current_timeframe": timeframe,
                    "total_tests_run": len(existing),
                    "last_heartbeat": datetime.now().isoformat(),
                }
            )

            log(f"[Loop] Testing {ticker} {timeframe} {date}", level="info")

            result = run_one_backtest(ticker, timeframe, date)

            if result is not None:
                loop_completed_tests += 1
                if loop_completed_tests % 10 == 0:
                    log(
                        f"[Loop] Running - tests: {len(_load_results_list())}",
                        level="info",
                    )

                prev_len = len(_load_results_list())
                count = append_result(result)
                added = count > prev_len

                if added and not result.get("skipped"):
                    outcome = result.get("outcome", "?")
                    pnl = float(result.get("pnl_dollars", 0) or 0)
                    log(
                        f"[Loop] #{count} {ticker} {timeframe} {date}: {outcome} ${pnl:.2f}",
                        level="info",
                    )
                    if result.get("outcome") in ("WIN", "LOSS"):
                        tests_since_improve += 1

                if added and count > 0 and count % 5 == 0:
                    all_results = _load_results_list()
                    stats = calculate_stats(all_results)
                    save_json(STATS_FILE, stats)
                    log(
                        f"[Loop] Stats updated: WR={stats.get('win_rate_pct', 0)}% "
                        f"Trades={stats.get('total_trades', 0)}",
                        level="info",
                    )

                if tests_since_improve >= IMPROVE_EVERY:
                    log("[Loop] Running improvement cycle...", level="info")
                    tests_since_improve = 0
                    snap = _load_results_list()
                    threading.Thread(target=run_improvement_cycle, args=(snap,), daemon=True).start()

            update_state(
                {
                    "status": "idle",
                    "last_heartbeat": datetime.now().isoformat(),
                    "total_tests_run": len(_load_results_list()),
                    "tests_since_improve": tests_since_improve,
                    "last_session_at": datetime.now().isoformat(),
                }
            )

            time.sleep(3)

        except Exception as e:  # noqa: BLE001
            log(f"[Loop] Error: {e}", level="error")
            log(f"[Loop] {traceback.format_exc()}", level="error")
            time.sleep(10)
            continue

    log("[Loop] Stopped", level="info")
    update_state({"status": "stopped"})


def start_continuous_backtest() -> bool:
    global _backtest_thread
    if _backtest_thread and _backtest_thread.is_alive():
        log("[Backtest] Continuous loop already running", level="info")
        return False

    _stop_flag.clear()
    _backtest_thread = threading.Thread(target=continuous_backtest_loop, name="continuous_backtest", daemon=True)
    _backtest_thread.start()

    save_json(
        ENABLED_FILE,
        {"enabled": True, "started_at": datetime.now().isoformat()},
    )
    log("[Backtest] Continuous loop started", level="info")
    return True


def stop_continuous_backtest() -> bool:
    _stop_flag.set()
    save_json(
        ENABLED_FILE,
        {
            "enabled": False,
            "stopped_at": datetime.now().isoformat(),
        },
    )
    log("[Backtest] Loop stopping (toggle off)...", level="info")
    return True


def shutdown_continuous_backtest(join_timeout: float = 8.0) -> None:
    """Process shutdown: stop worker thread without changing the enabled toggle file."""
    _stop_flag.set()
    t = _backtest_thread
    if t and t.is_alive():
        t.join(timeout=join_timeout)
    _stop_flag.clear()


def trigger_improvement_now() -> dict[str, Any]:
    if is_improving():
        return {"started": False, "error": "Improvement already running"}
    results = _load_results_list()
    completed = [
        r
        for r in results
        if r and not r.get("skipped") and r.get("outcome") in ("WIN", "LOSS")
    ]
    if len(completed) < 10:
        return {"started": False, "error": "Need 10+ completed WIN/LOSS trades first"}
    threading.Thread(target=run_improvement_cycle, args=(list(results),), daemon=True).start()
    return {"started": True}


def get_stats() -> dict[str, Any]:
    return load_json(STATS_FILE, default={}) or {}


def get_learned() -> dict[str, Any]:
    """Latest improvement report from ``learned_weights.json`` on the data volume."""
    data = load_json(LEARNED_FILE, default=None)
    if isinstance(data, dict) and data:
        return data
    return {
        "analysis_summary": "",
        "new_rules": [],
        "reliable_signals": [],
        "unreliable_signals": [],
        "main_loss_reasons": [],
        "recommendation": "",
        "expected_improvement": "",
        "total_trades_analyzed": 0,
    }


def get_learned_history() -> list[dict[str, Any]]:
    """Learned-rule history archives are not written separately; use ``learned_weights.json``."""
    return []


def get_improving_state() -> dict[str, Any]:
    return load_json(IMPROVING_FILE, default={})


def get_improve_debug() -> dict[str, Any]:
    data = load_json(IMPROVE_DEBUG_FILE, default=None)
    if isinstance(data, dict) and data:
        return data
    return {"message": "No debug data yet"}
