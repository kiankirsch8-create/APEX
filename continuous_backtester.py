"""
Continuous autonomous backtesting loop: random historical setups, forward outcomes,
rolling stats, and periodic self-improvement. All state is simple JSON on ``DATA_DIR``
(default ``/data`` on Railway with a volume; falls back to ``./results`` locally).
"""
from __future__ import annotations

STRATEGY_VERSION = "v7.5-backtest-locked-trail"
# v7.5: locked strategies execute in backtest; NEUTRAL+momentum uses ranging/low-vol filter;
# trailing regime logging + trade analytics fields; T04/SMC10/T08/M06 status, recipe, 1-candle guards.
# v7.4 master: macro 7d price alignment, locked confluence count, NEUTRAL momentum block,
# M03 tickers / M02 off 4h, 1w mid-week + Monday sizing, trailing regime + recipe boost,
# condition profiling (chrono) + /api/strategy_conditions/{id}.
# v7.3 session21: FIX 23–30 (macro confidence post-enforce, B02 stops + blocked, trend_manager,
# chrono strategy confluence, strategy_status). Calendar + regime + macro from prior prompts.
# v7.2: strategy_status.json; UNTESTED loose EMA200/ADX paths (chrono only).
# v7.5+: LOCKED/TESTING/UNTESTED all execute in backtest; only BLOCKED skipped.
# v7.1: chrono phase scan (1w→1d→4h→intraday), risk/tp multipliers, separate 4H currency pool,
# deterministic Layer-2 strategy order, $25 trailing floor + 0.75R/1.5R locks, exotic USD cap exemption,
# consecutive-loss extended cooldown, expanded universe (CHF + QQQ).
# v7.0 baseline: same-day ticker dedupe, currency caps, R01 RSI gates, L2 EMA200/ADX floor, TP1 2R floor.
# v6 baseline: LAST_TRADE_DATES (never reset per day); Layer 2+ Python-forced; Layer 1 prompt.
#
# DO NOT CHANGE (contract):
#   RISK_BY_CONFIDENCE numeric dict entries (v7.4 only adds optional M03 JPY perfect-storm override)
#   append_result / load_all_results implementations
#   calculate_stats function
#   File I/O and storage paths (DATA_DIR results)

import gc
import json
import math
import os
from collections import defaultdict
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from anthropic import Anthropic

import pandas_ta  # noqa: F401

from intelligence_fetch_cached import (
    cached_apply_trend_filter,
    cached_calendar_historical,
    cached_claude_master_text,
    cached_complete_briefing,
    cached_macro_bias,
    cached_regime,
)
from macro_manager import (
    align_macro_bias_with_price,
    apply_macro_confidence_adjustment,
    macro_result_fields,
    merged_macro_result_fields,
    set_backtest_mode,
)
from utils import DATA_DIR, env, load_json, log, save_json, utcnow_iso

set_backtest_mode(True)

# ── COOLDOWN TRACKER — module-level, NEVER inside any loop ──
# Key: "TICKER_tf" (tf lowercased). Value: "YYYY-MM-DD" of last trade.
LAST_TRADE_DATES: dict[str, str] = {}
# v7.0 chrono: one trade per ticker per calendar scan day (dedupe across timeframes).
TRADED_TICKERS_TODAY: set[str] = set()
# FIX 29 — distinct strategy ids per (ticker, direction) accumulated during chrono calendar day.
CHRONO_DAY_PREFILTER_SIDS: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
# v7.4 — cross-timeframe confluence (1w+1d+4h same sym+direction same calendar day)
CHRONO_SYMDIR_TFS: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
# v7.4 — perfect storm JPY snapshot (sym -> last macro/trend/conf for that chrono day)
CHRONO_JPY_STORM_SNAPSHOT: dict[str, dict[str, Any]] = {}
CHRONO_JPY_RISK_DAY: float = 0.0
# v7.4 — JPY storm pairs that produced at least one qualifying prefilter row today (chrono)
CHRONO_JPY_PAIRS_SIGNALLED: set[str] = set()

JPY_STORM_PAIRS: frozenset[str] = frozenset(
    {"USDJPY", "CADJPY", "AUDJPY", "GBPJPY", "NZDJPY", "CHFJPY"},
)
V74_ALLOWED_4H_STRATEGIES: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "T07_MA_RIBBON_ALIGNMENT",
        "R01_EXTREME_ZONE_REVERSION",
    },
)

CONFLUENCE_COUNTING_STRATEGIES: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "R01_EXTREME_ZONE_REVERSION",
        "SMC05_EQUAL_HL_HUNT",
        "M02_MACD_ZERO_CROSS",
        "T07_MA_RIBBON_ALIGNMENT",
        "M03_RSI_MOMENTUM_CONTINUATION",
        "B09_RSI_MOMENTUM_BREAK",
        "T10_200EMA_BOUNCE",
        "SMC10_CHOCH",
        "T08_DONCHIAN_BREAKOUT",
        "M06_PRICE_ACCELERATION",
    },
)

# Next apex_trader.py LIVE_STRATEGIES update (v7.5 — do not import on VPS until live deploy):
V75_LIVE_STRATEGIES_ADDITIONS: frozenset[str] = frozenset(
    {
        "SMC10_CHOCH",
        "T08_DONCHIAN_BREAKOUT",
        "M06_PRICE_ACCELERATION",
    },
)

M03_ALLOWED_TICKERS: frozenset[str] = frozenset(
    {
        "AUDJPY",
        "CADJPY",
        "CHFJPY",
        "NZDJPY",
        "USDJPY",
        "USDSEK",
        "USDNOK",
        "USDMXN",
        "EURCHF",
        "EURAUD",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "USDZAR",
    },
)

MOMENTUM_STRATEGIES: frozenset[str] = frozenset(
    {
        "M03_RSI_MOMENTUM_CONTINUATION",
        "M06_PRICE_ACCELERATION",
        "T08_DONCHIAN_BREAKOUT",
        "B10_WEEKLY_RANGE_BREAK",
        "T04_ADX_TREND_ENTRY",
    },
)
# v7.0 chrono: completed trades per currency per scan day (sequential sim; caps clustering).
OPEN_CURRENCY_COUNT: dict[str, int] = {}
# v7.1 chrono: 4h + intraday share a separate per-day currency pool from 1w/1d.
OPEN_CURRENCY_COUNT_4H: dict[str, int] = {}
# USD side of these pairs is exempt from FIX 2 currency cap (FIX 15).
EXOTIC_PAIRS: frozenset[str] = frozenset({"USDMXN", "USDZAR", "USDNOK", "USDSEK"})
# Global completed-trade counts by strategy_id (hydrated at chrono job start from results + job file).
STRATEGY_TRADE_COUNT: dict[str, int] = {}
# Extended loss streak (FIX 16): key TICKER_tf_DIRECTION → consecutive loss count.
CONSECUTIVE_LOSSES: dict[str, int] = {}
# Last completed trade date per directional combo (for 21-day extended gate).
LAST_COMBO_TRADE_DATE: dict[str, str] = {}
# v7.18: strategies with zero trades get sort priority next scan day.
ZERO_TRADE_STRATEGIES: set[str] = set()
TRADE_COOLDOWN_DAYS: dict[str, int] = {
    "1w": 7,
    "1d": 2,
    "4h": 1,
    "1h": 0,
    "30m": 0,
    "15m": 0,
}

MODEL = "claude-sonnet-4-5-20250929"
CLAUDE_MODEL = MODEL

RESULTS_FILE = DATA_DIR / "backtest_results.json"
STATS_FILE = DATA_DIR / "backtest_stats.json"
LEARNED_FILE = DATA_DIR / "learned_weights.json"
STATE_FILE = DATA_DIR / "backtest_state.json"
ENABLED_FILE = DATA_DIR / "backtest_enabled.json"
IMPROVING_FILE = DATA_DIR / "improving.json"
IMPROVE_DEBUG_FILE = DATA_DIR / "improve_debug.json"
SESSION_CONFIG_FILE = DATA_DIR / "session_config.json"
ACTIVE_CHRONO_FILE = DATA_DIR / "active_chrono_job.json"
STRATEGY_STATUS_FILE = DATA_DIR / "strategy_status.json"

STARTING_CAPITAL = 10000.0
LEVERAGE = 50
IMPROVE_EVERY = 100

# Parallel rolling batch (I/O-bound jobs). Chrono stays sequential: same-day capital,
# TRADED_TICKERS_TODAY, and currency caps mutate between scans — parallel chrono would
# change which trades pass gates and is intentionally not used here.
MAX_WORKERS = int(os.environ.get("APEX_PARALLEL_WORKERS", "8"))
BATCH_SIZE = 10
BACKTEST_CLAUDE_MAX_TOKENS = 1200
CLAUDE_HTTP_TIMEOUT_SEC = 25.0

# Chronological walk-forward backtest (API-triggered; separate from rolling loop)
CHRONO_START_DATE = "2024-01-01"
CHRONO_END_DATE = "2026-05-17"
CHRONO_TIMEFRAMES: list[str] = ["4h", "1d", "1w", "1h"]  # 15m/30m appended in chrono loop when scan date is recent
CHRONO_TICKERS = [
    # Majors (USD)
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
    # EUR crosses
    "EURGBP",
    "EURJPY",
    "EURAUD",
    "EURNZD",
    "EURCAD",
    "EURCHF",
    # GBP crosses
    "GBPJPY",
    "GBPNZD",
    "GBPAUD",
    "GBPCAD",
    "GBPCHF",
    # AUD / NZD crosses
    "AUDJPY",
    "AUDCAD",
    "AUDNZD",
    "NZDCAD",
    "NZDJPY",
    # JPY crosses
    "CADJPY",
    "CHFJPY",
    # Commodity / EM
    "USDMXN",
    "USDZAR",
    "USDNOK",
    "USDSEK",
    # Index (non-forex; Yahoo symbol)
    "QQQ",
]

# Live scan status for Lovable (updated each ticker during chrono)
CHRONO_LIVE_STATUS: dict[str, Any] = {
    "job_id": None,
    "current_date": None,
    "current_ticker": None,
    "current_timeframe": None,
    "status": "idle",
    "trades_today": 0,
    "capital": STARTING_CAPITAL,
    "days_processed": 0,
    "ticker_position": 0,
    "total_tickers": 0,
    "scan_counts": {},
}

# Fairness: how many times each ticker has been scanned this chrono run (session)
CHRONO_SCAN_COUNTS: dict[str, int] = {}

SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "asia": (22, 8),
    "london": (7, 12),
    "new_york": (12, 21),
}

# Rolling batch: only scan pairs relevant to the active UTC session (Lovable toggles via session_config).
SESSION_PAIRS: dict[str, tuple[str, ...]] = {
    "asia": (
        "AUDUSD",
        "NZDUSD",
        "USDJPY",
        "AUDJPY",
        "CADJPY",
        "NZDCAD",
        "GBPJPY",
    ),
    "london": (
        "EURUSD",
        "GBPUSD",
        "EURGBP",
        "EURNZD",
        "GBPNZD",
        "EURAUD",
        "GBPAUD",
        "USDSEK",
        "USDNOK",
        "USDCHF",
    ),
    "new_york": (
        "USDCAD",
        "USDMXN",
        "USDZAR",
        "NZDUSD",
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDNOK",
        "USDSEK",
    ),
}

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
        "T01_EMA_PULLBACK",
        "R01_EXTREME_ZONE_REVERSION",
        "T07_MA_RIBBON_ALIGNMENT",
    }
)

TIMEFRAMES: list[str] = ["15m", "30m", "4h", "1d", "1w"]

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

from strategies_v5_data import STRATEGIES

ALL_STRATEGY_IDS: tuple[str, ...] = tuple(sorted(STRATEGIES.keys()))

LAYER1_STRATEGY_IDS: frozenset[str] = frozenset(
    {"T01_EMA_PULLBACK", "R01_EXTREME_ZONE_REVERSION"},
)

_INTRADAY_STRATEGY_IDS: frozenset[str] = frozenset(
    sid for sid, meta in STRATEGIES.items() if str(meta.get("category", "")).strip() == "INTRADAY"
)

# Correlation groups — max 2 open trades per group at once
CORRELATION_GROUPS: dict[str, list[str]] = {
    "USD_EM": ["USDMXN", "USDZAR", "USDNOK", "USDSEK"],
    "JPY_CROSSES": ["CADJPY", "AUDJPY", "GBPJPY", "USDJPY", "NZDJPY", "EURJPY", "CHFJPY"],
    "COMMODITY": ["AUDUSD", "NZDUSD", "USDCAD", "AUDNZD", "AUDCAD", "NZDCAD"],
    "EUR_CROSSES": ["EURUSD", "EURGBP", "EURNZD", "EURAUD", "EURCAD"],
    "GBP_CROSSES": ["GBPUSD", "GBPJPY", "GBPNZD", "GBPAUD", "GBPCAD"],
}

# Layer 3 intraday: max hold before forced exit
INTRADAY_MAX_CANDLES: dict[str, int] = {
    "15m": 6,
    "30m": 8,
    "4h": 2,
}

# Stop minimum — prevents stops inside daily noise
MIN_STOP_PCT: dict[str, float] = {
    "1w": 0.8,
    "1d": 0.5,
    "4h": 0.8,
    "1h": 0.3,
    "30m": 0.15,
    "15m": 0.10,
}




from prefilter_v6 import python_prefilter


def get_currencies(ticker: str) -> list[str]:
    """Forex spot symbols as 6-letter AAA/BBB; non-forex (e.g. QQQ) → no currencies (FIX 17)."""
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return [t[:3], t[3:]]
    return []


# ── v7.2 strategy status — LOCKED_STRATEGY_IDS = confluence counting only (not execution skip) ──
STRATEGY_STATUS: dict[str, Any] = {}
LOCKED_STRATEGY_IDS: frozenset[str] = frozenset()
UNTESTED_STRATEGIES_V72: frozenset[str] = frozenset()
BLOCKED_STRATEGIES: frozenset[str] = frozenset()


def _v72_locked_ids_default() -> frozenset[str]:
    return frozenset(
        {
            "T01_EMA_PULLBACK",
            "R01_EXTREME_ZONE_REVERSION",
            "SMC05_EQUAL_HL_HUNT",
            "T03_HH_HL_CONTINUATION",
            "T07_MA_RIBBON_ALIGNMENT",
            "T02_EMA_CROSSOVER",
            "SMC01_SR_FLIP",
            "M07_VOLUME_SURGE_MOMENTUM",
            "M03_RSI_MOMENTUM_CONTINUATION",
            "SMC10_CHOCH",
            "T08_DONCHIAN_BREAKOUT",
            "M06_PRICE_ACCELERATION",
        }
    )


def _v72_testing_ids_default() -> frozenset[str]:
    return frozenset(
        {
            "B01_RANGE_BREAKOUT",
            "B09_RSI_MOMENTUM_BREAK",
            "T04_ADX_TREND_ENTRY",
            "T10_200EMA_BOUNCE",
            "T09_KELTNER_TREND_RIDE",
            "M02_MACD_ZERO_CROSS",
            "R03_BB_EXTREME_TOUCH",
            "B06_TRIANGLE_BREAKOUT",
            "B08_KEY_LEVEL_RETEST",
        }
    )


def _v72_blocked_ids_default() -> frozenset[str]:
    return frozenset(
        {
            "B02_VOLATILITY_COMPRESSION",
            "V02_ATR_EXPANSION_ENTRY",
            "R09_WEEKLY_GAP_FILL",
        }
    )


def _v72_default_status_payload() -> dict[str, Any]:
    locked = sorted(_v72_locked_ids_default())
    testing = sorted(_v72_testing_ids_default())
    blocked = sorted(_v72_blocked_ids_default())
    untested = sorted(set(ALL_STRATEGY_IDS) - set(locked) - set(testing) - set(blocked))
    return {
        "locked": locked,
        "testing": testing,
        "blocked": blocked,
        "untested": untested,
        "solid": ["T10_200EMA_BOUNCE", "B09_RSI_MOMENTUM_BREAK"],
        "watch": [],
        "restricted": ["M03_RSI_MOMENTUM_CONTINUATION", "M02_MACD_ZERO_CROSS"],
        "last_updated": "2026-05-28",
    }


def _v75_migrate_strategy_status(raw: dict[str, Any]) -> bool:
    """Apply v7.5 forensic promotions (idempotent)."""
    changed = False

    def _as_set(key: str) -> set[str]:
        return {str(x).strip().upper() for x in (raw.get(key) or []) if isinstance(x, str) and str(x).strip()}

    locked = _as_set("locked")
    testing = _as_set("testing")
    blocked = _as_set("blocked")
    untested = _as_set("untested")

    if "T04_ADX_TREND_ENTRY" in blocked:
        blocked.discard("T04_ADX_TREND_ENTRY")
        testing.add("T04_ADX_TREND_ENTRY")
        changed = True
    for sid in ("SMC10_CHOCH", "T08_DONCHIAN_BREAKOUT", "M06_PRICE_ACCELERATION"):
        for bucket in (testing, blocked, untested):
            if sid in bucket:
                bucket.discard(sid)
                changed = True
        if sid not in locked:
            locked.add(sid)
            changed = True

    watch = _as_set("watch")
    if "SMC10_CHOCH" in watch and "SMC10_CHOCH" in locked:
        watch.discard("SMC10_CHOCH")
        changed = True

    if changed:
        raw["locked"] = sorted(locked)
        raw["testing"] = sorted(testing)
        raw["blocked"] = sorted(blocked)
        raw["untested"] = sorted(untested)
        raw["watch"] = sorted(watch)
        raw["last_updated"] = "2026-05-28"
    return changed


def _v72_load_strategy_status(*, log_startup: bool = False) -> None:
    """Load or create ``strategy_status.json``; refresh module-level LOCKED / UNTESTED / BLOCKED sets."""
    global STRATEGY_STATUS, LOCKED_STRATEGY_IDS, UNTESTED_STRATEGIES_V72, BLOCKED_STRATEGIES
    default_payload = _v72_default_status_payload()
    raw: Any = None
    if STRATEGY_STATUS_FILE.is_file():
        raw = load_json(STRATEGY_STATUS_FILE, default=None)
    if not isinstance(raw, dict) or not isinstance(raw.get("locked"), list):
        save_json(STRATEGY_STATUS_FILE, default_payload)
        raw = default_payload
    else:
        # Ensure all 68 ids are classified (append missing to untested)
        seen: set[str] = set()
        for k in ("locked", "testing", "untested", "blocked"):
            for x in raw.get(k) or []:
                if isinstance(x, str):
                    seen.add(x.strip().upper())
        missing = [s for s in ALL_STRATEGY_IDS if s not in seen]
        if missing:
            ut = [str(x).strip().upper() for x in (raw.get("untested") or []) if isinstance(x, str)]
            ut.extend(missing)
            raw["untested"] = sorted(set(ut))
            raw["last_updated"] = date.today().isoformat()
            save_json(STRATEGY_STATUS_FILE, raw)
    if _v75_migrate_strategy_status(raw):
        save_json(STRATEGY_STATUS_FILE, raw)
    STRATEGY_STATUS = raw
    for tag_k, default_vals in (
        ("solid", ["T10_200EMA_BOUNCE", "B09_RSI_MOMENTUM_BREAK"]),
        ("watch", ["SMC10_CHOCH"]),
        ("restricted", ["M03_RSI_MOMENTUM_CONTINUATION", "M02_MACD_ZERO_CROSS"]),
    ):
        if not isinstance(raw.get(tag_k), list):
            raw[tag_k] = list(default_vals)
            raw["last_updated"] = date.today().isoformat()
            save_json(STRATEGY_STATUS_FILE, raw)
    LOCKED_STRATEGY_IDS = frozenset(str(x).strip().upper() for x in (raw.get("locked") or []) if x)
    UNTESTED_STRATEGIES_V72 = frozenset(str(x).strip().upper() for x in (raw.get("untested") or []) if x)
    blk = raw.get("blocked")
    if not isinstance(blk, list):
        blk = list(_v72_blocked_ids_default())
        raw["blocked"] = blk
        save_json(STRATEGY_STATUS_FILE, raw)
    BLOCKED_STRATEGIES = frozenset(str(x).strip().upper() for x in blk if x)
    if log_startup:
        log(
            f"[v7.2] strategy_status locked={len(LOCKED_STRATEGY_IDS)} "
            f"testing={len(raw.get('testing') or [])} untested={len(UNTESTED_STRATEGIES_V72)} "
            f"blocked={len(BLOCKED_STRATEGIES)}",
            level="info",
        )


def _v72_sid_allowed_on_tf(sid: str, tf_key: str) -> bool:
    """INTRADAY (I*) only on 4h/15m/30m per v7.2; others use STRATEGIES timeframes (empty = all)."""
    sid_u = sid.strip().upper()
    tf_l = (tf_key or "").strip().lower()
    if sid_u in _INTRADAY_STRATEGY_IDS:
        return tf_l in ("4h", "15m", "30m")
    meta = STRATEGIES.get(sid_u) or {}
    tfs = meta.get("timeframes") or []
    if not tfs:
        return True
    allowed = {str(x).strip().lower() for x in tfs}
    return tf_l in allowed


def _v72_one_loose_untested_candidate(
    sym: str,
    tf_key: str,
    price: float,
    ind: dict[str, Any],
    *,
    chrono_yfinance: bool,
    existing_sids: set[str],
) -> tuple[str, str, int, dict[str, Any]] | None:
    """Return a single FIX-21 synthetic row for the best-priority UNTESTED id, or None."""
    if not chrono_yfinance or not UNTESTED_STRATEGIES_V72:
        return None
    close = float(ind.get("close", price) or price)
    ema200 = float(ind.get("ema200", close) or close)
    atrv = float(ind.get("atr", 0) or 0)
    adxv = float(ind.get("adx", 0) or 0)
    if atrv <= 0 or not math.isfinite(close) or not math.isfinite(ema200):
        return None
    tf_l = (tf_key or "").strip().lower()

    def _loose_dir_sl_tp(sid: str) -> tuple[str, float, float, float] | None:
        direction: str | None = None
        sl_atr = tp1_atr = tp2_atr = 0.0
        if tf_l == "1w":
            if sid in _INTRADAY_STRATEGY_IDS:
                return None
            if close > ema200:
                direction = "LONG"
            elif close < ema200:
                direction = "SHORT"
            sl_atr, tp1_atr, tp2_atr = 1.5, 3.0, 4.5
        elif tf_l == "1d":
            if sid in _INTRADAY_STRATEGY_IDS:
                return None
            if adxv < 14.0:
                return None
            if close > ema200:
                direction = "LONG"
            elif close < ema200:
                direction = "SHORT"
            sl_atr, tp1_atr, tp2_atr = 1.5, 3.0, 4.5
        elif tf_l in ("4h", "15m", "30m"):
            if close > ema200:
                direction = "LONG"
            elif close < ema200:
                direction = "SHORT"
            sl_atr, tp1_atr, tp2_atr = (1.0, 2.0, 3.0)
        else:
            return None
        if direction is None:
            return None
        return direction, sl_atr, tp1_atr, tp2_atr

    sids_sorted = sorted(
        UNTESTED_STRATEGIES_V72,
        key=lambda s: (0 if s in ZERO_TRADE_STRATEGIES else 1, STRATEGY_TRADE_COUNT.get(s, 0), s),
    )
    for sid in sids_sorted:
        sid_u = sid.strip().upper()
        if sid_u in existing_sids or sid_u in LOCKED_STRATEGY_IDS:
            continue
        if not _v72_sid_allowed_on_tf(sid_u, tf_l):
            continue
        pack = _loose_dir_sl_tp(sid_u)
        if pack is None:
            continue
        direction, sl_atr, tp1_atr, tp2_atr = pack
        mult = 1 if direction == "LONG" else -1
        entry = round(close, 5)
        sl = round(entry - mult * sl_atr * atrv, 5)
        tp1 = round(entry + mult * tp1_atr * atrv, 5)
        tp2 = round(entry + mult * tp2_atr * atrv, 5)
        meta = {
            "v7_fallback": True,
            "v72_untested": True,
            "reasoning": f"{sid_u}: UNTESTED — gathering first data",
            "confidence": "LOW",
            "risk_mode": "PYTHON_LAYER2",
            "direction": direction,
            "entry": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp2,
        }
        return (sid_u, direction, 1, meta)
    return None


def _v72_merge_loose_untested_into_layer2(
    layer2_rows: list[tuple[str, str, int, dict[str, Any] | None]],
    sym: str,
    tf_key: str,
    price: float,
    ind: dict[str, Any],
    *,
    chrono_yfinance: bool,
) -> list[tuple[str, str, int, dict[str, Any] | None]]:
    """Append one FIX-21 loose UNTESTED row only when strict Layer-2 produced no candidates."""
    if not chrono_yfinance or layer2_rows:
        return layer2_rows
    have = {str(r[0]).strip().upper() for r in layer2_rows}
    one = _v72_one_loose_untested_candidate(
        sym, tf_key, price, ind, chrono_yfinance=chrono_yfinance, existing_sids=have
    )
    if one is None:
        return layer2_rows
    merged = list(layer2_rows)
    merged.append(one)
    return merged


def _locked_confluence_sid(sid: str) -> bool:
    """Only LOCKED list strategies count toward confluence multiplier (backtest)."""
    return str(sid or "").strip().upper() in LOCKED_STRATEGY_IDS


# Prefilter has no rules for these ids — v7.1 uses deterministic “turn” ordering instead of random (FIX 10).
_V7_STALE_FALLBACK_IDS: frozenset[str] = frozenset(
    {
        "T05_SUPERTREND_FLIP",
        "T06_PARABOLIC_SAR_FLIP",
        "R07_VWAP_DEVIATION_SWING",
        "R10_COT_EXTREME_REVERSION",
        "B03_LONDON_OPEN_BREAKOUT",
        "B04_NY_OPEN_BREAKOUT",
        "Q01_DAY_OF_WEEK_EDGE",
        "Q02_TIME_OF_DAY_MOMENTUM",
        "Q03_MONTHLY_SEASONALITY",
        "Q04_CARRY_TRADE_MOMENTUM",
        "Q05_CORRELATION_DIVERGENCE",
        "Q06_REGIME_DETECTION",
    }
)


