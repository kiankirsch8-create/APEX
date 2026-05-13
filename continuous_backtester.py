"""
Continuous autonomous backtesting loop: random historical setups, forward outcomes,
rolling stats, and periodic self-improvement (timestamped backup of ``learned_weights.json``,
``learned_v*.json`` + ``learned_latest.json`` snapshots).
"""
from __future__ import annotations

import json
import math
import random
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401

from utils import RESULTS_DIR, env, load_json, log, save_json, utcnow_iso

CLAUDE_MODEL = "claude-opus-4-5"

RESULTS_FILE = RESULTS_DIR / "backtest_results.json"
STATS_FILE = RESULTS_DIR / "backtest_stats.json"
LEARNED_FILE = RESULTS_DIR / "learned_weights.json"
LEARNED_LATEST_FILE = RESULTS_DIR / "learned_latest.json"
STATE_FILE = RESULTS_DIR / "backtest_state.json"
ENABLED_FILE = RESULTS_DIR / "backtest_enabled.json"
IMPROVING_FILE = RESULTS_DIR / "improving.json"
IMPROVE_DEBUG_FILE = RESULTS_DIR / "improve_debug.json"

STARTING_CAPITAL = 10000.0
POSITION_SIZE_PCT = 0.05  # 5% of capital
LEVERAGE = 50  # 50x on notional exposure
FORWARD_CANDLES = 30
IMPROVE_EVERY = 100

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

TIMEFRAMES = ["1h", "4h", "1d"]

_TF_MAP: dict[str, str] = {
    "1h": "1h",
    "4h": "1h",
    "1d": "1d",
    "daily": "1d",
    "1w": "1wk",
    "weekly": "1wk",
}

_backtest_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_results_lock = threading.Lock()


def _load_learned_for_context() -> dict[str, Any]:
    """Prefer the newest improvement report; fall back to legacy ``learned_weights.json``."""
    latest = load_json(LEARNED_LATEST_FILE, default=None)
    if isinstance(latest, dict) and latest:
        return latest
    return load_json(LEARNED_FILE, default={}) or {}


def _next_learned_version() -> int:
    max_v = 0
    for p in RESULTS_DIR.glob("learned_v*.json"):
        m = re.match(r"learned_v(\d+)\.json$", p.name, re.IGNORECASE)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max_v + 1


def log_learned_startup_preview() -> None:
    """Log first 500 chars of the current learned report for Railway / ops visibility."""
    raw = load_json(LEARNED_LATEST_FILE, default={})
    if not raw:
        raw = load_json(LEARNED_FILE, default={})
    try:
        snippet = json.dumps(raw, indent=2, default=str)[:500]
    except (TypeError, ValueError):
        snippet = str(raw)[:500]
    log(f"[Learned] Current report: {snippet}", level="info")


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


def _load_results_list() -> list[dict[str, Any]]:
    with _results_lock:
        raw = load_json(RESULTS_FILE, default=[])
        if isinstance(raw, list):
            return raw
        return []


def _append_result(row: dict[str, Any]) -> list[dict[str, Any]]:
    with _results_lock:
        raw = load_json(RESULTS_FILE, default=[])
        results = raw if isinstance(raw, list) else []
        results.append(row)
        save_json(RESULTS_FILE, results)
        return results


def validate_rr(plan: dict[str, Any]) -> dict[str, Any]:
    """Ensure reward at TP1 vs stop risk meets minimum R/R; downgrade to NO TRADE if not."""
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

    if rr < 1.5:
        log(f"[Backtest] RR {rr:.2f} too low — rejecting trade", level="info")
        plan["direction"] = "NO TRADE"
        plan["skip_reason"] = f"R/R {rr:.2f} below minimum 1.5"

    plan["rr_ratio"] = f"1:{rr:.2f}"
    return plan


