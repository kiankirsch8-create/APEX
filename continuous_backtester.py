"""
Continuous autonomous backtesting loop: random historical setups, forward outcomes,
rolling stats, and periodic self-improvement. All state is simple JSON on ``DATA_DIR``
(default ``/data`` on Railway with a volume; falls back to ``./results`` locally).
"""
from __future__ import annotations

STRATEGY_VERSION = "v3.0-definitive"
# Built from 300+ backtested trades — May 2026
#
# Baseline performance this was built from:
#   135 trades: 48.9% WR, +$1,606, 4.4% max drawdown
#   149 trades: 51.4% WR, +$1,991, 3.1% max drawdown
#
# RULES THAT MUST NEVER BE CHANGED:
#   S04 never on 1H or 4H (proven 20%/37% WR losers)
#   S08 never on 4H (proven 25% WR loser)
#   strategy_met must be True to trade (no fallback trades)
#   No weekly bias filter (blocked valid S04 extreme trades)
#   Position sizing: HIGH=2%, MEDIUM=1%, LOW=0.5%
#   Trailing: breakeven at TP1, +1R at TP2, 1.5R trail after TP3
#   Max conviction score: 8 (lesson from -$300 loss at HIGH conf)
#
# TO ADD NEW STRATEGIES: create S19+, add to STRATEGIES dict,
# give them LOW confidence until 30+ trades of data exist.
# Never rewrite enforcement rules, position sizing, or trailing.

import json
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401

from utils import DATA_DIR, env, load_json, log, save_json, utcnow_iso

MODEL = "claude-sonnet-4-5-20250929"
CLAUDE_MODEL = MODEL

RESULTS_FILE = DATA_DIR / "backtest_results.json"
STATS_FILE = DATA_DIR / "backtest_stats.json"
LEARNED_FILE = DATA_DIR / "learned_weights.json"
STATE_FILE = DATA_DIR / "backtest_state.json"
ENABLED_FILE = DATA_DIR / "backtest_enabled.json"
IMPROVING_FILE = DATA_DIR / "improving.json"
IMPROVE_DEBUG_FILE = DATA_DIR / "improve_debug.json"

STARTING_CAPITAL = 10000.0
LEVERAGE = 50
IMPROVE_EVERY = 100

# Parallel backtest loop (speed)
MAX_WORKERS = 8
BATCH_SIZE = 25
BACKTEST_CLAUDE_MAX_TOKENS = 1200
CLAUDE_HTTP_TIMEOUT_SEC = 25.0

RISK_BY_CONFIDENCE: dict[str, float] = {
    "HIGH": 0.020,
    "MEDIUM": 0.010,
    "LOW": 0.005,
}
POSITION_SIZE_PCT = RISK_BY_CONFIDENCE["MEDIUM"]

MAX_STOP_PCT: dict[str, float] = {
    "1w": 2.5,
    "1d": 1.5,
    "4h": 1.0,
    "1h": 0.5,
    "30m": 0.3,
    "15m": 0.2,
}

TF_WEIGHTS: dict[str, float] = {
    "1w": 0.20,
    "1d": 0.50,
    "4h": 0.20,
    "1h": 0.05,
    "30m": 0.03,
    "15m": 0.02,
}

ALLOWED_1H_STRATEGIES: frozenset[str] = frozenset(
    {
        "S11_SR_FLIP",
        "S02_LIQUIDITY_SWEEP",
        "S17_LONDON_OPEN_BREAKOUT",
    }
)

TIMEFRAMES: list[str] = ["15m", "30m", "1h", "4h", "1d", "1w"]

TF_FORWARD_CANDLES: dict[str, int] = {
    "15m": 96,
    "30m": 72,
    "1h": 48,
    "4h": 30,
    "1d": 20,
    "1w": 8,
}

TF_MAX_STOP_PCT: dict[str, float] = {
    "15m": MAX_STOP_PCT["15m"] / 100.0,
    "30m": MAX_STOP_PCT["30m"] / 100.0,
    "1h": MAX_STOP_PCT["1h"] / 100.0,
    "4h": MAX_STOP_PCT["4h"] / 100.0,
    "1d": MAX_STOP_PCT["1d"] / 100.0,
    "1w": MAX_STOP_PCT["1w"] / 100.0,
}

TF_MAX_TP_PCT: dict[str, float] = {
    "15m": 0.006,
    "30m": 0.010,
    "1h": 0.012,
    "4h": 0.020,
    "1d": 0.030,
    "1w": 0.060,
}

TF_DESCRIPTIONS: dict[str, str] = {
    "15m": "15M — intraday S13–S18; LOW confidence; news blackout aware",
    "30m": "30M — intraday S13–S18; LOW confidence",
    "1h": "1H — 23% WR; only S11 / S02 / S17 (enforced)",
    "4h": "4H — S03 primary; no S04/S08 on 4H",
    "1d": "1D — primary swing timeframe",
    "1w": "1W — highest WR swing; S04 priority on extremes",
}

FORWARD_CANDLES = TF_FORWARD_CANDLES.get("4h", 30)

_PROMPT_V3_FILE = Path(__file__).resolve().parent / "prompts" / "apex_master_v3.txt"