def _v7_filter_layer2_qualifiers(
    qualifying: list[tuple[str, str, int]],
    *,
    sym: str,
    tf_key: str,
    zone_pct: float,
    ind: dict[str, Any],
    price: float,
) -> list[tuple[str, str, int, dict[str, Any] | None]]:
    """Layer 2 post-filters (FIX 5 / FIX 6 / FIX 12 / FIX 13 / FIX 14). Layer 1 tuples pass through unchanged."""
    out: list[tuple[str, str, int, dict[str, Any] | None]] = []
    tf_l = (tf_key or "").strip().lower()
    sym_u = (sym or "").strip().upper()
    for sid, direction, score in qualifying:
        sid_u = str(sid).strip().upper()
        if sid_u in LAYER1_STRATEGY_IDS:
            out.append((sid, direction, score, None))
            continue

        if tf_l == "4h" and sid_u not in V74_ALLOWED_4H_STRATEGIES:
            log(
                f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — 4h allowlist excludes this strategy (v7.4)",
                level="info",
            )
            continue

        d_raw = str(direction or "").strip().upper()
        if d_raw == "BOTH":
            d_eff = "LONG" if float(zone_pct) < 50.0 else "SHORT"
        elif d_raw in ("LONG", "SHORT"):
            d_eff = d_raw
        else:
            out.append((sid, direction, score, None))
            continue

        if sym_u == "NZDUSD" and tf_l == "1w" and d_eff == "SHORT" and float(zone_pct) < 35.0:
            log(
                f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — NZDUSD 1w SHORT blocked: zone_pct "
                f"{float(zone_pct)} below 35%, shorting at discount is not permitted",
                level="info",
            )
            continue

        if (
            sym_u == "GBPUSD"
            and tf_l == "1d"
            and d_eff == "SHORT"
            and sid_u in ("T03_HH_HL_CONTINUATION", "SMC10_CHOCH", "T07_MA_RIBBON_ALIGNMENT")
            and float(zone_pct) < 80.0
        ):
            log(
                f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — GBPUSD 1d SHORT suppressed: {sid_u} in zone "
                f"{float(zone_pct)}% below 80% confirmed losing pattern",
                level="info",
            )
            continue

        if tf_l == "1d":
            adx_raw = ind.get("adx") if ind.get("adx") is not None else ind.get("ADX")
            if adx_raw is not None:
                try:
                    adx_f = float(adx_raw)
                except (TypeError, ValueError):
                    adx_f = None
                if adx_f is not None and adx_f < 14.0:
                    log(
                        f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — "
                        f"Layer 2 daily blocked: ADX {adx_f} below 14 minimum",
                        level="info",
                    )
                    continue

        ema200 = ind.get("ema200") if ind.get("ema200") is not None else ind.get("EMA200")
        current_close = (
            ind.get("close")
            if ind.get("close") is not None
            else (ind.get("price") if ind.get("price") is not None else price)
        )
        if ema200 is not None and current_close is not None:
            try:
                e200 = float(ema200)
                ccl = float(current_close)
            except (TypeError, ValueError):
                e200 = ccl = float("nan")
            if math.isfinite(e200) and math.isfinite(ccl):
                if d_eff == "LONG" and ccl < e200:
                    log(
                        f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — "
                        "Layer 2 LONG blocked: price below EMA200",
                        level="info",
                    )
                    continue
                if d_eff == "SHORT" and ccl > e200:
                    log(
                        f"[PreFilterV7] {sym} {tf_key}: skip {sid_u} — "
                        "Layer 2 SHORT blocked: price above EMA200",
                        level="info",
                    )
                    continue

        if sid_u == "B02_VOLATILITY_COMPRESSION":
            try:
                e20 = float(ind.get("ema20", price) or price)
                e50 = float(ind.get("ema50", price) or price)
                e200f = float(ind.get("ema200", price) or price)
            except (TypeError, ValueError):
                e20 = e50 = e200f = float(price)
            if d_eff == "SHORT" and e20 > e50 > e200f:
                log(
                    f"[PreFilterV7] {sym} {tf_key}: skip B02 SHORT — bullish EMA stack",
                    level="info",
                )
                continue
            if d_eff == "LONG" and e20 < e50 < e200f:
                log(
                    f"[PreFilterV7] {sym} {tf_key}: skip B02 LONG — bearish EMA stack",
                    level="info",
                )
                continue
            try:
                atr14 = float(ind.get("atr", 0) or 0)
            except (TypeError, ValueError):
                atr14 = 0.0
            entry_hint = float(price)
            mult = 1 if d_eff == "LONG" else -1
            sl_b02 = round(entry_hint - mult * 1.5 * atr14, 5) if atr14 > 0 else None
            meta_b02: dict[str, Any] | None = (
                {"b02_atr14": atr14, "b02_entry": entry_hint, "b02_sl": sl_b02}
                if atr14 > 0 and sl_b02 is not None
                else None
            )
            out.append((sid, direction, score, meta_b02))
            continue

        out.append((sid, direction, score, None))
    return out


def _v7_python_prefilter_bundle(
    sym: str,
    tf_key: str,
    price: float,
    ind: dict[str, Any],
    zone_pct: float,
    *,
    analysis_date: str | None,
    past: pd.DataFrame | None,
) -> tuple[bool, list[tuple[str, str, int, dict[str, Any] | None]], str]:
    """Call ``python_prefilter`` then apply v7 Layer 2 gates (no random fallback; FIX 10)."""
    raw_ok, raw_list, raw_reason = python_prefilter(
        sym,
        tf_key,
        float(price),
        ind,
        zone_pct,
        analysis_date=analysis_date,
        past=past,
    )
    raw_ids = [str(x[0]) for x in raw_list]
    filtered = _v7_filter_layer2_qualifiers(
        raw_list,
        sym=sym,
        tf_key=tf_key,
        zone_pct=float(zone_pct),
        ind=ind,
        price=float(price),
    )
    filtered = [
        r
        for r in filtered
        if _v75_backtest_strategy_allowed(str(r[0]), past=past, ind=ind)
    ]
    post_ids = [str(x[0]) for x in filtered]
    log(
        f"[PreFilterAudit] {sym} {tf_key} {analysis_date or ''}: raw={raw_ids} post_v7={post_ids}",
        level="info",
    )
    if not filtered:
        if not raw_ok:
            return False, [], raw_reason
        return False, [], "v7 post-filter removed all strategy candidates"
    return True, filtered, raw_reason


def _strategy_applicable_on_tf(strategy_id: str, tf_key: str) -> bool:
    meta = STRATEGIES.get(strategy_id) or {}
    tfs = meta.get("timeframes") or []
    if not tfs:
        return True
    want = (tf_key or "").strip().lower()
    return want in {str(x).strip().lower() for x in tfs}


def _chrono_combo_key(ticker: str, timeframe: str, direction: str) -> str:
    return (
        f"{(ticker or '').strip().upper()}_"
        f"{(timeframe or '').strip().lower()}_"
        f"{(direction or '').strip().upper()}"
    )


def _rebuild_strategy_trade_counts_from_rows(rows: list[dict[str, Any]]) -> None:
    STRATEGY_TRADE_COUNT.clear()
    for r in rows:
        if not isinstance(r, dict) or r.get("skipped"):
            continue
        if r.get("outcome") not in ("WIN", "LOSS"):
            continue
        sid = str(r.get("strategy_id", "")).strip().upper()
        if sid and sid != "SKIP":
            STRATEGY_TRADE_COUNT[sid] = STRATEGY_TRADE_COUNT.get(sid, 0) + 1


def _rebuild_consecutive_losses_from_rows(rows: list[dict[str, Any]]) -> None:
    CONSECUTIVE_LOSSES.clear()
    LAST_COMBO_TRADE_DATE.clear()
    completed = [
        r
        for r in rows
        if isinstance(r, dict) and not r.get("skipped") and r.get("outcome") in ("WIN", "LOSS")
    ]
    completed.sort(key=lambda r: str(r.get("date", ""))[:10])
    running: dict[str, int] = {}
    for r in completed:
        k = _chrono_combo_key(
            str(r.get("ticker", "")),
            str(r.get("timeframe", "")),
            str(r.get("direction", "")),
        )
        d0 = str(r.get("date", ""))[:10]
        if d0:
            LAST_COMBO_TRADE_DATE[k] = d0
        if r.get("outcome") == "LOSS":
            running[k] = running.get(k, 0) + 1
        else:
            running[k] = 0
    CONSECUTIVE_LOSSES.update(running)


def _chrono_extended_loss_cooldown_block(
    ticker: str,
    timeframe: str,
    direction: str,
    date_str: str,
) -> tuple[bool, str]:
    """FIX 16 — block same ticker+tf+direction for 21d after 3+ consecutive losses."""
    key = _chrono_combo_key(ticker, timeframe, direction)
    if CONSECUTIVE_LOSSES.get(key, 0) < 3:
        return True, ""
    last = LAST_COMBO_TRADE_DATE.get(key)
    if not last:
        return True, ""
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
        curr_dt = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
        days_since = (curr_dt - last_dt).days
    except (TypeError, ValueError):
        return True, ""
    if days_since < 21:
        return (
            False,
            f"Extended cooldown: {key} has 3+ consecutive losses, waiting 21 days "
            f"(day {days_since}/21)",
        )
    return True, ""


def _layer2_tuple_for_deterministic_pick(
    sym: str,
    tf_key: str,
    zone_pct: float,
    layer2_rows: list[tuple[str, str, int, dict[str, Any] | None]],
) -> tuple[str, str, int, dict[str, Any] | None] | None:
    """FIX 10 + FIX 18 — least-tested Layer 2 first; zero-trade priority; stale ids auto-fire on turn."""
    by_sid: dict[str, list[tuple[str, str, int, dict[str, Any] | None]]] = {}
    for row in layer2_rows:
        sid_u = str(row[0]).strip().upper()
        by_sid.setdefault(sid_u, []).append(row)

    l2_ids = [s for s in ALL_STRATEGY_IDS if s not in LAYER1_STRATEGY_IDS]

    def _sort_key(sid: str) -> tuple[int, int, str]:
        if not _strategy_applicable_on_tf(sid, tf_key):
            return (9_000_000, 9_000_000, sid)
        pri = 0 if sid in ZERO_TRADE_STRATEGIES else 1
        return (pri, STRATEGY_TRADE_COUNT.get(sid, 0), sid)

    l2_sorted = sorted(l2_ids, key=_sort_key)
    # Pass 1 — real prefilter hits only (never lose a qualified strategy to a stale fallback
    # that sorts earlier at equal trade-count).
    for sid in l2_sorted:
        if _sort_key(sid)[0] >= 9_000_000:
            continue
        if sid in by_sid:
            return by_sid[sid][0]
    # Pass 2 — stale / no-conditions strategies on their deterministic turn
    for sid in l2_sorted:
        if _sort_key(sid)[0] >= 9_000_000:
            continue
        if sid in _V7_STALE_FALLBACK_IDS:
            dire = "LONG" if float(zone_pct) < 50.0 else "SHORT"
            reasoning = f"{sid} FALLBACK: no detection conditions yet, gathering initial data"
            meta: dict[str, Any] = {
                "v7_fallback": True,
                "reasoning": reasoning,
                "confidence": "LOW",
                "risk_mode": "PYTHON_LAYER2",
            }
            return (sid, dire, 1, meta)
    return None


def _apply_chrono_risk_tp_multipliers(
    ai: dict[str, Any],
    *,
    price: float,
    chrono_risk_mult: float,
    chrono_tp_mult: float,
) -> None:
    """FIX 9 — scale dollar risk and TP distances only (stop price unchanged)."""
    if chrono_risk_mult != 1.0:
        try:
            mrd = float(ai.get("_max_risk_dollars", 0) or 0)
            if mrd > 0:
                ai["_max_risk_dollars"] = round(mrd * chrono_risk_mult, 2)
        except (TypeError, ValueError):
            pass
        try:
            entry_f = float(ai.get("entry", price) or price)
            stop_f = float(ai.get("stop_loss", 0) or 0)
            sd = abs(entry_f - stop_f)
            mrd2 = float(ai.get("_max_risk_dollars", 0) or 0)
            if sd > 0 and mrd2 > 0:
                ps = mrd2 / sd
                ai["_position_size"] = round(ps, 2)
                ai["_leveraged_exposure"] = round(ps * entry_f, 2)
        except (TypeError, ValueError):
            pass

    if chrono_tp_mult != 1.0:
        try:
            e = float(ai.get("entry", price) or price)
            d0 = str(ai.get("direction", "")).strip().upper()
            m = 1 if d0 == "LONG" else -1
            for fld in ("tp1", "tp2", "tp3"):
                if fld in ai and ai.get(fld) is not None:
                    t = float(ai[fld])
                    ai[fld] = round(e + (t - e) * chrono_tp_mult, 5)
        except (TypeError, ValueError):
            pass


def _chrono_currency_cap_blocks(ticker: str, *, use_4h_pool: bool) -> tuple[bool, str | None]:
    """FIX 2 + FIX 9 + FIX 15 — return (blocked, ccy_or_None)."""
    tkr_u = (ticker or "").strip().upper()
    bucket = OPEN_CURRENCY_COUNT_4H if use_4h_pool else OPEN_CURRENCY_COUNT
    for ccy in get_currencies(tkr_u):
        if tkr_u in EXOTIC_PAIRS and ccy == "USD":
            continue
        if bucket.get(ccy, 0) >= 2:
            return True, ccy
    return False, None


def _chrono_v71_phases(days_ago: int, *, scan_date: date) -> list[tuple[str, list[str], float, float, bool]]:
    """(log label, tf list, risk_mult, tp_mult, use_4h_currency_pool)."""
    phases: list[tuple[str, list[str], float, float, bool]] = [
        ("PHASE 1 WEEKLY SCAN", ["1w"], 1.0, 1.0, False),
        ("PHASE 2 DAILY SCAN", ["1d"], 0.85, 0.85, False),
        ("PHASE 2B 1H STRUCTURE", ["1h"], 0.85, 0.85, True),
        ("PHASE 3 4H SCAN", ["4h"], 0.70, 0.70, True),
    ]
    if not scan_date_has_yf_chrono_hourly_data(scan_date):
        before = len(phases)
        phases = [p for p in phases if p[1] != ["1h"] and p[1] != ["4h"]]
        if len(phases) < before:
            dsk = scan_date.isoformat()
            if dsk not in _yf_hourly_skip_logged_dates:
                _yf_hourly_skip_logged_dates.add(dsk)
                log(
                    f"[SKIP] 1h/4h data not available for dates before "
                    f"{yf_chrono_hourly_earliest_scan_date().isoformat()}",
                    level="info",
                )
    if days_ago <= 55:
        phases.append(("PHASE 4 INTRADAY SCAN", ["15m", "30m"], 0.70, 0.70, True))
    return phases

OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_BASE_URL = "https://api-fxpractice.oanda.com"
OANDA_INSTRUMENT_MAP: dict[str, str] = {
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
    "AUDUSD": "AUD_USD",
    "USDCAD": "USD_CAD",
    "NZDUSD": "NZD_USD",
    "USDCHF": "USD_CHF",
    "EURGBP": "EUR_GBP",
    "EURJPY": "EUR_JPY",
    "EURAUD": "EUR_AUD",
    "EURNZD": "EUR_NZD",
    "EURCAD": "EUR_CAD",
    "EURCHF": "EUR_CHF",
    "GBPCHF": "GBP_CHF",
    "GBPJPY": "GBP_JPY",
    "GBPNZD": "GBP_NZD",
    "GBPAUD": "GBP_AUD",
    "GBPCAD": "GBP_CAD",
    "AUDJPY": "AUD_JPY",
    "AUDCAD": "AUD_CAD",
    "AUDNZD": "AUD_NZD",
    "NZDCAD": "NZD_CAD",
    "NZDJPY": "NZD_JPY",
    "CADJPY": "CAD_JPY",
    "CHFJPY": "CHF_JPY",
    "USDMXN": "USD_MXN",
    "USDZAR": "USD_ZAR",
    "USDNOK": "USD_NOK",
    "USDSEK": "USD_SEK",
}


def check_cooldown(ticker: str, timeframe: str, date_str: str) -> tuple[bool, str]:
    """Return (allowed, reason) for ticker+timeframe vs last trade date."""
    pos_key = f"{(ticker or '').strip().upper()}_{(timeframe or '').strip().lower()}"
    last_date = LAST_TRADE_DATES.get(pos_key)
    if not last_date:
        return True, ""
    try:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        curr_dt = datetime.strptime(date_str, "%Y-%m-%d")
        days_since = (curr_dt - last_dt).days
        tf_lc = (timeframe or "").strip().lower()
        cooldown = int(TRADE_COOLDOWN_DAYS.get(tf_lc, 1))
        if days_since < cooldown:
            return False, (
                f"Cooldown: {ticker} {timeframe} traded {days_since}d ago "
                f"(need {cooldown}d gap)"
            )
        return True, ""
    except Exception:  # noqa: BLE001
        return True, ""


def record_trade(ticker: str, timeframe: str, date_str: str) -> None:
    """Record last trade date for cooldown tracking."""
    pos_key = f"{(ticker or '').strip().upper()}_{(timeframe or '').strip().lower()}"
    LAST_TRADE_DATES[pos_key] = date_str.strip()[:10]




