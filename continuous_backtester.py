"""
Continuous autonomous backtesting loop: random historical setups, forward outcomes,
rolling stats, and periodic Claude-driven self-improvement (writes ``learned_weights.json``).
"""
from __future__ import annotations

import json
import math
import random
import re
import threading
import time
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
STATE_FILE = RESULTS_DIR / "backtest_state.json"
ENABLED_FILE = RESULTS_DIR / "backtest_enabled.json"
IMPROVING_FILE = RESULTS_DIR / "improving.json"

STARTING_CAPITAL = 10000.0
POSITION_SIZE_PCT = 0.02
FORWARD_CANDLES = 20
IMPROVE_EVERY = 100

BACKTEST_TICKERS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "GBPAUD",
    "EURAUD",
    "AUDUSD",
    "EURGBP",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "ACN",
    "ADBE",
    "MSFT",
    "GOOGL",
    "PYPL",
    "ZTS",
    "V",
    "AMZN",
    "AMD",
    "NVDA",
]

TIMEFRAMES = ["4h", "1d"]

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

        learned = load_json(LEARNED_FILE, default={}) or {}
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
STRICT RULES:
- ADX below 20 = WAIT (no trade)
- RSI 40-60 with no other confluence = WAIT
- Stop loss max 1.5% for forex, 3% for stocks
- TP1 max 2% for forex, 4% for stocks
- TP2 max 3.5% for forex, 6% for stocks
- Use ATR for precise stop/TP calculation
- TP must be on correct side of entry