STRATEGIES: dict[str, dict[str, Any]] = {
    "S03_EMA_PULLBACK": {
        "name": "EMA Trend Pullback",
        "category": "TREND_FOLLOWING",
        "proven_wr": 57.7,
        "best_tf": ["4h", "1w"],
        "blocked_tf": [],
    },
    "S04_EXTREME_REVERSION": {
        "name": "Extreme Zone Reversion",
        "category": "MEAN_REVERSION",
        "proven_wr": 66.7,
        "best_tf": ["1d", "1w"],
        "blocked_tf": ["1h", "4h"],
    },
    "S11_SR_FLIP": {
        "name": "Support Resistance Flip",
        "category": "STRUCTURE",
        "proven_wr": 100.0,
        "best_tf": ["1h", "4h", "1d"],
        "blocked_tf": [],
    },
    "S01_BREAKOUT_RETEST": {
        "name": "Breakout Retest",
        "category": "TREND_FOLLOWING",
        "proven_wr": None,
        "best_tf": ["4h", "1d"],
        "blocked_tf": [],
    },
    "S02_LIQUIDITY_SWEEP": {
        "name": "Liquidity Sweep Reversal",
        "category": "SMART_MONEY",
        "proven_wr": None,
        "best_tf": ["1h", "4h"],
        "blocked_tf": [],
    },
    "S05_MACD_DIVERGENCE": {
        "name": "MACD Divergence",
        "category": "MOMENTUM",
        "proven_wr": None,
        "best_tf": ["4h", "1d"],
        "blocked_tf": [],
    },
    "S06_ORDER_BLOCK": {
        "name": "Institutional Order Block",
        "category": "SMART_MONEY",
        "proven_wr": None,
        "best_tf": ["4h", "1d"],
        "blocked_tf": [],
    },
    "S07_FAIR_VALUE_GAP": {
        "name": "Fair Value Gap Fill",
        "category": "SMART_MONEY",
        "proven_wr": None,
        "best_tf": ["4h", "1d"],
        "blocked_tf": [],
    },
    "S08_RANGE_BREAKOUT": {
        "name": "Range Breakout",
        "category": "BREAKOUT",
        "proven_wr": 44.4,
        "best_tf": ["1d"],
        "blocked_tf": ["4h"],
    },
    "S12_VOLATILITY_COMPRESSION": {
        "name": "Volatility Compression",
        "category": "BREAKOUT",
        "proven_wr": None,
        "best_tf": ["1d"],
        "blocked_tf": [],
    },
    "S13_NEWS_MOMENTUM": {
        "name": "News Momentum",
        "category": "NEWS_DRIVEN",
        "proven_wr": None,
        "best_tf": ["15m", "30m"],
        "blocked_tf": ["1h", "4h", "1d", "1w"],
    },
    "S14_OPENING_RANGE_BREAKOUT": {
        "name": "Opening Range Breakout",
        "category": "INTRADAY",
        "proven_wr": None,
        "best_tf": ["15m", "30m"],
        "blocked_tf": ["1h", "4h", "1d", "1w"],
    },
    "S15_VWAP_DEVIATION": {
        "name": "VWAP Deviation Reversion",
        "category": "INTRADAY",
        "proven_wr": None,
        "best_tf": ["15m", "30m"],
        "blocked_tf": ["1h", "4h", "1d", "1w"],
    },
    "S16_HTF_LEVEL_REJECTION": {
        "name": "HTF Level Rejection",
        "category": "INTRADAY",
        "proven_wr": None,
        "best_tf": ["15m", "30m"],
        "blocked_tf": ["1h", "4h", "1d", "1w"],
    },
    "S17_LONDON_OPEN_BREAKOUT": {
        "name": "London Open Breakout",
        "category": "INTRADAY",
        "proven_wr": None,
        "best_tf": ["15m", "30m", "1h"],
        "blocked_tf": ["4h", "1d", "1w"],
    },
    "S18_NY_OPEN_MOMENTUM": {
        "name": "NY Open Momentum",
        "category": "INTRADAY",
        "proven_wr": None,
        "best_tf": ["15m", "30m"],
        "blocked_tf": ["1h", "4h", "1d", "1w"],
    },
}

EXCLUDED_PAIRS = frozenset(
    {
        "AUDCAD",
        "AUDCHF",
        "AUDNZD",
        "CADCHF",
        "CHFJPY",
        "EURCAD",
        "EURCHF",
        "EURJPY",
        "GBPAUD",
        "GBPCHF",
        "NZDCHF",
        "NZDJPY",
        "USDCHF",
        "USDCZK",
        "USDDKK",
        "USDHKD",
        "USDHUF",
        "USDPLN",
        "USDSGD",
        "USDTRY",
    }
)

PRIORITY_ORDER: list[str] = [
    "EURNZD",
    "NZDCAD",
    "GBPNZD",
    "EURAUD",
    "CADJPY",
    "EURUSD",
    "AUDUSD",
    "USDCAD",
    "EURGBP",
    "USDSEK",
    "USDNOK",
    "USDMXN",
    "GBPJPY",
    "USDJPY",
    "NZDUSD",
    "USDZAR",
    "AUDJPY",
    "GBPUSD",
]
PRIORITY_PAIRS = frozenset(PRIORITY_ORDER)

HARD_EXCLUDED_TICKERS = sorted(EXCLUDED_PAIRS)

EXOTIC_REDUCE: frozenset[str] = frozenset()

BANNED_SIGNALS: list[str] = []

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
    """Prefer PRIORITY pairs among symbols not in EXCLUDED_PAIRS."""
    eligible = [t for t in BACKTEST_TICKERS if t not in EXCLUDED_PAIRS]
    if not eligible:
        return "EURUSD"
    priority = [t for t in PRIORITY_ORDER if t in eligible]
    other = [t for t in eligible if t not in PRIORITY_PAIRS]
    if priority and random.random() < 0.65:
        return random.choice(priority)
    return random.choice(other or eligible)


