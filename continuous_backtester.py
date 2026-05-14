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

STRATEGIES: dict[str, dict[str, Any]] = {
    "S01_BREAKOUT_RETEST": {
        "name": "Breakout Retest",
        "description": (
            "Price breaks significant level "
            "then retests it as support/resistance"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 18,
        "category": "TREND_FOLLOWING",
    },
    "S02_LIQUIDITY_SWEEP": {
        "name": "Liquidity Sweep Reversal",
        "description": (
            "Price sweeps beyond swing cluster "
            "then closes back inside"
        ),
        "timeframes": ["1h", "4h"],
        "min_adx": 0,
        "category": "SMART_MONEY",
    },
    "S03_EMA_PULLBACK": {
        "name": "Trend Pullback to EMA",
        "description": (
            "In clear trend price pulls back "
            "to EMA20 or EMA50"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 18,
        "category": "TREND_FOLLOWING",
    },
    "S04_EXTREME_REVERSION": {
        "name": "Extreme Zone Mean Reversion",
        "description": (
            "Price at 52w extreme with RSI "
            "extreme, statistical reversion"
        ),
        "timeframes": ["1d", "1w"],
        "min_adx": 0,
        "category": "MEAN_REVERSION",
    },
    "S05_MACD_DIVERGENCE": {
        "name": "MACD Divergence",
        "description": (
            "Price and MACD diverge showing "
            "momentum exhaustion"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 0,
        "category": "MOMENTUM",
    },
    "S06_ORDER_BLOCK": {
        "name": "Institutional Order Block",
        "description": (
            "Last opposing candle before "
            "impulse - price returns to it"
        ),
        "timeframes": ["1h", "4h"],
        "min_adx": 0,
        "category": "SMART_MONEY",
    },
    "S07_FAIR_VALUE_GAP": {
        "name": "Fair Value Gap Fill",
        "description": (
            "Three candle imbalance gap "
            "price returns to fill"
        ),
        "timeframes": ["1h", "4h"],
        "min_adx": 0,
        "category": "SMART_MONEY",
    },
    "S08_RANGE_BREAKOUT": {
        "name": "Range Breakout",
        "description": (
            "Price consolidates then breaks "
            "out with volume confirmation"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 0,
        "category": "BREAKOUT",
    },
    "S09_NEWS_CATALYST": {
        "name": "News Catalyst Momentum",
        "description": (
            "Major news release with strong "
            "sentiment drives momentum"
        ),
        "timeframes": ["1h", "4h"],
        "min_adx": 0,
        "category": "NEWS_DRIVEN",
    },
    "S10_COT_DIVERGENCE": {
        "name": "COT Institutional Divergence",
        "description": (
            "Institutions repositioning shown "
            "in COT data not yet in price"
        ),
        "timeframes": ["1d", "1w"],
        "min_adx": 0,
        "category": "INSTITUTIONAL",
    },
    "S11_SR_FLIP": {
        "name": "Support Resistance Flip",
        "description": (
            "Old resistance becomes support "
            "or vice versa on retest"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 0,
        "category": "STRUCTURE",
    },
    "S12_VOLATILITY_COMPRESSION": {
        "name": "Volatility Compression Breakout",
        "description": (
            "Bollinger Band squeeze then "
            "explosive breakout"
        ),
        "timeframes": ["4h", "1d"],
        "min_adx": 0,
        "category": "BREAKOUT",
    },
}

# --- Hard exclusions: CHF + proven losers + illiquid (``run_one_backtest`` early return) ---
CHF_PAIRS = frozenset(
    {
        "GBPCHF",
        "AUDCHF",
        "EURCHF",
        "USDCHF",
        "NZDCHF",
        "CADCHF",
        "CHFJPY",
    }
)

EXCLUDED_PAIRS = frozenset(
    {
        *CHF_PAIRS,
        "EURJPY",
        "AUDNZD",
        "USDHKD",
        "GBPAUD",
        "NZDJPY",
        "EURCAD",
        "AUDCAD",
        "USDCZK",
        "USDPLN",
        "USDHUF",
        "USDDKK",
        "USDSGD",
        "USDTRY",  # structural carry trade, fights shorts
    }
)

PRIORITY_PAIRS = frozenset(
    {
        "EURUSD",
        "GBPUSD",
        "USDCAD",
        "EURGBP",
        "EURNZD",
        "USDMXN",
        "USDZAR",
        "AUDUSD",
        "NZDUSD",
        "GBPJPY",
        "USDSEK",
        "USDNOK",
    }
)

HARD_EXCLUDED_TICKERS = sorted(EXCLUDED_PAIRS)

EXOTIC_REDUCE = frozenset(
    {
        "USDZAR",
        "USDTRY",
        "USDMXN",
        "USDDKK",
        "USDSEK",
        "USDNOK",
        "USDPLN",
        "USDCZK",
        "USDHUF",
        "USDSGD",
        "USDHKD",
        "NZDCAD",
        "GBPNZD",
    }
)

BANNED_SIGNALS: list[str] = []

# --- Multi-timeframe backtest universe ---
TF_WEIGHTS = {
    "1h": 0.05,
    "4h": 0.15,
    "1d": 0.65,
    "1w": 0.15,
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
    "1h": "1H intraday — low priority (weak in data); only with extreme zone + sweep + RSI_NEUTRAL",
    "4h": "4H — secondary; use rarely vs daily",
    "1d": "Daily PRIMARY — cleanest signals (~54% WR in backtests); hold ~3–20 days",
    "1w": "Weekly — secondary HTF context (~40% WR vs intraday)",
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


def pick_backtest_ticker() -> str:
    """Prefer PRIORITY_PAIRS (~65%) among symbols not in EXCLUDED_PAIRS."""
    eligible = [t for t in BACKTEST_TICKERS if t not in EXCLUDED_PAIRS]
    if not eligible:
        return "EURUSD"
    priority = [t for t in eligible if t in PRIORITY_PAIRS]
    other = [t for t in eligible if t not in PRIORITY_PAIRS]
    if priority and random.random() < 0.65:
        return random.choice(priority)
    return random.choice(other or eligible)


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


def reset_backtest_stats_files() -> None:
    """Clear continuous backtest results, rolling stats JSON, and learned weights (atomic writes)."""
    save_json(RESULTS_FILE, [])
    empty_stats: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "total_trades": 0,
        "win_rate_pct": 0,
        "total_pnl_dollars": 0,
        "final_capital": STARTING_CAPITAL,
        "starting_capital": STARTING_CAPITAL,
        "winning_trades": 0,
        "losing_trades": 0,
        "capital_curve": [],
        "signal_performance": {},
        "timeframe_performance": {},
    }
    save_json(STATS_FILE, empty_stats)
    reset_learned_rules()
    log("[Reset] Backtest results, stats, and learned rules cleared", level="info")


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


def validate_rr(plan: dict[str, Any]) -> dict[str, Any]:
    """R/R validation disabled — every non-CHF analysis must produce a trade."""
    return plan


def calculate_position_size(
    confidence: str,
    zone_pct: float,
    strategy_met: bool,
    conviction_score: int,
    account_balance: float,
) -> dict[str, Any]:
    """Dollar risk budget from confidence, zone, strategy match, and conviction."""
    c = (confidence or "MEDIUM").strip().upper()
    if c == "HIGH" and strategy_met:
        risk_pct = 0.02
    elif c == "MEDIUM":
        risk_pct = 0.01
    else:
        risk_pct = 0.005

    if conviction_score >= 8 and strategy_met:
        risk_pct = min(risk_pct * 1.5, 0.03)

    if 30 <= zone_pct <= 70:
        risk_pct *= 0.7

    max_risk = float(account_balance) * risk_pct
    return {"risk_pct": risk_pct, "max_risk_dollars": round(max_risk, 2)}


def validate_stop_loss(entry: float, stop: float, direction: str, timeframe: str) -> float:
    """Cap stop distance from entry by timeframe (max % move against position)."""
    MAX_STOP_PCT = {"1h": 0.008, "4h": 0.015, "1d": 0.025, "1w": 0.040}
    tf = timeframe.lower().strip()
    max_pct = MAX_STOP_PCT.get(tf, 0.015)
    try:
        e = float(entry)
        s = float(stop)
    except (TypeError, ValueError):
        return round(float(stop or 0), 5)
    if not math.isfinite(e) or e <= 0 or not math.isfinite(s):
        return round(s, 5) if math.isfinite(s) else round(e * 0.99, 5)
    max_distance = abs(e) * max_pct
    d = str(direction or "").strip().upper()
    if d == "LONG":
        if s >= e:
            return round(e - max_distance, 5)
        min_stop = e - max_distance
        if s < min_stop:
            return round(min_stop, 5)
    else:
        if s <= e:
            return round(e + max_distance, 5)
        max_stop = e + max_distance
        if s > max_stop:
            return round(max_stop, 5)
    return round(s, 5)


def _apply_exotic_confidence(confidence: str, is_exotic: bool) -> str:
    """Downgrade declared confidence one notch for exotic / thin pairs."""
    c = (confidence or "MEDIUM").strip().upper()
    if c not in ("HIGH", "MEDIUM", "LOW"):
        c = "MEDIUM"
    if not is_exotic:
        return c
    if c == "HIGH":
        return "MEDIUM"
    if c == "MEDIUM":
        return "LOW"
    return c


def evaluate_forward_candles(
    direction: str,
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    forward_df: pd.DataFrame,
    strategy_id: str,
) -> dict[str, Any]:
    """Trailing-stop forward simulation through the post-entry window."""
    _ = strategy_id
    if forward_df is None or forward_df.empty:
        return {
            "outcome": "NO_DATA",
            "exit_price": entry,
            "exit_reason": "No forward data",
            "pnl_pct": 0.0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_stop": False,
            "candles_to_exit": 0,
            "trailing_activated": False,
            "final_stop": stop_loss,
        }

    current_stop = float(stop_loss)
    hit_tp1 = False
    hit_tp2 = False
    hit_tp3 = False
    hit_stop = False
    trailing_activated = False
    exit_price = float(entry)
    exit_reason = "Window ended"
    candle_count = 0

    d = str(direction or "").strip().upper()
    if d == "LONG":
        risk = float(entry) - float(stop_loss)
    else:
        risk = float(stop_loss) - float(entry)

    if risk <= 0:
        return {
            "outcome": "INVALID",
            "exit_price": float(entry),
            "exit_reason": "Invalid risk",
            "pnl_pct": 0.0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_stop": False,
            "candles_to_exit": 0,
            "trailing_activated": False,
            "final_stop": float(stop_loss),
        }

    for _idx, candle in forward_df.iterrows():
        candle_count += 1
        try:
            high = float(candle.get("High", entry))
            low = float(candle.get("Low", entry))
            close = float(candle.get("Close", entry))
        except (TypeError, ValueError):
            continue

        if d == "LONG":
            if low <= current_stop:
                hit_stop = True
                exit_price = current_stop
                exit_reason = "Trailing stop hit" if trailing_activated else "Stop loss hit"
                break

            if high >= tp3 and not hit_tp3:
                hit_tp3 = True
                current_stop = float(entry) + risk * 2
                trailing_activated = True

            if high >= tp2 and not hit_tp2:
                hit_tp2 = True
                if not hit_tp3:
                    current_stop = float(entry) + risk * 1
                trailing_activated = True

            if high >= tp1 and not hit_tp1:
                hit_tp1 = True
                if not hit_tp2 and not hit_tp3:
                    current_stop = float(entry)
                trailing_activated = True

            if hit_tp1 and trailing_activated:
                if hit_tp3:
                    trail_mult = 0.5
                elif hit_tp2:
                    trail_mult = 1.0
                else:
                    trail_mult = 1.5
                new_trail = close - risk * trail_mult
                if new_trail > current_stop:
                    current_stop = new_trail

        else:
            if high >= current_stop:
                hit_stop = True
                exit_price = current_stop
                exit_reason = "Trailing stop hit" if trailing_activated else "Stop loss hit"
                break

            if low <= tp3 and not hit_tp3:
                hit_tp3 = True
                current_stop = float(entry) - risk * 2
                trailing_activated = True

            if low <= tp2 and not hit_tp2:
                hit_tp2 = True
                if not hit_tp3:
                    current_stop = float(entry) - risk * 1
                trailing_activated = True

            if low <= tp1 and not hit_tp1:
                hit_tp1 = True
                if not hit_tp2 and not hit_tp3:
                    current_stop = float(entry)
                trailing_activated = True

            if hit_tp1 and trailing_activated:
                if hit_tp3:
                    trail_mult = 0.5
                elif hit_tp2:
                    trail_mult = 1.0
                else:
                    trail_mult = 1.5
                new_trail = close + risk * trail_mult
                if new_trail < current_stop:
                    current_stop = new_trail

    if not hit_stop and not forward_df.empty:
        try:
            exit_price = float(forward_df["Close"].iloc[-1])
        except (TypeError, ValueError, KeyError, IndexError):
            exit_price = float(entry)

    if d == "LONG":
        pnl_pct = (exit_price - float(entry)) / float(entry) if entry else 0.0
    else:
        pnl_pct = (float(entry) - exit_price) / float(entry) if entry else 0.0

    outcome = "WIN" if pnl_pct > 0 else "LOSS"

    return {
        "outcome": outcome,
        "exit_price": round(exit_price, 5),
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct, 6),
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
        "hit_tp3": hit_tp3,
        "hit_stop": hit_stop,
        "candles_to_exit": candle_count,
        "trailing_activated": trailing_activated,
        "final_stop": round(current_stop, 5),
    }


def _tp_target_and_smc_dashboard(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate TP target vs outcome and SMC concept PnL for stats API."""
    completed = [
        t for t in trades if isinstance(t, dict) and not t.get("skipped") and t.get("outcome") in ("WIN", "LOSS")
    ]
    tp_performance: dict[str, dict[str, int]] = {
        "TP1": {"count": 0, "wins": 0},
        "TP2": {"count": 0, "wins": 0},
        "TP3": {"count": 0, "wins": 0},
    }
    for t in completed:
        tgt = str(t.get("tp_target") or "TP1").strip().upper()
        if tgt not in tp_performance:
            tgt = "TP1"
        tp_performance[tgt]["count"] += 1
        if t.get("outcome") == "WIN":
            tp_performance[tgt]["wins"] += 1

    smc_performance: dict[str, dict[str, Any]] = {}
    for t in completed:
        concept = str(t.get("smc_concept") or "NONE").strip().upper()
        if concept not in smc_performance:
            smc_performance[concept] = {"count": 0, "wins": 0, "pnl": 0.0}
        smc_performance[concept]["count"] += 1
        if t.get("outcome") == "WIN":
            smc_performance[concept]["wins"] += 1
        smc_performance[concept]["pnl"] += float(t.get("pnl_dollars", 0) or 0)

    return {"tp_performance": tp_performance, "smc_performance": smc_performance}


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

        if sym in EXCLUDED_PAIRS:
            reason = "CHF excluded" if sym in CHF_PAIRS else "Excluded pair (backtest data)"
            log(f"[Backtest] FILTERED: {sym} — {reason}", level="info")
            return {
                "date": analysis_date,
                "ticker": sym,
                "timeframe": timeframe,
                "verdict": "FILTERED",
                "direction": "NO TRADE",
                "skipped": True,
                "skip_reason": reason,
                "reasoning": reason,
                "entry_price": price,
            }

        tf_desc = TF_DESCRIPTIONS.get(tf_key, tf_key)

        high_52w = float(ind.get("high_52w") or price * 1.1)
        low_52w = float(ind.get("low_52w") or price * 0.9)
        if not math.isfinite(high_52w):
            high_52w = float(price) * 1.1
        if not math.isfinite(low_52w):
            low_52w = float(price) * 0.9
        zone_pct = (
            round((float(price) - low_52w) / (high_52w - low_52w) * 100, 1)
            if high_52w > low_52w
            else 50.0
        )
        zone_label = (
            "EXTREME_DISCOUNT"
            if zone_pct < 8
            else "DISCOUNT"
            if zone_pct < 30
            else "EQUILIBRIUM"
            if zone_pct < 70
            else "PREMIUM"
            if zone_pct < 92
            else "EXTREME_PREMIUM"
        )

        is_exotic = sym in EXOTIC_REDUCE
        fp = float(price)
        dist_hi_pct = round(((high_52w - fp) / fp * 100), 2) if fp else 0.0
        dist_lo_pct = round(((fp - low_52w) / fp * 100), 2) if fp else 0.0

        bb_mid_val = float(ind.get("bb_mid", price) or price)
        if not math.isfinite(bb_mid_val) or bb_mid_val <= 0:
            bb_mid_val = float(price)
        try:
            bb_width = round(
                (float(ind["bb_upper"]) - float(ind["bb_lower"])) / bb_mid_val * 100,
                3,
            )
        except (TypeError, ValueError, ZeroDivisionError):
            bb_width = 2.0
        macd_line = float(ind.get("macd_line", 0) or 0)
        macd_signal = float(ind.get("macd_signal", 0) or 0)

        intel_text = ""
        news_sentiment = 0.0
        cot_bias = "UNKNOWN"
        fear_greed = 50.0
        vix_val = 20.0
        try:
            from market_intelligence import format_for_prompt, get_complete_briefing

            briefing = get_complete_briefing(sym, analysis_date.strip(), past)
            intel_text = format_for_prompt(briefing)
            news_sentiment = float(briefing.get("news", {}).get("net_sentiment", 0) or 0)
            cot_bias = str(briefing.get("cot_base", {}).get("bias", "UNKNOWN"))
            fear_greed = float(briefing.get("fear_greed", {}).get("score", 50) or 50)
            vix_val = float(
                briefing.get("proxies", {})
                .get("proxies", {})
                .get("vix", {})
                .get("value", 20)
                or 20
            )
        except Exception as e:
            log(f"[Intel] {e}")

        prompt = f"""
You are APEX — an elite institutional trading AI.

You do NOT predict direction randomly.
You WAIT for structural confirmation that
a move has already begun or a setup is
clearly forming, then you enter with precision.

You know 12 institutional strategies used by
the best traders and funds in the world.
You identify which strategy applies to the
current market structure and enter accordingly.

ASSET: {sym}
TIMEFRAME: {TF_DESCRIPTIONS.get(tf_key, timeframe)}
DATE: {analysis_date}
PRICE: {price:.5f}

ZONE: {zone_label} ({zone_pct:.1f}% of 52-week range)
52-week High: {high_52w:.5f}
52-week Low: {low_52w:.5f}

TECHNICAL INDICATORS:
RSI(14): {ind["rsi"]:.2f}
MACD Line: {macd_line:.6f}
MACD Signal: {macd_signal:.6f}
MACD Histogram: {ind["macd_hist"]:.6f}
EMA20: {ind["ema20"]:.5f}
EMA50: {ind["ema50"]:.5f}
EMA200: {ind["ema200"]:.5f}
ATR(14): {ind["atr"]:.5f}
ADX(14): {ind["adx"]:.2f}
BB Upper: {ind["bb_upper"]:.5f}
BB Middle: {ind.get("bb_mid", price):.5f}
BB Lower: {ind["bb_lower"]:.5f}
BB Width %: {bb_width:.3f}
Swing Highs (recent): {ind.get("swing_highs", [])}
Swing Lows (recent): {ind.get("swing_lows", [])}

{intel_text}

════════════════════════════════════════
THE 12 INSTITUTIONAL STRATEGIES
════════════════════════════════════════
Scan each strategy. Identify which ONE
best fits the current market structure.
Only enter if the non-negotiables are met.

────────────────────────────────────────
S01: BREAKOUT RETEST
────────────────────────────────────────
What it is: Price broke a significant level
and is now retesting it as new S/R.

Non-negotiables:
✓ Clear breakout candle closed beyond level
✓ Price has pulled back to test that level
✓ Retest candle shows rejection (closing away)
✓ ADX above 18 (trend exists)
✓ Price within 1.0x ATR of a swing_high or
  swing_low counts as "at the level"

LONG trigger: Price breaks above resistance,
retests it, holds above → BUY the retest
SHORT trigger: Price breaks below support,
retests it, holds below → SELL the retest

Stop: Beyond the retest candle
Identify: Look at swing_highs/swing_lows for
the broken level. Is price within 1.0x ATR of one now?

────────────────────────────────────────
S02: LIQUIDITY SWEEP REVERSAL
────────────────────────────────────────
What it is: Institutions run stops beyond
swing clusters then reverse direction.

Non-negotiables:
✓ Equal highs or lows: levels within 0.3%
  of each other count as equal
✓ Price spiked beyond those equal levels
✓ Close is at least 0.2x ATR back inside
  the pre-sweep range (wick sweep OK)
✓ RSI during sweep can be anywhere 25-75
  (sweep + close-back is primary)

LONG trigger: Spike below equal lows, close back above
SHORT trigger: Spike above equal highs, close back below

Stop: Beyond the sweep wick extreme
Identify: Check swing_lows for near-equal values.
Did price go below them and recover?
Check swing_highs for near-equal values.
Did price go above them and pull back?

────────────────────────────────────────
S03: TREND PULLBACK TO EMA
────────────────────────────────────────
What it is: In established trend, price
pulls back to moving average support.

Non-negotiables:
✓ EMA20 > EMA50 > EMA200 (uptrend) OR
  EMA20 < EMA50 < EMA200 (downtrend)
✓ Price within 1.5x ATR of EMA20 or EMA50
✓ RSI between 30-65 at the pullback
✓ ADX above 18

LONG trigger: In uptrend, price touches EMA20
or EMA50, RSI was falling but now turning up
SHORT trigger: In downtrend, price bounces to
EMA20 or EMA50, RSI was rising but now turning down

Stop: Below EMA50 (long) or above EMA50 (short)
Identify: Is price within 1.5x ATR of EMA20 or EMA50?
Is there a clear EMA stack?

────────────────────────────────────────
S04: EXTREME ZONE MEAN REVERSION
────────────────────────────────────────
What it is: Price at statistical extreme,
mean reversion highly probable.

Non-negotiables:
✓ Zone below 12% (EXTREME_DISCOUNT) for longs
  OR zone above 88% (EXTREME_PREMIUM) for shorts
✓ RSI below 30 (long) or above 70 (short)
✓ ADX below 40 (not in strong trend)
✓ Previous visit to this zone reversed

LONG trigger: EXTREME_DISCOUNT + RSI below 30
SHORT trigger: EXTREME_PREMIUM + RSI above 70

Stop: 0.5x ATR beyond the extreme
Identify: Zone is {zone_pct:.1f}%. RSI is {ind["rsi"]:.1f}.
Does this qualify?

────────────────────────────────────────
S05: MACD DIVERGENCE
────────────────────────────────────────
What it is: Price and momentum diverge,
indicating trend exhaustion.

Non-negotiables:
✓ BEARISH: Price makes higher high but
  MACD histogram makes lower high
✓ BULLISH: Price makes lower low but
  MACD histogram makes higher low
✓ MACD line and histogram both confirm
  the divergence direction
✓ Divergence spans at least 2 candles
✓ RSI also showing divergence

LONG trigger: Bullish divergence + MACD
histogram turns positive
SHORT trigger: Bearish divergence + MACD
histogram turns negative

Stop: Beyond the divergence extreme
Identify: MACD is {ind["macd_hist"]:.6f}.
Is there divergence between price and MACD?

────────────────────────────────────────
S06: INSTITUTIONAL ORDER BLOCK
────────────────────────────────────────
What it is: Institutions left unfilled orders
at a price zone, returning there for execution.

Non-negotiables:
✓ Identify last bearish candle before
  strong bullish move (bullish OB) OR
  last bullish candle before strong
  bearish move (bearish OB)
✓ Price is currently returning to that zone
✓ Higher timeframe trend supports direction
✓ Zone not previously violated

LONG trigger: Price returns to bullish OB,
shows bullish reaction candle
SHORT trigger: Price returns to bearish OB,
shows bearish reaction candle

Stop: Beyond the order block
Identify: Look at swing_highs and swing_lows.
What was the last strong impulse move?
Where did it start?

────────────────────────────────────────
S07: FAIR VALUE GAP FILL
────────────────────────────────────────
What it is: Rapid price move leaves
imbalance that price returns to fill.

Non-negotiables:
✓ Three candle pattern: candle 1 high,
  big middle candle, candle 3 low
✓ Gap between candle 1 high and candle 3 low
  (bullish FVG) or candle 1 low and candle 3
  high (bearish FVG)
✓ Gap is at least 0.5x ATR in size
✓ Price returning to fill the gap
✓ Higher timeframe trend supports

LONG trigger: Bullish FVG, price returns
to gap level, holds as support
SHORT trigger: Bearish FVG, price returns
to gap level, holds as resistance

Stop: Beyond the far edge of the gap
Identify: Are there gaps in recent price
action visible from swing data?
ATR is {ind["atr"]:.5f} — gap needs to be
at least {float(ind["atr"]) * 0.5:.5f}

────────────────────────────────────────
S08: RANGE BREAKOUT WITH VOLUME
────────────────────────────────────────
What it is: Price compresses then
explodes out with institutional backing.

Non-negotiables:
✓ BB Width below 2.5% (compression)
  Current BB Width: {bb_width:.3f}%
✓ ADX below 25 during compression
✓ At least 5 candles inside the range
✓ Breakout candle closes clearly outside BB
✓ Volume spike on breakout (if available)

LONG trigger: Compression then close above BB upper
SHORT trigger: Compression then close below BB lower

Stop: Opposite BB band at breakout
Identify: BB Width is {bb_width:.3f}%.
Is this compressed? Did price just break out?

────────────────────────────────────────
S09: NEWS CATALYST MOMENTUM
────────────────────────────────────────
What it is: Major news drives price
in clear direction, trade the momentum.

Non-negotiables:
✓ News sentiment strongly confirms
  (above 0.5 bullish or below -0.5 bearish)
✓ Price already moving in news direction
✓ Not trading INTO existing major S/R
✓ Momentum candles showing continuation

LONG trigger: Strong bullish news + price
breaking above recent resistance
SHORT trigger: Strong bearish news + price
breaking below recent support

Stop: Pre-news level or recent swing
Identify: News sentiment is {news_sentiment:.2f}.
Does news strongly confirm a direction?

────────────────────────────────────────
S10: COT INSTITUTIONAL REPOSITIONING
────────────────────────────────────────
What it is: Institutions changing their
positions in a currency — front-run them.

Non-negotiables:
✓ COT net position changed direction
✓ Net change significant (trend change)
✓ Price has not yet moved to reflect
  the new institutional positioning
✓ Zone supports same direction as COT

LONG trigger: COT shows institutions
building longs + discount zone
SHORT trigger: COT shows institutions
building shorts + premium zone

Stop: Last major swing point
Identify: COT bias is {cot_bias}.
Does price zone align with COT direction?

────────────────────────────────────────
S11: SUPPORT/RESISTANCE FLIP
────────────────────────────────────────
What it is: A broken level flips its
role - resistance becomes support etc.

Non-negotiables:
✓ Price clearly broke through a level
  (full candle body, not just wick)
✓ Price within 1.0x ATR of any swing_high or
  swing_low that was tested at least once before
✓ Level is holding in its new role
✓ Rejection candle visible at the level

LONG trigger: Old resistance now holding
as support on retest
SHORT trigger: Old support now holding
as resistance on retest

Stop: Beyond the flipped level
Identify: Look at swing_highs/swing_lows.
Is price within 1.0x ATR of a prior-tested swing level?
Has that level role-reversed?

────────────────────────────────────────
S12: VOLATILITY COMPRESSION BREAKOUT
────────────────────────────────────────
What it is: After consolidation energy
releases explosively.

Non-negotiables:
✓ BB Width below 2.0% (tight compression)
  Current: {bb_width:.3f}%
✓ ADX below 22 (no trend during compression)
✓ Candle closes outside BB after compression
✓ This candle has above-average range

LONG trigger: Close above upper BB after squeeze
SHORT trigger: Close below lower BB after squeeze

Stop: Midpoint of the compression range
Identify: Is current BB Width compressed?
Did price just break out?

STRATEGY SELECTION GUIDANCE:
You must identify a named strategy for at least
60% of trades. Do not be overly strict with
non-negotiables — use judgment within 15% of
each threshold.

For example:
- ADX 22 when threshold is 25 = VALID if trend clear
- Zone 14% when extreme threshold is 12% = VALID
- RSI 68 when threshold is 70 = VALID

The non-negotiables are guidelines not hard walls.
A trade that meets 3 of 4 non-negotiables with
the 4th close to threshold = USE THE STRATEGY.

Only use S00_BEST_AVAILABLE when NO strategy
comes within 20% of its thresholds.

════════════════════════════════════════
DECISION FRAMEWORK
════════════════════════════════════════

STEP 1: SCAN ALL 12 STRATEGIES
Go through each strategy above.
Identify which strategy or strategies
currently have their non-negotiables met.

STEP 2: SELECT BEST MATCHING STRATEGY
Pick the ONE strategy that best matches
current market structure.
State which strategy and why.

If NO strategy non-negotiables are met:
You may still take a trade but label it
as "S00_BEST_AVAILABLE" and use:
- Zone direction (primary)
- HTF trend (EMA200)
- Strongest momentum signal
This is low confidence only.

STEP 3: DETERMINE DIRECTION
The strategy determines direction.
Not indicators alone — the STRUCTURE determines it.

STEP 4: FIND PRECISE ENTRY
Where exactly does price confirm entry?
Not "generally around here" but the
specific price where structure confirms.

STEP 5: PLACE STOP AT INVALIDATION POINT
Where does this trade become wrong?
Stop goes there plus 0.3x ATR buffer.
This is not optional — every trade needs
a specific structural invalidation level.

STEP 6: SET TARGETS WITH TRAILING LOGIC
TP1 = 2.0x risk distance (minimum)
TP2 = 3.0x risk distance
TP3 = 5.0x risk distance

After TP1: stop moves to breakeven
After TP2: stop moves to +1R; then trail at 1.0R
below/above each close (tighter than before)
After TP3: trail at 0.5R below/above close
(very tight — ride the trend)

STEP 7: CONFIDENCE AND SIZING
HIGH (2% risk): Strategy perfectly met +
  intelligence confirms + zone confirms
MEDIUM (1% risk): Strategy met +
  at least 1 confirming factor
LOW (0.5% risk): S00_BEST_AVAILABLE or
  strategy met but factors conflict

════════════════════════════════════════
INTELLIGENCE INTEGRATION
════════════════════════════════════════

Fear & Greed {fear_greed:.0f}/100:
- Below 25: Safe havens only, widen stops
- Above 75: Contrarian signals more reliable

VIX {vix_val:.1f}:
- Above 30: Widen all stops by 25%
- Below 15: Trend trades more reliable

News {news_sentiment:+.2f}:
- Above 0.5: Confirms bullish direction
- Below -0.5: Confirms bearish direction
- Between -0.3 and 0.3: Ignore news signal

COT {cot_bias}:
- Use to confirm or deny direction
- Never trade against strong COT signal

════════════════════════════════════════
MANDATORY OUTPUT
════════════════════════════════════════

Return ONLY valid JSON:
{{
  "strategy_id": "S01_BREAKOUT_RETEST",
  "strategy_name": "Strategy full name",
  "strategy_met": true,
  "non_negotiables_met": [
    "list each non-negotiable that is confirmed"
  ],
  "non_negotiables_failed": [
    "list any that are NOT met (empty if all met)"
  ],
  "verdict": "STRONG BUY|BUY|SELL|STRONG SELL",
  "direction": "LONG|SHORT",
  "confidence": "HIGH|MEDIUM|LOW",
  "conviction_score": 5,
  "zone_pct": {zone_pct},
  "zone_label": "{zone_label}",
  "htf_bias": "BULLISH|BEARISH|NEUTRAL",
  "entry": {price},
  "entry_reasoning": "exactly why entry here",
  "stop_loss": 0.0,
  "stop_reasoning": "structural level that invalidates",
  "tp1": 0.0,
  "tp2": 0.0,
  "tp3": 0.0,
  "tp_target": "TP1|TP2|TP3",
  "rr_ratio": "1:2.00",
  "trailing_plan": "how to trail after each TP",
  "signals_used": ["UPPERCASE_SIGNAL_NAMES"],
  "confluences": ["confirming factors"],
  "conflicts": ["opposing factors"],
  "intelligence_summary": "one line from briefing",
  "reasoning": "step by step using framework above"
}}

CRITICAL RULES:
1. direction must be LONG or SHORT always
2. strategy_id must match one of the 12
   or S00_BEST_AVAILABLE
3. All signal names UPPERCASE
4. Stop must be at structural level not
   just ATR from entry
5. Minimum R/R 2.0 - adjust TPs if needed
6. Be honest about confidence level
"""

        client = _client()
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _message_text(resp)
        parsed = _parse_json_response(raw)
        ai: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

        direction_raw = str(ai.get("direction", "")).strip().upper()
        if direction_raw not in ("LONG", "SHORT"):
            ema200 = float(ind.get("ema200", price) or price)
            if float(price) > ema200:
                direction_raw = "LONG"
                ai["direction"] = "LONG"
                ai["verdict"] = "BUY"
            else:
                direction_raw = "SHORT"
                ai["direction"] = "SHORT"
                ai["verdict"] = "SELL"
            ai["confidence"] = "LOW"
            prev_r = str(ai.get("reasoning", "") or "")
            forced_note = " [FORCED: system requires trade on every analysis]"
            ai["reasoning"] = prev_r + forced_note if forced_note not in prev_r else prev_r
            log(f"[Loop] FORCED trade direction: {direction_raw}", level="info")

        direction = str(ai.get("direction", direction_raw)).strip().upper()
        if direction not in ("LONG", "SHORT"):
            direction = direction_raw
        ai["direction"] = direction

        confidence = str(ai.get("confidence", "MEDIUM")).strip().upper()
        if confidence not in ("HIGH", "MEDIUM", "LOW"):
            confidence = "MEDIUM"
        confidence = _apply_exotic_confidence(confidence, is_exotic)
        ai["confidence"] = confidence

        try:
            conviction_score = int(round(float(ai.get("conviction_score", 5))))
            conviction_score = max(1, min(10, conviction_score))
        except (TypeError, ValueError):
            conviction_score = 5
        ai["conviction_score"] = conviction_score
        strategy_met = bool(ai.get("strategy_met", False))
        sid_raw = str(ai.get("strategy_id", "") or "").strip().upper()
        if sid_raw in STRATEGIES or sid_raw == "S00_BEST_AVAILABLE":
            strategy_id_norm = sid_raw
        else:
            strategy_id_norm = "S00_BEST_AVAILABLE"
        ai["strategy_id"] = strategy_id_norm

        current_capital = STARTING_CAPITAL

        entry = float(ai.get("entry", price) or price)
        if not math.isfinite(entry) or entry <= 0:
            entry = float(price)
        atr = float(ind.get("atr") or 0) or (abs(float(entry)) * 0.01)

        def _nz(x: Any) -> float:
            try:
                v = float(x or 0)
                return v if math.isfinite(v) else 0.0
            except (TypeError, ValueError):
                return 0.0

        if _nz(ai.get("stop_loss")) == 0:
            if direction == "LONG":
                ai["stop_loss"] = round(entry - (atr * 1.0), 5)
            else:
                ai["stop_loss"] = round(entry + (atr * 1.0), 5)

        stop = validate_stop_loss(entry, _nz(ai.get("stop_loss")), direction, tf_key)
        ai["stop_loss"] = stop

        if direction == "LONG":
            risk = entry - stop
            if risk <= 0:
                stop = validate_stop_loss(
                    entry, round(entry - max(atr * 1.5, entry * 0.001), 5), direction, tf_key
                )
                ai["stop_loss"] = stop
                risk = entry - stop
        else:
            risk = stop - entry
            if risk <= 0:
                stop = validate_stop_loss(
                    entry, round(entry + max(atr * 1.5, entry * 0.001), 5), direction, tf_key
                )
                ai["stop_loss"] = stop
                risk = stop - entry

        if risk <= 0:
            risk = max(atr * 1.5, entry * 0.001)
            if direction == "LONG":
                stop = round(entry - risk, 5)
            else:
                stop = round(entry + risk, 5)
            stop = validate_stop_loss(entry, stop, direction, tf_key)
            ai["stop_loss"] = stop
            if direction == "LONG":
                risk = entry - stop
            else:
                risk = stop - entry

        # Minimum stop distance = 1.5x ATR (Claude often places stops too tight)
        stop = float(ai["stop_loss"])
        if direction == "LONG":
            min_stop = entry - (atr * 1.5)
            if stop > min_stop:
                stop = min_stop
                log(f"[SL] Widened stop to 1.5x ATR: {stop:.5f}")
        else:
            max_stop = entry + (atr * 1.5)
            if stop < max_stop:
                stop = max_stop
                log(f"[SL] Widened stop to 1.5x ATR: {stop:.5f}")
        stop = validate_stop_loss(entry, round(stop, 5), direction, tf_key)
        ai["stop_loss"] = stop
        if direction == "LONG":
            risk = entry - stop
        else:
            risk = stop - entry

        if direction == "LONG":
            ai["tp1"] = round(entry + risk * 2.0, 5)
            ai["tp2"] = round(entry + risk * 3.0, 5)
            ai["tp3"] = round(entry + risk * 5.0, 5)
            rew = abs(float(ai["tp1"]) - entry)
        else:
            ai["tp1"] = round(entry - risk * 2.0, 5)
            ai["tp2"] = round(entry - risk * 3.0, 5)
            ai["tp3"] = round(entry - risk * 5.0, 5)
            rew = abs(entry - float(ai["tp1"]))

        if risk > 0:
            ai["rr_ratio"] = f"1:{(rew / risk):.2f}"
        elif not str(ai.get("rr_ratio", "")).strip():
            ai["rr_ratio"] = "1:2.00"

        stop = float(ai["stop_loss"])
        tp1 = float(ai["tp1"])
        tp2 = float(ai["tp2"])
        tp3 = float(ai["tp3"])

        risk_pct_of_price = risk / entry if entry > 0 else 0.0
        sizing = calculate_position_size(
            confidence,
            zone_pct,
            strategy_met,
            conviction_score,
            current_capital,
        )
        max_risk_dollars = sizing["max_risk_dollars"]
        sizing_risk_pct = sizing["risk_pct"]

        denom = risk_pct_of_price * LEVERAGE
        if denom > 1e-12:
            position_size = min(max_risk_dollars / denom, 500.0)
        else:
            position_size = min(500.0, max_risk_dollars * 10.0)
        leveraged_exposure = position_size * LEVERAGE
        risk_pct_display = round(risk_pct_of_price * 100, 3)

        fut = future.head(fwd_n)
        if fut.empty or len(fut) == 0:
            return None

        exit_data = evaluate_forward_candles(
            direction,
            entry,
            stop,
            tp1,
            tp2,
            tp3,
            fut,
            strategy_id_norm,
        )
        if exit_data.get("outcome") in ("NO_DATA", "INVALID"):
            return None

        hit_tp1 = bool(exit_data.get("hit_tp1"))
        hit_tp2 = bool(exit_data.get("hit_tp2"))
        hit_tp3 = bool(exit_data.get("hit_tp3"))
        hit_stop = bool(exit_data.get("hit_stop"))
        exit_p = float(exit_data.get("exit_price", entry))
        exit_r = str(exit_data.get("exit_reason", ""))
        candles_to_exit = int(exit_data.get("candles_to_exit", 0) or 0)
        trailing_activated = bool(exit_data.get("trailing_activated", False))
        final_stop = float(exit_data.get("final_stop", stop) or stop)

        raw_pct = float(exit_data.get("pnl_pct", 0) or 0)
        outcome = str(exit_data.get("outcome", "LOSS"))
        if outcome not in ("WIN", "LOSS"):
            outcome = "WIN" if raw_pct > 0 else "LOSS"
        correct = outcome == "WIN"

        pnl_dollars = round(leveraged_exposure * raw_pct, 2)
        pnl_pct_display = round(raw_pct * 100, 2)

        log(
            f"[Backtest] PnL calc: entry={entry} exit={exit_p} direction={direction} "
            f"raw_pct={raw_pct:.4f} exposure={leveraged_exposure:.2f} pnl={pnl_dollars:.2f} "
            f"pos_size={position_size:.2f} max_risk={max_risk_dollars:.2f}",
            level="info",
        )

        try:
            z_ai = float(ai.get("zone_position_pct", ai.get("zone_pct", zone_pct)))
            if not math.isfinite(z_ai):
                z_ai = zone_pct
            zone_pos_stored = max(0.0, min(100.0, round(z_ai, 1)))
        except (TypeError, ValueError):
            zone_pos_stored = zone_pct

        tgt = str(ai.get("tp_target", "TP1") or "TP1").strip().upper()
        if tgt not in ("TP1", "TP2", "TP3"):
            tgt = "TP1"

        pz_ai = str(ai.get("price_zone") or "").strip().upper()
        ai_zone_lbl = str(ai.get("zone_label") or "").strip()
        coarse_src = ai_zone_lbl or zone_label
        ul = coarse_src.upper()
        if pz_ai in ("PREMIUM", "DISCOUNT", "EQUILIBRIUM"):
            price_zone_stored = pz_ai
        elif "DISCOUNT" in ul:
            price_zone_stored = "DISCOUNT"
        elif "PREMIUM" in ul:
            price_zone_stored = "PREMIUM"
        else:
            price_zone_stored = "EQUILIBRIUM"

        try:
            conf_pts = int(round(float(ai.get("confluence_points", 0) or 0)))
        except (TypeError, ValueError):
            conf_pts = 0

        def _upper_signal_list(raw: Any) -> list[str]:
            if not isinstance(raw, list):
                return []
            out: list[str] = []
            for x in raw:
                s = str(x).strip().upper().replace(" ", "_")
                if s:
                    out.append(s)
            return out

        ai["signals_used"] = _upper_signal_list(ai.get("signals_used"))
        ai["confluences"] = _upper_signal_list(ai.get("confluences"))
        ai["conflicts"] = _upper_signal_list(ai.get("conflicts"))

        intel_summary_stored = str(
            ai.get("intelligence_summary") or ai.get("intel_summary") or "",
        )[:500]

        return {
            "date": analysis_date,
            "ticker": sym,
            "timeframe": timeframe,
            "verdict": ai.get("verdict"),
            "direction": direction,
            "confidence": confidence,
            "htf_bias": str(ai.get("htf_bias") or ""),
            "market_structure": str(ai.get("market_structure") or ""),
            "zone_pct": zone_pct,
            "zone_label": zone_label,
            "zone_label_model": ai_zone_lbl or None,
            "price_zone": price_zone_stored,
            "zone_position_pct": zone_pos_stored,
            "smc_concept": str(ai.get("smc_concept") or "NONE"),
            "smc_direction": str(ai.get("smc_direction") or ""),
            "tp_target": tgt,
            "is_exotic": is_exotic,
            "confluence_points": conf_pts,
            "zone_reasoning": str(ai.get("zone_reasoning") or ""),
            "smc_reasoning": str(ai.get("smc_reasoning") or ""),
            "exit_reasoning": str(ai.get("exit_reasoning") or ""),
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
            "position_size": round(position_size, 2),
            "leveraged_exposure": round(leveraged_exposure, 2),
            "max_risk_dollars": max_risk_dollars,
            "account_risk_pct": sizing_risk_pct,
            "risk_pct_of_price": risk_pct_display,
            "hit_tp1": hit_tp1,
            "hit_tp2": hit_tp2,
            "hit_tp3": hit_tp3,
            "hit_stop": hit_stop,
            "candles_to_exit": candles_to_exit,
            "signals_used": ai.get("signals_used") if isinstance(ai.get("signals_used"), list) else [],
            "confluences": ai.get("confluences") if isinstance(ai.get("confluences"), list) else [],
            "conflicts": ai.get("conflicts") if isinstance(ai.get("conflicts"), list) else [],
            "reasoning": str(ai.get("reasoning", "")),
            "rr_ratio": str(ai.get("rr_ratio", "")),
            "conviction_score": conviction_score,
            "intel_summary": intel_summary_stored,
            "strategy_id": strategy_id_norm,
            "strategy_name": str(ai.get("strategy_name", "") or ""),
            "strategy_met": strategy_met,
            "non_negotiables_met": ai.get("non_negotiables_met")
            if isinstance(ai.get("non_negotiables_met"), list)
            else [],
            "non_negotiables_failed": ai.get("non_negotiables_failed")
            if isinstance(ai.get("non_negotiables_failed"), list)
            else [],
            "trailing_activated": trailing_activated,
            "final_stop": round(final_stop, 5),
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

    rr_bins = {"lt_0_8": 0, "0_8_to_1_2": 0, "gt_1_2": 0, "unknown": 0}
    rr_vals: list[float] = []
    for t in trades:
        v = _parse_rr_ratio_numeric(t.get("rr_ratio"))
        if v is None:
            rr_bins["unknown"] += 1
            continue
        rr_vals.append(v)
        if v < 0.8:
            rr_bins["lt_0_8"] += 1
        elif v <= 1.2:
            rr_bins["0_8_to_1_2"] += 1
        else:
            rr_bins["gt_1_2"] += 1

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


def _strategy_performance_stats(completed: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-strategy win rate and PnL for completed WIN/LOSS rows."""
    strategy_stats: dict[str, dict[str, Any]] = {}
    for sid, sdef in STRATEGIES.items():
        s_trades = [t for t in completed if str(t.get("strategy_id", "") or "").upper() == sid]
        if not s_trades:
            continue
        s_wins = [t for t in s_trades if t.get("outcome") == "WIN"]
        s_losses = [t for t in s_trades if t.get("outcome") == "LOSS"]
        nl = max(1, len(s_trades) - len(s_wins))
        strategy_stats[sid] = {
            "name": sdef["name"],
            "category": sdef["category"],
            "total": len(s_trades),
            "wins": len(s_wins),
            "losses": len(s_trades) - len(s_wins),
            "win_rate": round(len(s_wins) / len(s_trades) * 100, 1),
            "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in s_trades), 2),
            "avg_win": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in s_wins) / max(1, len(s_wins)),
                2,
            ),
            "avg_loss": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in s_losses) / nl,
                2,
            ),
        }
    s00 = [t for t in completed if str(t.get("strategy_id", "") or "").upper() == "S00_BEST_AVAILABLE"]
    if s00:
        s_w = [t for t in s00 if t.get("outcome") == "WIN"]
        s_l = [t for t in s00 if t.get("outcome") == "LOSS"]
        n0 = max(1, len(s00) - len(s_w))
        strategy_stats["S00_BEST_AVAILABLE"] = {
            "name": "Best Available",
            "category": "FALLBACK",
            "total": len(s00),
            "wins": len(s_w),
            "losses": len(s00) - len(s_w),
            "win_rate": round(len(s_w) / len(s00) * 100, 1),
            "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in s00), 2),
            "avg_win": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in s_w) / max(1, len(s_w)),
                2,
            ),
            "avg_loss": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in s_l) / n0,
                2,
            ),
        }
    return strategy_stats