Return ONLY valid JSON:
{{
  "verdict": "STRONG BUY|BUY|WAIT|SELL|STRONG SELL",
  "direction": "LONG|SHORT|NO TRADE",
  "confidence": "HIGH|MEDIUM|LOW",
  "entry": {price},
  "stop_loss": 0.0,
  "tp1": 0.0,
  "tp2": 0.0,
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

        direction = str(ai.get("direction", "NO TRADE")).strip().upper()
        if direction in ("NO TRADE", "WAIT", ""):
            return {
                "date": analysis_date,
                "ticker": sym,
                "timeframe": timeframe,
                "verdict": ai.get("verdict", "WAIT"),
                "direction": "NO TRADE",
                "skipped": True,
                "entry_price": price,
                "reasoning": str(ai.get("reasoning", "")),
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

        hit_tp1 = hit_stop = False
        c_tp1: int | None = None
        c_stop: int | None = None

        for i, (h, l) in enumerate(zip(highs, lows)):
            if direction == "LONG":
                if not hit_tp1 and tp1 > 0 and h >= tp1:
                    hit_tp1 = True
                    c_tp1 = i + 1
                if not hit_stop and stop > 0 and l <= stop:
                    hit_stop = True
                    c_stop = i + 1
            elif direction == "SHORT":
                if not hit_tp1 and tp1 > 0 and l <= tp1:
                    hit_tp1 = True
                    c_tp1 = i + 1
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

        trade_size = STARTING_CAPITAL * POSITION_SIZE_PCT
        if entry == 0:
            entry = price

        if hit_stop and (not hit_tp1 or c_stop is None or c_tp1 is None or c_stop < c_tp1):
            outcome = "LOSS"
            correct = False
            if direction == "LONG":
                pnl_pct = (stop - entry) / entry
            else:
                pnl_pct = (entry - stop) / entry
            exit_p = stop
            exit_r = "Stop loss hit"
        elif hit_tp1:
            outcome = "WIN"
            correct = True
            if direction == "LONG":
                pnl_pct = (tp1 - entry) / entry
            else:
                pnl_pct = (entry - tp1) / entry
            exit_p = tp1
            exit_r = "TP1 hit"
        else:
            final = float(closes[-1])
            if direction == "LONG":
                pnl_pct = (final - entry) / entry
            else:
                pnl_pct = (entry - final) / entry
            correct = pnl_pct > 0
            outcome = "WIN" if correct else "LOSS"
            exit_p = final
            exit_r = "Window ended"

        pnl_d = round(trade_size * float(pnl_pct), 2)

        return {
            "date": analysis_date,
            "ticker": sym,
            "timeframe": timeframe,
            "verdict": ai.get("verdict"),
            "direction": direction,
            "confidence": ai.get("confidence"),
            "entry_price": entry,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "exit_price": round(float(exit_p), 5),
            "exit_reason": exit_r,
            "outcome": outcome,
            "correct": correct,
            "pnl_pct": round(float(pnl_pct) * 100, 2),
            "pnl_dollars": pnl_d,
            "hit_tp1": hit_tp1,
            "hit_stop": hit_stop,
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


def calculate_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [r for r in results if r and not r.get("skipped")]
    if not trades:
        return {
            "generated_at": datetime.now().isoformat(),
            "total_trades": 0,
            "win_rate_pct": 0.0,
            "total_pnl_dollars": 0.0,
            "final_capital": STARTING_CAPITAL,
            "starting_capital": STARTING_CAPITAL,
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
        capital += float(t.get("pnl_dollars", 0) or 0)
        curve.append({"date": t.get("date"), "capital": round(capital, 2)})
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


def run_improvement_cycle(results_snapshot: list[dict[str, Any]]) -> None:
    trades = [r for r in results_snapshot if r and not r.get("skipped")]
    if len(trades) < 20:
        log("[Improve] Need 20+ completed trades to improve", level="info")
        return

    log(f"[Improve] Analyzing {len(trades)} trades...", level="info")
    save_json(IMPROVING_FILE, {"running": True, "started_at": datetime.now().isoformat(), "trades_analyzed": len(trades)})

    sig_stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        for s in t.get("signals_used") or []:
            if not isinstance(s, str):
                continue
            if s not in sig_stats:
                sig_stats[s] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
            if t.get("outcome") == "WIN":
                sig_stats[s]["wins"] += 1
            else:
                sig_stats[s]["losses"] += 1
            sig_stats[s]["total_pnl"] += float(t.get("pnl_dollars", 0) or 0)

    sig_win_rates: dict[str, Any] = {}
    for s, st in sig_stats.items():
        total = int(st["wins"]) + int(st["losses"])
        if total >= 3:
            wr = int(st["wins"]) / total
            sig_win_rates[s] = {
                "win_rate": round(wr * 100, 1),
                "total": total,
                "pnl": round(float(st["total_pnl"]), 2),
                "adjustment": (
                    "INCREASE_WEIGHT" if wr > 0.65 else "DECREASE_WEIGHT" if wr < 0.40 else "KEEP_WEIGHT"
                ),
            }

    loss_patterns: list[dict[str, Any]] = []
    for t in trades:
        if t.get("outcome") == "LOSS":
            loss_patterns.append(
                {
                    "ticker": t.get("ticker"),
                    "date": t.get("date"),
                    "signals": t.get("signals_used", []),
                    "conflicts": t.get("conflicts", []),
                    "reasoning": str(t.get("reasoning", ""))[:200],
                }
            )

    win_patterns: list[dict[str, Any]] = []
    for t in trades:
        if t.get("outcome") == "WIN":
            win_patterns.append(
                {
                    "ticker": t.get("ticker"),
                    "date": t.get("date"),
                    "signals": t.get("signals_used", []),
                    "pnl": t.get("pnl_dollars", 0),
                }
            )

    ticker_perf: dict[str, dict[str, Any]] = {}
    for t in trades:
        tk = str(t.get("ticker", ""))
        if tk not in ticker_perf:
            ticker_perf[tk] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t.get("outcome") == "WIN":
            ticker_perf[tk]["wins"] += 1
        else:
            ticker_perf[tk]["losses"] += 1
        ticker_perf[tk]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    n_wins = len([t for t in trades if t.get("outcome") == "WIN"])
    wr = round(n_wins / len(trades) * 100, 1) if trades else 0.0

    prompt = f"""
You are the APEX trading AI analyzing your own
backtesting results to improve your accuracy.

OVERALL STATS:
Total trades: {len(trades)}
Wins: {n_wins}
Losses: {len(trades) - n_wins}
Win rate: {wr}%

SIGNAL WIN RATES:
{json.dumps(sig_win_rates, indent=2, default=str)}

TICKER PERFORMANCE:
{json.dumps(ticker_perf, indent=2, default=str)}

SAMPLE LOSS PATTERNS (last 5):
{json.dumps(loss_patterns[-5:], indent=2, default=str)}

SAMPLE WIN PATTERNS (last 5):
{json.dumps(win_patterns[-5:], indent=2, default=str)}

Analyze this data deeply and provide:

1. What are the main reasons for losses?
2. What signals are most reliable (keep/increase)?
3. What signals cause false signals (decrease/remove)?
4. Which tickers work best with this approach?
5. What specific rules should be added to
   improve win rate above 60%?
6. Write 3-5 specific new rules for the system.

Be specific. Use the actual data.
These rules will be applied to future analysis.

Return as JSON:
{{
  "analysis_summary": "string",
  "main_loss_reasons": ["string"],
  "reliable_signals": ["string"],
  "unreliable_signals": ["string"],
  "best_tickers": ["string"],
  "worst_tickers": ["string"],
  "new_rules": ["string"],
  "expected_win_rate_improvement": "string",
  "signal_adjustments": {{"signal_name": "INCREASE|DECREASE|REMOVE"}},
  "recommendation": "string"
}}
"""

    try:
        client = _client()
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _message_text(resp)
        improvement = _parse_json_response(raw) or {}

        raw_adj = improvement.get("signal_adjustments", {})
        adj_norm = _normalize_signal_adjustments(raw_adj) if isinstance(raw_adj, dict) else {}

        learned: dict[str, Any] = {
            "updated_at": datetime.now().isoformat(),
            "total_trades_analyzed": len(trades),
            "signal_win_rates": sig_win_rates,
            "signal_adjustments": adj_norm,
            "new_rules": improvement.get("new_rules") if isinstance(improvement.get("new_rules"), list) else [],
            "reliable_signals": improvement.get("reliable_signals") if isinstance(improvement.get("reliable_signals"), list) else [],
            "unreliable_signals": improvement.get("unreliable_signals") if isinstance(improvement.get("unreliable_signals"), list) else [],
            "best_tickers": improvement.get("best_tickers") if isinstance(improvement.get("best_tickers"), list) else [],
            "worst_tickers": improvement.get("worst_tickers") if isinstance(improvement.get("worst_tickers"), list) else [],
            "main_loss_reasons": improvement.get("main_loss_reasons") if isinstance(improvement.get("main_loss_reasons"), list) else [],
            "recommendation": str(improvement.get("recommendation", "")),
            "analysis_summary": str(improvement.get("analysis_summary", "")),
            "expected_improvement": str(improvement.get("expected_win_rate_improvement", "")),
            "source": "continuous_backtester",
        }
        save_json(LEARNED_FILE, learned)
        log(f"[Improve] Self-improvement complete. New rules count: {len(learned.get('new_rules', []))}", level="info")
    except Exception as e:  # noqa: BLE001
        log(f"[Improve] Improvement error: {e}", level="warning")
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
    trades = [r for r in results if r and not r.get("skipped")]
    if len(trades) < 20:
        return {"started": False, "error": "Need 20+ completed trades first"}
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
    return load_json(LEARNED_FILE, default={})


def get_improving_state() -> dict[str, Any]:
    return load_json(IMPROVING_FILE, default={})