def eligible_backtest_tickers() -> list[str]:
    """All symbols used for rolling batch construction (excludes hard-filtered pairs)."""
    return [t for t in BACKTEST_TICKERS if t not in EXCLUDED_PAIRS]


def analyse_one_backtest(ticker: str, timeframe: str, analysis_date: str) -> dict[str, Any] | None:
    """One full backtest job (safe for ThreadPoolExecutor workers)."""
    try:
        return run_one_backtest(ticker, timeframe, analysis_date)
    except Exception as e:  # noqa: BLE001
        log(f"[Error] {ticker} {timeframe}: {e}", level="warning")
        log(traceback.format_exc(), level="warning")
        return None


_backtest_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_results_lock = threading.Lock()
_loop_counters_lock = threading.Lock()


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
    return Anthropic(api_key=key, timeout=CLAUDE_HTTP_TIMEOUT_SEC)


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
        "15m": {"interval": "15m", "days_back": 60, "days_fwd": 7},
        "30m": {"interval": "30m", "days_back": 120, "days_fwd": 14},
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
    """R/R validation disabled — model plan passes through."""
    return plan


def near_major_news_calendar(_sym: str, _analysis_date: str) -> bool:
    """Reserved hook — Finnhub calendar handled in ``market_intelligence.is_news_blackout``."""
    return False


def _load_master_prompt_v3(
    *,
    ticker: str,
    tf_label: str,
    analysis_date: str,
    price: float,
    zone_label: str,
    zone_pct: float,
    high_52w: float,
    low_52w: float,
    ind: dict[str, Any],
    bb_width: float,
    intel_text: str,
    news_sentiment: float,
    cot_bias: str,
    fear_greed: float,
    vix_val: float,
) -> str:
    tpl = _PROMPT_V3_FILE.read_text(encoding="utf-8")
    return tpl.format(
        ticker=ticker,
        tf_label=tf_label,
        analysis_date=analysis_date,
        price=price,
        zone_label=zone_label,
        zone_pct=zone_pct,
        high_52w=high_52w,
        low_52w=low_52w,
        rsi=float(ind.get("rsi", 50) or 50),
        macd_hist=float(ind.get("macd_hist", 0) or 0),
        macd_line=float(ind.get("macd_line", 0) or 0),
        macd_sig=float(ind.get("macd_signal", 0) or 0),
        ema20=float(ind.get("ema20", price) or price),
        ema50=float(ind.get("ema50", price) or price),
        ema200=float(ind.get("ema200", price) or price),
        atr=float(ind.get("atr", 0) or 0),
        adx=float(ind.get("adx", 0) or 0),
        bb_width=bb_width,
        bb_upper=float(ind.get("bb_upper", price) or price),
        bb_lower=float(ind.get("bb_lower", price) or price),
        swing_highs=ind.get("swing_highs", []),
        swing_lows=ind.get("swing_lows", []),
        intel_text=intel_text,
        news_sentiment=news_sentiment,
        cot_bias=cot_bias,
        fear_greed=fear_greed,
        vix_val=vix_val,
    )


def enforce_rules(ai: dict[str, Any], timeframe: str, price: float, ticker: str) -> dict[str, Any]:
    """Post-parse enforcement — cannot be overridden by model text."""
    tf = (timeframe or "").strip().lower()
    strategy_id = str(ai.get("strategy_id") or "SKIP").strip().upper()
    direction = str(ai.get("direction", "NONE")).strip().upper()
    try:
        entry = float(ai.get("entry", price) or price)
    except (TypeError, ValueError):
        entry = float(price)
    try:
        stop = float(ai.get("stop_loss", 0) or 0)
    except (TypeError, ValueError):
        stop = 0.0
    try:
        zone_pct = float(ai.get("zone_pct", 50) or 50)
    except (TypeError, ValueError):
        zone_pct = 50.0
    strategy_met = bool(ai.get("strategy_met", False))

    if ai.get("skip_trade", False):
        return ai

    if not strategy_met:
        ai["skip_trade"] = True
        ai["skip_reason"] = "strategy_met=False — no fallback trades allowed"
        return ai

    if strategy_id == "S04_EXTREME_REVERSION":
        if tf in ("1h", "4h"):
            ai["skip_trade"] = True
            ai["skip_reason"] = f"S04 blocked on {tf}: proven loser (4H=37%, 1H=20%)"
            return ai
        if not (zone_pct <= 15 or zone_pct >= 85):
            ai["skip_trade"] = True
            ai["skip_reason"] = (
                f"S04 zone {zone_pct:.1f}% not extreme — need below 15 or above 85"
            )
            return ai

    if strategy_id == "S08_RANGE_BREAKOUT" and tf == "4h":
        ai["skip_trade"] = True
        ai["skip_reason"] = "S08 blocked on 4H — proven 25% WR"
        return ai

    if tf == "1h":
        allowed_1h = ("S11_SR_FLIP", "S02_LIQUIDITY_SWEEP", "S17_LONDON_OPEN_BREAKOUT")
        if strategy_id not in allowed_1h:
            ai["skip_trade"] = True
            ai["skip_reason"] = f"1H blocked: {strategy_id} not allowed (1H overall 23% WR)"
            return ai
        ai["confidence"] = "LOW"

    intraday_only = (
        "S13_NEWS_MOMENTUM",
        "S14_OPENING_RANGE_BREAKOUT",
        "S15_VWAP_DEVIATION",
        "S16_HTF_LEVEL_REJECTION",
        "S18_NY_OPEN_MOMENTUM",
    )
    if strategy_id in intraday_only and tf not in ("15m", "30m"):
        ai["skip_trade"] = True
        ai["skip_reason"] = f"{strategy_id} only on 15m/30m"
        return ai

    if strategy_id == "S17_LONDON_OPEN_BREAKOUT" and tf not in ("15m", "30m", "1h"):
        ai["skip_trade"] = True
        ai["skip_reason"] = "S17 only on 15m/30m/1h"
        return ai

    try:
        ai["conviction_score"] = min(int(round(float(ai.get("conviction_score", 5)))), 8)
    except (TypeError, ValueError):
        ai["conviction_score"] = 5

    if stop and entry and abs(stop - entry) > 1e-12:
        stop_dist_pct = abs(entry - stop) / entry * 100.0
        max_stop = float(MAX_STOP_PCT.get(tf, 1.5))
        if stop_dist_pct > max_stop + 1e-9:
            log(f"[StopFix] {ticker} stop {stop_dist_pct:.2f}% capped at {max_stop}%", level="info")
            mult = 1 if direction == "LONG" else -1
            new_stop = entry * (1.0 - mult * max_stop / 100.0)
            risk = abs(entry - new_stop)
            ai["stop_loss"] = round(new_stop, 5)
            ai["tp1"] = round(entry + mult * risk * 2.0, 5)
            ai["tp2"] = round(entry + mult * risk * 3.0, 5)
            ai["tp3"] = round(entry + mult * risk * 5.0, 5)

    conf = str(ai.get("confidence", "LOW")).strip().upper()
    if conf not in RISK_BY_CONFIDENCE:
        conf = "LOW"
    risk_pct = float(RISK_BY_CONFIDENCE.get(conf, 0.005))
    risk_dollars = STARTING_CAPITAL * risk_pct
    try:
        entry = float(ai.get("entry", price) or price)
        stop = float(ai.get("stop_loss", 0) or 0)
    except (TypeError, ValueError):
        entry, stop = float(price), 0.0
    stop_dist = abs(entry - stop)
    if stop_dist > 0:
        pos_size = risk_dollars / stop_dist
        exposure = pos_size * entry
        if exposure < 1500:
            pos_size = 1500.0 / entry
        ai["_position_size"] = round(pos_size, 2)
        ai["_leveraged_exposure"] = round(pos_size * entry, 2)
        ai["_max_risk_dollars"] = round(risk_dollars, 2)
        ai["_account_risk_pct"] = risk_pct

    return ai