EXCLUDED_PAIRS = frozenset(
    {
        "AUDCAD",
        "AUDCHF",
        "AUDNZD",
        "CADCHF",
        "EURCAD",
        "EURJPY",
        "GBPAUD",
        "NZDCHF",
        "NZDJPY",
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


def get_current_session() -> str:
    """Return which session is active right now (UTC)."""
    hour = datetime.now(timezone.utc).hour
    if hour >= 22 or hour < 8:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 21:
        return "new_york"
    return "off_hours"


def is_ticker_in_session(ticker: str, session: str) -> bool:
    """Return True if ticker belongs to the given session list."""
    sym = (ticker or "").strip().upper()
    if session == "off_hours":
        return False
    allowed = SESSION_PAIRS.get(session, ())
    return sym in allowed


def load_session_config() -> dict[str, bool]:
    """Persisted session toggles for the rolling engine (``SESSION_CONFIG_FILE``)."""
    raw = load_json(SESSION_CONFIG_FILE, default=None)
    defaults = {"asia": True, "london": True, "new_york": True, "off_hours": False}
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for k in defaults:
        if k in raw:
            out[k] = bool(raw[k])
    return out


def save_session_config(config: dict[str, Any]) -> None:
    """Overwrite session toggles JSON."""
    save_json(SESSION_CONFIG_FILE, dict(config))


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
CHRONO_RUNNING = False
_yf_lock = threading.Semaphore(1)

# Yahoo intraday history for chrono is only reliable within ~730 days; older scans skip 1h/4h.
YF_CHRONO_HOURLY_MAX_DAYS = 730
_chrono_ohlc_cache: dict[tuple[str, str, str], tuple[Any, Any]] = {}
_chrono_ohlc_cache_lock = threading.Lock()
_chrono_ohlc_prefetch_done: set[tuple[str, int, int]] = set()
_yf_hourly_skip_logged_dates: set[str] = set()


def yf_chrono_hourly_earliest_scan_date() -> date:
    return datetime.now(timezone.utc).date() - timedelta(days=YF_CHRONO_HOURLY_MAX_DAYS)


def scan_date_has_yf_chrono_hourly_data(scan_d: date) -> bool:
    return scan_d >= yf_chrono_hourly_earliest_scan_date()


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


def load_all_results() -> list[dict[str, Any]]:
    """Public read of persisted rolling backtest rows (same backing store as ``_load_results_list``)."""
    return _load_results_list()


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


def _atr_series_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR on OHLC (same spirit as live trader)."""
    try:
        h = pd.to_numeric(df["High"], errors="coerce").astype(float)
        l = pd.to_numeric(df["Low"], errors="coerce").astype(float)
        c = pd.to_numeric(df["Close"], errors="coerce").astype(float)
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    prev = c.shift(1)
    tr = (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _trade_condition_snapshot_fields(
    analysis_date: str,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
) -> dict[str, Any]:
    """v7.4 [BACKTEST] — volatility / structure labels for condition profiling (no new network I/O)."""
    out: dict[str, Any] = {
        "volatility_regime": "UNKNOWN",
        "market_phase": "UNKNOWN",
        "session_day": "",
        "weekly_candle_age": -1,
    }
    try:
        d0 = date.fromisoformat((analysis_date or "")[:10])
    except (TypeError, ValueError):
        return out
    day_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    out["session_day"] = day_names[d0.weekday()] if 0 <= d0.weekday() < 7 else ""
    out["weekly_candle_age"] = int(d0.weekday()) if d0.weekday() <= 4 else 4

    if past is None or past.empty or "Close" not in past.columns:
        return out
    try:
        c = pd.to_numeric(past["Close"], errors="coerce").dropna()
        if len(c) < 25:
            return out
        atr_s = _atr_series_wilder(past, 14)
        if atr_s.empty or len(atr_s) < 21:
            return out
        cur_atr = float(atr_s.iloc[-1])
        avg_atr = float(atr_s.iloc[-20:].mean())
        if not math.isfinite(cur_atr) or not math.isfinite(avg_atr) or avg_atr <= 0:
            return out
        ratio = cur_atr / avg_atr
        if ratio < 0.8:
            out["volatility_regime"] = "LOW_VOL"
        elif ratio < 1.2:
            out["volatility_regime"] = "NORMAL_VOL"
        elif ratio < 2.0:
            out["volatility_regime"] = "HIGH_VOL"
        else:
            out["volatility_regime"] = "EXTREME_VOL"

        ema20 = c.ewm(span=20, adjust=False).mean()
        e_now = float(ema20.iloc[-1])
        e_prev = float(ema20.iloc[-4]) if len(ema20) >= 4 else e_now
        slope = e_now - e_prev
        px = float(c.iloc[-1])
        cross_recent = False
        if len(c) >= 4 and len(ema20) >= 4:
            for j in range(1, 4):
                i = -(j + 1)
                a = float(c.iloc[i]) - float(ema20.iloc[i])
                b = float(c.iloc[i + 1]) - float(ema20.iloc[i + 1])
                if a == 0 or b == 0:
                    continue
                if (a > 0) != (b > 0):
                    cross_recent = True
                    break
        if cross_recent:
            out["market_phase"] = "BREAKOUT"
        elif px > e_now and slope > 0:
            out["market_phase"] = "TRENDING_UP"
        elif px < e_now and slope < 0:
            out["market_phase"] = "TRENDING_DOWN"
        else:
            out["market_phase"] = "RANGING"
    except Exception:  # noqa: BLE001
        pass
    return out


def _v75_atr_metrics(past: pd.DataFrame | None, ind: dict[str, Any]) -> tuple[float, float, float]:
    """Current ATR(14), 20-bar mean ATR, and ratio (for entry guards / V02)."""
    if past is not None and not past.empty:
        atr_s = _atr_series_wilder(past, 14)
        if len(atr_s) >= 20:
            cur = float(atr_s.iloc[-1])
            avg = float(atr_s.iloc[-20:].mean())
            if math.isfinite(cur) and math.isfinite(avg) and avg > 0:
                return cur, avg, cur / avg
    cur = float(ind.get("atr", 0) or 0)
    if cur > 0 and math.isfinite(cur):
        return cur, cur, 1.0
    return 0.0, 0.0, 1.0


def _v75_entry_candle_body(past: pd.DataFrame | None) -> float:
    if past is None or past.empty:
        return 0.0
    try:
        row = past.iloc[-1]
        o = float(row.get("Open", row.get("Close", 0)) or 0)
        c = float(row.get("Close", 0) or 0)
        return c - o
    except (TypeError, ValueError, KeyError, IndexError):
        return 0.0


def _momentum_neutral_ranging_skip(
    *,
    sym: str,
    strat_id: str,
    macro_bias: str,
    analysis_date: str,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
) -> tuple[bool, str]:
    """
    Skip momentum strategies only when macro is NEUTRAL and conditions are ranging / low vol.
    Uses trade-condition snapshot (market_phase, volatility_regime) — not sizing regime_manager tiers.
    """
    sid = str(strat_id or "").strip().upper()
    if sid not in MOMENTUM_STRATEGIES:
        return False, ""
    if str(macro_bias or "").strip().upper() != "NEUTRAL":
        return False, ""
    snap = _trade_condition_snapshot_fields(analysis_date, past, ind)
    mp = str(snap.get("market_phase", "")).strip().upper()
    vr = str(snap.get("volatility_regime", "")).strip().upper()
    if mp == "RANGING" or vr == "LOW_VOL":
        msg = (
            f"[NEUTRAL+RANGING SKIP] {sid} {sym.strip().upper()}: NEUTRAL macro + "
            f"{mp or 'UNKNOWN'} market / {vr or 'UNKNOWN'} vol — momentum strategy skipped"
        )
        log(msg, level="info")
        return True, msg
    return False, ""


def _v75_v02_backtest_allowed(past: pd.DataFrame | None, ind: dict[str, Any]) -> tuple[bool, str]:
    """V02 stays BLOCKED in strategy_status but may fire in backtest when vol is expanding."""
    snap = _trade_condition_snapshot_fields("", past, ind)
    vr = str(snap.get("volatility_regime", "UNKNOWN")).strip().upper()
    if vr not in ("HIGH_VOL", "EXTREME_VOL"):
        return False, f"volatility_regime={vr}"
    cur, avg, _ = _v75_atr_metrics(past, ind)
    if avg <= 0 or cur < avg * 1.2:
        log(
            f"[V02 REGIME SKIP] ATR {cur:.5f} < 1.2× 20-period avg {avg:.5f} — not enough volatility expansion",
            level="info",
        )
        return False, "ATR not expanding enough"
    return True, ""


def _v75_backtest_strategy_allowed(
    sid: str,
    *,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
) -> bool:
    sid_u = str(sid or "").strip().upper()
    if sid_u == "V02_ATR_EXPANSION_ENTRY":
        ok, _ = _v75_v02_backtest_allowed(past, ind)
        return ok
    return sid_u not in BLOCKED_STRATEGIES


def _v75_buffer_stop_price(normal_stop: float, entry: float, direction: str, atr: float) -> float:
    """Widen stop by 0.3×ATR for the first forward candle (v7.5)."""
    a = float(atr or 0)
    if a <= 0 or not math.isfinite(a):
        return float(normal_stop)
    d = str(direction or "").strip().upper()
    ns = float(normal_stop)
    e = float(entry)
    if d == "LONG":
        return ns - 0.3 * a
    if d == "SHORT":
        return ns + 0.3 * a
    return ns


def _v75_entry_guards(
    *,
    sym: str,
    tf_key: str,
    direction: str,
    entry: float,
    normal_stop: float,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    """
    Fix B/C entry filters + compute buffer/normal stops.
    Returns (ok, skip_reason, metadata dict for trade rows).
    """
    sym_u = sym.strip().upper()
    tf_u = tf_key.strip().lower()
    d = direction.strip().upper()
    meta: dict[str, Any] = {
        "buffer_stop_used": False,
        "buffer_stop_price": None,
        "normal_stop_price": round(float(normal_stop), 5),
        "entry_atr": 0.0,
        "entry_atr_vs_avg": 0.0,
        "entry_candle_body": 0.0,
        "low_atr_skipped": False,
        "body_skipped": False,
    }
    cur_atr, avg_atr, ratio = _v75_atr_metrics(past, ind)
    meta["entry_atr"] = round(cur_atr, 5)
    meta["entry_atr_vs_avg"] = round(ratio, 4)
    body = _v75_entry_candle_body(past)
    meta["entry_candle_body"] = round(body, 5)

    if avg_atr > 0 and cur_atr < avg_atr * 0.5:
        meta["low_atr_skipped"] = True
        log(
            f"[LOW ATR SKIP] {sym_u} {tf_u}: ATR {cur_atr:.5f} < 50% of 20-period avg "
            f"{avg_atr:.5f} — stop too tight, skipping",
            level="info",
        )
        return (
            False,
            f"[LOW ATR SKIP] {sym_u} {tf_u}: ATR {cur_atr:.5f} < 50% of avg {avg_atr:.5f}",
            meta,
        )

    if cur_atr > 0:
        if d == "LONG" and body < -(1.5 * cur_atr):
            meta["body_skipped"] = True
            log(
                f"[BODY SKIP] {sym_u} {tf_u} LONG: current candle body {body:.5f} < -1.5*ATR — "
                f"adverse momentum, skipping",
                level="info",
            )
            return (
                False,
                f"[BODY SKIP] {sym_u} {tf_u} LONG: body {body:.5f} < -1.5*ATR",
                meta,
            )
        if d == "SHORT" and body > (1.5 * cur_atr):
            meta["body_skipped"] = True
            log(
                f"[BODY SKIP] {sym_u} {tf_u} SHORT: current candle body {body:.5f} > 1.5*ATR — "
                f"adverse momentum, skipping",
                level="info",
            )
            return (
                False,
                f"[BODY SKIP] {sym_u} {tf_u} SHORT: body {body:.5f} > 1.5*ATR",
                meta,
            )

    ns = validate_stop_loss(float(entry), float(normal_stop), d, tf_u)
    meta["normal_stop_price"] = round(ns, 5)
    buf = _v75_buffer_stop_price(ns, float(entry), d, cur_atr)
    meta["buffer_stop_price"] = round(buf, 5)
    meta["buffer_stop_used"] = abs(buf - ns) > 1e-9
    return True, "", meta


def _v75_log_one_candle_loss(
    *,
    sym: str,
    tf_key: str,
    strat_id: str,
    direction: str,
    entry: float,
    normal_stop: float,
    buffer_stop: float,
    forward_df: pd.DataFrame,
    atr: float,
    candle_body: float,
    exit_price: float,
) -> None:
    try:
        low = float(forward_df["Low"].iloc[0])
    except (TypeError, ValueError, KeyError, IndexError):
        low = entry
    log(
        f"[1-CANDLE LOSS] {sym} {tf_key} {strat_id}: entry={entry:.5f} stop={normal_stop:.5f} "
        f"buffer_stop={buffer_stop:.5f} low={low:.5f} ATR={atr:.5f} body={candle_body:.5f} — "
        f"stopped at {exit_price:.5f}",
        level="info",
    )


def build_strategy_conditions_report(strategy_id: str) -> dict[str, Any]:
    """
    GET /api/strategy_conditions/{strategy_id} — WR and P&L by
    volatility_regime × market_phase × macro_bias (completed trades only).
    """
    sid = (strategy_id or "").strip().upper()
    rows = load_all_results()
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("skipped"):
            continue
        if str(r.get("strategy_id", "")).strip().upper() != sid:
            continue
        if r.get("outcome") not in ("WIN", "LOSS"):
            continue
        vr = str(r.get("volatility_regime", "UNKNOWN") or "UNKNOWN")
        mp = str(r.get("market_phase", "UNKNOWN") or "UNKNOWN")
        mb = str(r.get("macro_bias", "UNKNOWN") or "UNKNOWN").strip().upper()
        key = f"{vr}|{mp}|{mb}"
        b = buckets.setdefault(key, {"count": 0, "wins": 0, "pnl": 0.0})
        b["count"] += 1
        if r.get("outcome") == "WIN":
            b["wins"] += 1
        b["pnl"] += float(r.get("pnl_dollars", 0) or 0)
    breakdown: list[dict[str, Any]] = []
    for key, b in sorted(buckets.items()):
        n = int(b["count"])
        wr = (100.0 * float(b["wins"]) / n) if n else 0.0
        parts = key.split("|", 2)
        breakdown.append(
            {
                "volatility_regime": parts[0] if len(parts) > 0 else "",
                "market_phase": parts[1] if len(parts) > 1 else "",
                "macro_bias": parts[2] if len(parts) > 2 else "",
                "trades": n,
                "win_rate_pct": round(wr, 2),
                "pnl_dollars": round(float(b["pnl"]), 2),
            }
        )
    return {"strategy_id": sid, "buckets": breakdown, "bucket_count": len(breakdown)}


def safe_yf_fetch(
    yf_ticker: str,
    start: str,
    end: str,
    interval: str,
    retries: int = 3,
) -> pd.DataFrame | None:
    """Sequential yfinance download: global lock + mandatory spacing + rate-limit backoff."""
    for attempt in range(retries):
        try:
            with _yf_lock:
                df = yf.download(
                    yf_ticker,
                    start=start,
                    end=end,
                    interval=interval,
                    progress=False,
                    auto_adjust=True,
                )
                time.sleep(2.0)
            return df
        except Exception as e:  # noqa: BLE001
            err = str(e)
            if "Too Many Requests" in err or "Rate" in err or "429" in err:
                wait = 60 * (attempt + 1)
                log(f"[YF RateLimit] attempt {attempt + 1} — waiting {wait}s", level="warning")
                time.sleep(wait)
            else:
                log(f"[YF Error] {yf_ticker}: {err}", level="warning")
                return None
    return None


def safe_yf_download(
    ticker: str,
    start: str,
    end: str,
    interval: str,
    retries: int = 3,
) -> pd.DataFrame | None:
    """Backward-compatible alias for chrono OHLCV path."""
    return safe_yf_fetch(ticker, start, end, interval, retries)


def _get_ohlcv_download_impl(
    yf_ticker: str,
    timeframe: str,
    analysis_date: str,
    *,
    chrono_yfinance: bool = False,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Download + split past/future OHLCV (no chrono batch cache or 730-day log)."""
    if not chrono_yfinance and CHRONO_RUNNING:
        return None, None
    tf_key = timeframe.lower().strip()
    if chrono_yfinance and tf_key in ("1h", "4h"):
        try:
            ad = datetime.strptime(analysis_date.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            ad = None
        if ad is not None and not scan_date_has_yf_chrono_hourly_data(ad):
            return None, None
    tf_cfg: dict[str, dict[str, Any]] = {
        "15m": {"interval": "15m", "days_back": 60, "days_fwd": 7},
        "30m": {"interval": "30m", "days_back": 120, "days_fwd": 14},
        "1h": {"interval": "1h", "days_back": 180, "days_fwd": 14},
        "4h": {"interval": "1h", "resample": "4h", "days_back": 365, "days_fwd": 30},
        "1d": {"interval": "1d", "days_back": 730, "days_fwd": 60},
        "1w": {"interval": "1wk", "days_back": 1825, "days_fwd": 120},
    }
    cfg = dict(tf_cfg.get(tf_key, tf_cfg["4h"]))
    if chrono_yfinance and tf_key == "15m":
        cfg["days_back"] = 55
        cfg["interval"] = "15m"
    elif chrono_yfinance and tf_key == "30m":
        cfg["days_back"] = 55
        cfg["interval"] = "30m"
    elif chrono_yfinance and tf_key == "4h":
        cfg = {"interval": "4h", "days_back": 50, "days_fwd": 30}
    target = datetime.strptime(analysis_date.strip(), "%Y-%m-%d")
    start = target - timedelta(days=int(cfg["days_back"]))
    end = target + timedelta(days=int(cfg["days_fwd"]))
    start_s = start.strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")
    interval_s = str(cfg["interval"])

    if chrono_yfinance:
        df = safe_yf_download(yf_ticker, start_s, end_s, interval_s)
        if df is None or df.empty:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
    else:
        with _yf_lock:
            df = yf.Ticker(yf_ticker).history(
                start=start_s,
                end=end_s,
                interval=interval_s,
            )
            time.sleep(2.0)
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


def get_ohlcv(
    yf_ticker: str,
    timeframe: str,
    analysis_date: str,
    *,
    chrono_yfinance: bool = False,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Fetch OHLCV for backtest window; resample 1h→4h when needed."""
    if not chrono_yfinance and CHRONO_RUNNING:
        return None, None
    tf_key = timeframe.lower().strip()
    ds = analysis_date.strip()[:10]
    if chrono_yfinance and tf_key in ("1h", "4h"):
        try:
            ad = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            ad = None
        if ad is not None and not scan_date_has_yf_chrono_hourly_data(ad):
            if ds not in _yf_hourly_skip_logged_dates:
                _yf_hourly_skip_logged_dates.add(ds)
                log(
                    f"[SKIP] 1h/4h data not available for dates before "
                    f"{yf_chrono_hourly_earliest_scan_date().isoformat()}",
                    level="info",
                )
            return None, None
    if chrono_yfinance:
        ck = (yf_ticker.strip().upper(), tf_key, ds)
        with _chrono_ohlc_cache_lock:
            if ck in _chrono_ohlc_cache:
                return _chrono_ohlc_cache.pop(ck)
    return _get_ohlcv_download_impl(yf_ticker, timeframe, analysis_date, chrono_yfinance=chrono_yfinance)


def _chrono_prefetch_ohlc_for_phase_matrix(
    date_str: str,
    phases: list[tuple[str, list[str], float, float, bool]],
    tickers: list[str],
    v71_pi_s: int,
    v71_tj_s: int,
) -> None:
    """Warm Yahoo OHLC for each (phase × timeframe) slice in parallel (chrono only)."""
    workers = max(1, int(os.environ.get("APEX_PARALLEL_WORKERS", "8")))
    for pi_pf in range(v71_pi_s, len(phases)):
        _label_pf, tf_list_pf, _, _, _ = phases[pi_pf]
        tj0_pf = v71_tj_s if pi_pf == v71_pi_s else 0
        for tj_pf in range(tj0_pf, len(tf_list_pf)):
            tf_raw = tf_list_pf[tj_pf]
            tf_key_pf = str(tf_raw).lower().strip()
            pf_key = (date_str, pi_pf, tj_pf)
            with _chrono_ohlc_cache_lock:
                if pf_key in _chrono_ohlc_prefetch_done:
                    continue
                _chrono_ohlc_prefetch_done.add(pf_key)
            log(
                f"[CHRONO] Parallel OHLC prefetch: {len(tickers)} tickers {tf_key_pf} "
                f"(workers={workers})",
                level="info",
            )

            def _warm_one(sym: str) -> None:
                s = (sym or "").strip().upper()
                is_fx = len(s) == 6 and s.isalpha()
                yf_t = s + "=X" if is_fx else s
                ck = (yf_t.strip().upper(), tf_key_pf, date_str.strip()[:10])
                try:
                    past_f, fut_f = _get_ohlcv_download_impl(
                        yf_t, str(tf_raw), date_str, chrono_yfinance=True
                    )
                except Exception as e_w:  # noqa: BLE001
                    log(f"[ChronoPrefetch] {s} {tf_key_pf}: {e_w}", level="warning")
                    past_f, fut_f = None, None
                with _chrono_ohlc_cache_lock:
                    _chrono_ohlc_cache[ck] = (past_f, fut_f)

            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_warm_one, tickers))


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
    session_context: str,
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
        session_context=session_context,
        news_sentiment=news_sentiment,
        cot_bias=cot_bias,
        fear_greed=fear_greed,
        vix_val=vix_val,
    )


def _format_apex_master_v4(
    *,
    ticker: str,
    tf_key: str,
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
    session_line: str,
    qualifying_str: str,
) -> str:
    """APEX master prompt v4.0 (three-layer + forensic framing)."""
    e20 = float(ind.get("ema20", price) or price)
    e50 = float(ind.get("ema50", price) or price)
    e200 = float(ind.get("ema200", price) or price)
    rsi = float(ind.get("rsi", 50) or 50)
    adx = float(ind.get("adx", 20) or 20)
    atr = float(ind.get("atr", 0) or 0) or abs(price) * 0.01
    macd_hist = float(ind.get("macd_hist", 0) or 0)
    bb_u = float(ind.get("bb_upper", price) or price)
    bb_l = float(ind.get("bb_lower", price) or price)
    sh = ind.get("swing_highs", []) or []
    sl = ind.get("swing_lows", []) or []

    dist_e20 = abs(price - e20)
    dist_e50 = abs(price - e50)
    chk_both_above = "YES" if e20 > e200 and e50 > e200 else "NO"
    chk_both_below = "YES" if e20 < e200 and e50 < e200 else "NO"
    m_e20_atr = "✓" if dist_e20 <= atr * 2 else "✗"
    m_e50_atr = "✓" if dist_e50 <= atr * 2 else "✗"
    m_rsi_band = "✓" if 25 <= rsi <= 70 else "✗"
    m_adx = "✓" if adx >= 12 else "✗"
    lbl_s03_long = "✓" if zone_pct < 50 else "✗ BLOCKED"
    lbl_s03_short = "✓" if zone_pct > 50 else "✗ BLOCKED"
    s04_zone_gate = (
        "✓"
        if (tf_key == "1d" and (zone_pct <= 10 or zone_pct >= 90))
        or (tf_key == "1w" and (zone_pct <= 15 or zone_pct >= 85))
        else "✗ NOT EXTREME ENOUGH"
    )
    lbl_s04_rsi_l = "✓" if rsi < 35 else "✗"
    lbl_s04_rsi_s = "✓" if rsi > 65 else "✗"
    lbl_s04_adx = "✓" if adx < 45 else "✗"
    s08_bb_label = "qualifies" if bb_width < 3.0 else "not compressed"
    s15_lbl = (
        "LONG candidate"
        if rsi <= 30
        else "SHORT candidate"
        if rsi >= 70
        else "neutral"
    )

    head = f"""You are APEX — elite institutional forex trading AI.

Forensic analysis of 2,475 real trades reveals exactly what works.
Every rule below is backed by hard performance data.
Your job: confirm setups Python found, or reject with a clear reason.
SKIP aggressively. Professionals skip 60-70% of potential setups.

=======================================================
MARKET DATA
=======================================================
Asset:      {ticker}
Timeframe:  {tf_key.upper()}
Context:    {tf_label}
Date:       {analysis_date}
Price:      {price:.5f}
Session:    {session_line}

52w Zone:   {zone_label} ({zone_pct:.1f}%)
52w High:   {high_52w:.5f}
52w Low:    {low_52w:.5f}

EMA20:      {e20:.5f}  (distance: {dist_e20:.5f})
EMA50:      {e50:.5f}  (distance: {dist_e50:.5f})
EMA200:     {e200:.5f}
ATR:        {atr:.5f}  (2x ATR: {atr * 2:.5f})
RSI:        {rsi:.1f}
ADX:        {adx:.1f}
MACD Hist:  {macd_hist:.6f}
BB Width:   {bb_width:.2f}%
BB Upper:   {bb_u:.5f}
BB Lower:   {bb_l:.5f}
Swing H:    {sh}
Swing L:    {sl}

Python pre-filter found these potential setups:
{qualifying_str}
(Confirm or reject each one based on full analysis below)

{intel_text}

=======================================================
FORENSIC PERFORMANCE DATA — 2,475 REAL TRADES
These numbers are non-negotiable facts, not estimates
=======================================================

TIMEFRAME REALITY:
  1W:  +$4,267 total P&L — WHERE ALL THE PROFIT COMES FROM
  1D:  +$230   total P&L — marginal positive
  4H:  -$996   total P&L — NET NEGATIVE (general strategies)

  4H IS NEGATIVE BECAUSE: stops too tight (hit by daily noise
  before trades develop). Layer 3 intraday strategies on 4H
  exit same-session so they avoid this problem.

S03 EMA PULLBACK:
  Overall: 42.9% WR, +$2,040 total
  1W:  66.7% WR — best timeframe for S03
  1D:  50.0% WR — solid
  4H:  ONLY allowed if minimum stop 0.8%, all 4/4 met, HIGH conviction
  ZONE RULES (proven from data):
    LONG in discount (<50%): 53% WR, +$3,597
    LONG in equilibrium (>50%): 28% WR, -$4,065 — FORBIDDEN
    SHORT only in premium (>50%)

S04 EXTREME REVERSION:
  1W:  75.0% WR — #1 strategy
  1D:  66.7% WR — excellent
  4H:  BLOCKED PERMANENTLY — forensic data:
       USDZAR 4H: 0% WR, USDMXN 4H: 12% WR, GBPJPY 4H weak
  REQUIREMENTS:
    1W: zone <15% or >85%, RSI <35 or >65, ADX <45
    1D: zone <10% or >90%, RSI <35 or >65, ADX <45

CORRELATION WARNING:
  USDMXN + USDZAR + USDNOK + USDSEK = same trade (USD vs EM)
  Max 2 from same correlation group open simultaneously

MACRO REGIME FILTER:
  Before S04 LONG: has this pair made lower lows for 3+ weeks?
  If yes: structural breakdown, not correction. SKIP.

=======================================================
THREE-LAYER STRATEGY SYSTEM
=======================================================

LAYER 1 — WEEKLY ANCHOR (highest conviction, largest size)
═══════════════════════════════════════════════════════

S03: EMA TREND PULLBACK — 66.7% WR on 1W
Best timeframe: 1W. Good on 1D. 4H only with strict conditions.

Non-negotiables (need 3 of 4 for MEDIUM, all 4 for HIGH):
  □ EMA20 and EMA50 on same side of EMA200
    Current: EMA20={e20:.5f}, EMA50={e50:.5f}, EMA200={e200:.5f}
    Both above EMA200: {chk_both_above}
    Both below EMA200: {chk_both_below}
  □ Price within 2x ATR of EMA20 or EMA50
    EMA20 dist: {dist_e20:.5f} vs 2xATR: {atr * 2:.5f} → {m_e20_atr}
    EMA50 dist: {dist_e50:.5f} vs 2xATR: {atr * 2:.5f} → {m_e50_atr}
  □ RSI between 25 and 70 (room to run)
    Current RSI: {rsi:.1f} → {m_rsi_band}
  □ ADX above 12 (trend exists)
    Current ADX: {adx:.1f} → {m_adx}

Zone rules (HARD — never override):
  LONG: zone must be below 50% (discount) → currently {zone_pct:.1f}% {lbl_s03_long}
  SHORT: zone must be above 50% (premium) → currently {zone_pct:.1f}% {lbl_s03_short}
  For HIGH confidence: LONG needs zone <25%, SHORT needs zone >75%

Stop: beyond EMA50 + 0.3x ATR minimum. On 4H: minimum 0.8% stop.

S04: EXTREME ZONE REVERSION — 75% WR on 1W, 66.7% on 1D
HARD BLOCK: NEVER use on 4H, 1H, 30M, 15M.

Non-negotiables (need ALL 3 for HIGH, minimum 2 for MEDIUM):
  □ Zone extreme: 1D needs <10% or >90%, 1W needs <15% or >85%
    Current zone: {zone_pct:.1f}% on {tf_key} → {s04_zone_gate}
  □ RSI extreme: LONG needs RSI<35, SHORT needs RSI>65
    Current RSI: {rsi:.1f} → LONG {lbl_s04_rsi_l} / SHORT {lbl_s04_rsi_s}
  □ ADX below 45 (not a runaway trend)
    Current ADX: {adx:.1f} → {lbl_s04_adx}

LAYER 2 — DAILY MOMENTUM (LOW confidence, gathering data)
═══════════════════════════════════════════════════════
S11 SR FLIP, S01 BREAKOUT RETEST, S05 MACD, S02 LIQUIDITY SWEEP,
S06 ORDER BLOCK, S07 FVG, S08 RANGE BREAKOUT (1D only), S12 VOL COMPRESSION.
Swing data: Highs={sh} Lows={sl}

S08 RANGE BREAKOUT (1D ONLY, blocked 4H)
  BB Width <3.0% + ADX <30 + clean close outside BB
  Current BB Width: {bb_width:.2f}% → {s08_bb_label}

S12: BB Width <2.0% + ADX <20 = coiled spring
  Current: BB {bb_width:.2f}% ADX {adx:.1f}

LAYER 3 — INTRADAY EVENT-DRIVEN (15M/30M, exit same session)
═══════════════════════════════════════════════════════
S13–S18: LOW confidence; no overnight; TP1 faster (1.5R intraday).
S15 VWAP: RSI below 30 LONG / above 70 SHORT — current RSI {rsi:.1f} → {s15_lbl}

=======================================================
INTELLIGENCE DATA
=======================================================
Fear & Greed:   {fear_greed:.0f}/100
VIX:            {vix_val:.1f}
News sentiment: {news_sentiment:+.2f}
COT bias:       {cot_bias}

Intelligence adjusts conviction ±1 only. Never trade on intel alone.

=======================================================
HOW TO DECIDE — MANDATORY CHECKLIST
=======================================================
STEP 1: HARD BLOCKS — S04 off 4H/1H/30M/15M; S03 LONG zone<50; S03 SHORT zone>50;
  S08 off 4H; Layer 3 not on 1D/1W; 4H stop ≥0.8%.
STEP 2: SCAN ALL — score each strategy; pick highest score passing STEP 1.
STEP 3: SETUP QUALITY — structural stop, genuine edge.
STEP 4: MACRO — sustained lower lows → skip S04 LONG.
STEP 5: CONFIDENCE — HIGH 2% S03/S04 1W proven; MEDIUM 1%; LOW 0.5%; Layer3 always LOW.
STEP 6: STOPS/TARGETS — TP1=2R (intraday 1.5R), TP2=3R, TP3=5R; trail per plan.
STEP 7: CONVICTION 1–8 max.

=======================================================
OUTPUT — VALID JSON ONLY. NO PROSE. NO PREAMBLE.
=======================================================
"""

    tail = _APEX_V4_OUTPUT_SCHEMA_FALLBACK
    return head + "\n" + tail.format(
        zone_pct=zone_pct,
        zone_label=zone_label,
        price=price,
    )


def _format_apex_master_v5(
    *,
    ticker: str,
    tf_key: str,
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
    session_line: str,
    qualifying_str: str,
) -> str:
    """APEX master prompt v5.0 — 68 strategies, condensed categories."""
    _ = (tf_label, news_sentiment, cot_bias, fear_greed, vix_val)
    e20 = float(ind.get("ema20", price) or price)
    e50 = float(ind.get("ema50", price) or price)
    e200 = float(ind.get("ema200", price) or price)
    rsi = float(ind.get("rsi", 50) or 50)
    adx = float(ind.get("adx", 20) or 20)
    atr = float(ind.get("atr", 0) or 0) or abs(price) * 0.01
    macd_hist = float(ind.get("macd_hist", 0) or 0)
    sh = ind.get("swing_highs", []) or []
    sl = ind.get("swing_lows", []) or []

    head = f"""
You are APEX v5.0 — elite institutional forex trading AI with 68 strategies.

Python pre-filter found these candidates: {qualifying_str}
Your job: confirm the BEST setup, set precise entry/stop/targets, or SKIP.

MARKET DATA:
Asset: {ticker} | TF: {tf_key} | Date: {analysis_date} | Session: {session_line}
Price: {price:.5f} | Zone: {zone_label} ({zone_pct:.1f}%)
52w: {low_52w:.5f} — {high_52w:.5f}
EMA20: {e20:.5f} | EMA50: {e50:.5f} | EMA200: {e200:.5f}
ATR: {atr:.5f} | RSI: {rsi:.1f} | ADX: {adx:.1f}
MACD: {macd_hist:.6f} | BB Width: {bb_width:.2f}%
Swings H: {sh} | L: {sl}

{intel_text}

═══ STRATEGY CATEGORIES (68 total) ═══

TREND (T01-T10): EMA pullback, crossover, HH/HL, ADX entry, supertrend,
SAR flip, MA ribbon, Donchian, Keltner, 200 EMA bounce.
USE WHEN: EMAs aligned, ADX>15, price pulling back in trend direction.

REVERSION (R01-R10): Zone extreme, RSI divergence, BB touch, stochastic,
CCI, Williams %R, VWAP deviation, monthly level, weekly gap, COT extreme.
USE WHEN: Zone <20% or >80%, RSI extreme, price at structural level.

BREAKOUT (B01-B10): Range, compression, London open, NY open, opening range,
triangle, inside bar, key level retest, RSI momentum, weekly range.
USE WHEN: BB compressed (<3%), ADX low (<25), price at range boundary.

MOMENTUM (M01-M08): MACD divergence, zero cross, RSI continuation, ROC,
stochastic cross, price acceleration, volume surge, news.
USE WHEN: MACD confirming direction, RSI 40-60 crossing center, ADX rising.

STRUCTURE (SMC01-SMC10): SR flip, order block, FVG, liquidity sweep,
equal HL hunt, inducement, breaker, mitigation, premium/discount+MSS, CHoCH.
USE WHEN: Price at swing level with 2+ touches, structural break visible.

VOLATILITY (V01-V06): VIX spike, ATR expansion, squeeze, pre-news,
post-news, vol mean reversion.
USE WHEN: BB width extreme (very narrow or very wide), ATR expanding/contracting.

STATISTICAL (Q01-Q06): Day of week, time of day, seasonality, carry trade,
correlation divergence, regime detection.
USE WHEN: Statistical edge from timing or positioning data.

INTRADAY (I01-I08): VWAP scalp, HTF rejection, first hour reversal,
power hour, gap fade, Asia fade, micro structure, session close.
USE WHEN: 15M/30M timeframe during active session with RSI extreme.

═══ DECISION RULES ═══

1. CHECK each candidate from Python pre-filter list above
2. For each one: does the chart ACTUALLY show this setup clearly?
3. Pick the BEST one (highest conviction after visual confirmation)
4. If NONE are genuine setups → SKIP (don't force trades)
5. Minimum R:R = 1:2. Stop at structural invalidation level.

CONFIDENCE:
HIGH (2%): Proven strategy (T01/R01) with all conditions met on 1D/1W
MEDIUM (1%): Strong setup on any TF, or proven strategy partially met
LOW (0.5%): New/untested strategy, intraday, or marginal setup

IMPORTANT — RELAXED RULES vs v4.0:
- T01 (EMA pullback): needs 2 of 4 non-negotiables (was 3 of 4)
- R01 (zone reversion): zone <20% or >80% (was <15% or >85%)
- R01: RSI <40 or >60 (was <35 or >65)
- New strategies start at LOW confidence, graduate to MEDIUM after 10+ trades
- 4H trades allowed for ALL strategies except R01 reversion
- Multiple strategies can qualify — pick the highest conviction one

═══ OUTPUT — VALID JSON ONLY ═══
"""

    tail = _APEX_V5_OUTPUT_SCHEMA_FALLBACK
    return head.strip() + "\n" + tail.format(
        zone_pct=zone_pct,
        zone_label=zone_label,
        price=price,
    )


_APEX_V4_OUTPUT_SCHEMA_FALLBACK = """
If skipping:
{{
  "skip_trade":             true,
  "skip_reason":            "specific reason max 20 words",
  "strategy_id":            "SKIP",
  "strategy_name":          "",
  "strategy_met":           false,
  "verdict":                "SKIP",
  "direction":              "NONE",
  "confidence":             "SKIP",
  "conviction_score":       0,
  "zone_pct":               {zone_pct},
  "zone_label":             "{zone_label}",
  "price_zone":             "{zone_label}",
  "zone_position_pct":      {zone_pct},
  "smc_concept":            "NONE",
  "smc_direction":          "",
  "is_exotic":              false,
  "confluence_points":      0,
  "entry":                  0,
  "stop_loss":              0,
  "tp1":                    0,
  "tp2":                    0,
  "tp3":                    0,
  "tp_target":              "TP1",
  "rr_ratio":               "",
  "signals_used":           [],
  "confluences":            [],
  "conflicts":              [],
  "htf_bias":               "",
  "market_structure":       "",
  "core_signals_met":       [],
  "core_signals_failed":    [],
  "non_negotiables_met":    [],
  "non_negotiables_failed": [],
  "reasoning":              "why no setup exists max 20 words"
}}

If trading:
{{
  "skip_trade":             false,
  "strategy_id":            "S03_EMA_PULLBACK",
  "strategy_name":          "EMA Trend Pullback",
  "strategy_met":           true,
  "core_signals_met":       [],
  "core_signals_failed":    [],
  "non_negotiables_met":    [],
  "non_negotiables_failed": [],
  "verdict":                "BUY",
  "direction":              "LONG",
  "confidence":             "MEDIUM",
  "conviction_score":       6,
  "zone_pct":               {zone_pct},
  "zone_label":             "{zone_label}",
  "price_zone":             "DISCOUNT",
  "zone_position_pct":      {zone_pct},
  "htf_bias":               "BULLISH",
  "market_structure":       "UPTREND_PULLBACK",
  "smc_concept":            "NONE",
  "smc_direction":          "",
  "is_exotic":              false,
  "confluence_points":      3,
  "entry":                  {price},
  "entry_reasoning":        "price at EMA20 pullback in uptrend",
  "stop_loss":              0.00000,
  "stop_reasoning":         "below EMA50 structural support",
  "tp1":                    0.00000,
  "tp2":                    0.00000,
  "tp3":                    0.00000,
  "tp_target":              "TP2",
  "rr_ratio":               "1:3.0",
  "trailing_plan":          "BE at TP1 (2R), +1R at TP2 (3R), trail 1.5R at TP3 (5R)",
  "signals_used":           [],
  "confluences":            [],
  "conflicts":              [],
  "intel_summary":          "VIX 18 neutral, COT bullish +1",
  "reasoning":              "S03 1W: all 4/4 non-negotiables, discount zone 18%, 66.7% WR timeframe."
}}

HARD OUTPUT RULES:
  direction: LONG or SHORT only (never NONE for a trade)
  strategy_id: S01-S18 or SKIP
  strategy_met: true ONLY if 2+ non-negotiables confirmed
  conviction_score: integer 1-8, never 9+
  rr_ratio: minimum 1:2.0
  reasoning: maximum 60 words
  All signal names: UPPERCASE_WITH_UNDERSCORES
"""


_APEX_V5_OUTPUT_SCHEMA_FALLBACK = """
If skipping:
{{
  "skip_trade": true,
  "skip_reason": "specific reason max 20 words",
  "strategy_id": "SKIP",
  "strategy_name": "",
  "strategy_met": false,
  "verdict": "SKIP",
  "direction": "NONE",
  "confidence": "SKIP",
  "conviction_score": 0,
  "zone_pct": {zone_pct},
  "zone_label": "{zone_label}",
  "price_zone": "{zone_label}",
  "zone_position_pct": {zone_pct},
  "smc_concept": "NONE",
  "smc_direction": "",
  "is_exotic": false,
  "confluence_points": 0,
  "entry": 0, "stop_loss": 0, "tp1": 0, "tp2": 0, "tp3": 0,
  "tp_target": "TP1", "rr_ratio": "",
  "signals_used": [], "confluences": [], "conflicts": [],
  "htf_bias": "", "market_structure": "",
  "core_signals_met": [], "core_signals_failed": [],
  "non_negotiables_met": [], "non_negotiables_failed": [],
  "reasoning": "max 30 words"
}}

If trading:
{{
  "skip_trade": false,
  "strategy_id": "T01_EMA_PULLBACK",
  "strategy_name": "EMA Trend Pullback",
  "strategy_met": true,
  "core_signals_met": ["EMA_ALIGNED", "PRICE_AT_EMA20"],
  "core_signals_failed": [],
  "non_negotiables_met": ["EMA_ALIGNED", "PRICE_AT_EMA20"],
  "non_negotiables_failed": [],
  "verdict": "BUY",
  "direction": "LONG",
  "confidence": "MEDIUM",
  "conviction_score": 6,
  "zone_pct": {zone_pct},
  "zone_label": "{zone_label}",
  "price_zone": "DISCOUNT",
  "zone_position_pct": {zone_pct},
  "htf_bias": "BULLISH",
  "market_structure": "UPTREND_PULLBACK",
  "smc_concept": "NONE",
  "smc_direction": "",
  "is_exotic": false,
  "confluence_points": 3,
  "entry": {price},
  "entry_reasoning": "max 15 words",
  "stop_loss": 0.00000,
  "stop_reasoning": "max 15 words",
  "tp1": 0.00000,
  "tp2": 0.00000,
  "tp3": 0.00000,
  "tp_target": "TP2",
  "rr_ratio": "1:3.0",
  "trailing_plan": "BE at TP1, +1R at TP2, trail 1.5R at TP3",
  "signals_used": ["EMA_BULLISH", "RSI_NEUTRAL"],
  "confluences": ["ADX trend confirmed", "discount zone"],
  "conflicts": [],
  "intel_summary": "max 15 words",
  "reasoning": "max 40 words"
}}

HARD RULES:
- direction: LONG or SHORT only (never NONE for trades)
- strategy_id: use the exact ID from the 68 strategies above
- strategy_met: true ONLY if setup is genuinely visible on chart
- conviction_score: integer 1-8, hard cap 8
- minimum R:R 1:2
- reasoning: maximum 40 words
"""


def _format_apex_master_v6(
    *,
    ticker: str,
    tf_key: str,
    analysis_date: str,
    price: float,
    zone_label: str,
    zone_pct: float,
    high_52w: float,
    low_52w: float,
    ind: dict[str, Any],
    bb_width: float,
    intel_text: str,
    qualifying_str: str,
) -> str:
    """APEX v7.0 — Layer 1 (T01 / R01) only; hardened skip language."""
    return f"""
You are APEX v7.0 — institutional forex trading AI.

Python pre-filter confirmed: {qualifying_str}
These are LAYER 1 setups only (T01_EMA_PULLBACK or R01_EXTREME_ZONE_REVERSION).
Your job: confirm or deny each setup with precise entry/stop/targets.

MARKET DATA:
Pair:    {ticker}
TF:      {tf_key}
Date:    {analysis_date}
Price:   {price:.5f}
Zone:    {zone_label} ({zone_pct:.1f}%)
52w Lo:  {low_52w:.5f}
52w Hi:  {high_52w:.5f}
EMA20:   {float(ind.get("ema20", price) or price):.5f}
EMA50:   {float(ind.get("ema50", price) or price):.5f}
EMA200:  {float(ind.get("ema200", price) or price):.5f}
ATR:     {float(ind.get("atr", 0) or 0):.5f}
RSI:     {float(ind.get("rsi", 50) or 50):.1f}
ADX:     {float(ind.get("adx", 20) or 20):.1f}
MACD H:  {float(ind.get("macd_hist", 0) or 0):.6f}
BB Wid:  {bb_width:.2f}%
Swings:  H={ind.get("swing_highs", [])} L={ind.get("swing_lows", [])}

{intel_text}

═══ T01 EMA PULLBACK — non-negotiables ═══
Direction LONG:  EMA20/50 both above EMA200 AND zone_pct < 60%
Direction SHORT: EMA20/50 both below EMA200 AND zone_pct > 40%
Entry at: price within 2.5x ATR of EMA20 or EMA50
ADX: any value >= 12

═══ R01 EXTREME ZONE — non-negotiables ═══
R01 SHORT non-negotiables (ALL must be met, no exceptions):
1. zone_pct >= 80% (extreme premium)
2. RSI >= 60 — REQUIRED, not optional. If RSI < 60, SKIP.
3. ADX <= 45 — REQUIRED. If ADX > 45, trend too strong, SKIP.
4. Timeframe: 1D or 1W only.

R01 LONG non-negotiables (ALL must be met, no exceptions):
1. zone_pct <= 20% (extreme discount)
2. RSI <= 40 — REQUIRED, not optional. If RSI > 40, SKIP.
3. ADX <= 45 — REQUIRED.
4. Timeframe: 1D or 1W only.

CRITICAL INSTRUCTION TO CLAUDE: Do NOT skip the RSI check and fall back to ADX alone.
Both RSI AND ADX must be satisfied. If RSI does not meet threshold, return verdict: SKIP
regardless of how extreme the zone is.

BLOCKED on 4H (1D and 1W only)

═══ RULES ═══
1. Check if the qualifying strategy actually matches the data above
2. If YES → set precise stop (structural level, not arbitrary %)
3. If NO → SKIP with specific reason (max 15 words)
4. Minimum R:R = 1:2. Preferred = 1:3.
5. Confidence HIGH (2%) only if ALL non-negotiables met on 1D/1W
6. Confidence MEDIUM (1%) if most met or on 4H
7. DO NOT skip because of "no forensic data" — that is not a reason
8. DO NOT skip because "Layer 2 strategy" — those are handled elsewhere

═══ VALID JSON OUTPUT ONLY ═══

If skipping:
{{
  "skip_trade": true,
  "skip_reason": "specific reason max 15 words",
  "strategy_id": "SKIP",
  "strategy_name": "",
  "strategy_met": false,
  "verdict": "SKIP",
  "direction": "NONE",
  "confidence": "SKIP",
  "conviction_score": 0,
  "zone_pct": {zone_pct},
  "zone_label": "{zone_label}",
  "price_zone": "{zone_label}",
  "zone_position_pct": {zone_pct},
  "smc_concept": "NONE",
  "smc_direction": "",
  "is_exotic": false,
  "confluence_points": 0,
  "entry": 0, "stop_loss": 0, "tp1": 0, "tp2": 0, "tp3": 0,
  "tp_target": "TP1", "rr_ratio": "",
  "signals_used": [], "confluences": [], "conflicts": [],
  "htf_bias": "", "market_structure": "",
  "core_signals_met": [], "core_signals_failed": [],
  "non_negotiables_met": [], "non_negotiables_failed": [],
  "reasoning": "max 20 words"
}}

If trading:
{{
  "skip_trade": false,
  "strategy_id": "T01_EMA_PULLBACK",
  "strategy_name": "EMA Trend Pullback",
  "strategy_met": true,
  "core_signals_met": ["EMA_ALIGNED_BULLISH", "WITHIN_2ATR_EMA20"],
  "core_signals_failed": [],
  "non_negotiables_met": ["EMA_BULL", "ZONE_BELOW_60"],
  "non_negotiables_failed": [],
  "verdict": "BUY",
  "direction": "LONG",
  "confidence": "HIGH",
  "conviction_score": 7,
  "zone_pct": {zone_pct},
  "zone_label": "{zone_label}",
  "price_zone": "DISCOUNT",
  "zone_position_pct": {zone_pct},
  "htf_bias": "BULLISH",
  "market_structure": "UPTREND_PULLBACK",
  "smc_concept": "NONE",
  "smc_direction": "",
  "is_exotic": false,
  "confluence_points": 3,
  "entry": {price:.5f},
  "entry_reasoning": "price at EMA20 in uptrend",
  "stop_loss": 0.00000,
  "stop_reasoning": "below most recent swing low",
  "tp1": 0.00000,
  "tp2": 0.00000,
  "tp3": 0.00000,
  "tp_target": "TP2",
  "rr_ratio": "1:3.0",
  "trailing_plan": "BE at TP1, +1R at TP2, trail 1.5R at TP3",
  "signals_used": ["EMA_BULLISH", "RSI_PULLBACK"],
  "confluences": ["ADX confirmed trend", "discount zone support"],
  "conflicts": [],
  "intel_summary": "max 12 words",
  "reasoning": "max 30 words"
}}

HARD RULES:
- direction must be LONG or SHORT (never NONE for trades)
- strategy_id must be T01_EMA_PULLBACK or R01_EXTREME_ZONE_REVERSION
- strategy_met: true ONLY if setup genuinely visible in the data above
- conviction_score: integer 1-8, cap at 8
- minimum R:R 1:2 or SKIP
- reasoning: maximum 30 words, no filler
""".strip()


def enforce_rules(
    ai: dict[str, Any],
    timeframe: str,
    price: float,
    ticker: str,
    *,
    rsi: float | None = None,
) -> dict[str, Any]:
    """Post-parse enforcement — v7.0 Layer 1 + Python-forced Layer 2 sizing (model text cannot override)."""
    tf = (timeframe or "").strip().lower()
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
    strategy_id = str(ai.get("strategy_id", "SKIP")).strip().upper()
    strategy_met = bool(ai.get("strategy_met", False))

    if ai.get("skip_trade", False):
        return ai

    if not strategy_met:
        ai["skip_trade"] = True
        ai["skip_reason"] = "strategy_met is False"
        return ai

    if strategy_id == "R01_EXTREME_ZONE_REVERSION":
        if tf in ("4h", "1h", "30m", "15m"):
            ai["skip_trade"] = True
            ai["skip_reason"] = f"R01 blocked on {tf}"
            return ai
        if rsi is not None:
            try:
                rsi_f = float(rsi)
            except (TypeError, ValueError):
                rsi_f = None
            if rsi_f is not None:
                if direction == "SHORT" and rsi_f < 60.0:
                    ai["skip_trade"] = True
                    ai["skip_reason"] = "R01 SHORT requires RSI >= 60, got " + str(rsi_f)
                    return ai
                if direction == "LONG" and rsi_f > 40.0:
                    ai["skip_trade"] = True
                    ai["skip_reason"] = "R01 LONG requires RSI <= 40, got " + str(rsi_f)
                    return ai

    if strategy_id == "T01_EMA_PULLBACK":
        if direction == "LONG" and zone_pct > 60:
            ai["skip_trade"] = True
            ai["skip_reason"] = f"T01 LONG blocked zone {zone_pct:.0f}%>60%"
            return ai
        if direction == "SHORT" and zone_pct < 40:
            ai["skip_trade"] = True
            ai["skip_reason"] = f"T01 SHORT blocked zone {zone_pct:.0f}%<40%"
            return ai

    if strategy_id in _INTRADAY_STRATEGY_IDS and tf not in ("15m", "30m"):
        ai["skip_trade"] = True
        ai["skip_reason"] = f"{strategy_id} only on 15M/30M"
        return ai
    if strategy_id in _INTRADAY_STRATEGY_IDS:
        ai["confidence"] = "LOW"

    try:
        ai["conviction_score"] = min(int(round(float(ai.get("conviction_score", 5)))), 8)
    except (TypeError, ValueError):
        ai["conviction_score"] = 5

    if stop and entry and stop != entry:
        stop_pct = abs(entry - stop) / entry * 100
        mn = float(MIN_STOP_PCT.get(tf, 0.3))
        mx = float(MAX_STOP_PCT.get(tf, 1.5))
        mult = 1 if direction == "LONG" else -1
        if stop_pct < mn:
            new_stop = entry * (1 - mult * mn / 100)
            risk = abs(entry - new_stop)
            ai["stop_loss"] = round(new_stop, 5)
            ai["tp1"] = round(entry + mult * risk * 2, 5)
            ai["tp2"] = round(entry + mult * risk * 3, 5)
            ai["tp3"] = round(entry + mult * risk * 5, 5)
        elif stop_pct > mx:
            new_stop = entry * (1 - mult * mx / 100)
            risk = abs(entry - new_stop)
            ai["stop_loss"] = round(new_stop, 5)
            ai["tp1"] = round(entry + mult * risk * 2, 5)
            ai["tp2"] = round(entry + mult * risk * 3, 5)
            ai["tp3"] = round(entry + mult * risk * 5, 5)

    stop = float(ai.get("stop_loss", 0) or 0)
    entry = float(ai.get("entry", price) or price)

    confidence = str(ai.get("confidence", "LOW")).strip().upper()
    if confidence not in RISK_BY_CONFIDENCE:
        confidence = "LOW"
    risk_pct = float(RISK_BY_CONFIDENCE.get(confidence, 0.005))
    risk_dollars = STARTING_CAPITAL * risk_pct
    stop_dist = abs(entry - float(ai.get("stop_loss", entry * 0.99) or 0))
    if stop_dist > 0:
        pos_size = risk_dollars / stop_dist
        exposure = pos_size * entry
        if exposure < 1500:
            pos_size = 1500 / entry
        ai["_position_size"] = round(pos_size, 2)
        ai["_leveraged_exposure"] = round(pos_size * entry, 2)
        ai["_max_risk_dollars"] = round(risk_dollars, 2)
        ai["_account_risk_pct"] = risk_pct

    return ai


def _v73_refresh_risk_fields_from_ai_confidence(
    ai: dict[str, Any],
    timeframe: str,
    price: float,
    ticker: str,
) -> None:
    """FIX 23 — recompute dollar risk from ``ai['confidence']`` after macro upgrade."""
    if ai.get("skip_trade"):
        return
    tf = (timeframe or "").strip().lower()
    try:
        entry = float(ai.get("entry", price) or price)
    except (TypeError, ValueError):
        entry = float(price)
    direction = str(ai.get("direction", "NONE")).strip().upper()
    stop = float(ai.get("stop_loss", 0) or 0)
    if stop and entry and stop != entry:
        stop_pct = abs(entry - stop) / entry * 100
        mn = float(MIN_STOP_PCT.get(tf, 0.3))
        mx = float(MAX_STOP_PCT.get(tf, 1.5))
        mult = 1 if direction == "LONG" else -1
        if stop_pct < mn:
            new_stop = entry * (1 - mult * mn / 100)
            risk = abs(entry - new_stop)
            ai["stop_loss"] = round(new_stop, 5)
            ai["tp1"] = round(entry + mult * risk * 2, 5)
            ai["tp2"] = round(entry + mult * risk * 3, 5)
            ai["tp3"] = round(entry + mult * risk * 5, 5)
        elif stop_pct > mx:
            new_stop = entry * (1 - mult * mx / 100)
            risk = abs(entry - new_stop)
            ai["stop_loss"] = round(new_stop, 5)
            ai["tp1"] = round(entry + mult * risk * 2, 5)
            ai["tp2"] = round(entry + mult * risk * 3, 5)
            ai["tp3"] = round(entry + mult * risk * 5, 5)

    entry = float(ai.get("entry", price) or price)
    confidence = str(ai.get("confidence", "LOW")).strip().upper()
    if confidence not in RISK_BY_CONFIDENCE:
        confidence = "LOW"
    risk_pct = float(RISK_BY_CONFIDENCE.get(confidence, 0.005))
    if tf == "4h":
        risk_pct *= 0.6
    if ai.get("_v74_perfect_storm_m03_jpy") and confidence == "HIGH":
        risk_pct = 0.028
    balance = float(ai.get("_balance_for_sizing") or STARTING_CAPITAL)
    risk_dollars = balance * risk_pct
    risk_dollars = min(risk_dollars, balance * 0.055)
    if ai.get("_jpy_risk_headroom") is not None:
        try:
            hr = float(ai.get("_jpy_risk_headroom"))
            if hr >= 0:
                risk_dollars = min(risk_dollars, hr)
        except (TypeError, ValueError):
            pass
    stop_dist = abs(entry - float(ai.get("stop_loss", entry * 0.99) or 0))
    if stop_dist > 0:
        pos_size = risk_dollars / stop_dist
        exposure = pos_size * entry
        if exposure < 1500:
            pos_size = 1500 / entry
        ai["_position_size"] = round(pos_size, 2)
        ai["_leveraged_exposure"] = round(pos_size * entry, 2)
        ai["_max_risk_dollars"] = round(risk_dollars, 2)
        ai["_account_risk_pct"] = risk_pct


def _v73_post_enforce_macro_confidence(
    ai: dict[str, Any],
    macro_bt: dict[str, Any] | None,
    *,
    tf_key: str,
    price: float,
    sym: str,
) -> None:
    """FIX 23 — apply macro confidence upgrade/downgrade after ``enforce_rules``."""
    if not macro_bt or ai.get("skip_trade"):
        return
    pre = str(ai.get("confidence", "LOW")).strip().upper()
    if pre not in ("HIGH", "MEDIUM", "LOW"):
        pre = "LOW"
    new_c = apply_macro_confidence_adjustment(pre, macro_bt)
    if new_c != pre:
        ai["confidence_pre_upgrade"] = pre
        ai["confidence"] = new_c
        log(
            f"Macro upgrade: {sym} {pre} -> {new_c} confidence "
            f"(macro_confidence_upgrade={int(macro_bt.get('confidence_upgrade', 0) or 0)})",
            level="info",
        )
        _v73_refresh_risk_fields_from_ai_confidence(ai, tf_key, float(price), sym)


def _strategy_confluence_multiplier(strategy_count: int, *, triple_tf_agreement: bool = False) -> float:
    """v7.4 — multiplier from locked-strategy confluence count only (1 / 1.25 / 1.50 cap)."""
    _ = triple_tf_agreement
    if strategy_count >= 3:
        return 1.5
    if strategy_count == 2:
        return 1.25
    return 1.0


def _chrono_accumulate_prefilter_signals(sym: str, rows: list[Any], tf_key: str) -> None:
    """FIX 29 — record qualifying strategy ids per (ticker, direction) for the chrono day."""
    su = (sym or "").strip().upper()
    tft = (tf_key or "").strip().lower()
    for row in rows:
        if not row or len(row) < 3:
            continue
        sid = str(row[0]).strip().upper()
        dr = str(row[1]).strip().upper()
        if dr == "BOTH":
            CHRONO_DAY_PREFILTER_SIDS[(su, "LONG")].add(sid)
            CHRONO_DAY_PREFILTER_SIDS[(su, "SHORT")].add(sid)
            CHRONO_SYMDIR_TFS[(su, "LONG")].add(tft)
            CHRONO_SYMDIR_TFS[(su, "SHORT")].add(tft)
        elif dr in ("LONG", "SHORT"):
            CHRONO_DAY_PREFILTER_SIDS[(su, dr)].add(sid)
            CHRONO_SYMDIR_TFS[(su, dr)].add(tft)
    if su in JPY_STORM_PAIRS:
        for row in rows or []:
            if row and len(row) >= 2 and str(row[1]).strip().upper() in ("LONG", "SHORT", "BOTH"):
                CHRONO_JPY_PAIRS_SIGNALLED.add(su)
                break


def _local_prefilter_confluence_count(sym: str, direction: str, rows: list[Any]) -> int:
    """Distinct *locked-list* strategy ids from prefilter agreeing with ``direction`` (non-chrono)."""
    d = (direction or "").strip().upper()
    s: set[str] = set()
    for row in rows or []:
        if not row or len(row) < 2:
            continue
        sid = str(row[0]).strip().upper()
        if not _locked_confluence_sid(sid):
            continue
        dr = str(row[1]).strip().upper()
        if dr == "BOTH" or dr == d:
            s.add(sid)
    return len(s)


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


def _pnl_dollars_if_stop_hit(
    entry_price: float,
    stop_price: float,
    direction: str,
    position_size: float,
) -> float:
    """Dollar P&L if position exits at stop_price (FIX 11)."""
    if position_size <= 0 or not math.isfinite(position_size):
        return float("inf")
    d = (direction or "").strip().upper()
    if d == "LONG":
        return (float(stop_price) - float(entry_price)) * float(position_size)
    if d == "SHORT":
        return (float(entry_price) - float(stop_price)) * float(position_size)
    return float("inf")


def _apply_trailing_dollar_floor(
    entry_price: float,
    current_stop: float,
    proposed_stop: float,
    direction: str,
    position_size: float,
) -> float:
    """Do not tighten stop if locked profit would be under $25 (FIX 11 part B)."""
    if _pnl_dollars_if_stop_hit(entry_price, proposed_stop, direction, position_size) < 25.0:
        return current_stop
    return proposed_stop


def detect_market_regime(macro_bias: str, trend_strength: float, rate_diff: float) -> str:
    """Legacy helper — forward sim uses ``detect_trailing_regime`` (v7.4 master thresholds)."""
    mb = str(macro_bias or "").strip().upper()
    try:
        ts = float(trend_strength)
    except (TypeError, ValueError):
        ts = 0.0
    try:
        rd = float(rate_diff)
    except (TypeError, ValueError):
        rd = 0.0
    if mb == "STRONG_TAILWIND" and ts > 0.70 and rd > 2.0:
        return "TRENDING"
    return "CHOPPY"


def detect_trailing_regime(macro_bias_adjusted: str, trend_strength: float, macro_rate_diff: float) -> str:
    """v7.4 master — TRENDING trail mode when macro tailwind + trend + carry diff > 150bp."""
    mb = str(macro_bias_adjusted or "").strip().upper()
    try:
        ts = float(trend_strength)
    except (TypeError, ValueError):
        ts = 0.0
    try:
        rd = float(macro_rate_diff)
    except (TypeError, ValueError):
        rd = 0.0
    if mb in ("STRONG_TAILWIND", "TAILWIND") and ts > 0.65 and rd > 1.5:
        return "TRENDING"
    return "CHOPPY"


def detect_perfect_storm() -> tuple[bool, int]:
    """
    v7.4 — ≥3 listed JPY pairs signalled today, all STRONG_TAILWIND + HIGH conf + trend>0.65
    + same weekly direction_bias + rate differential > 2.5%.
    """
    firing = sorted(s for s in JPY_STORM_PAIRS if s in CHRONO_JPY_PAIRS_SIGNALLED)
    if len(firing) < 3:
        return False, len(firing)
    snaps: list[dict[str, Any]] = []
    for s in firing:
        sn = CHRONO_JPY_STORM_SNAPSHOT.get(s)
        if isinstance(sn, dict):
            snaps.append(sn)
    if len(snaps) < 3:
        return False, len(snaps)
    biases = {str(x.get("trend_bias", "NEUTRAL")).strip().upper() for x in snaps}
    if biases != {"LONG"} and biases != {"SHORT"}:
        return False, len(snaps)
    for x in snaps:
        if str(x.get("macro_bias", "")).strip().upper() != "STRONG_TAILWIND":
            return False, len(snaps)
        if str(x.get("confidence", "")).strip().upper() != "HIGH":
            return False, len(snaps)
        if float(x.get("trend_strength", 0) or 0) <= 0.65:
            return False, len(snaps)
        if float(x.get("rate_differential", 0) or 0) <= 2.5:
            return False, len(snaps)
    return True, len(snaps)


def evaluate_forward_candles(
    direction: str,
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    forward_df: pd.DataFrame,
    strategy_id: str = "",
    *,
    position_size: float = 0.0,
    leverage: int = 0,
    timeframe: str = "",
    macro_bias: str = "",
    trend_strength: float = 0.0,
    rate_differential: float = 0.0,
    atr: float = 0.0,
    buffer_stop_price: float | None = None,
    ticker: str = "",
) -> dict[str, Any]:
    """v7.5 — first-candle buffer stop; v7.4+ regime TP ladders and trailing (TRENDING vs CHOPPY)."""
    _ = strategy_id
    _ = leverage
    _ = tp1
    _ = tp2
    _ = tp3
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
            "trail_market_regime": "CHOPPY",
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
            "trail_market_regime": "CHOPPY",
        }

    d = str(direction or "").strip().upper()
    if d not in ("LONG", "SHORT"):
        return {
            "outcome": "INVALID",
            "exit_price": float(entry),
            "exit_reason": "Invalid direction",
            "pnl_pct": 0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_stop": False,
            "candles_to_exit": 0,
            "trailing_activated": False,
            "final_stop": float(stop_loss),
            "trail_market_regime": "CHOPPY",
        }

    entry_price = float(entry)
    tf_lc = (timeframe or "").strip().lower()
    regime = (
        "CHOPPY"
        if tf_lc == "4h"
        else detect_trailing_regime(macro_bias, trend_strength, rate_differential)
    )
    sym_l = (ticker or "").strip().upper() or "?"
    log(
        f"[TRAIL REGIME] {sym_l} {tf_lc or '?'}: {regime} "
        f"(macro={str(macro_bias or '').strip().upper()} trend={trend_strength:.2f} "
        f"rate_diff={rate_differential:.2f})",
        level="info",
    )
    mult1, mult2, mult3 = (2.0, 4.0, 7.0) if regime == "TRENDING" else (1.5, 3.0, 5.0)
    sign = 1.0 if d == "LONG" else -1.0
    tp1p = entry_price + sign * risk * mult1
    tp2p = entry_price + sign * risk * mult2
    tp3p = entry_price + sign * risk * mult3

    atr_use = float(atr or 0) or risk
    if atr_use <= 0:
        atr_use = abs(entry_price) * 0.001

    ps = float(position_size or 0.0)
    rem = 1.0
    realized = 0.0
    normal_stop = float(stop_loss)
    buf_stop = float(buffer_stop_price) if buffer_stop_price is not None else normal_stop
    use_buffer = abs(buf_stop - normal_stop) > 1e-9
    current_stop = buf_stop if use_buffer else normal_stop
    buffer_active = use_buffer
    hit_tp1 = hit_tp2 = hit_tp3 = hit_stop = False
    trailing_activated = False
    exit_price = float(entry_price)
    exit_reason = "Window ended"
    candle_count = 0
    choppy_tb_done = False
    peak_hi = float("-inf")
    peak_lo = float("inf")
    candles_to_tp1: int | None = None
    peak_profit_dollars = 0.0

    def _px_move(px: float) -> float:
        if d == "LONG":
            return px - entry_price
        return entry_price - px

    def _close_frac(frac: float, px: float) -> None:
        nonlocal rem, realized
        if frac <= 0 or rem <= 0 or ps <= 0:
            return
        q = ps * rem * min(1.0, frac)
        realized += q * _px_move(px)
        rem = max(0.0, rem * (1.0 - min(1.0, frac)))

    rows = list(forward_df.iterrows())
    for i, (_, candle) in enumerate(rows):
        if buffer_active and i >= 1 and not hit_tp1:
            current_stop = normal_stop
            buffer_active = False
        candle_count += 1
        try:
            high = float(candle.get("High", entry_price))
            low = float(candle.get("Low", entry_price))
            close = float(candle.get("Close", entry_price))
        except (TypeError, ValueError):
            continue

        prev_row = rows[i - 1][1] if i > 0 else None
        try:
            prev_low = float(prev_row.get("Low", close)) if prev_row is not None else low
            prev_high = float(prev_row.get("High", close)) if prev_row is not None else high
        except (TypeError, ValueError):
            prev_low = low
            prev_high = high

        if ps > 0 and rem > 0:
            mark = close
            peak_profit_dollars = max(peak_profit_dollars, realized + ps * rem * _px_move(mark))

        if regime == "CHOPPY" and hit_tp1 and not hit_tp3 and prev_row is not None:
            if d == "LONG":
                prop = prev_low - 0.5 * atr_use
                if prop > current_stop:
                    ns = _apply_trailing_dollar_floor(entry_price, current_stop, prop, d, ps * rem)
                    if ns > current_stop:
                        trailing_activated = True
                    current_stop = ns
            else:
                prop = prev_high + 0.5 * atr_use
                if prop < current_stop:
                    ns = _apply_trailing_dollar_floor(entry_price, current_stop, prop, d, ps * rem)
                    if ns < current_stop:
                        trailing_activated = True
                    current_stop = ns

        if d == "LONG":
            if low <= current_stop and rem > 0:
                hit_stop = True
                q = ps * rem
                realized += q * (current_stop - entry_price)
                rem = 0.0
                exit_price = current_stop
                exit_reason = "Trailing stop" if trailing_activated or hit_tp1 else "Stop loss"
                break

            if high >= tp1p and not hit_tp1:
                hit_tp1 = True
                candles_to_tp1 = candle_count
                trailing_activated = True
                current_stop = _apply_trailing_dollar_floor(
                    entry_price, current_stop, entry_price, d, ps * rem,
                )
                if regime == "CHOPPY":
                    _close_frac(0.25, tp1p)
                    log(
                        f"[TRAIL] TP1 hit CHOPPY — stop breakeven, closed 25%, trailing started",
                        level="info",
                    )
                else:
                    log(f"[TRAIL] TP1 hit — stop moved to breakeven, full position kept", level="info")

            if high >= tp2p and not hit_tp2 and hit_tp1:
                hit_tp2 = True
                trailing_activated = True
                if regime == "TRENDING":
                    _close_frac(0.20, tp2p)
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, tp1p, d, ps * rem,
                    )
                else:
                    _close_frac(0.40, tp2p)
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, tp1p, d, ps * rem,
                    )

            if high >= tp3p and not hit_tp3 and hit_tp2:
                hit_tp3 = True
                trailing_activated = True
                if regime == "CHOPPY":
                    _close_frac(1.0, tp3p)
                    exit_price = tp3p
                    exit_reason = "Take profit TP3 (choppy full exit)"
                    break
                _close_frac(0.30, tp3p)
                peak_hi = max(peak_hi, high, close, tp3p)
                prop = peak_hi - 2.0 * atr_use
                current_stop = _apply_trailing_dollar_floor(
                    entry_price, current_stop, max(current_stop, prop), d, ps * rem,
                )

            if regime == "TRENDING" and hit_tp3 and rem > 0:
                peak_hi = max(peak_hi, high, close)
                prop = peak_hi - 2.0 * atr_use
                if prop > current_stop:
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, prop, d, ps * rem,
                    )

        else:
            if high >= current_stop and rem > 0:
                hit_stop = True
                q = ps * rem
                realized += q * (entry_price - current_stop)
                rem = 0.0
                exit_price = current_stop
                exit_reason = "Trailing stop" if trailing_activated or hit_tp1 else "Stop loss"
                break

            if low <= tp1p and not hit_tp1:
                hit_tp1 = True
                candles_to_tp1 = candle_count
                trailing_activated = True
                current_stop = _apply_trailing_dollar_floor(
                    entry_price, current_stop, entry_price, d, ps * rem,
                )
                if regime == "CHOPPY":
                    _close_frac(0.25, tp1p)
                    log(
                        f"[TRAIL] TP1 hit CHOPPY — stop breakeven, closed 25%, trailing started",
                        level="info",
                    )
                else:
                    log(f"[TRAIL] TP1 hit — stop moved to breakeven, full position kept", level="info")

            if low <= tp2p and not hit_tp2 and hit_tp1:
                hit_tp2 = True
                trailing_activated = True
                if regime == "TRENDING":
                    _close_frac(0.20, tp2p)
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, tp1p, d, ps * rem,
                    )
                else:
                    _close_frac(0.40, tp2p)
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, tp1p, d, ps * rem,
                    )

            if low <= tp3p and not hit_tp3 and hit_tp2:
                hit_tp3 = True
                trailing_activated = True
                if regime == "CHOPPY":
                    _close_frac(1.0, tp3p)
                    exit_price = tp3p
                    exit_reason = "Take profit TP3 (choppy full exit)"
                    break
                _close_frac(0.30, tp3p)
                peak_lo = min(peak_lo, low, close, tp3p)
                prop = peak_lo + 2.0 * atr_use
                current_stop = _apply_trailing_dollar_floor(
                    entry_price, current_stop, min(current_stop, prop), d, ps * rem,
                )

            if regime == "TRENDING" and hit_tp3 and rem > 0:
                peak_lo = min(peak_lo, low, close)
                prop = peak_lo + 2.0 * atr_use
                if prop < current_stop:
                    current_stop = _apply_trailing_dollar_floor(
                        entry_price, current_stop, prop, d, ps * rem,
                    )

        if regime == "CHOPPY" and hit_tp1 and not hit_tp2 and candle_count >= 4 and not choppy_tb_done:
            half_tp2 = abs(tp2p - entry_price) * 0.5
            if half_tp2 > 0:
                prog = abs(close - entry_price) if d == "LONG" else abs(entry_price - close)
                if prog < half_tp2:
                    _close_frac(0.5, close)
                    choppy_tb_done = True
                    trailing_activated = True
                    if rem <= 1e-12:
                        hit_stop = True
                        exit_price = close
                        exit_reason = "Time-based partial (choppy)"
                        break

    if not hit_stop and rem > 0 and not forward_df.empty:
        try:
            exit_price = float(forward_df["Close"].iloc[-1])
        except (TypeError, ValueError, KeyError, IndexError):
            exit_price = float(entry_price)
        if ps > 0:
            realized += ps * rem * _px_move(exit_price)
        rem = 0.0

    denom = ps * entry_price if ps > 0 and entry_price > 0 else 0.0
    pnl_pct = (realized / denom) if denom > 0 else 0.0

    exit_reason_norm = exit_reason
    er_l = exit_reason.lower()
    if hit_tp3 and "tp3" in er_l:
        exit_reason_norm = "TP3"
    elif hit_tp2 and "tp2" in er_l:
        exit_reason_norm = "TP2"
    elif hit_tp1 and not hit_stop and "window" in er_l:
        exit_reason_norm = "TP1"
    elif "time-based" in er_l or "time exit" in er_l:
        exit_reason_norm = "TIME_EXIT"
    elif hit_stop and trailing_activated:
        exit_reason_norm = "TRAIL_STOP"
    elif hit_stop:
        exit_reason_norm = "STOP"

    exit_vs_peak = (
        round((realized / peak_profit_dollars) * 100.0, 2)
        if peak_profit_dollars > 1e-9
        else None
    )

    return {
        "outcome": "WIN" if pnl_pct > 0 else "LOSS",
        "exit_price": round(exit_price, 5),
        "exit_reason": exit_reason_norm,
        "pnl_pct": round(pnl_pct, 6),
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
        "hit_tp3": hit_tp3,
        "hit_stop": hit_stop,
        "candles_to_exit": candle_count,
        "candles_to_tp1": candles_to_tp1,
        "trailing_activated": trailing_activated,
        "final_stop": round(current_stop, 5),
        "trail_market_regime": regime,
        "peak_profit_dollars": round(peak_profit_dollars, 2),
        "exit_vs_peak_pct": exit_vs_peak,
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


def _v73_regime_row_fields(reg: dict[str, Any] | None) -> dict[str, Any]:
    r = reg or {}
    return {
        "regime": str(r.get("regime", "NORMAL")),
        "regime_size_multiplier": float(r.get("size_multiplier", 1.0) or 1.0),
        "regime_wr_10": float(r.get("wr_10", 0.5) or 0.5),
        "regime_consecutive_losses": int(r.get("consecutive_losses", 0) or 0),
    }


def _v74_apply_recipe_and_monday_boosts(
    ai: dict[str, Any],
    *,
    sym: str,
    strat_id: str,
    locked_confluence: int,
    tf_key: str,
    analysis_date: str,
) -> None:
    """v7.5 — Monday 1w boost (tailwind macro) + CADJPY/USDJPY/CHFJPY recipe sizing (after base stack)."""
    bal = float(ai.get("_balance_for_sizing") or STARTING_CAPITAL)
    try:
        ent = float(ai.get("entry", 0) or 0)
        stp = float(ai.get("stop_loss", 0) or 0)
    except (TypeError, ValueError):
        return
    sd = abs(ent - stp)
    if sd <= 0 or not math.isfinite(sd):
        return
    mrd = float(ai.get("_max_risk_dollars", 0) or 0)
    if mrd <= 0:
        return
    cap_hi = bal * float(RISK_BY_CONFIDENCE["HIGH"]) * 1.5
    sym_u = sym.strip().upper()
    mb = str(ai.get("macro_bias", "")).strip().upper()
    conf = str(ai.get("confidence", "")).strip().upper()
    d0: date | None = None
    try:
        d0 = date.fromisoformat(analysis_date.strip()[:10])
    except (TypeError, ValueError):
        pass
    if (
        tf_key.strip().lower() == "1w"
        and d0 is not None
        and d0.weekday() == 0
        and mb in ("STRONG_TAILWIND", "TAILWIND")
    ):
        mrd = min(mrd * 1.25, cap_hi)
        log(
            f"[MONDAY BOOST] {sym_u} {tf_key.upper()} "
            f"{str(ai.get('direction', '')).strip().upper()}: 1.25x Monday multiplier applied",
            level="info",
        )
    rec_sid = {
        "M03_RSI_MOMENTUM_CONTINUATION",
        "B09_RSI_MOMENTUM_BREAK",
        "T01_EMA_PULLBACK",
        "M06_PRICE_ACCELERATION",
        "SMC10_CHOCH",
        "T08_DONCHIAN_BREAKOUT",
        "M02_MACD_ZERO_CROSS",
    }
    if (
        sym_u in ("CADJPY", "USDJPY", "CHFJPY")
        and mb == "STRONG_TAILWIND"
        and conf == "MEDIUM"
        and 2 <= int(locked_confluence) <= 4
        and strat_id in rec_sid
    ):
        cap5 = bal * 0.05
        mrd = min(mrd * 1.5, cap5, cap_hi)
        log(
            f"[RECIPE BOOST] {sym_u} {strat_id} STRONG_TAILWIND MEDIUM conf:{locked_confluence} — 1.5x boost",
            level="info",
        )
    mrd = max(25.0, min(mrd, cap_hi))
    ai["_max_risk_dollars"] = round(mrd, 2)
    ai["_position_size"] = round(mrd / sd, 2)
    ai["_leveraged_exposure"] = round((mrd / sd) * ent, 2)


def _v73_apply_calendar_regime_position_size(
    ai: dict[str, Any],
    *,
    confidence: str,
    calendar_risk: dict[str, Any] | None,
    regime: dict[str, Any] | None,
    macro: dict[str, Any] | None = None,
    trend_mult: float = 1.0,
    confluence_mult: float = 1.0,
    entry: float,
) -> None:
    """Stack calendar × regime × macro × trend × strategy-confluence on ``_max_risk_dollars`` (FIX 28–29)."""
    cal = calendar_risk or {"size_multiplier": 1.0}
    reg = regime or {"size_multiplier": 1.0}
    cm = max(0.0, float(cal.get("size_multiplier", 1.0) or 0.0))
    rm = max(0.0, float(reg.get("size_multiplier", 1.0) or 0.0))
    mm = max(0.0, float((macro or {}).get("size_multiplier", 1.0) or 0.0))
    tm = max(0.0, float(trend_mult or 0.0))
    cf = max(0.0, float(confluence_mult or 0.0))
    comb = cm * rm * mm * tm * cf
    ent = float(entry)
    mrd0 = float(ai.get("_max_risk_dollars", 0) or 0)
    st0 = float(ai.get("stop_loss", 0) or 0)
    if mrd0 <= 0 or ent <= 0:
        return
    sd0 = abs(ent - st0)
    if sd0 <= 0:
        return
    mrd1 = round(mrd0 * comb, 2)
    c = str(confidence or "LOW").strip().upper()
    if c not in RISK_BY_CONFIDENCE:
        c = "LOW"
    bal_sz = float(ai.get("_balance_for_sizing") or STARTING_CAPITAL)
    cap = bal_sz * float(RISK_BY_CONFIDENCE["HIGH"]) * 1.5
    mrd1 = max(25.0, min(mrd1, cap))
    ps = mrd1 / sd0
    ai["_max_risk_dollars"] = mrd1
    ai["_position_size"] = round(ps, 2)
    ai["_leveraged_exposure"] = round(ps * ent, 2)


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
        "calendar_action": str(ai.get("calendar_action") or "CLEAR"),
        "calendar_reason": str(
            ai.get("calendar_reason") or "No high-impact events within 48h",
        ),
        **_v73_regime_row_fields(
            {
                "regime": ai.get("regime"),
                "size_multiplier": ai.get("regime_size_multiplier"),
                "wr_10": ai.get("regime_wr_10"),
                "consecutive_losses": ai.get("regime_consecutive_losses"),
            }
        ),
        **merged_macro_result_fields(ai),
        "confidence_pre_upgrade": ai.get("confidence_pre_upgrade"),
        "trend": str(ai.get("trend") or "RANGING"),
        "trend_strength": float(ai.get("trend_strength", 0) or 0),
        "trend_bias": str(ai.get("trend_bias") or "NEUTRAL"),
        "trend_size_mult": float(ai.get("trend_size_mult", 1.0) or 1.0),
        "trend_block": bool(ai.get("trend_block", False)),
        "strategy_confluence_count": int(ai.get("strategy_confluence_count", 0) or 0),
        "strategy_confluence_mult": float(ai.get("strategy_confluence_mult", 1.0) or 1.0),
        "strategies_agreed": ai.get("strategies_agreed")
        if isinstance(ai.get("strategies_agreed"), list)
        else [],
    }


def _python_forced_layer2_trade(
    *,
    sym: str,
    timeframe: str,
    analysis_date: str,
    tf_key: str,
    price: float,
    zone_pct: float,
    zone_label: str,
    is_exotic: bool,
    future: pd.DataFrame,
    layer2: list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]],
    rsi_live: float = 50.0,
    chrono_risk_mult: float = 1.0,
    chrono_tp_mult: float = 1.0,
    chrono_job: bool = False,
    calendar_risk: dict[str, Any] | None = None,
    regime_ctx: dict[str, Any] | None = None,
    chrono_balance: float | None = None,
    chrono_day_pnl: float = 0.0,
    atr_ref: float | None = None,
    past: pd.DataFrame | None = None,
    ind: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute Layer 2+ setup without Claude (v7.1)."""
    if not layer2:
        return None
    best = layer2[0] if len(layer2) == 1 else max(layer2, key=lambda x: int(x[2]))
    strat_id = str(best[0]).strip().upper()
    meta = best[3] if len(best) > 3 else None
    direction = str(best[1]).strip().upper()
    if direction == "BOTH":
        direction = "LONG" if float(zone_pct) < 50 else "SHORT"
    if direction not in ("LONG", "SHORT"):
        return None

    if isinstance(meta, dict) and meta.get("v72_untested"):
        direction = str(meta.get("direction", direction)).strip().upper()
        if direction not in ("LONG", "SHORT"):
            return None
        entry = round(float(meta.get("entry", price)), 5)
        stop = round(float(meta["stop_loss"]), 5)
        tp1 = round(float(meta["tp1"]), 5)
        tp2 = round(float(meta["tp2"]), 5)
        tp3 = round(float(meta.get("tp3", tp2)), 5)
        ai = {
            "skip_trade": False,
            "strategy_id": strat_id,
            "strategy_name": str(STRATEGIES.get(strat_id, {}).get("name", strat_id)),
            "strategy_met": True,
            "direction": direction,
            "confidence": "LOW",
            "conviction_score": 3,
            "entry": entry,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "verdict": "BUY" if direction == "LONG" else "SELL",
            "zone_pct": zone_pct,
            "confluences": [str(x[0]) for x in layer2],
            "signals_used": [strat_id],
            "reasoning": str(meta.get("reasoning") or f"{strat_id}: UNTESTED — gathering first data"),
            "rr_ratio": "1:2.0",
            "tp_target": "TP1",
            "htf_bias": "UNKNOWN",
            "market_structure": "UNKNOWN",
            "core_signals_met": ["PYTHON_LAYER2", "V72_UNTESTED_LOOSE"],
            "core_signals_failed": [],
        }
    else:
        mult = 1 if direction == "LONG" else -1
        entry = round(float(price), 5)
        if isinstance(meta, dict) and meta.get("b02_sl") is not None and float(meta.get("b02_atr14", 0) or 0) > 0:
            stop = round(float(meta["b02_sl"]), 5)
        else:
            stop = round(entry * (1 - mult * 0.005), 5)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        tp1 = round(entry + mult * risk * 2, 5)
        tp2 = round(entry + mult * risk * 3, 5)
        tp3 = round(entry + mult * risk * 5, 5)

        ai = {
            "skip_trade": False,
            "strategy_id": strat_id,
            "strategy_name": str(STRATEGIES.get(strat_id, {}).get("name", strat_id)),
            "strategy_met": True,
            "direction": direction,
            "confidence": "LOW",
            "conviction_score": 3,
            "entry": entry,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "verdict": "BUY" if direction == "LONG" else "SELL",
            "zone_pct": zone_pct,
            "confluences": [str(x[0]) for x in layer2],
            "signals_used": [strat_id],
            "reasoning": f"Python-forced {strat_id}: gathering data at LOW confidence",
            "rr_ratio": "1:2.0",
            "tp_target": "TP1",
            "htf_bias": "UNKNOWN",
            "market_structure": "UNKNOWN",
            "core_signals_met": ["PYTHON_LAYER2"],
            "core_signals_failed": [],
        }
    if isinstance(meta, dict) and meta.get("v7_fallback") and not meta.get("v72_untested"):
        ai["reasoning"] = str(meta.get("reasoning") or ai["reasoning"])
        ai["confidence"] = str(meta.get("confidence") or "LOW").strip().upper()

    scan_d_macro: date | None = None
    try:
        scan_d_macro = date.fromisoformat(analysis_date.strip()[:10])
    except (TypeError, ValueError):
        scan_d_macro = None
    macro_bt = cached_macro_bias(sym, direction, scan_d_macro)
    macro_bt = align_macro_bias_with_price(
        sym,
        direction,
        macro_bt,
        as_of_date=scan_d_macro,
        price_df=past if past is not None and not getattr(past, "empty", True) else None,
        log_fn=log,
    )
    ai.update(macro_result_fields(macro_bt))
    log(
        f"[MACRO] {sym} {direction} — {macro_bt['bias']} (score {macro_bt['composite_score']:+.2f}) "
        f"rate_diff={macro_bt['rate_differential']:+.2f}% {macro_bt['price_trend']}",
        level="info",
    )

    ai = enforce_rules(ai, tf_key, float(price), sym, rsi=float(rsi_live))
    if chrono_job:
        ai["_balance_for_sizing"] = float(chrono_balance or STARTING_CAPITAL)
    _v73_post_enforce_macro_confidence(ai, macro_bt, tf_key=tf_key, price=float(price), sym=sym)
    cal = calendar_risk or {
        "action": "CLEAR",
        "reason": "No high-impact events within 48h",
        "size_multiplier": 1.0,
    }
    if ai.get("skip_trade"):
        ai2 = dict(ai)
        ai2["calendar_action"] = str(cal.get("action", "CLEAR"))
        ai2["calendar_reason"] = str(cal.get("reason", ""))
        ai2.update(_v73_regime_row_fields(regime_ctx))
        return _skipped_backtest_row(
            sym=sym,
            timeframe=timeframe,
            analysis_date=analysis_date,
            price=float(price),
            zone_pct=zone_pct,
            zone_label=zone_label,
            skip_reason=str(ai.get("skip_reason") or "enforce after Python force"),
            ai=ai2,
            tf_key=tf_key,
            is_exotic=is_exotic,
        )

    strat_id = str(ai.get("strategy_id", strat_id)).strip().upper()
    direction = str(ai.get("direction", direction)).strip().upper()
    entry_v75 = float(ai.get("entry", price) or price)
    stop_v75 = float(ai.get("stop_loss", 0) or 0)
    ok_v75, skip_v75, v75_meta = _v75_entry_guards(
        sym=sym,
        tf_key=tf_key,
        direction=direction,
        entry=entry_v75,
        normal_stop=stop_v75,
        past=past,
        ind=ind or {},
    )
    if not ok_v75:
        ai2 = dict(ai)
        ai2.update(v75_meta)
        ai2["calendar_action"] = str(cal.get("action", "CLEAR"))
        ai2["calendar_reason"] = str(cal.get("reason", ""))
        ai2.update(_v73_regime_row_fields(regime_ctx))
        return _skipped_backtest_row(
            sym=sym,
            timeframe=timeframe,
            analysis_date=analysis_date,
            price=float(price),
            zone_pct=zone_pct,
            zone_label=zone_label,
            skip_reason=skip_v75,
            ai=ai2,
            tf_key=tf_key,
            is_exotic=is_exotic,
        )
    normal_stop_v75 = float(v75_meta["normal_stop_price"])
    buffer_stop_v75 = float(v75_meta["buffer_stop_price"] or normal_stop_v75)
    ai["stop_loss"] = normal_stop_v75

    sym_xu = sym.strip().upper()
    if strat_id == "M03_RSI_MOMENTUM_CONTINUATION" and sym_xu not in M03_ALLOWED_TICKERS:
        log(f"[M03 BLOCKED] {sym_xu} not in allowed ticker list", level="info")
        ai2 = dict(ai)
        ai2["calendar_action"] = str(cal.get("action", "CLEAR"))
        ai2["calendar_reason"] = str(cal.get("reason", ""))
        ai2.update(_v73_regime_row_fields(regime_ctx))
        return _skipped_backtest_row(
            sym=sym,
            timeframe=timeframe,
            analysis_date=analysis_date,
            price=float(price),
            zone_pct=zone_pct,
            zone_label=zone_label,
            skip_reason=f"[M03 BLOCKED] {sym_xu} not in allowed ticker list",
            ai=ai2,
            tf_key=tf_key,
            is_exotic=is_exotic,
        )
    ok_mom, rs_mom = _momentum_neutral_ranging_skip(
        sym=sym,
        strat_id=strat_id,
        macro_bias=str(ai.get("macro_bias", "")),
        analysis_date=analysis_date,
        past=past,
        ind=ind or {},
    )
    if ok_mom:
        ai2 = dict(ai)
        ai2["calendar_action"] = str(cal.get("action", "CLEAR"))
        ai2["calendar_reason"] = str(cal.get("reason", ""))
        ai2.update(_v73_regime_row_fields(regime_ctx))
        return _skipped_backtest_row(
            sym=sym,
            timeframe=timeframe,
            analysis_date=analysis_date,
            price=float(price),
            zone_pct=zone_pct,
            zone_label=zone_label,
            skip_reason=rs_mom,
            ai=ai2,
            tf_key=tf_key,
            is_exotic=is_exotic,
        )

    trend_result = cached_apply_trend_filter(
        sym,
        str(ai.get("direction", direction)).strip().upper(),
        strat_id,
        as_of_date=analysis_date.strip()[:10],
    )
    if trend_result.get("action") == "BLOCK":
        log(f"[TREND BLOCK] {sym} {direction} — {trend_result.get('reason', '')}", level="info")
        ai2 = dict(ai)
        ai2["calendar_action"] = str(cal.get("action", "CLEAR"))
        ai2["calendar_reason"] = str(cal.get("reason", ""))
        ai2.update(_v73_regime_row_fields(regime_ctx))
        ai2["skip_reason"] = str(trend_result.get("reason") or "trend block")
        return _skipped_backtest_row(
            sym=sym,
            timeframe=timeframe,
            analysis_date=analysis_date,
            price=float(price),
            zone_pct=zone_pct,
            zone_label=zone_label,
            skip_reason=str(trend_result.get("reason") or "trend block"),
            ai=ai2,
            tf_key=tf_key,
            is_exotic=is_exotic,
        )
    trend_mult = float(trend_result.get("size_multiplier", 1.0) or 1.0)

    sym_u = sym.upper()
    dir_u = str(ai.get("direction", direction)).strip().upper()
    if chrono_job and sym_u in JPY_STORM_PAIRS:
        tr_snap = trend_result.get("trend") or {}
        if not isinstance(tr_snap, dict):
            tr_snap = {}
        CHRONO_JPY_STORM_SNAPSHOT[sym_u] = {
            "macro_bias": str((macro_bt or {}).get("bias", "")),
            "confidence": str(ai.get("confidence", "LOW")).strip().upper(),
            "trend_strength": float(tr_snap.get("strength", 0) or 0),
            "trend_bias": str(tr_snap.get("direction_bias", "NEUTRAL")).strip().upper(),
            "rate_differential": float((macro_bt or {}).get("rate_differential", 0) or 0),
        }

    if chrono_job:
        raw_sids = CHRONO_DAY_PREFILTER_SIDS.get((sym_u, dir_u), set())
        scount = len({s for s in raw_sids if _locked_confluence_sid(s)})
        triple = {"1w", "1d", "4h"}.issubset(CHRONO_SYMDIR_TFS.get((sym_u, dir_u), set()))
    else:
        layer_sids = {
            str(x[0]).strip().upper()
            for x in layer2
            if len(x) >= 2 and (str(x[1]).strip().upper() in ("BOTH", dir_u))
        }
        scount = len({s for s in layer_sids if _locked_confluence_sid(s)})
        triple = False
    cf_mult = _strategy_confluence_multiplier(scount, triple_tf_agreement=triple)
    if scount >= 3 and str(ai.get("tp_target") or "TP1").strip().upper() == "TP2":
        ai["tp_target"] = "TP3"

    ai["_v74_perfect_storm_m03_jpy"] = False
    ai.pop("_jpy_risk_headroom", None)
    if chrono_job:
        bal = float(chrono_balance or STARTING_CAPITAL)
        lim = max(5000.0, bal * 0.05)
        ps_ok, nj = detect_perfect_storm()
        daily_ok = float(chrono_day_pnl) > -lim
        if (
            ps_ok
            and daily_ok
            and strat_id == "M03_RSI_MOMENTUM_CONTINUATION"
            and sym_u in JPY_STORM_PAIRS
            and str(ai.get("confidence", "")).strip().upper() == "HIGH"
        ):
            ai["_v74_perfect_storm_m03_jpy"] = True
            log(
                f"[PERFECT STORM] {nj} JPY pairs aligned — M03 sizing: 2.8% base risk",
                level="info",
            )
        if sym_u.endswith("JPY"):
            hr = bal * 0.20 - float(CHRONO_JPY_RISK_DAY)
            ai["_jpy_risk_headroom"] = max(0.0, hr)

    _v73_refresh_risk_fields_from_ai_confidence(ai, tf_key, float(price), sym)

    _apply_chrono_risk_tp_multipliers(
        ai,
        price=float(price),
        chrono_risk_mult=chrono_risk_mult,
        chrono_tp_mult=chrono_tp_mult,
    )

    _v73_apply_calendar_regime_position_size(
        ai,
        confidence=str(ai.get("confidence", "LOW")),
        calendar_risk=cal,
        regime=regime_ctx,
        macro=macro_bt,
        trend_mult=trend_mult,
        confluence_mult=cf_mult,
        entry=float(ai.get("entry", price) or price),
    )

    _v74_apply_recipe_and_monday_boosts(
        ai,
        sym=sym,
        strat_id=str(ai.get("strategy_id", strat_id)).strip().upper(),
        locked_confluence=int(scount),
        tf_key=tf_key,
        analysis_date=analysis_date,
    )

    entry = float(ai.get("entry", entry) or entry)
    stop = float(ai.get("stop_loss", stop) or stop)
    tp1 = float(ai.get("tp1", tp1) or tp1)
    tp2 = float(ai.get("tp2", tp2) or tp2)
    tp3 = float(ai.get("tp3", tp3) or tp3)
    strat_id = str(ai.get("strategy_id", strat_id)).strip().upper()

    fwd_n = int(TF_FORWARD_CANDLES.get(tf_key, FORWARD_CANDLES))
    fut = future.head(fwd_n)
    if fut.empty or len(fut) < 1:
        return None

    if chrono_job and strat_id in UNTESTED_STRATEGIES_V72 and STRATEGY_TRADE_COUNT.get(strat_id, 0) == 0:
        log(
            f"UNTESTED strategy {strat_id} firing for first time — gathering data",
            level="info",
        )

    log(
        f"[LAYER2-FORCED] {strat_id} {direction} {sym} {tf_key} {analysis_date}",
        level="info",
    )

    tr_ev = trend_result.get("trend") or {}
    if not isinstance(tr_ev, dict):
        tr_ev = {}
    ts_ev = float(tr_ev.get("strength", 0) or 0)

    exit_data = evaluate_forward_candles(
        direction,
        entry,
        normal_stop_v75,
        tp1,
        tp2,
        tp3,
        fut,
        strat_id,
        position_size=float(ai.get("_position_size", 0) or 0),
        leverage=LEVERAGE,
        timeframe=tf_key,
        macro_bias=str(ai.get("macro_bias", "") or ""),
        trend_strength=ts_ev,
        rate_differential=float(ai.get("macro_rate_diff", 0) or 0),
        atr=float(atr_ref or v75_meta.get("entry_atr", 0) or 0),
        buffer_stop_price=buffer_stop_v75,
        ticker=sym,
    )
    if exit_data.get("outcome") in ("NO_DATA", "INVALID"):
        return None

    if (
        int(exit_data.get("candles_to_exit", 0) or 0) == 1
        and bool(exit_data.get("hit_stop"))
        and str(exit_data.get("outcome", "")).upper() == "LOSS"
    ):
        _v75_log_one_candle_loss(
            sym=sym,
            tf_key=tf_key,
            strat_id=strat_id,
            direction=direction,
            entry=entry,
            normal_stop=normal_stop_v75,
            buffer_stop=buffer_stop_v75,
            forward_df=fut,
            atr=float(v75_meta.get("entry_atr", 0) or 0),
            candle_body=float(v75_meta.get("entry_candle_body", 0) or 0),
            exit_price=float(exit_data.get("exit_price", entry) or entry),
        )

    position_size = float(ai.get("_position_size", 0) or 0)
    leveraged_exposure = float(ai.get("_leveraged_exposure", 0) or 0)
    max_risk_dollars = float(ai.get("_max_risk_dollars", 0) or 0)
    sizing_risk_pct = float(ai.get("_account_risk_pct", 0) or 0)
    risk_pct_of_price = abs(entry - stop) / entry if entry > 0 else 0.0
    risk_pct_display = round(risk_pct_of_price * 100, 3)

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
    confidence = str(ai.get("confidence", "LOW")).strip().upper()
    td = trend_result.get("trend") or {}
    if not isinstance(td, dict):
        td = {}
    if chrono_job:
        agreed_list = sorted(CHRONO_DAY_PREFILTER_SIDS.get((sym.upper(), direction), set()))
    else:
        agreed_list = sorted(
            {
                str(x[0]).strip().upper()
                for x in layer2
                if len(x) >= 2 and str(x[1]).strip().upper() in ("BOTH", direction)
            }
        )

    return {
        "date": analysis_date,
        "ticker": sym,
        "timeframe": timeframe,
        "verdict": ai.get("verdict"),
        "direction": direction,
        "confidence": confidence,
        "confidence_pre_upgrade": ai.get("confidence_pre_upgrade"),
        "htf_bias": str(ai.get("htf_bias") or "UNKNOWN"),
        "market_structure": str(ai.get("market_structure") or "UNKNOWN"),
        "zone_pct": zone_pct,
        "zone_label": zone_label,
        "zone_label_model": None,
        "price_zone": "EQUILIBRIUM",
        "zone_position_pct": zone_pct,
        "smc_concept": "NONE",
        "smc_direction": "",
        "tp_target": str(ai.get("tp_target") or "TP1"),
        "is_exotic": is_exotic,
        "confluence_points": 0,
        "zone_reasoning": "",
        "smc_reasoning": "",
        "exit_reasoning": "",
        "entry_price": round(entry, 5),
        "stop_loss": round(normal_stop_v75, 5),
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
        "signals_used": [strat_id],
        "confluences": ai.get("confluences") if isinstance(ai.get("confluences"), list) else [],
        "conflicts": [],
        "reasoning": str(ai.get("reasoning", "")),
        "rr_ratio": str(ai.get("rr_ratio", "1:2.0")),
        "conviction_score": int(ai.get("conviction_score", 3) or 3),
        "confluence_score_python": 0,
        "risk_mode": "PYTHON_LAYER2",
        "weekly_bias_chart": "",
        "intel_summary": "",
        "strategy_id": strat_id,
        "strategy_name": str(STRATEGIES.get(strat_id, {}).get("name", strat_id)),
        "strategy_met": True,
        "core_signals_met": ["PYTHON_LAYER2"],
        "core_signals_failed": [],
        "non_negotiables_met": ["PYTHON_LAYER2"],
        "non_negotiables_failed": [],
        "trailing_plan": "",
        "timeframe_restricted": tf_key == "1h",
        "tf_strategy_allowed": tf_key != "1h" or strat_id in ALLOWED_1H_STRATEGIES,
        "trailing_activated": trailing_activated,
        "final_stop": round(final_stop, 5),
        "trail_market_regime": str(exit_data.get("trail_market_regime") or "CHOPPY"),
        "candles_to_tp1": exit_data.get("candles_to_tp1"),
        "peak_profit_dollars": exit_data.get("peak_profit_dollars"),
        "exit_vs_peak_pct": exit_data.get("exit_vs_peak_pct"),
        "v74_perfect_storm": bool(ai.get("_v74_perfect_storm_m03_jpy")),
        "skipped": False,
        "skip_trade": False,
        "calendar_action": str(cal.get("action", "CLEAR")),
        "calendar_reason": str(cal.get("reason", "")),
        **_v73_regime_row_fields(regime_ctx),
        **merged_macro_result_fields(ai),
        "trend": str(td.get("trend", "RANGING")),
        "trend_strength": float(td.get("strength", 0) or 0),
        "trend_bias": str(td.get("direction_bias", "NEUTRAL")),
        "trend_size_mult": float(trend_mult),
        "trend_block": False,
        "strategy_confluence_count": int(scount),
        "strategy_confluence_mult": float(cf_mult),
        "strategies_agreed": agreed_list,
        **v75_meta,
        **(
            _trade_condition_snapshot_fields(analysis_date, past, ind or {})
            if chrono_job
            else {}
        ),
    }


def run_one_backtest(
    ticker: str,
    timeframe: str,
    analysis_date: str,
    *,
    chrono_yfinance: bool = False,
    chrono_risk_mult: float = 1.0,
    chrono_tp_mult: float = 1.0,
    chrono_enforce_extended_cooldown: bool = False,
    chrono_regime: dict[str, Any] | None = None,
    chrono_balance: float | None = None,
    chrono_day_pnl: float = 0.0,
) -> dict[str, Any] | None:
    try:
        sym = (ticker or "").strip().upper()
        tf_key = timeframe.lower().strip()
        try:
            scan_d = date.fromisoformat(analysis_date.strip()[:10])
        except (TypeError, ValueError):
            scan_d = None

        if chrono_regime is not None:
            regime_ctx = chrono_regime
        else:
            regime_ctx = cached_regime(
                os.environ.get("ROLLING_REGIME_JOB_ID", "rolling"),
                scan_d,
            )

        hist_cal: dict[str, Any] = {
            "action": "CLEAR",
            "reason": "No high-impact events within 48h",
            "size_multiplier": 1.0,
        }
        if scan_d is not None:
            hist_cal = cached_calendar_historical(sym, scan_d)
            if hist_cal.get("action") == "BLOCK":
                return _skipped_backtest_row(
                    sym=sym,
                    timeframe=timeframe,
                    analysis_date=analysis_date.strip(),
                    price=0.0,
                    zone_pct=50.0,
                    zone_label="EQUILIBRIUM",
                    skip_reason=str(hist_cal.get("reason") or "Calendar block (simulated)"),
                    ai={
                        "strategy_id": "SKIP",
                        "strategy_met": False,
                        "skip_trade": True,
                        "direction": "NONE",
                        "conviction_score": 0,
                        "calendar_action": "BLOCK",
                        "calendar_reason": str(hist_cal.get("reason") or ""),
                        **_v73_regime_row_fields(regime_ctx),
                    },
                    tf_key=tf_key,
                    is_exotic=sym in EXOTIC_REDUCE,
                )

        is_forex = len(sym) == 6 and sym.isalpha()
        yf_ticker = sym + "=X" if is_forex else sym

        past, future = get_ohlcv(yf_ticker, tf_key, analysis_date.strip(), chrono_yfinance=chrono_yfinance)
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

        gc.collect()

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
        ind["bb_width"] = bb_width

        qualifies, qualifying_strategies, filter_reason = _v7_python_prefilter_bundle(
            sym,
            tf_key,
            float(price),
            ind,
            zone_pct,
            analysis_date=analysis_date,
            past=past,
        )
        if chrono_yfinance and qualifies:
            _chrono_accumulate_prefilter_signals(sym, qualifying_strategies, tf_key)
        if not qualifies:
            pr = f"Python pre-filter: {filter_reason}"
            log(f"[PreFilter] {sym} {timeframe} {analysis_date}: {pr}", level="info")
            return _skipped_backtest_row(
                sym=sym,
                timeframe=timeframe,
                analysis_date=analysis_date,
                price=float(price),
                zone_pct=zone_pct,
                zone_label=zone_label,
                skip_reason=pr,
                ai={
                    "skip_trade": True,
                    "strategy_id": "SKIP",
                    "strategy_met": False,
                    "skip_reason": pr,
                    "direction": "NONE",
                    "conviction_score": 0,
                },
                tf_key=tf_key,
                is_exotic=is_exotic,
            )

        intel_text = ""
        news_sentiment = 0.0
        cot_bias = "UNKNOWN"
        fear_greed = 50.0
        vix_val = 20.0
        try:
            from market_intelligence import (
                format_for_prompt,
                is_news_blackout,
            )

            briefing = cached_complete_briefing(sym, analysis_date.strip())
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

            if is_news_blackout(30) and tf_key in ("15m", "30m"):
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

        layer1_list = [s for s in qualifying_strategies if s[0] in LAYER1_STRATEGY_IDS]
        layer2_list = [s for s in qualifying_strategies if s[0] not in LAYER1_STRATEGY_IDS]
        if chrono_yfinance:
            layer2_list = _v72_merge_loose_untested_into_layer2(
                layer2_list,
                sym,
                tf_key,
                float(price),
                ind,
                chrono_yfinance=True,
            )

        if layer2_list and not layer1_list:
            picked = _layer2_tuple_for_deterministic_pick(sym, tf_key, zone_pct, layer2_list)
            if picked is None:
                return _skipped_backtest_row(
                    sym=sym,
                    timeframe=timeframe,
                    analysis_date=analysis_date,
                    price=float(price),
                    zone_pct=zone_pct,
                    zone_label=zone_label,
                    skip_reason="No Layer 2 strategy qualifies after v7.1 deterministic ordering",
                    ai={
                        "skip_trade": True,
                        "strategy_id": "SKIP",
                        "strategy_met": False,
                        "skip_reason": "No Layer 2 after v7.1 ordering",
                        "direction": "NONE",
                        "conviction_score": 0,
                    },
                    tf_key=tf_key,
                    is_exotic=is_exotic,
                )
            d_pick = str(picked[1]).strip().upper()
            if d_pick == "BOTH":
                d_pick = "LONG" if float(zone_pct) < 50.0 else "SHORT"
            if chrono_enforce_extended_cooldown:
                ok_e, rs_e = _chrono_extended_loss_cooldown_block(sym, timeframe, d_pick, analysis_date)
                if not ok_e:
                    return _skipped_backtest_row(
                        sym=sym,
                        timeframe=timeframe,
                        analysis_date=analysis_date,
                        price=float(price),
                        zone_pct=zone_pct,
                        zone_label=zone_label,
                        skip_reason=rs_e,
                        ai={
                            "skip_trade": True,
                            "strategy_id": "SKIP",
                            "strategy_met": False,
                            "skip_reason": rs_e,
                            "direction": "NONE",
                            "conviction_score": 0,
                        },
                        tf_key=tf_key,
                        is_exotic=is_exotic,
                    )
            forced = _python_forced_layer2_trade(
                sym=sym,
                timeframe=timeframe,
                analysis_date=analysis_date,
                tf_key=tf_key,
                price=float(price),
                zone_pct=zone_pct,
                zone_label=zone_label,
                is_exotic=is_exotic,
                future=future,
                layer2=[picked],
                rsi_live=float(ind.get("rsi", 50) or 50),
                chrono_risk_mult=chrono_risk_mult,
                chrono_tp_mult=chrono_tp_mult,
                chrono_job=chrono_yfinance,
                calendar_risk=hist_cal,
                regime_ctx=regime_ctx,
                chrono_balance=chrono_balance,
                chrono_day_pnl=chrono_day_pnl,
                atr_ref=float(ind.get("atr", 0) or 0) or None,
                past=past,
                ind=ind,
            )
            return forced

        if not layer1_list:
            return _skipped_backtest_row(
                sym=sym,
                timeframe=timeframe,
                analysis_date=analysis_date,
                price=float(price),
                zone_pct=zone_pct,
                zone_label=zone_label,
                skip_reason="No Layer 1 (T01/R01) candidates for Claude",
                ai={
                    "skip_trade": True,
                    "strategy_id": "SKIP",
                    "strategy_met": False,
                    "skip_reason": "No Layer 1 for Claude",
                    "direction": "NONE",
                    "conviction_score": 0,
                },
                tf_key=tf_key,
                is_exotic=is_exotic,
            )

        qualifying_str = str([s[0] for s in layer1_list])

        prompt = _format_apex_master_v6(
            ticker=sym,
            tf_key=tf_key,
            analysis_date=analysis_date,
            price=float(price),
            zone_label=zone_label,
            zone_pct=zone_pct,
            high_52w=high_52w,
            low_52w=low_52w,
            ind=ind,
            bb_width=bb_width,
            intel_text=intel_text,
            qualifying_str=qualifying_str,
        )
        try:
            client = _client()
            raw = cached_claude_master_text(
                client,
                model=CLAUDE_MODEL,
                max_tokens=BACKTEST_CLAUDE_MAX_TOKENS,
                prompt=prompt,
                ticker=sym,
                analysis_date=analysis_date.strip(),
                scan_d=scan_d,
            )
        except Exception as e:  # noqa: BLE001
            et = type(e).__name__
            em = str(e).lower()
            if "timeout" in em or "timeout" in et.lower():
                log(f"[Timeout] {sym} {timeframe} — skipping", level="warning")
                return None
            log(f"[Backtest] Claude API error {sym} {timeframe}: {e}", level="warning")
            return None
        parsed = _parse_json_response(raw)
        ai: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

        def _skip_out(reason: str, src: dict[str, Any] | None = None) -> dict[str, Any]:
            log(f"[SKIP] {sym} {timeframe}: {reason}", level="info")
            merged_ai = dict(src if src is not None else ai)
            merged_ai["calendar_action"] = str(hist_cal.get("action", "CLEAR"))
            merged_ai["calendar_reason"] = str(hist_cal.get("reason", ""))
            merged_ai.update(_v73_regime_row_fields(regime_ctx))
            return _skipped_backtest_row(
                sym=sym,
                timeframe=timeframe,
                analysis_date=analysis_date,
                price=float(price),
                zone_pct=zone_pct,
                zone_label=zone_label,
                skip_reason=reason,
                ai=merged_ai,
                tf_key=tf_key,
                is_exotic=is_exotic,
            )

        if not ai:
            return _skip_out("empty or invalid JSON from model", {})

        _legacy_sid = {
            "S03_EMA_PULLBACK": "T01_EMA_PULLBACK",
            "S04_EXTREME_REVERSION": "R01_EXTREME_ZONE_REVERSION",
            "R01_EXTREME_ZONE": "R01_EXTREME_ZONE_REVERSION",
            "S08_RANGE_BREAKOUT": "B01_RANGE_BREAKOUT",
        }
        _sid_raw = str(ai.get("strategy_id", "")).strip().upper()
        if _sid_raw in _legacy_sid:
            ai["strategy_id"] = _legacy_sid[_sid_raw]

        _sanitize_signal_lists(ai)
        ai["zone_pct"] = zone_pct

        macro_bt: dict[str, Any] | None = None

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

            macro_bt = cached_macro_bias(sym, d0, scan_d)
            macro_bt = align_macro_bias_with_price(
                sym,
                d0,
                macro_bt,
                as_of_date=scan_d,
                price_df=past if past is not None and not getattr(past, "empty", True) else None,
                log_fn=log,
            )
            ai.update(macro_result_fields(macro_bt))
            log(
                f"[MACRO] {sym} {d0} — {macro_bt['bias']} (score {macro_bt['composite_score']:+.2f}) "
                f"rate_diff={macro_bt['rate_differential']:+.2f}% {macro_bt['price_trend']}",
                level="info",
            )

        ai = enforce_rules(
            ai,
            tf_key,
            float(price),
            sym,
            rsi=float(ind.get("rsi", 50) or 50),
        )

        if ai.get("skip_trade"):
            return _skip_out(str(ai.get("skip_reason") or "enforcement skip"), ai)

        if macro_bt is not None:
            _v73_post_enforce_macro_confidence(ai, macro_bt, tf_key=tf_key, price=float(price), sym=sym)

        if chrono_yfinance:
            ai["_balance_for_sizing"] = float(chrono_balance or STARTING_CAPITAL)
            _v73_refresh_risk_fields_from_ai_confidence(ai, tf_key, float(price), sym)

        if chrono_enforce_extended_cooldown:
            d_pre = str(ai.get("direction", "")).strip().upper()
            if d_pre in ("LONG", "SHORT"):
                ok_e2, rs_e2 = _chrono_extended_loss_cooldown_block(sym, timeframe, d_pre, analysis_date)
                if not ok_e2:
                    return _skip_out(rs_e2, ai)

        strategy_id_norm = str(ai.get("strategy_id", "")).strip().upper()
        if strategy_id_norm not in STRATEGIES:
            return _skip_out(f"unsupported strategy_id: {strategy_id_norm}", ai)
        if strategy_id_norm not in LAYER1_STRATEGY_IDS:
            return _skip_out(
                f"Layer 1 Claude path requires T01 or R01, got {strategy_id_norm}",
                ai,
            )

        direction = str(ai.get("direction", "")).strip().upper()
        if direction not in ("LONG", "SHORT"):
            return _skip_out("no valid LONG/SHORT after enforcement", ai)

        if strategy_id_norm == "M03_RSI_MOMENTUM_CONTINUATION" and sym.strip().upper() not in M03_ALLOWED_TICKERS:
            log(f"[M03 BLOCKED] {sym.strip().upper()} not in allowed ticker list", level="info")
            return _skip_out(f"[M03 BLOCKED] {sym.strip().upper()} not in allowed ticker list", ai)
        ok_mom, rs_mom = _momentum_neutral_ranging_skip(
            sym=sym,
            strat_id=strategy_id_norm,
            macro_bias=str(ai.get("macro_bias", "")),
            analysis_date=analysis_date,
            past=past,
            ind=ind,
        )
        if ok_mom:
            return _skip_out(rs_mom, ai)

        trend_result = cached_apply_trend_filter(
            sym,
            direction,
            strategy_id_norm,
            as_of_date=analysis_date.strip()[:10],
        )
        if trend_result.get("action") == "BLOCK":
            log(f"[TREND BLOCK] {sym} {direction} — {trend_result.get('reason', '')}", level="info")
            return _skip_out(str(trend_result.get("reason") or "trend block"), ai)
        trend_mult = float(trend_result.get("size_multiplier", 1.0) or 1.0)

        if chrono_yfinance and sym.upper() in JPY_STORM_PAIRS and macro_bt is not None:
            tr_snap = trend_result.get("trend") or {}
            if not isinstance(tr_snap, dict):
                tr_snap = {}
            CHRONO_JPY_STORM_SNAPSHOT[sym.upper()] = {
                "macro_bias": str(macro_bt.get("bias", "")),
                "confidence": str(ai.get("confidence", "LOW")).strip().upper(),
                "trend_strength": float(tr_snap.get("strength", 0) or 0),
                "trend_bias": str(tr_snap.get("direction_bias", "NEUTRAL")).strip().upper(),
                "rate_differential": float(macro_bt.get("rate_differential", 0) or 0),
            }

        if chrono_yfinance:
            raw_s = CHRONO_DAY_PREFILTER_SIDS.get((sym.upper(), direction), set())
            scount = len({s for s in raw_s if _locked_confluence_sid(s)})
            triple = {"1w", "1d", "4h"}.issubset(CHRONO_SYMDIR_TFS.get((sym.upper(), direction), set()))
        else:
            scount = _local_prefilter_confluence_count(sym, direction, qualifying_strategies)
            triple = False
        cf_mult = _strategy_confluence_multiplier(scount, triple_tf_agreement=triple)

        strategy_met = bool(ai.get("strategy_met", False))
        try:
            conviction_score = int(round(float(ai.get("conviction_score", 5))))
        except (TypeError, ValueError):
            conviction_score = 5
        conviction_score = max(0, min(8, conviction_score))

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

        ok_v75, skip_v75, v75_meta = _v75_entry_guards(
            sym=sym,
            tf_key=tf_key,
            direction=direction,
            entry=entry,
            normal_stop=stop,
            past=past,
            ind=ind,
        )
        if not ok_v75:
            return _skip_out(skip_v75, {**ai, **v75_meta})

        normal_stop_v75 = float(v75_meta["normal_stop_price"])
        buffer_stop_v75 = float(v75_meta["buffer_stop_price"] or normal_stop_v75)
        stop = normal_stop_v75
        ai["stop_loss"] = normal_stop_v75

        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return _skip_out("invalid risk", ai)

        if scount >= 3 and str(ai.get("tp_target") or "TP1").strip().upper() == "TP2":
            ai["tp_target"] = "TP3"

        tp1 = round(entry + mult * risk * 2.0, 5)
        tp2 = round(entry + mult * risk * 3.0, 5)
        tp3 = round(entry + mult * risk * 5.0, 5)
        ai["tp1"], ai["tp2"], ai["tp3"] = tp1, tp2, tp3
        rr_tp2 = 3.0
        ai["rr_ratio"] = f"1:{rr_tp2:.2f}"

        _apply_chrono_risk_tp_multipliers(
            ai,
            price=float(price),
            chrono_risk_mult=chrono_risk_mult,
            chrono_tp_mult=chrono_tp_mult,
        )
        tp1 = float(ai.get("tp1", tp1) or tp1)
        tp2 = float(ai.get("tp2", tp2) or tp2)
        tp3 = float(ai.get("tp3", tp3) or tp3)

        _v73_apply_calendar_regime_position_size(
            ai,
            confidence=str(ai.get("confidence", "LOW")),
            calendar_risk=hist_cal,
            regime=regime_ctx,
            macro=macro_bt,
            trend_mult=trend_mult,
            confluence_mult=cf_mult,
            entry=float(ai.get("entry", entry) or entry),
        )

        _v74_apply_recipe_and_monday_boosts(
            ai,
            sym=sym,
            strat_id=strategy_id_norm,
            locked_confluence=int(scount),
            tf_key=tf_key,
            analysis_date=analysis_date,
        )

        position_size = float(ai.get("_position_size", 0) or 0)
        leveraged_exposure = float(ai.get("_leveraged_exposure", 0) or 0)
        max_risk_dollars = float(ai.get("_max_risk_dollars", 0) or 0)
        sizing_risk_pct = float(ai.get("_account_risk_pct", 0) or 0)
        risk_pct_of_price = risk / entry if entry > 0 else 0.0
        risk_pct_display = round(risk_pct_of_price * 100, 3)

        fut = future.head(fwd_n)
        if fut.empty or len(fut) == 0:
            return None

        tr_ev2 = trend_result.get("trend") or {}
        if not isinstance(tr_ev2, dict):
            tr_ev2 = {}
        ts_cl = float(tr_ev2.get("strength", 0) or 0)

        exit_data = evaluate_forward_candles(
            direction,
            entry,
            normal_stop_v75,
            tp1,
            tp2,
            tp3,
            fut,
            strategy_id_norm,
            position_size=position_size,
            leverage=LEVERAGE,
            timeframe=tf_key,
            macro_bias=str(ai.get("macro_bias", "") or ""),
            trend_strength=ts_cl,
            rate_differential=float(ai.get("macro_rate_diff", 0) or 0),
            atr=float(ind.get("atr", 0) or v75_meta.get("entry_atr", 0) or 0),
            buffer_stop_price=buffer_stop_v75,
            ticker=sym,
        )
        if exit_data.get("outcome") in ("NO_DATA", "INVALID"):
            del fut
            del past
            del future
            gc.collect()
            return None

        if (
            int(exit_data.get("candles_to_exit", 0) or 0) == 1
            and bool(exit_data.get("hit_stop"))
            and str(exit_data.get("outcome", "")).upper() == "LOSS"
        ):
            _v75_log_one_candle_loss(
                sym=sym,
                tf_key=tf_key,
                strat_id=strategy_id_norm,
                direction=direction,
                entry=entry,
                normal_stop=normal_stop_v75,
                buffer_stop=buffer_stop_v75,
                forward_df=fut,
                atr=float(v75_meta.get("entry_atr", 0) or 0),
                candle_body=float(v75_meta.get("entry_candle_body", 0) or 0),
                exit_price=float(exit_data.get("exit_price", entry) or entry),
            )

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

        cond_snap = (
            _trade_condition_snapshot_fields(analysis_date, past, ind)
            if chrono_yfinance
            else {}
        )

        del fut
        del past
        del future
        gc.collect()

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

        if chrono_yfinance:
            agreed_list = sorted(CHRONO_DAY_PREFILTER_SIDS.get((sym.upper(), direction), set()))
        else:
            agreed_list = sorted(
                str(x[0]).strip().upper()
                for x in qualifying_strategies
                if len(x) >= 2 and str(x[1]).strip().upper() in ("BOTH", direction)
            )
        td = trend_result.get("trend") or {}
        if not isinstance(td, dict):
            td = {}

        return {
            "date": analysis_date,
            "ticker": sym,
            "timeframe": timeframe,
            "verdict": ai.get("verdict"),
            "direction": direction,
            "confidence": str(ai.get("confidence", confidence)),
            "confidence_pre_upgrade": ai.get("confidence_pre_upgrade"),
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
            "stop_loss": round(normal_stop_v75, 5),
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
            "trail_market_regime": str(exit_data.get("trail_market_regime") or "CHOPPY"),
            "candles_to_tp1": exit_data.get("candles_to_tp1"),
            "peak_profit_dollars": exit_data.get("peak_profit_dollars"),
            "exit_vs_peak_pct": exit_data.get("exit_vs_peak_pct"),
            "v74_perfect_storm": bool(ai.get("_v74_perfect_storm_m03_jpy")),
            "skipped": False,
            "skip_trade": False,
            "calendar_action": str(hist_cal.get("action", "CLEAR")),
            "calendar_reason": str(hist_cal.get("reason", "")),
            **_v73_regime_row_fields(regime_ctx),
            **merged_macro_result_fields(ai),
            "trend": str(td.get("trend", "RANGING")),
            "trend_strength": float(td.get("strength", 0) or 0),
            "trend_bias": str(td.get("direction_bias", "NEUTRAL")),
            "trend_size_mult": float(trend_mult),
            "trend_block": False,
            "strategy_confluence_count": int(scount),
            "strategy_confluence_mult": float(cf_mult),
            "strategies_agreed": agreed_list,
            **v75_meta,
            **cond_snap,
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
        for tf in TIMEFRAMES:
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
        "15m": 50,
        "30m": 50,
        "1h": 180,
        "4h": 365,
        "1d": 730,
        "1w": 1095,
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
    session = get_current_session()
    cfg = load_session_config()
    if session == "off_hours":
        return []
    if not cfg.get(session, True):
        return []

    tickers = [t for t in eligible_backtest_tickers() if is_ticker_in_session(t, session)]
    if not tickers:
        return []
    timeframe_list = list(TIMEFRAMES)

    batch: list[tuple[str, str, str]] = []
    for _ in range(batch_size):
        ticker = random.choice(tickers)
        tf = random.choice(timeframe_list)
        date = pick_random_date(tf)
        batch.append((ticker, tf, date))

    return batch


def continuous_backtest_loop() -> None:
    global CHRONO_RUNNING
    log("[Loop] Starting continuous backtest loop", level="info")
    tests_since_improve = 0
    loop_completed_tests = 0

    while not _stop_flag.is_set():
        if CHRONO_RUNNING:
            log("[RandomEngine] Sleeping — chrono engine is running", level="info")
            time.sleep(30)
            continue
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
            if not work_items:
                log(
                    "[Loop] No batch jobs (off-hours, session disabled, or no pairs for session) — sleeping",
                    level="info",
                )
                time.sleep(30)
                continue

            if CHRONO_RUNNING:
                log("[RandomEngine] Sleeping — chrono engine is running", level="info")
                time.sleep(30)
                continue

            batch_session = get_current_session()

            update_state(
                {
                    "status": "testing",
                    "current_ticker": work_items[0][0],
                    "current_date": work_items[0][2],
                    "current_timeframe": work_items[0][1],
                    "total_tests_run": len(_load_results_list()),
                    "last_heartbeat": datetime.now().isoformat(),
                    "session": batch_session,
                }
            )

            log(
                f"[Loop] Batch {len(work_items)} jobs (max_workers={MAX_WORKERS}, session={batch_session})",
                level="info",
            )

            try:
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {
                        executor.submit(analyse_one_backtest, t, tf, d): (t, tf, d)
                        for t, tf, d in work_items
                    }
                    for future in as_completed(futures, timeout=120):
                        ticker, tf, date = futures[future]
                        try:
                            result = future.result(timeout=60)
                        except Exception as e:  # noqa: BLE001
                            log(f"[Thread error] {ticker} {tf}: {e}", level="error")
                            continue

                        if result is None:
                            continue

                        if isinstance(result, dict):
                            result["session"] = batch_session

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

            except Exception as e:  # noqa: BLE001
                log(f"[Batch crashed] {e} — continuing", level="error")

            time.sleep(5)

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


def chrono_stop_flag_path(job_id: str) -> Path:
    return DATA_DIR / f"chrono_stop_{job_id}.flag"


def request_chrono_stop(job_id: str) -> None:
    p = chrono_stop_flag_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stop", encoding="utf-8")


def clear_chrono_stop_flag(job_id: str) -> None:
    try:
        chrono_stop_flag_path(job_id).unlink(missing_ok=True)
    except OSError:
        pass


def chrono_stop_requested(job_id: str) -> bool:
    return chrono_stop_flag_path(job_id).is_file()


def set_active_chrono(job_id: str) -> None:
    save_json(
        ACTIVE_CHRONO_FILE,
        {"job_id": job_id, "started_at": datetime.now().isoformat()},
    )


def clear_active_chrono() -> None:
    p = ACTIVE_CHRONO_FILE
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


def get_active_chrono() -> dict[str, Any] | None:
    data = load_json(ACTIVE_CHRONO_FILE, default=None)
    return data if isinstance(data, dict) else None


def get_active_chrono_job_id() -> str | None:
    active = get_active_chrono()
    if not isinstance(active, dict):
        return None
    jid = active.get("job_id")
    if jid is None or not str(jid).strip():
        return None
    return str(jid).strip()


def chrono_results_path(job_id: str) -> Path:
    """Persisted chronological backtest state (``DATA_DIR`` / volume on Railway)."""
    return DATA_DIR / f"chrono_results_{job_id}.json"


def _chrono_session_for_timeframe(timeframe: str) -> str:
    """Session bucket for P&L rollups (UTC-oriented labels; see ``SESSION_WINDOWS``)."""
    tf = (timeframe or "").strip().lower()
    if tf in ("15m", "30m"):
        return "london"
    if tf == "1h":
        return "london"
    return "new_york"


def _calc_session_performance(trades: list[dict[str, Any]]) -> dict[str, Any]:
    sessions: dict[str, Any] = {}
    for s in ("asia", "london", "new_york"):
        s_trades = [t for t in trades if t.get("session") == s]
        if not s_trades:
            continue
        wins = [t for t in s_trades if t.get("outcome") == "WIN"]
        losses = [t for t in s_trades if t.get("outcome") == "LOSS"]
        sessions[s] = {
            "total": len(s_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(s_trades)) * 100, 1),
            "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in s_trades), 2),
        }
    return sessions


def _calc_ticker_performance(trades: list[dict[str, Any]]) -> dict[str, Any]:
    tickers: dict[str, Any] = {}
    for t in trades:
        tk = str(t.get("ticker", "") or "")
        if not tk:
            continue
        if tk not in tickers:
            tickers[tk] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        tickers[tk]["total"] += 1
        if t.get("outcome") == "WIN":
            tickers[tk]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            tickers[tk]["losses"] += 1
        tickers[tk]["pnl"] = round(
            float(tickers[tk]["pnl"]) + float(t.get("pnl_dollars", 0) or 0),
            2,
        )
    for tk in tickers:
        tickers[tk]["win_rate"] = round(
            tickers[tk]["wins"] / max(1, tickers[tk]["total"]) * 100,
            1,
        )
    return tickers


def _calc_strategy_performance(trades: list[dict[str, Any]]) -> dict[str, Any]:
    strats: dict[str, Any] = {}
    for t in trades:
        sid = str(t.get("strategy_id", "") or "UNKNOWN")
        if sid not in strats:
            strats[sid] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        strats[sid]["total"] += 1
        if t.get("outcome") == "WIN":
            strats[sid]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            strats[sid]["losses"] += 1
        strats[sid]["pnl"] = round(
            float(strats[sid]["pnl"]) + float(t.get("pnl_dollars", 0) or 0),
            2,
        )
    for s in strats:
        strats[s]["win_rate"] = round(
            strats[s]["wins"] / max(1, strats[s]["total"]) * 100,
            1,
        )
    return strats


def _calc_tf_performance(trades: list[dict[str, Any]]) -> dict[str, Any]:
    tfs: dict[str, Any] = {}
    for t in trades:
        tf = str(t.get("timeframe", "") or "")
        if tf in ("15m", "30m"):
            continue
        if not tf:
            continue
        if tf not in tfs:
            tfs[tf] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        tfs[tf]["total"] += 1
        if t.get("outcome") == "WIN":
            tfs[tf]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            tfs[tf]["losses"] += 1
        tfs[tf]["pnl"] = round(
            float(tfs[tf]["pnl"]) + float(t.get("pnl_dollars", 0) or 0),
            2,
        )
    for tf in tfs:
        tfs[tf]["win_rate"] = round(
            tfs[tf]["wins"] / max(1, tfs[tf]["total"]) * 100,
            1,
        )
    return tfs


def _v74_chrono_trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """v7.4 — compact 4h vs 1w/1d stats for chrono JSON (also used by export header)."""
    done = [t for t in trades if isinstance(t, dict) and t.get("outcome") in ("WIN", "LOSS")]

    def _sub(tf: str) -> dict[str, Any]:
        s = [t for t in done if str(t.get("timeframe", "")).strip().lower() == tf]
        pnl = sum(float(x.get("pnl_dollars", 0) or 0) for x in s)
        w = sum(1 for x in s if x.get("outcome") == "WIN")
        cavg = sum(int(x.get("candles_to_exit", 0) or 0) for x in s) / max(1, len(s))
        return {
            "count": len(s),
            "wins": w,
            "pnl": round(pnl, 2),
            "win_rate_pct": round(100 * w / max(1, len(s)), 1),
            "avg_candles": round(cavg, 2),
        }

    return {
        "by_tf": {"1w": _sub("1w"), "1d": _sub("1d"), "4h": _sub("4h")},
        "perfect_storm_trades": sum(1 for t in done if t.get("v74_perfect_storm")),
        "trending_trail_trades": sum(1 for t in done if str(t.get("trail_market_regime", "")).upper() == "TRENDING"),
        "choppy_trail_trades": sum(1 for t in done if str(t.get("trail_market_regime", "")).upper() == "CHOPPY"),
    }


def _v74_strategy_short(strategy_id: str) -> str:
    s = (strategy_id or "").strip().upper()
    if "_" in s:
        return s.split("_", 1)[0]
    return s[:3] if len(s) >= 3 else s


def build_chrono_job_export_text(job_id: str) -> str | None:
    """Plain-text compact export of completed WIN/LOSS trades for a chrono job (v7.4)."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    path = chrono_results_path(jid)
    if not path.is_file():
        return None
    data = load_json(path, default=None) or {}
    trades = [
        t
        for t in (data.get("all_trades") or [])
        if isinstance(t, dict) and not t.get("skipped") and t.get("outcome") in ("WIN", "LOSS")
    ]
    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    wr = round(100 * len(wins) / max(1, len(trades)), 1) if trades else 0.0
    pnl_total = round(sum(float(t.get("pnl_dollars", 0) or 0) for t in trades), 2)
    cap0 = float(STARTING_CAPITAL)
    sc = data.get("start_capital")
    if sc is not None:
        try:
            cap0 = float(sc)
        except (TypeError, ValueError):
            cap0 = float(STARTING_CAPITAL)
    cap1 = float(data.get("final_capital", data.get("capital", cap0)) or cap0)
    d0 = str(data.get("start_date", "") or "")
    d1 = str(data.get("end_date", "") or "")
    strat_lines: dict[str, int] = {}
    for t in trades:
        sid = str(t.get("strategy_id", "UNK") or "UNK")
        strat_lines[sid] = strat_lines.get(sid, 0) + 1
    strat_txt = " | ".join(f"{k}: {v}" for k, v in sorted(strat_lines.items(), key=lambda kv: (-kv[1], kv[0])))

    pstorm_days: set[str] = set()
    trend_days: set[str] = set()
    chop_days: set[str] = set()
    for t in trades:
        ds = str(t.get("date", ""))[:10]
        if not ds:
            continue
        if t.get("v74_perfect_storm"):
            pstorm_days.add(ds)
        rg = str(t.get("trail_market_regime", "CHOPPY")).upper()
        if rg == "TRENDING":
            trend_days.add(ds)
        else:
            chop_days.add(ds)
    for ds in pstorm_days:
        trend_days.discard(ds)
        chop_days.discard(ds)
    for ds in trend_days:
        chop_days.discard(ds)

    lines: list[str] = []
    lines.append(
        f"JOB: {jid} | ${cap0:.0f} → ${cap1:.0f} | {d0} - {d1}",
    )
    lines.append(
        f"TRADES: {len(trades)} actual | {len(wins)}W/{len(losses)}L | {wr}% WR | P&L: ${pnl_total:+,.0f}",
    )
    lines.append(f"STRATEGIES: {strat_txt or 'none'}")
    lines.append(
        f"REGIME: PERFECT_STORM days: {len(pstorm_days)} | TRENDING days: {len(trend_days)} | CHOPPY days: {len(chop_days)}",
    )
    lines.append("")
    for t in sorted(trades, key=lambda x: str(x.get("date", ""))[:10]):
        ds = str(t.get("date", ""))[:10]
        tk = str(t.get("ticker", "")).upper()
        tf = str(t.get("timeframe", "")).lower()
        dr = str(t.get("direction", "")).upper()
        sid = str(t.get("strategy_id", "")).upper()
        pre = str(t.get("confidence_pre_upgrade", "") or "").upper() or "?"
        post = str(t.get("confidence", "") or "").upper()
        mb = str(t.get("macro_bias", "")).upper()
        mm = float(t.get("macro_size_multiplier", 1.0) or 1.0)
        trn = str(t.get("trend", "")).upper()
        tm = float(t.get("trend_size_mult", 1.0) or 1.0)
        cn = int(t.get("strategy_confluence_count", 0) or 0)
        cm = float(t.get("strategy_confluence_mult", 1.0) or 1.0)
        rsk = float(t.get("max_risk_dollars", 0) or 0)
        oc = str(t.get("outcome", "")).upper()
        pnl = float(t.get("pnl_dollars", 0) or 0)
        exr = str(t.get("exit_reason", "") or "").replace("|", "/")
        nc = int(t.get("candles_to_exit", 0) or 0)
        rg = str(t.get("trail_market_regime", "CHOPPY")).upper()
        lines.append(
            f"{ds} | {tk} {tf} | {dr} | {_v74_strategy_short(sid)} | {pre}→{post} | {mb} {mm}x | "
            f"trend:{trn} {tm}x | conf:{cn} {cm}x | risk:${rsk:.0f} | {oc} ${pnl:+.0f} | {exr} | {nc}c | regime:{rg}",
        )
    lines.append("")
    lines.append("DATE       | P&L      | TRADES | W  | L  | CAPITAL    | REGIME")
    daily = data.get("daily_pnl") or []
    cap_run = float(cap0)
    for row in daily:
        if not isinstance(row, dict):
            continue
        dsd = str(row.get("date", ""))[:10]
        pnl_d = float(row.get("pnl", 0) or 0)
        tw = int(row.get("wins", 0) or 0)
        tl = int(row.get("losses", 0) or 0)
        tc = int(row.get("trades", 0) or 0)
        cap_run = float(row.get("capital", cap_run + pnl_d) or (cap_run + pnl_d))
        if dsd in pstorm_days:
            rgl = "PERFECT_STORM"
        elif dsd in trend_days:
            rgl = "TRENDING"
        else:
            rgl = "CHOPPY"
        lines.append(
            f"{dsd} | {pnl_d:+.0f}$   | {tc:<6} | {tw:<2} | {tl:<2} | ${cap_run:,.0f}   | {rgl}",
        )
    return "\n".join(lines) + "\n"


def run_chronological_backtest(
    start_date: str = CHRONO_START_DATE,
    end_date: str = CHRONO_END_DATE,
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Walk forward calendar day by day from ``start_date`` to ``end_date``.
    Each weekday runs FIX 9 phased scans (weekly, then daily, then 4h, then optional
    intraday when the scan date is within 55 days of today). Within each phase every
    ticker is scanned in list order. Uses ``run_one_backtest`` (Layer 2+ Python-forced
    trades skip Claude; Layer 1 uses v7 prompt).
    Persists to ``chrono_results_{job_id}.json`` under ``DATA_DIR`` (resumable).
    """
    global CHRONO_JPY_RISK_DAY

    if job_id is None:
        job_id = str(uuid.uuid4())[:8]

    if not env("ANTHROPIC_API_KEY"):
        log("[Chrono] ANTHROPIC_API_KEY missing — cannot run chronological backtest", level="warning")
        return {
            "job_id": job_id,
            "status": "failed",
            "error": "ANTHROPIC_API_KEY is not configured",
        }

    chrono_path = chrono_results_path(job_id)
    log(f"[Chrono] Starting chronological backtest {job_id}: {start_date} → {end_date}", level="info")

    chrono_data: dict[str, Any]
    if chrono_path.is_file():
        chrono_data = load_json(chrono_path, default={}) or {}
        if chrono_data.get("status") == "complete":
            log(f"[Chrono {job_id}] Job already complete — returning saved file", level="info")
            return chrono_data
    else:
        chrono_data = {
            "job_id": job_id,
            "start_date": start_date,
            "end_date": end_date,
            "status": "running",
            "current_date": start_date,
            "capital": STARTING_CAPITAL,
            "daily_pnl": [],
            "all_trades": [],
            "skipped": [],
            "days_processed": 0,
        }
        save_json(chrono_path, chrono_data)

    global CHRONO_RUNNING
    CHRONO_RUNNING = True
    CHRONO_SCAN_COUNTS.clear()
    clear_chrono_stop_flag(job_id)
    set_active_chrono(job_id)
    try:
        merge_hist: list[dict[str, Any]] = []
        merge_hist.extend(x for x in (chrono_data.get("all_trades") or []) if isinstance(x, dict))
        try:
            merge_hist.extend(x for x in load_all_results() if isinstance(x, dict))
        except Exception:  # noqa: BLE001
            pass
        _rebuild_strategy_trade_counts_from_rows(merge_hist)
        _rebuild_consecutive_losses_from_rows(merge_hist)
        _v72_load_strategy_status(log_startup=True)
        # FIX 18 — restore zero-trade priority after restart (same rule as end-of-day)
        for _z0 in ALL_STRATEGY_IDS:
            if STRATEGY_TRADE_COUNT.get(_z0, 0) == 0:
                ZERO_TRADE_STRATEGIES.add(_z0)

        for _k, _v in (
            ("daily_pnl", []),
            ("all_trades", []),
            ("skipped", []),
            ("capital", STARTING_CAPITAL),
            ("days_processed", 0),
        ):
            chrono_data.setdefault(_k, _v)

        CHRONO_LIVE_STATUS.update(
            {
                "job_id": job_id,
                "current_date": None,
                "current_ticker": None,
                "current_timeframe": None,
                "status": "running",
                "trades_today": 0,
                "capital": round(float(chrono_data.get("capital", STARTING_CAPITAL) or STARTING_CAPITAL), 2),
                "days_processed": int(chrono_data.get("days_processed", 0) or 0),
                "ticker_position": 0,
                "total_tickers": len(CHRONO_TICKERS),
                "scan_counts": {},
            }
        )
        try:
            end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
        except ValueError:
            log(f"[Chrono {job_id}] Invalid end_date {end_date}", level="error")
            chrono_data["status"] = "failed"
            chrono_data["error"] = "invalid end_date"
            save_json(chrono_path, chrono_data)
            return chrono_data

        capital = float(chrono_data.get("capital", STARTING_CAPITAL) or STARTING_CAPITAL)
        daily = chrono_data.get("daily_pnl") or []
        if daily:
            try:
                last_day = datetime.strptime(str(daily[-1]["date"]), "%Y-%m-%d")
                current = last_day + timedelta(days=1)
                capital = float(daily[-1].get("capital", capital) or capital)
                chrono_data["capital"] = round(capital, 2)
            except (ValueError, KeyError, TypeError):
                try:
                    current = datetime.strptime(
                        str(chrono_data.get("current_date", start_date)),
                        "%Y-%m-%d",
                    )
                except ValueError:
                    current = datetime.strptime(start_date.strip(), "%Y-%m-%d")
        else:
            try:
                current = datetime.strptime(start_date.strip(), "%Y-%m-%d")
            except ValueError:
                log(f"[Chrono {job_id}] Invalid start_date {start_date}", level="error")
                chrono_data["status"] = "failed"
                chrono_data["error"] = "invalid start_date"
                save_json(chrono_path, chrono_data)
                return chrono_data

        tickers = list(CHRONO_TICKERS)
        if not tickers:
            tickers = ["EURUSD"]

        intraday_res = chrono_data.get("chrono_intraday")
        if isinstance(intraday_res, dict) and intraday_res.get("date"):
            dsd = str(intraday_res["date"])
            try:
                nxt_idx = int(intraday_res.get("next_ticker_idx", 0) or 0)
            except (TypeError, ValueError):
                nxt_idx = 0
            if 0 <= nxt_idx <= len(tickers):
                try:
                    cand = datetime.strptime(dsd, "%Y-%m-%d")
                    if cand <= end_dt:
                        current = cand
                except ValueError:
                    pass


        while current <= end_dt:
            if chrono_stop_requested(job_id):
                log(f"[Chrono {job_id}] Stop requested — exiting early", level="info")
                chrono_data["status"] = "cancelled"
                chrono_data["cancel_reason"] = "stop_requested"
                chrono_data.pop("chrono_intraday", None)
                save_json(chrono_path, chrono_data)
                CHRONO_LIVE_STATUS["status"] = "cancelled"
                return chrono_data

            date_str = current.strftime("%Y-%m-%d")
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue

            log(f"[Chrono {job_id}] Processing {date_str}", level="info")
            with _chrono_ohlc_cache_lock:
                _chrono_ohlc_cache.clear()
                _chrono_ohlc_prefetch_done.clear()

            cin = chrono_data.get("chrono_intraday")
            if isinstance(cin, dict) and str(cin.get("date", "")) and str(cin.get("date", "")) < date_str:
                chrono_data.pop("chrono_intraday", None)

            intra = chrono_data.get("chrono_intraday")
            finalize_day_only = False
            resume_idx = 0
            v71_pi_s = v71_ti_s = v71_tj_s = 0
            day_trades: list[dict[str, Any]] = []
            day_skipped: list[dict[str, Any]] = []
            day_pnl = 0.0
            capital = float(chrono_data.get("capital", STARTING_CAPITAL) or STARTING_CAPITAL)
            open_positions: dict[str, dict[str, Any]] = {}

            if isinstance(intra, dict) and str(intra.get("date", "")) == date_str:
                try:
                    nidx = int(intra.get("next_ticker_idx", 0) or 0)
                except (TypeError, ValueError):
                    nidx = 0
                if nidx >= len(tickers):
                    finalize_day_only = True
                    dt_raw = intra.get("day_trades") or []
                    ds_raw = intra.get("day_skipped") or []
                    day_trades = [dict(x) for x in dt_raw] if isinstance(dt_raw, list) else []
                    day_skipped = [dict(x) for x in ds_raw] if isinstance(ds_raw, list) else []
                    day_pnl = float(intra.get("day_pnl", 0) or 0)
                    icap = intra.get("capital")
                    if icap is not None:
                        capital = float(icap)
                else:
                    resume_idx = max(0, nidx)
                    if intra.get("v71_phase_idx") is not None:
                        try:
                            v71_pi_s = int(intra.get("v71_phase_idx") or 0)
                            v71_ti_s = int(intra.get("v71_ticker_i") or 0)
                            v71_tj_s = int(intra.get("v71_tf_j") or 0)
                        except (TypeError, ValueError):
                            v71_pi_s = v71_ti_s = v71_tj_s = 0
                    else:
                        v71_pi_s, v71_ti_s, v71_tj_s = 0, resume_idx, 0
                    dt_raw = intra.get("day_trades") or []
                    ds_raw = intra.get("day_skipped") or []
                    day_trades = [dict(x) for x in dt_raw] if isinstance(dt_raw, list) else []
                    day_skipped = [dict(x) for x in ds_raw] if isinstance(ds_raw, list) else []
                    day_pnl = float(intra.get("day_pnl", 0) or 0)
                    icap = intra.get("capital")
                    if icap is not None:
                        capital = float(icap)

            TRADED_TICKERS_TODAY.clear()
            CHRONO_DAY_PREFILTER_SIDS.clear()
            CHRONO_SYMDIR_TFS.clear()
            CHRONO_JPY_STORM_SNAPSHOT.clear()
            CHRONO_JPY_PAIRS_SIGNALLED.clear()
            CHRONO_JPY_RISK_DAY = 0.0
            OPEN_CURRENCY_COUNT.clear()
            OPEN_CURRENCY_COUNT_4H.clear()
            for _row in day_trades:
                _tk = str(_row.get("ticker", "")).strip().upper()
                if _tk:
                    TRADED_TICKERS_TODAY.add(_tk)
                _tf_r = str(_row.get("timeframe", "")).strip().lower()
                _use4 = _tf_r in ("4h", "15m", "30m")
                for _ccy in get_currencies(_tk):
                    if _tk in EXOTIC_PAIRS and _ccy == "USD":
                        continue
                    if _use4:
                        OPEN_CURRENCY_COUNT_4H[_ccy] = OPEN_CURRENCY_COUNT_4H.get(_ccy, 0) + 1
                    else:
                        OPEN_CURRENCY_COUNT[_ccy] = OPEN_CURRENCY_COUNT.get(_ccy, 0) + 1

            CHRONO_LIVE_STATUS.update(
                {
                    "job_id": job_id,
                    "current_date": date_str,
                    "current_ticker": None,
                    "current_timeframe": None,
                    "status": "scanning",
                    "trades_today": len(day_trades),
                    "capital": round(capital, 2),
                    "days_processed": int(chrono_data.get("days_processed", 0) or 0),
                    "ticker_position": 0,
                    "total_tickers": len(CHRONO_TICKERS),
                    "scan_counts": dict(CHRONO_SCAN_COUNTS),
                }
            )

            chrono_abort = False
            if not finalize_day_only:
                scan_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                day_regime = cached_regime(job_id, scan_date)
                log(
                    f"[REGIME] {day_regime['regime']} — WR10={day_regime['wr_10']:.0%} "
                    f"WR20={day_regime['wr_20']:.0%} Streak={day_regime['consecutive_losses']} losses "
                    f"Size={day_regime['size_multiplier']}x",
                    level="info",
                )
                _utc_today = datetime.now(timezone.utc).date()
                days_ago = (_utc_today - scan_date).days
                phases = _chrono_v71_phases(days_ago, scan_date=scan_date)
                _chrono_prefetch_ohlc_for_phase_matrix(date_str, phases, tickers, v71_pi_s, v71_tj_s)

                def _v71_next_step(pi: int, ti: int, tj: int) -> tuple[int, int, int]:
                    _label, tf_list, _rm, _tm, _u4 = phases[pi]
                    if tj + 1 < len(tf_list):
                        return pi, ti, tj + 1
                    if ti + 1 < len(tickers):
                        return pi, ti + 1, 0
                    if pi + 1 < len(phases):
                        return pi + 1, 0, 0
                    return pi, ti, tj

                for pi in range(v71_pi_s, len(phases)):
                    label, tf_list, risk_m, tp_m, use4h_pool = phases[pi]
                    log(f"[Chrono {job_id}] {date_str} — {label}", level="info")
                    t_start = v71_ti_s if pi == v71_pi_s else 0
                    for ti in range(t_start, len(tickers)):
                        ticker = tickers[ti]
                        j_start = v71_tj_s if (pi == v71_pi_s and ti == v71_ti_s) else 0
                        for tj in range(j_start, len(tf_list)):
                            timeframe = tf_list[tj]
                            if chrono_stop_requested(job_id):
                                chrono_abort = True
                                break
                            session = _chrono_session_for_timeframe(timeframe)
                            tpos = CHRONO_TICKERS.index(ticker) + 1 if ticker in CHRONO_TICKERS else 0
                            CHRONO_LIVE_STATUS.update(
                                {
                                    "current_ticker": ticker,
                                    "current_timeframe": timeframe,
                                    "current_date": date_str,
                                    "trades_today": len(day_trades),
                                    "capital": round(capital, 2),
                                    "status": "scanning",
                                    "ticker_position": tpos,
                                    "total_tickers": len(CHRONO_TICKERS),
                                    "scan_counts": dict(CHRONO_SCAN_COUNTS),
                                }
                            )
                            pos_key = f"{ticker}_{timeframe}"
                            if pos_key in open_positions:
                                result = {
                                    "date": date_str,
                                    "ticker": ticker,
                                    "timeframe": timeframe,
                                    "session": session,
                                    "job_id": job_id,
                                    "skipped": True,
                                    "skip_trade": True,
                                    "outcome": "SKIPPED",
                                    "pnl_dollars": 0.0,
                                    "skip_reason": f"Already in open position: {pos_key}",
                                    "verdict": "SKIP",
                                }
                                append_result(result)
                                day_skipped.append(result)
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            cd_ok, cd_reason = check_cooldown(ticker, timeframe, date_str)
                            if not cd_ok:
                                log(
                                    f"[COOLDOWN] {ticker} {timeframe} {date_str}: {cd_reason}",
                                    level="info",
                                )
                                result = {
                                    "date": date_str,
                                    "ticker": ticker,
                                    "timeframe": timeframe,
                                    "session": session,
                                    "job_id": job_id,
                                    "skipped": True,
                                    "skip_trade": True,
                                    "outcome": "SKIPPED",
                                    "pnl_dollars": 0.0,
                                    "skip_reason": cd_reason,
                                    "verdict": "COOLDOWN",
                                }
                                append_result(result)
                                day_skipped.append(result)
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            tkr_u = str(ticker).strip().upper()
                            if tkr_u in TRADED_TICKERS_TODAY:
                                result = {
                                    "date": date_str,
                                    "ticker": ticker,
                                    "timeframe": timeframe,
                                    "session": session,
                                    "job_id": job_id,
                                    "skipped": True,
                                    "skip_trade": True,
                                    "outcome": "SKIPPED",
                                    "pnl_dollars": 0.0,
                                    "skip_reason": "Ticker already traded today on another timeframe",
                                    "verdict": "SKIP",
                                }
                                append_result(result)
                                day_skipped.append(result)
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            cap_blocked, cap_ccy = _chrono_currency_cap_blocks(ticker, use_4h_pool=use4h_pool)
                            if cap_blocked:
                                result = {
                                    "date": date_str,
                                    "ticker": ticker,
                                    "timeframe": timeframe,
                                    "session": session,
                                    "job_id": job_id,
                                    "skipped": True,
                                    "skip_trade": True,
                                    "outcome": "SKIPPED",
                                    "pnl_dollars": 0.0,
                                    "skip_reason": f"Currency cap: {cap_ccy} already has 2 open trades",
                                    "verdict": "SKIP",
                                }
                                append_result(result)
                                day_skipped.append(result)
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            try:
                                res = run_one_backtest(
                                    ticker,
                                    timeframe,
                                    date_str,
                                    chrono_yfinance=True,
                                    chrono_risk_mult=risk_m,
                                    chrono_tp_mult=tp_m,
                                    chrono_enforce_extended_cooldown=True,
                                    chrono_regime=day_regime,
                                    chrono_balance=capital,
                                    chrono_day_pnl=day_pnl,
                                )
                            except Exception as e:  # noqa: BLE001
                                log(
                                    f"[Chrono {job_id}] Error {ticker} {timeframe} {date_str}: {e}",
                                    level="warning",
                                )
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            if res is None:
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            if res.get("skipped"):
                                row = {
                                    "date": date_str,
                                    "ticker": str(res.get("ticker", ticker)),
                                    "timeframe": str(res.get("timeframe", timeframe)),
                                    "session": session,
                                    "job_id": job_id,
                                    "skipped": True,
                                    "outcome": "SKIPPED",
                                    "pnl_dollars": 0.0,
                                    "skip_reason": str(res.get("skip_reason", "") or ""),
                                    "verdict": res.get("verdict"),
                                }
                                row.update(
                                    _v73_regime_row_fields(
                                        {
                                            "regime": res.get("regime"),
                                            "size_multiplier": res.get("regime_size_multiplier"),
                                            "wr_10": res.get("regime_wr_10"),
                                            "consecutive_losses": res.get("regime_consecutive_losses"),
                                        }
                                    )
                                )
                                day_skipped.append(row)
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            oc = str(res.get("outcome", "") or "")
                            if oc not in ("WIN", "LOSS"):
                                npi, nti, ntj = _v71_next_step(pi, ti, tj)
                                chrono_data["chrono_intraday"] = {
                                    "date": date_str,
                                    "v71_phase_idx": npi,
                                    "v71_ticker_i": nti,
                                    "v71_tf_j": ntj,
                                    "next_ticker_idx": nti,
                                    "day_trades": day_trades,
                                    "day_skipped": day_skipped,
                                    "day_pnl": round(day_pnl, 2),
                                    "capital": round(capital, 2),
                                }
                                chrono_data["capital"] = round(capital, 2)
                                chrono_data["current_date"] = date_str
                                chrono_data["status"] = "running"
                                save_json(chrono_path, chrono_data)
                                continue

                            pnl = float(res.get("pnl_dollars", 0) or 0)
                            row = dict(res)
                            row["job_id"] = job_id
                            row["session"] = session
                            row.setdefault("date", date_str)
                            day_trades.append(row)
                            day_pnl += pnl
                            capital += pnl

                            if tkr_u.endswith("JPY"):
                                CHRONO_JPY_RISK_DAY += float(row.get("max_risk_dollars", 0) or 0)

                            open_positions[pos_key] = row
                            if oc in ("WIN", "LOSS"):
                                open_positions.pop(pos_key, None)
                                record_trade(ticker, timeframe, date_str)
                                TRADED_TICKERS_TODAY.add(tkr_u)
                                tf_lc = str(timeframe).strip().lower()
                                use_major = tf_lc not in ("4h", "15m", "30m")
                                for _ccy in get_currencies(tkr_u):
                                    if tkr_u in EXOTIC_PAIRS and _ccy == "USD":
                                        continue
                                    if use_major:
                                        OPEN_CURRENCY_COUNT[_ccy] = OPEN_CURRENCY_COUNT.get(_ccy, 0) + 1
                                    else:
                                        OPEN_CURRENCY_COUNT_4H[_ccy] = OPEN_CURRENCY_COUNT_4H.get(_ccy, 0) + 1
                                _rsid2 = str(row.get("strategy_id", "")).strip().upper()
                                if _rsid2:
                                    STRATEGY_TRADE_COUNT[_rsid2] = STRATEGY_TRADE_COUNT.get(_rsid2, 0) + 1
                                    ZERO_TRADE_STRATEGIES.discard(_rsid2)
                                drow = str(row.get("direction", "")).strip().upper()
                                lk = _chrono_combo_key(ticker, timeframe, drow)
                                if oc == "LOSS":
                                    CONSECUTIVE_LOSSES[lk] = CONSECUTIVE_LOSSES.get(lk, 0) + 1
                                else:
                                    CONSECUTIVE_LOSSES[lk] = 0
                                LAST_COMBO_TRADE_DATE[lk] = date_str

                            npi, nti, ntj = _v71_next_step(pi, ti, tj)
                            chrono_data["chrono_intraday"] = {
                                "date": date_str,
                                "v71_phase_idx": npi,
                                "v71_ticker_i": nti,
                                "v71_tf_j": ntj,
                                "next_ticker_idx": nti,
                                "day_trades": day_trades,
                                "day_skipped": day_skipped,
                                "day_pnl": round(day_pnl, 2),
                                "capital": round(capital, 2),
                            }
                            chrono_data["capital"] = round(capital, 2)
                            chrono_data["current_date"] = date_str
                            chrono_data["status"] = "running"
                            save_json(chrono_path, chrono_data)

                        if chrono_abort:
                            break
                        CHRONO_SCAN_COUNTS[ticker] = CHRONO_SCAN_COUNTS.get(ticker, 0) + 1
                        time.sleep(2.0)
                    if chrono_abort:
                        break
                if chrono_abort:
                    break

            if chrono_stop_requested(job_id) or chrono_abort:
                log(f"[Chrono {job_id}] Stop requested — exiting early", level="info")
                chrono_data["status"] = "cancelled"
                chrono_data["cancel_reason"] = "stop_requested"
                chrono_data.pop("chrono_intraday", None)
                save_json(chrono_path, chrono_data)
                CHRONO_LIVE_STATUS["status"] = "stopped"
                return chrono_data

            daily_summary = {
                "date": date_str,
                "pnl": round(day_pnl, 2),
                "trades": len(day_trades),
                "skipped": len(day_skipped),
                "capital": round(capital, 2),
                "wins": sum(1 for t in day_trades if t.get("outcome") == "WIN"),
                "losses": sum(1 for t in day_trades if t.get("outcome") == "LOSS"),
            }

            chrono_data.setdefault("daily_pnl", []).append(daily_summary)
            chrono_data.setdefault("all_trades", []).extend(day_trades)
            chrono_data.setdefault("skipped", []).extend(day_skipped)
            chrono_data.pop("chrono_intraday", None)
            chrono_data["capital"] = round(capital, 2)
            chrono_data["current_date"] = date_str
            chrono_data["status"] = "running"
            prev_dp = int(chrono_data.get("days_processed", 0) or 0)
            chrono_data["days_processed"] = prev_dp + 1
            save_json(chrono_path, chrono_data)

            log(
                f"[Chrono {job_id}] {date_str}: {len(day_trades)} trades, "
                f"P&L {day_pnl:+.2f}, capital {capital:.2f}",
                level="info",
            )

            CHRONO_LIVE_STATUS.update(
                {
                    "current_ticker": None,
                    "current_timeframe": None,
                    "trades_today": 0,
                    "capital": round(capital, 2),
                    "days_processed": int(chrono_data.get("days_processed", 0) or 0),
                    "ticker_position": 0,
                    "total_tickers": len(CHRONO_TICKERS),
                    "scan_counts": dict(CHRONO_SCAN_COUNTS),
                }
            )

            # FIX 18 — next calendar day prioritises strategies still at zero lifetime trades
            for _zsid in ALL_STRATEGY_IDS:
                if STRATEGY_TRADE_COUNT.get(_zsid, 0) == 0:
                    ZERO_TRADE_STRATEGIES.add(_zsid)

            time.sleep(5.0)
            current += timedelta(days=1)

        all_trades: list[dict[str, Any]] = list(chrono_data.get("all_trades") or [])
        wins = [t for t in all_trades if t.get("outcome") == "WIN"]
        losses = [t for t in all_trades if t.get("outcome") == "LOSS"]

        chrono_data["status"] = "complete"
        chrono_data["final_capital"] = round(capital, 2)
        chrono_data["total_pnl"] = round(capital - STARTING_CAPITAL, 2)
        chrono_data["summary"] = {
            "total_trades": len(all_trades),
            "total_wins": len(wins),
            "total_losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(all_trades)) * 100, 1),
            "total_pnl": round(capital - STARTING_CAPITAL, 2),
            "total_pnl_pct": round((capital - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 1),
            "avg_win": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in wins) / max(1, len(wins)),
                2,
            ),
            "avg_loss": round(
                sum(float(t.get("pnl_dollars", 0) or 0) for t in losses) / max(1, len(losses)),
                2,
            ),
            "session_performance": _calc_session_performance(all_trades),
            "ticker_performance": _calc_ticker_performance(all_trades),
            "strategy_performance": _calc_strategy_performance(all_trades),
            "timeframe_performance": _calc_tf_performance(all_trades),
        }
        chrono_data["v74_metrics"] = _v74_chrono_trade_metrics(all_trades)
        save_json(chrono_path, chrono_data)
        log(
            f"[Chrono {job_id}] Complete. Capital: {capital:.2f}, P&L: {capital - STARTING_CAPITAL:+.2f}",
            level="info",
        )
        return chrono_data
    finally:
        active = get_active_chrono()
        if isinstance(active, dict) and str(active.get("job_id")) == str(job_id):
            clear_active_chrono()
        if get_active_chrono() is None:
            CHRONO_RUNNING = False
        clear_chrono_stop_flag(job_id)
        log("[Chrono] Flag cleared — random engine can resume", level="info")
        CHRONO_LIVE_STATUS.update(
            {
                "job_id": None,
                "current_date": None,
                "current_ticker": None,
                "current_timeframe": None,
                "status": "idle",
                "trades_today": 0,
                "capital": STARTING_CAPITAL,
                "days_processed": 0,
                "ticker_position": 0,
                "total_tickers": len(CHRONO_TICKERS),
                "scan_counts": {},
            }
        )
        CHRONO_SCAN_COUNTS.clear()


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


_v72_load_strategy_status(log_startup=True)