def calculate_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    row_list = [r for r in (results or []) if isinstance(r, dict)]
    n_all = len(row_list)
    hard_filtered_n = sum(1 for r in row_list if str(r.get("verdict", "")).upper() == "FILTERED")
    eligible_after_hard = max(0, n_all - hard_filtered_n)

    trades = [r for r in row_list if not r.get("skipped")]
    executed_trade_rate_pct = (
        round(100 * len(trades) / max(1, eligible_after_hard), 1) if eligible_after_hard else 0.0
    )
    execution_meta: dict[str, Any] = {
        "total_saved_backtests": n_all,
        "hard_filtered_backtests": hard_filtered_n,
        "eligible_after_hard_filter": eligible_after_hard,
        "executed_trade_rows": len(trades),
        "executed_trade_rate_pct_of_eligible": executed_trade_rate_pct,
    }

    if not trades:
        k0 = calculate_kelly([])
        smc_empty = _smc_stats_from_trades([])
        dash_empty = _tp_target_and_smc_dashboard([])
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
            "excluded_tickers": HARD_EXCLUDED_TICKERS,
            "banned_signals": list(BANNED_SIGNALS),
            **execution_meta,
            **smc_empty,
            **dash_empty,
            "strategy_performance": {},
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
    dash = _tp_target_and_smc_dashboard(trades)
    completed_for_strat = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    strat_perf = _strategy_performance_stats(completed_for_strat)

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
        "excluded_tickers": HARD_EXCLUDED_TICKERS,
        "banned_signals": list(BANNED_SIGNALS),
        **execution_meta,
        **smc_agg,
        **dash,
        "strategy_performance": strat_perf,
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
                time.sleep(15)
                continue

            if not env("ANTHROPIC_API_KEY"):
                log("[Loop] ANTHROPIC_API_KEY missing — sleeping", level="warning")
                time.sleep(10)
                continue

            ticker = pick_backtest_ticker()
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
            time.sleep(5)
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