def _sanitize_signal_lists(ai: dict[str, Any]) -> None:
    """Strip toxic signal tokens from model output (Section 1A)."""
    banned = frozenset({"ADX_TRENDING", "RSI_NEUTRAL_ROOM"})

    def _clean(seq: Any) -> list[str]:
        if not isinstance(seq, list):
            return []
        out: list[str] = []
        for x in seq:
            s = str(x).strip().upper().replace(" ", "_")
            if not s or s in banned:
                continue
            out.append(s)
        return out

    ai["signals_used"] = _clean(ai.get("signals_used"))
    ai["confluences"] = _clean(ai.get("confluences"))
    ai["conflicts"] = _clean(ai.get("conflicts"))


def calculate_position_size(
    confidence: str,
    zone_pct: float,
    strategy_met: bool,
    conviction_score: int,
    account_balance: float,
) -> dict[str, Any]:
    """Legacy sizing helper — main backtest path uses ``enforce_rules`` position fields."""
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
    tf = timeframe.lower().strip()
    max_pct = float(MAX_STOP_PCT.get(tf, 1.5)) / 100.0
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
    strategy_id: str = "",
) -> dict[str, Any]:
    """Trailing: breakeven at TP1, +1R at TP2, 1.5R trail from close after TP3 (master v3)."""
    _ = strategy_id
    if forward_df is None or forward_df.empty:
        return {
            "outcome": "NO_DATA",
            "exit_price": entry,
            "exit_reason": "No forward data",
            "pnl_pct": 0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_stop": False,
            "candles_to_exit": 0,
            "trailing_activated": False,
            "final_stop": stop_loss,
        }

    risk = abs(float(entry) - float(stop_loss))
    if risk <= 0:
        return {
            "outcome": "INVALID",
            "exit_price": entry,
            "exit_reason": "Invalid risk",
            "pnl_pct": 0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_stop": False,
            "candles_to_exit": 0,
            "trailing_activated": False,
            "final_stop": stop_loss,
        }

    current_stop = float(stop_loss)
    hit_tp1 = hit_tp2 = hit_tp3 = hit_stop = False
    trailing_activated = False
    exit_price = float(entry)
    exit_reason = "Window ended"
    candle_count = 0
    d = str(direction or "").strip().upper()

    for _, candle in forward_df.iterrows():
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
                exit_reason = "Trailing stop" if trailing_activated else "Stop loss"
                break

            if high >= tp3 and not hit_tp3:
                hit_tp3 = True
                trailing_activated = True
                current_stop = float(entry) + risk * 2.0

            elif high >= tp2 and not hit_tp2:
                hit_tp2 = True
                trailing_activated = True
                current_stop = float(entry) + risk * 1.0

            elif high >= tp1 and not hit_tp1:
                hit_tp1 = True
                trailing_activated = True
                current_stop = float(entry)

            if hit_tp1 and trailing_activated:
                trail_level = close - risk * 1.5
                if trail_level > current_stop:
                    current_stop = trail_level

        else:
            if high >= current_stop:
                hit_stop = True
                exit_price = current_stop
                exit_reason = "Trailing stop" if trailing_activated else "Stop loss"
                break

            if low <= tp3 and not hit_tp3:
                hit_tp3 = True
                trailing_activated = True
                current_stop = float(entry) - risk * 2.0

            elif low <= tp2 and not hit_tp2:
                hit_tp2 = True
                trailing_activated = True
                current_stop = float(entry) - risk * 1.0

            elif low <= tp1 and not hit_tp1:
                hit_tp1 = True
                trailing_activated = True
                current_stop = float(entry)

            if hit_tp1 and trailing_activated:
                trail_level = close + risk * 1.5
                if trail_level < current_stop:
                    current_stop = trail_level

    if not hit_stop and not forward_df.empty:
        try:
            exit_price = float(forward_df["Close"].iloc[-1])
        except (TypeError, ValueError, KeyError, IndexError):
            exit_price = float(entry)

    if d == "LONG":
        pnl_pct = (exit_price - float(entry)) / float(entry) if entry else 0.0
    else:
        pnl_pct = (float(entry) - exit_price) / float(entry) if entry else 0.0

    return {
        "outcome": "WIN" if pnl_pct > 0 else "LOSS",
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


def _skipped_backtest_row(
    *,
    sym: str,
    timeframe: str,
    analysis_date: str,
    price: float,
    zone_pct: float,
    zone_label: str,
    skip_reason: str,
    ai: dict[str, Any],
    tf_key: str,
    is_exotic: bool,
) -> dict[str, Any]:
    """Row persisted when APEX declines to trade (no forward simulation)."""
    sid = str(ai.get("strategy_id") or "SKIP").strip().upper()
    intel_summary_stored = str(
        ai.get("intelligence_summary") or ai.get("intel_summary") or "",
    )[:500]
    core_met = ai.get("core_signals_met") if isinstance(ai.get("core_signals_met"), list) else []
    core_failed = (
        ai.get("core_signals_failed") if isinstance(ai.get("core_signals_failed"), list) else []
    )
    return {
        "date": analysis_date,
        "ticker": sym,
        "timeframe": timeframe,
        "verdict": "SKIP",
        "direction": str(ai.get("direction") or "NONE").strip().upper() or "NONE",
        "confidence": "SKIP",
        "skipped": True,
        "skip_trade": True,
        "skip_reason": skip_reason[:500],
        "outcome": "SKIPPED",
        "pnl_dollars": 0.0,
        "pnl_pct": 0.0,
        "entry_price": round(float(price), 5),
        "stop_loss": 0.0,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "exit_price": round(float(price), 5),
        "exit_reason": "SKIPPED",
        "correct": False,
        "hit_tp1": False,
        "hit_tp2": False,
        "hit_tp3": False,
        "hit_stop": False,
        "candles_to_exit": 0,
        "trailing_activated": False,
        "final_stop": round(float(price), 5),
        "strategy_id": sid or "SKIP",
        "strategy_name": str(ai.get("strategy_name") or ""),
        "strategy_met": bool(ai.get("strategy_met", False)),
        "core_signals_met": core_met,
        "core_signals_failed": core_failed,
        "non_negotiables_met": core_met,
        "non_negotiables_failed": core_failed,
        "reasoning": str(ai.get("reasoning") or "")[:4000],
        "rr_ratio": str(ai.get("rr_ratio") or ""),
        "conviction_score": int(ai.get("conviction_score") or 0),
        "intel_summary": intel_summary_stored,
        "zone_pct": zone_pct,
        "zone_label": zone_label,
        "zone_label_model": str(ai.get("zone_label") or "") or None,
        "price_zone": "EQUILIBRIUM",
        "zone_position_pct": zone_pct,
        "tp_target": "TP1",
        "signals_used": ai.get("signals_used") if isinstance(ai.get("signals_used"), list) else [],
        "confluences": ai.get("confluences") if isinstance(ai.get("confluences"), list) else [],
        "conflicts": ai.get("conflicts") if isinstance(ai.get("conflicts"), list) else [],
        "htf_bias": str(ai.get("htf_bias") or ""),
        "market_structure": str(ai.get("market_structure") or ""),
        "smc_concept": "NONE",
        "smc_direction": "",
        "is_exotic": is_exotic,
        "confluence_points": 0,
        "leverage": LEVERAGE,
        "position_size": 0.0,
        "leveraged_exposure": 0.0,
        "max_risk_dollars": 0.0,
        "account_risk_pct": 0.0,
        "risk_pct_of_price": 0.0,
        "timeframe_restricted": tf_key == "1h",
        "tf_strategy_allowed": tf_key != "1h" or sid in ALLOWED_1H_STRATEGIES,
        "trailing_plan": str(ai.get("trailing_plan") or "")[:500],
    }


def run_one_backtest(ticker: str, timeframe: str, analysis_date: str) -> dict[str, Any] | None:
    try:
        sym = (ticker or "").strip().upper()
        tf_key = timeframe.lower().strip()
        is_forex = len(sym) == 6 and sym.isalpha()
        yf_ticker = sym + "=X" if is_forex else sym

        past, future = get_ohlcv(yf_ticker, tf_key, analysis_date.strip())
        if past is None or future is None or past.empty or future.empty:
            return None
        min_past = (
            30
            if tf_key == "1w"
            else 40
            if tf_key in ("15m", "30m")
            else 50
        )
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
            reason = "Excluded pair (backtest data)"
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
            if zone_pct < 10
            else "DISCOUNT"
            if zone_pct < 30
            else "EQUILIBRIUM"
            if zone_pct < 70
            else "PREMIUM"
            if zone_pct < 90
            else "EXTREME_PREMIUM"
        )

        is_exotic = sym in EXOTIC_REDUCE

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
        macd_sig = macd_signal

        intel_text = ""
        news_sentiment = 0.0
        cot_bias = "UNKNOWN"
        fear_greed = 50.0
        vix_val = 20.0
        try:
            from market_intelligence import (
                format_for_prompt,
                get_complete_briefing,
                is_news_blackout,
            )

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

            if is_news_blackout(30) and tf_key in ("15m", "30m", "1h"):
                log(
                    f"[NewsBlackout] Skipping {sym} — high impact event within 30min",
                    level="info",
                )
                return _skipped_backtest_row(
                    sym=sym,
                    timeframe=timeframe,
                    analysis_date=analysis_date,
                    price=float(price),
                    zone_pct=zone_pct,
                    zone_label=zone_label,
                    skip_reason="News blackout — high impact event within 30min",
                    ai={
                        "skip_trade": True,
                        "strategy_id": "SKIP",
                        "strategy_met": False,
                        "skip_reason": "News blackout — high impact event within 30min",
                        "direction": "NONE",
                        "conviction_score": 0,
                    },
                    tf_key=tf_key,
                    is_exotic=is_exotic,
                )
        except Exception as e:
            log(f"[Intel] {e}")

        tf_label = TF_DESCRIPTIONS.get(tf_key, tf_key)
        prompt = _load_master_prompt_v3(
            ticker=sym,
            tf_label=tf_label,
            analysis_date=analysis_date,
            price=float(price),
            zone_label=zone_label,
            zone_pct=zone_pct,
            high_52w=high_52w,
            low_52w=low_52w,
            ind=ind,
            bb_width=bb_width,
            intel_text=intel_text,
            news_sentiment=news_sentiment,
            cot_bias=cot_bias,
            fear_greed=fear_greed,
            vix_val=vix_val,
        )
        try:
            client = _client()
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=BACKTEST_CLAUDE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            et = type(e).__name__
            em = str(e).lower()
            if "timeout" in em or "timeout" in et.lower():
                log(f"[Timeout] {sym} {timeframe} — skipping", level="warning")
                return None
            log(f"[Backtest] Claude API error {sym} {timeframe}: {e}", level="warning")
            return None
        raw = _message_text(resp)
        parsed = _parse_json_response(raw)
        ai: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

        def _skip_out(reason: str, src: dict[str, Any] | None = None) -> dict[str, Any]:
            log(f"[SKIP] {sym} {timeframe}: {reason}", level="info")
            return _skipped_backtest_row(
                sym=sym,
                timeframe=timeframe,
                analysis_date=analysis_date,
                price=float(price),
                zone_pct=zone_pct,
                zone_label=zone_label,
                skip_reason=reason,
                ai=src if src is not None else ai,
                tf_key=tf_key,
                is_exotic=is_exotic,
            )

        if not ai:
            return _skip_out("empty or invalid JSON from model", {})

        _sanitize_signal_lists(ai)
        ai["zone_pct"] = zone_pct

        if not bool(ai.get("skip_trade")):
            d0 = str(ai.get("direction", "")).strip().upper()
            if d0 not in ("LONG", "SHORT"):
                return _skip_out("no valid LONG/SHORT direction from model", ai)
            try:
                entry0 = float(ai.get("entry", price) or price)
            except (TypeError, ValueError):
                entry0 = float(price)
            atr0 = float(ind.get("atr") or 0) or abs(entry0) * 0.01
            try:
                st_m = float(ai.get("stop_loss") or 0)
            except (TypeError, ValueError):
                st_m = 0.0
            if st_m == 0:
                if d0 == "LONG":
                    ai["stop_loss"] = round(entry0 - atr0, 5)
                else:
                    ai["stop_loss"] = round(entry0 + atr0, 5)
            c0 = str(ai.get("confidence", "MEDIUM")).strip().upper()
            if c0 not in ("HIGH", "MEDIUM", "LOW"):
                c0 = "MEDIUM"
            ai["confidence"] = _apply_exotic_confidence(c0, is_exotic)
            if sym == "GBPJPY" and str(ai.get("confidence", "")).strip().upper() == "HIGH":
                ai["confidence"] = "MEDIUM"

        ai = enforce_rules(ai, tf_key, float(price), sym)

        if ai.get("skip_trade"):
            return _skip_out(str(ai.get("skip_reason") or "enforcement skip"), ai)

        strategy_id_norm = str(ai.get("strategy_id", "")).strip().upper()
        if strategy_id_norm not in STRATEGIES:
            return _skip_out(f"unsupported strategy_id: {strategy_id_norm}", ai)

        direction = str(ai.get("direction", "")).strip().upper()
        if direction not in ("LONG", "SHORT"):
            return _skip_out("no valid LONG/SHORT after enforcement", ai)

        strategy_met = bool(ai.get("strategy_met", False))
        try:
            conviction_score = int(round(float(ai.get("conviction_score", 5))))
        except (TypeError, ValueError):
            conviction_score = 5
        conviction_score = max(0, min(10, conviction_score))

        confidence = str(ai.get("confidence", "LOW")).strip().upper()
        if confidence not in ("HIGH", "MEDIUM", "LOW"):
            confidence = "LOW"

        entry = float(ai.get("entry", price) or price)
        if not math.isfinite(entry) or entry <= 0:
            entry = float(price)
        stop = float(ai.get("stop_loss", 0) or 0)

        mult = 1 if direction == "LONG" else -1
        if direction == "LONG" and (not math.isfinite(stop) or stop >= entry):
            return _skip_out("invalid stop for LONG", ai)
        if direction == "SHORT" and (not math.isfinite(stop) or stop <= entry):
            return _skip_out("invalid stop for SHORT", ai)

        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return _skip_out("invalid risk", ai)

        tp1 = round(entry + mult * risk * 2.0, 5)
        tp2 = round(entry + mult * risk * 3.0, 5)
        tp3 = round(entry + mult * risk * 5.0, 5)
        ai["tp1"], ai["tp2"], ai["tp3"] = tp1, tp2, tp3
        rr_tp2 = 3.0
        ai["rr_ratio"] = f"1:{rr_tp2:.2f}"

        position_size = float(ai.get("_position_size", 0) or 0)
        leveraged_exposure = float(ai.get("_leveraged_exposure", 0) or 0)
        max_risk_dollars = float(ai.get("_max_risk_dollars", 0) or 0)
        sizing_risk_pct = float(ai.get("_account_risk_pct", 0) or 0)
        risk_pct_of_price = risk / entry if entry > 0 else 0.0
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

        core_met = ai.get("core_signals_met") if isinstance(ai.get("core_signals_met"), list) else []
        core_failed = (
            ai.get("core_signals_failed") if isinstance(ai.get("core_signals_failed"), list) else []
        )
        strat_name = str(ai.get("strategy_name") or "").strip() or str(
            STRATEGIES.get(strategy_id_norm, {}).get("name", "Best Available")
        )

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
            "confluence_score_python": 0,
            "risk_mode": "NORMAL",
            "weekly_bias_chart": "",
            "intel_summary": intel_summary_stored,
            "strategy_id": strategy_id_norm,
            "strategy_name": strat_name,
            "strategy_met": strategy_met,
            "core_signals_met": core_met,
            "core_signals_failed": core_failed,
            "non_negotiables_met": core_met
            if core_met
            else (
                ai.get("non_negotiables_met")
                if isinstance(ai.get("non_negotiables_met"), list)
                else []
            ),
            "non_negotiables_failed": core_failed
            if core_failed
            else (
                ai.get("non_negotiables_failed")
                if isinstance(ai.get("non_negotiables_failed"), list)
                else []
            ),
            "trailing_plan": str(ai.get("trailing_plan", "") or "")[:500],
            "timeframe_restricted": tf_key == "1h",
            "tf_strategy_allowed": tf_key != "1h" or strategy_id_norm in ALLOWED_1H_STRATEGIES,
            "trailing_activated": trailing_activated,
            "final_stop": round(final_stop, 5),
            "skipped": False,
            "skip_trade": False,
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
    """Per-strategy win rate, PnL, profit factor, timeframe breakdown (v3 master)."""
    strategy_performance: dict[str, dict[str, Any]] = {}
    for sid, sdef in STRATEGIES.items():
        s_trades = [t for t in completed if str(t.get("strategy_id", "") or "").upper() == sid]
        if not s_trades:
            continue
        s_wins = [t for t in s_trades if t.get("outcome") == "WIN"]
        s_losses = [t for t in s_trades if t.get("outcome") == "LOSS"]
        total_pnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in s_trades)
        avg_win = (
            sum(float(t.get("pnl_dollars", 0) or 0) for t in s_wins) / len(s_wins) if s_wins else 0.0
        )
        avg_loss = (
            sum(float(t.get("pnl_dollars", 0) or 0) for t in s_losses) / len(s_losses)
            if s_losses
            else 0.0
        )
        win_sum = sum(float(t.get("pnl_dollars", 0) or 0) for t in s_wins)
        loss_sum = sum(float(t.get("pnl_dollars", 0) or 0) for t in s_losses)
        if s_losses and s_wins and loss_sum != 0:
            profit_factor = abs(win_sum) / abs(loss_sum)
        elif s_wins and not s_losses:
            profit_factor = 999.0
        elif s_losses and not s_wins:
            profit_factor = 0.0
        else:
            profit_factor = 0.0

        tf_breakdown: dict[str, Any] = {}
        for tf in ("15m", "30m", "1h", "4h", "1d", "1w"):
            tf_trades = [t for t in s_trades if str(t.get("timeframe", "") or "").lower() == tf]
            if tf_trades:
                tf_wins = [t for t in tf_trades if t.get("outcome") == "WIN"]
                tf_breakdown[tf] = {
                    "total": len(tf_trades),
                    "wins": len(tf_wins),
                    "win_rate": round(len(tf_wins) / len(tf_trades) * 100, 1),
                    "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in tf_trades), 2),
                }

        strategy_performance[sid] = {
            "name": sdef["name"],
            "category": sdef["category"],
            "proven_wr": sdef.get("proven_wr"),
            "total": len(s_trades),
            "wins": len(s_wins),
            "losses": len(s_losses),
            "win_rate": round(len(s_wins) / len(s_trades) * 100, 1) if s_trades else 0.0,
            "pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "timeframe_breakdown": tf_breakdown,
        }

    return strategy_performance