def run_one_backtest(ticker: str, timeframe: str, analysis_date: str) -> dict[str, Any] | None:
    try:
        sym = (ticker or "").strip().upper()
        interval = _TF_MAP.get(timeframe.lower().strip(), "1d")
        is_forex = len(sym) == 6 and sym.isalpha()
        yf_ticker = sym + "=X" if is_forex else sym

        target = datetime.strptime(analysis_date.strip(), "%Y-%m-%d")
        start = target - timedelta(days=730)
        end = target + timedelta(days=200)

        full_df = yf.Ticker(yf_ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval,
        )
        if full_df.empty or len(full_df) < 60:
            return None

        full_df = _strip_tz(full_df.sort_index())
        day_end = pd.Timestamp(analysis_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        past = full_df[full_df.index <= day_end].copy()
        future = full_df[full_df.index > day_end].copy()

        if len(past) < 50 or future.empty:
            return None
        if len(future) < 5:
            log(f"[Backtest] Not enough future data for {sym} {analysis_date}", level="info")
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

        bbl, _bbm, bbu = _pick_bb_cols(past)

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

        ind = {
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

        p = price
        ema20, ema50, ema200 = ind["ema20"], ind["ema50"], ind["ema200"]
        if p > ema20 > ema50 > ema200:
            trend = "STRONG UPTREND"
        elif p < ema20 < ema50 < ema200:
            trend = "STRONG DOWNTREND"
        elif p > ema50:
            trend = "UPTREND"
        elif p < ema50:
            trend = "DOWNTREND"
        else:
            trend = "RANGING"

        learned = _load_learned_for_context()
        adj = learned.get("signal_adjustments") if isinstance(learned, dict) else {}
        learned_context = ""
        if isinstance(adj, dict) and adj:
            good = [s for s, v in adj.items() if str(v).upper().startswith("INCREASE")]
            bad = [s for s, v in adj.items() if str(v).upper().startswith("DECREASE") or str(v).upper().startswith("REMOVE")]
            if good or bad:
                n = learned.get("total_trades_analyzed", 0) if isinstance(learned, dict) else 0
                learned_context = f"""
LEARNED FROM {n} past backtests:
High reliability signals (prefer these): {good}
Low reliability signals (be skeptical): {bad}
Recommendation: {learned.get("recommendation", "") if isinstance(learned, dict) else ""}
"""

        atr = ind["atr"]
        prompt = f"""
Analyze {sym} on {analysis_date} as a trader.
You see ONLY data up to this date. No future knowledge.

PRICE: {price}
TREND: {trend}
RSI(14): {ind["rsi"]} ({"oversold" if ind["rsi"] < 30 else "overbought" if ind["rsi"] > 70 else "neutral"})
MACD Histogram: {ind["macd_hist"]} ({"bullish" if ind["macd_hist"] > 0 else "bearish"})
EMA20: {ind["ema20"]} | EMA50: {ind["ema50"]} | EMA200: {ind["ema200"]}
ATR: {atr} | ADX: {ind["adx"]} ({"strong" if ind["adx"] > 25 else "weak"} trend)
BB Upper: {ind["bb_upper"]} | BB Lower: {ind["bb_lower"]}
Asset type: {"FOREX" if is_forex else "STOCK"}
{learned_context}
TRADING RULES:
- Only skip (NO TRADE) if trend is completely flat AND all indicators are neutral
- ADX below 12 = truly no trend = WAIT
- RSI between 48-52 with zero signals = WAIT
- Otherwise find the best trade available
- You should trade at least 50% of the time
- Exotic pairs often trend strongly - trade them
- Daily timeframe has clearer signals than 1H
- When in doubt favor the trend direction

STRICT RISK/REWARD RULES:
- Minimum R/R ratio: 1:1.5
- Preferred R/R ratio: 1:2.0 to 1:2.5
- Stop loss: 0.5x ATR from entry (tight)
- TP1: 1.5x ATR from entry (minimum)
- TP2: 2.5x ATR from entry
- TP3: 4x ATR from entry (runner)

For forex 4H/Daily:
Stop loss maximum: 0.8% from entry
TP1 minimum: 1.2% from entry
TP2 minimum: 2.0% from entry

NEVER place a stop loss wider than TP1.
If you cannot find a setup with 1:1.5 R/R
return NO TRADE instead.
A bad R/R setup is worse than no trade.

Current timeframe: {timeframe}. Use ATR={atr} with entry price for distances.

Return ONLY valid JSON:
{{
  "verdict": "STRONG BUY|BUY|WAIT|SELL|STRONG SELL",
  "direction": "LONG|SHORT|NO TRADE",
  "confidence": "HIGH|MEDIUM|LOW",
  "entry": {price},
  "stop_loss": 0.0,
  "tp1": 0.0,
  "tp2": 0.0,
  "tp3": 0.0,
  "rr_ratio": "string",
  "signals_used": ["string"],
  "confluences": ["string"],
  "conflicts": ["string"],
  "reasoning": "string"
}}
"""

        client = _client()
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
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
            }

        entry = float(ai.get("entry", price) or price)
        stop = float(ai.get("stop_loss", 0) or 0)
        tp1 = float(ai.get("tp1", 0) or 0)
        tp2 = float(ai.get("tp2", 0) or 0)

        fut = future.head(FORWARD_CANDLES)
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
            "entry_price": round(entry, 5),
            "stop_loss": round(stop, 5),
            "tp1": round(tp1, 5),
            "tp2": round(tp2, 5),
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

        if LEARNED_FILE.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(LEARNED_FILE, RESULTS_DIR / f"learned_{ts}.json")

        save_json(LEARNED_FILE, learned)
        save_json(LEARNED_LATEST_FILE, learned)
        ver = _next_learned_version()
        save_json(
            RESULTS_DIR / f"learned_v{ver}.json",
            {**learned, "improvement_version": ver, "versioned_filename": f"learned_v{ver}.json"},
        )

        log("[Improve] Saved to learned_weights.json (and learned_latest, learned_v snapshot)", level="info")
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
    log("[Backtest Loop] Starting continuous loop", level="info")
    tests_since_improve = 0

    while not _stop_flag.is_set():
        if not is_enabled():
            log("[Backtest Loop] Disabled, waiting...", level="info")
            time.sleep(30)
            continue

        if is_improving():
            log("[Backtest Loop] Improvement running, waiting...", level="info")
            time.sleep(60)
            continue

        if not env("ANTHROPIC_API_KEY"):
            log("[Backtest Loop] ANTHROPIC_API_KEY missing — sleeping", level="warning")
            time.sleep(60)
            continue

        results = _load_results_list()
        existing_keys = {f"{r.get('ticker')}|{r.get('date')}|{r.get('timeframe')}" for r in results if isinstance(r, dict)}

        ticker = random.choice(BACKTEST_TICKERS)
        timeframe = random.choice(TIMEFRAMES)
        is_fx = len(ticker) == 6 and ticker.isalpha()
        date = get_random_date(skip_weekends=not is_fx)
        key = f"{ticker}|{date}|{timeframe}"

        if key in existing_keys:
            time.sleep(0.2)
            continue

        update_state(
            {
                "status": "testing",
                "current_ticker": ticker,
                "current_date": date,
                "current_timeframe": timeframe,
                "total_tests_run": len(results),
            }
        )

        log(f"[Backtest Loop] Testing {ticker} {timeframe} on {date}", level="info")

        result = run_one_backtest(ticker, timeframe, date)
        if result:
            results_after = _append_result(result)
            tests_since_improve += 1

            if not result.get("skipped"):
                stats = calculate_stats(results_after)
                save_json(STATS_FILE, stats)
                log(
                    f"[Backtest Loop] {ticker} {date}: {result.get('outcome', '?')} "
                    f"${float(result.get('pnl_dollars', 0) or 0):.2f} | Total: {len(results_after)} | "
                    f"WR: {stats.get('win_rate_pct', 0)}%",
                    level="info",
                )

            if tests_since_improve >= IMPROVE_EVERY:
                log("[Backtest Loop] Running improvement cycle...", level="info")
                tests_since_improve = 0
                snap = _load_results_list()
                threading.Thread(target=run_improvement_cycle, args=(snap,), daemon=True).start()

        update_state(
            {
                "status": "idle",
                "tests_since_improve": tests_since_improve,
                "last_session_at": datetime.now().isoformat(),
                "total_tests_run": len(_load_results_list()),
            }
        )

        time.sleep(3)

    log("[Backtest Loop] Stopped", level="info")
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
    return load_json(STATS_FILE, default={})


def get_results_slice(limit: int = 50) -> list[dict[str, Any]]:
    results = _load_results_list()
    if limit <= 0:
        return []
    return results[-limit:]


def get_learned() -> dict[str, Any]:
    """Latest improvement report (``learned_latest.json``), not legacy-only weights."""
    data = load_json(LEARNED_LATEST_FILE, default=None)
    if isinstance(data, dict) and data:
        return data
    return load_json(LEARNED_FILE, default={})


def get_learned_history() -> list[dict[str, Any]]:
    """All versioned improvement cycles, oldest first (``learned_v*.json``)."""
    rows: list[tuple[int, dict[str, Any]]] = []
    for p in RESULTS_DIR.glob("learned_v*.json"):
        m = re.match(r"learned_v(\d+)\.json$", p.name, re.IGNORECASE)
        if not m:
            continue
        v = int(m.group(1))
        blob = load_json(p, default=None)
        if isinstance(blob, dict):
            rows.append((v, blob))
    rows.sort(key=lambda x: x[0])
    return [d for _, d in rows]


def get_improving_state() -> dict[str, Any]:
    return load_json(IMPROVING_FILE, default={})


def get_improve_debug() -> dict[str, Any]:
    data = load_json(IMPROVE_DEBUG_FILE, default=None)
    if isinstance(data, dict) and data:
        return data
    return {"message": "No debug data yet"}