def calculate_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    row_list = [r for r in (results or []) if isinstance(r, dict)]
    n_all = len(row_list)
    hard_filtered_n = sum(1 for r in row_list if str(r.get("verdict", "")).upper() == "FILTERED")
    eligible_after_hard = max(0, n_all - hard_filtered_n)

    trades = [r for r in row_list if str(r.get("outcome", "")).upper() in ("WIN", "LOSS")]

    skipped_rows = [
        r for r in row_list if r.get("skipped") or str(r.get("outcome", "")).upper() == "SKIPPED"
    ]
    skip_reasons: dict[str, int] = {}
    for t in skipped_rows:
        rkey = str(t.get("skip_reason") or t.get("reasoning") or "unknown")[:50]
        skip_reasons[rkey] = skip_reasons.get(rkey, 0) + 1
    skip_meta = {
        "total_analyses": n_all,
        "trades_taken": len(trades),
        "skipped_trades": len(skipped_rows),
        "skip_rate_pct": round(len(skipped_rows) / max(1, n_all) * 100, 1),
        "skip_reasons": skip_reasons,
    }

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
            **skip_meta,
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
        **skip_meta,
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

    log("[Improve] Calling Claude claude-sonnet-4-5...", level="info")
    log(f"[Improve] Prompt length: {len(prompt)} chars", level="info")

    raw = ""
    try:
        client = _client()
        resp = client.messages.create(
            model="claude-sonnet-4-5",
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


def pick_random_date(timeframe: str) -> str:
    """Random historical date in a window suited to ``timeframe`` (weekdays only)."""
    days_back = {
        "1w": 365 * 3,
        "1d": 365 * 2,
        "4h": 365,
        "1h": 180,
        "30m": 90,
        "15m": 60,
    }.get((timeframe or "").strip().lower(), 365)

    start = datetime.now() - timedelta(days=days_back)
    end = datetime.now() - timedelta(days=30)
    if start > end:
        start, end = end, start

    delta_days = max((end - start).days, 0)
    for _ in range(20):
        random_day = start + timedelta(days=random.randint(0, delta_days))
        if random_day.weekday() < 5:
            return random_day.strftime("%Y-%m-%d")

    d = end
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def build_work_batch(existing_keys: set[str], batch_size: int) -> list[tuple[str, str, str]]:
    """Build ``batch_size`` random (ticker, timeframe, date) jobs. ``existing_keys`` is unused (append_result dedupes)."""
    _ = existing_keys
    tickers = eligible_backtest_tickers()
    if not tickers:
        tickers = ["EURUSD"]
    timeframe_list = list(TIMEFRAMES)

    batch: list[tuple[str, str, str]] = []
    for _ in range(batch_size):
        ticker = random.choice(tickers)
        tf = random.choice(timeframe_list)
        date = pick_random_date(tf)
        batch.append((ticker, tf, date))

    return batch


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
                time.sleep(10)
                continue

            if not env("ANTHROPIC_API_KEY"):
                log("[Loop] ANTHROPIC_API_KEY missing — sleeping", level="warning")
                time.sleep(10)
                continue

            work_items = build_work_batch(set(), BATCH_SIZE)

            update_state(
                {
                    "status": "testing",
                    "current_ticker": work_items[0][0],
                    "current_date": work_items[0][2],
                    "current_timeframe": work_items[0][1],
                    "total_tests_run": len(_load_results_list()),
                    "last_heartbeat": datetime.now().isoformat(),
                }
            )

            log(
                f"[Loop] Batch {len(work_items)} jobs (max_workers={MAX_WORKERS})",
                level="info",
            )

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(analyse_one_backtest, t, tf, d): (t, tf, d)
                    for t, tf, d in work_items
                }
                for future in as_completed(futures):
                    ticker, tf, date = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:  # noqa: BLE001
                        log(f"[Thread error] {ticker} {tf}: {e}", level="warning")
                        continue

                    if result is None:
                        continue

                    with _loop_counters_lock:
                        loop_completed_tests += 1
                        lc = loop_completed_tests

                    if lc % 10 == 0:
                        log(
                            f"[Loop] Running - tests: {len(_load_results_list())}",
                            level="info",
                        )

                    prev_len = len(_load_results_list())
                    count = append_result(result)
                    added = count > prev_len

                    if added:
                        if result.get("skipped"):
                            log(
                                f"[Loop] SKIP {ticker} {tf} {date}: "
                                f"{result.get('skip_reason', '')}",
                                level="info",
                            )
                        else:
                            outcome = result.get("outcome", "?")
                            pnl = float(result.get("pnl_dollars", 0) or 0)
                            log(
                                f"[Loop] #{count} {ticker} {tf} {date}: {outcome} ${pnl:.2f}",
                                level="info",
                            )

                    should_improve = False
                    with _loop_counters_lock:
                        if added and result.get("outcome") in ("WIN", "LOSS"):
                            tests_since_improve += 1
                            if tests_since_improve >= IMPROVE_EVERY:
                                tests_since_improve = 0
                                should_improve = True

                    if added and count > 0 and count % 5 == 0:
                        all_results = _load_results_list()
                        stats = calculate_stats(all_results)
                        save_json(STATS_FILE, stats)
                        log(
                            f"[Loop] Stats updated: WR={stats.get('win_rate_pct', 0)}% "
                            f"Trades={stats.get('total_trades', 0)}",
                            level="info",
                        )

                    if should_improve:
                        log("[Loop] Running improvement cycle...", level="info")
                        snap = _load_results_list()
                        threading.Thread(target=run_improvement_cycle, args=(snap,), daemon=True).start()

            with _loop_counters_lock:
                tsi_snapshot = tests_since_improve
            update_state(
                {
                    "status": "idle",
                    "last_heartbeat": datetime.now().isoformat(),
                    "total_tests_run": len(_load_results_list()),
                    "tests_since_improve": tsi_snapshot,
                    "last_session_at": datetime.now().isoformat(),
                }
            )

        except Exception as e:  # noqa: BLE001
            log(f"[Loop] Error: {e}", level="error")
            log(f"[Loop] {traceback.format_exc()}", level="error")
            time.sleep(0.5)
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
