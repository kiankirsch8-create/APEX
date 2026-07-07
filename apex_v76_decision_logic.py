"""
v7.6 live decision logic — copied from ``continuous_backtester.py`` without ``pandas_ta``.

Used only by ``apex_trader_v76.py``. No numba / llvmlite dependency chain.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from calendar_manager import check_calendar_risk
from macro_manager import (
    apply_macro_confidence_adjustment,
    get_macro_bias,
    macro_result_fields,
    merged_macro_result_fields,
)
from prefilter_v6 import python_prefilter
from regime_manager import get_regime_cached
from strategies_v5_data import STRATEGIES
from trend_manager import apply_trend_filter

LogFn = Callable[..., None]

# ---------------------------------------------------------------------------
# Macro 7d price alignment (inlined — older VPS macro_manager may lack this)
# ---------------------------------------------------------------------------


def _v76_yf_symbol(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return f"{t}=X"
    return t


def _v76_fetch_daily_ohlc(ticker: str, as_of_date: date) -> pd.DataFrame | None:
    """~20d daily bars for macro price alignment when ``past`` OHLC is unavailable."""
    sym = _v76_yf_symbol(ticker)
    start_d = as_of_date - timedelta(days=20)
    end_d = as_of_date + timedelta(days=1)
    try:
        df = yf.download(
            sym,
            start=start_d.isoformat(),
            end=end_d.isoformat(),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).title() for c in df.columns})
    if "Adj Close" in df.columns and "Close" not in df.columns:
        df = df.rename(columns={"Adj Close": "Close"})
    return df.dropna(subset=["Close"]) if "Close" in df.columns else None


def _v76_macro_tier_economics(bias: str) -> tuple[float, int]:
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return 1.20, 1
    if b == "TAILWIND":
        return 1.10, 0
    if b == "NEUTRAL":
        return 1.0, 0
    if b == "HEADWIND":
        return 0.85, 0
    if b == "STRONG_HEADWIND":
        return 0.70, -1
    return 1.0, 0


def _v76_downgrade_macro_bias_one_level(bias: str) -> str:
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return "TAILWIND"
    if b == "TAILWIND":
        return "NEUTRAL"
    if b == "HEADWIND":
        return "STRONG_HEADWIND"
    return b


def _v76_downgrade_macro_bias_two_levels(bias: str) -> str:
    b = str(bias or "").strip().upper()
    if b == "STRONG_TAILWIND":
        return "NEUTRAL"
    if b == "TAILWIND":
        return "HEADWIND"
    if b == "NEUTRAL":
        return "HEADWIND"
    if b == "HEADWIND":
        return "STRONG_HEADWIND"
    return b


def _v76_check_macro_price_alignment(
    ticker: str,
    direction: str,
    macro_bias: str,
    price_data: pd.DataFrame | None,
    *,
    as_of_date: date | None = None,
) -> tuple[str, float | None, str]:
    tku = (ticker or "").strip().upper()
    dire = (direction or "").strip().upper()
    mb0 = str(macro_bias or "").strip().upper()
    if dire not in ("LONG", "SHORT") or not (len(tku) == 6 and tku.isalpha()):
        return mb0, None, "skip_non_fx"
    if mb0 not in ("STRONG_TAILWIND", "TAILWIND", "NEUTRAL", "HEADWIND", "STRONG_HEADWIND"):
        return mb0, None, "skip_bias_tier"

    df = price_data
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        end_d = as_of_date or date.today()
        df = _v76_fetch_daily_ohlc(tku, end_d)
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return mb0, None, "no_price_data"

    closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if len(closes) < 8:
        return mb0, None, "short_history"
    c_now = float(closes.iloc[-1])
    c_7 = float(closes.iloc[-8])
    if not math.isfinite(c_now) or not math.isfinite(c_7) or c_7 == 0:
        return mb0, None, "bad_closes"
    momentum = (c_now - c_7) / abs(c_7) * 100.0

    agree_hi = 0.3
    agree_lo = -0.3
    if dire == "LONG":
        if momentum > agree_hi:
            return mb0, momentum, "price_agrees"
        if agree_lo <= momentum <= agree_hi:
            return _v76_downgrade_macro_bias_one_level(mb0), momentum, "neutral_momentum_band"
        return _v76_downgrade_macro_bias_two_levels(mb0), momentum, "price_contradicts"
    if momentum < agree_lo:
        return mb0, momentum, "price_agrees"
    if agree_lo <= momentum <= agree_hi:
        return _v76_downgrade_macro_bias_one_level(mb0), momentum, "neutral_momentum_band"
    return _v76_downgrade_macro_bias_two_levels(mb0), momentum, "price_contradicts"


def align_macro_bias_with_price(
    ticker: str,
    direction: str,
    macro: dict[str, Any],
    *,
    as_of_date: date | None = None,
    price_df: pd.DataFrame | None = None,
    log_fn: LogFn | None = None,
) -> dict[str, Any]:
    """v7.4 7d price alignment — local copy for VPS without updated macro_manager."""
    out = dict(macro)
    mb0 = str(out.get("bias", "NEUTRAL")).strip().upper()
    adj, mom, tag = _v76_check_macro_price_alignment(
        ticker,
        direction,
        mb0,
        price_df,
        as_of_date=as_of_date,
    )
    if adj != mb0 and mom is not None:
        sm, cu = _v76_macro_tier_economics(adj)
        out["bias"] = adj
        out["size_multiplier"] = sm
        out["confidence_upgrade"] = cu
        msg = (
            f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} "
            f"(7d momentum: {mom:+.1f}%, price contradicting macro direction)"
            if "contradict" in tag
            else (
                f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} "
                f"(7d momentum: {mom:+.1f}%, neutral price action)"
                if "neutral_momentum" in tag
                else f"[MACRO TRANSITION] {ticker.upper()}: {mb0}→{adj} (7d momentum: {mom:+.1f}%, {tag})"
            )
        )
        if log_fn:
            try:
                log_fn(msg, level="info")
            except TypeError:
                log_fn(msg)
    return out


# ---------------------------------------------------------------------------
# Constants (v7.6 backtest parity)
# ---------------------------------------------------------------------------

STARTING_CAPITAL = 10000.0

RISK_BY_CONFIDENCE: dict[str, float] = {
    "HIGH": 0.020,
    "MEDIUM": 0.010,
    "LOW": 0.005,
}

MIN_STOP_PCT: dict[str, float] = {
    "1w": 0.8,
    "1d": 0.5,
    "4h": 0.8,
    "1h": 0.3,
    "30m": 0.15,
    "15m": 0.10,
}

MAX_STOP_PCT: dict[str, float] = {
    "1w": 2.5,
    "1d": 1.5,
    "4h": 1.0,
    "1h": 0.5,
    "30m": 0.3,
    "15m": 0.2,
}

LAYER1_STRATEGY_IDS: frozenset[str] = frozenset(
    {"T01_EMA_PULLBACK", "R01_EXTREME_ZONE_REVERSION"},
)

_INTRADAY_STRATEGY_IDS: frozenset[str] = frozenset(
    sid for sid, meta in STRATEGIES.items() if str(meta.get("category", "")).strip() == "INTRADAY"
)

ALL_STRATEGY_IDS: tuple[str, ...] = tuple(sorted(STRATEGIES.keys()))

V76_FORENSIC_BLOCKED: frozenset[str] = frozenset(
    {
        "Q06_REGIME_DETECTION",
        "SMC01_SR_FLIP",
        "R10_COT_EXTREME_REVERSION",
        "B07_INSIDE_BAR_BREAKOUT",
        "SMC02_ORDER_BLOCK",
        "R09_WEEKLY_GAP_FILL",
        "Q04_CARRY_TRADE_MOMENTUM",
        "T09_KELTNER_TREND_RIDE",
    },
)

V74_ALLOWED_4H_STRATEGIES: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "T07_MA_RIBBON_ALIGNMENT",
        "R01_EXTREME_ZONE_REVERSION",
    },
)

JPY_STORM_PAIRS: frozenset[str] = frozenset(
    {"USDJPY", "CADJPY", "AUDJPY", "GBPJPY", "NZDJPY", "CHFJPY"},
)

EXOTIC_REDUCE: frozenset[str] = frozenset()

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

FRAGILE_STRATEGIES: frozenset[str] = frozenset(
    {
        "M03_RSI_MOMENTUM_CONTINUATION",
        "M02_MACD_ZERO_CROSS",
        "B09_RSI_MOMENTUM_BREAK",
        "T08_DONCHIAN_BREAKOUT",
        "M06_PRICE_ACCELERATION",
    },
)

PROVEN_COMBINATIONS: tuple[tuple[str, str, int, float], ...] = (
    ("CADJPY", "M03_RSI_MOMENTUM_CONTINUATION", 30, 1.4),
    ("USDJPY", "M03_RSI_MOMENTUM_CONTINUATION", 30, 1.3),
    ("CHFJPY", "M03_RSI_MOMENTUM_CONTINUATION", 20, 1.2),
    ("CADJPY", "SMC10_CHOCH", 20, 1.3),
    ("USDJPY", "T08_DONCHIAN_BREAKOUT", 15, 1.2),
    ("USDJPY", "B09_RSI_MOMENTUM_BREAK", 15, 1.2),
)

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
    },
)

# Scan-day confluence (chrono-style, used on live v76 full scan)
CHRONO_DAY_PREFILTER_SIDS: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
CHRONO_SYMDIR_TFS: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
CHRONO_JPY_STORM_SNAPSHOT: dict[str, dict[str, Any]] = {}
CHRONO_JPY_RISK_DAY: float = 0.0
CHRONO_JPY_PAIRS_SIGNALLED: set[str] = set()

STRATEGY_STATUS: dict[str, Any] = {}
LOCKED_STRATEGY_IDS: frozenset[str] = frozenset()
UNTESTED_STRATEGIES_V72: frozenset[str] = frozenset()
BLOCKED_STRATEGIES: frozenset[str] = frozenset()
STRATEGY_TRADE_COUNT: dict[str, int] = {}
ZERO_TRADE_STRATEGIES: set[str] = set()
COMBO_TRADE_STATS: dict[tuple[str, str], dict[str, int]] = {}


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
            *V76_FORENSIC_BLOCKED,
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


def _v76_migrate_strategy_status(raw: dict[str, Any]) -> bool:
    changed = False

    def _as_set(key: str) -> set[str]:
        return {str(x).strip().upper() for x in (raw.get(key) or []) if isinstance(x, str) and str(x).strip()}

    locked = _as_set("locked")
    testing = _as_set("testing")
    blocked = _as_set("blocked")
    untested = _as_set("untested")

    for sid in V76_FORENSIC_BLOCKED:
        moved = False
        for bucket in (locked, testing, untested):
            if sid in bucket:
                bucket.discard(sid)
                moved = True
        if sid not in blocked:
            blocked.add(sid)
            moved = True
        if moved:
            changed = True

    if changed:
        raw["locked"] = sorted(locked)
        raw["testing"] = sorted(testing)
        raw["blocked"] = sorted(blocked)
        raw["untested"] = sorted(untested)
        raw["last_updated"] = date.today().isoformat()
    return changed


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def v72_load_strategy_status(data_dir: Path, *, log_fn: LogFn | None = None) -> None:
    global STRATEGY_STATUS, LOCKED_STRATEGY_IDS, UNTESTED_STRATEGIES_V72, BLOCKED_STRATEGIES
    status_file = data_dir / "strategy_status.json"
    default_payload = _v72_default_status_payload()
    raw: Any = _load_json(status_file, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("locked"), list):
        _save_json(status_file, default_payload)
        raw = default_payload
    else:
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
            _save_json(status_file, raw)
    if _v75_migrate_strategy_status(raw):
        _save_json(status_file, raw)
    if _v76_migrate_strategy_status(raw):
        _save_json(status_file, raw)
    STRATEGY_STATUS = raw
    LOCKED_STRATEGY_IDS = frozenset(str(x).strip().upper() for x in (raw.get("locked") or []) if x)
    UNTESTED_STRATEGIES_V72 = frozenset(str(x).strip().upper() for x in (raw.get("untested") or []) if x)
    blk = raw.get("blocked")
    if not isinstance(blk, list):
        blk = list(_v72_blocked_ids_default())
        raw["blocked"] = blk
        _save_json(status_file, raw)
    BLOCKED_STRATEGIES = frozenset(str(x).strip().upper() for x in blk if x)
    if log_fn:
        log_fn(
            f"[v76] strategy_status locked={len(LOCKED_STRATEGY_IDS)} "
            f"blocked={len(BLOCKED_STRATEGIES)}",
            level="info",
        )


def cached_regime(job_id: str, scan_d: date) -> dict[str, Any]:
    return get_regime_cached(job_id.strip() or "live", as_of_date=scan_d)


def _strategy_applicable_on_tf(strategy_id: str, tf_key: str) -> bool:
    meta = STRATEGIES.get(strategy_id) or {}
    tfs = meta.get("timeframes") or []
    if not tfs:
        return True
    want = (tf_key or "").strip().lower()
    return want in {str(x).strip().lower() for x in tfs}


def _locked_confluence_sid(sid: str) -> bool:
    return str(sid or "").strip().upper() in LOCKED_STRATEGY_IDS


def _v7_filter_layer2_qualifiers(
    qualifying: list[tuple[str, str, int]],
    *,
    sym: str,
    tf_key: str,
    zone_pct: float,
    ind: dict[str, Any],
    price: float,
    log_fn: LogFn | None = None,
) -> list[tuple[str, str, int, dict[str, Any] | None]]:
    out: list[tuple[str, str, int, dict[str, Any] | None]] = []
    tf_l = (tf_key or "").strip().lower()
    sym_u = (sym or "").strip().upper()
    for sid, direction, score in qualifying:
        sid_u = str(sid).strip().upper()
        if sid_u in LAYER1_STRATEGY_IDS:
            out.append((sid, direction, score, None))
            continue

        if tf_l == "4h" and sid_u not in V74_ALLOWED_4H_STRATEGIES:
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
            continue

        if (
            sym_u == "GBPUSD"
            and tf_l == "1d"
            and d_eff == "SHORT"
            and sid_u in ("T03_HH_HL_CONTINUATION", "SMC10_CHOCH", "T07_MA_RIBBON_ALIGNMENT")
            and float(zone_pct) < 80.0
        ):
            continue

        if tf_l == "1d":
            adx_raw = ind.get("adx") if ind.get("adx") is not None else ind.get("ADX")
            if adx_raw is not None:
                try:
                    adx_f = float(adx_raw)
                except (TypeError, ValueError):
                    adx_f = None
                if adx_f is not None and adx_f < 14.0:
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
                    continue
                if d_eff == "SHORT" and ccl > e200:
                    continue

        if sid_u == "B02_VOLATILITY_COMPRESSION":
            try:
                e20 = float(ind.get("ema20", price) or price)
                e50 = float(ind.get("ema50", price) or price)
                e200f = float(ind.get("ema200", price) or price)
            except (TypeError, ValueError):
                e20 = e50 = e200f = float(price)
            if d_eff == "SHORT" and e20 > e50 > e200f:
                continue
            if d_eff == "LONG" and e20 < e50 < e200f:
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
    log_fn: LogFn | None = None,
) -> tuple[bool, list[tuple[str, str, int, dict[str, Any] | None]], str]:
    raw_ok, raw_list, raw_reason = python_prefilter(
        sym,
        tf_key,
        float(price),
        ind,
        zone_pct,
        analysis_date=analysis_date,
        past=past,
    )
    filtered = _v7_filter_layer2_qualifiers(
        raw_list,
        sym=sym,
        tf_key=tf_key,
        zone_pct=float(zone_pct),
        ind=ind,
        price=float(price),
        log_fn=log_fn,
    )
    filtered = [
        r for r in filtered if _v75_backtest_strategy_allowed(str(r[0]), past=past, ind=ind)
    ]
    if not filtered:
        if not raw_ok:
            return False, [], raw_reason
        return False, [], "v7 post-filter removed all strategy candidates"
    return True, filtered, raw_reason


def _v75_v02_backtest_allowed(past: pd.DataFrame | None, ind: dict[str, Any]) -> tuple[bool, str]:
    snap = _trade_condition_snapshot_fields("", past, ind)
    vr = str(snap.get("volatility_regime", "UNKNOWN")).strip().upper()
    if vr not in ("HIGH_VOL", "EXTREME_VOL"):
        return False, f"volatility_regime={vr}"
    cur, avg, _ = _v75_atr_metrics(past, ind)
    if avg <= 0 or cur < avg * 1.2:
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


def _layer2_tuple_for_deterministic_pick(
    sym: str,
    tf_key: str,
    zone_pct: float,
    layer2_rows: list[tuple[str, str, int, dict[str, Any] | None]],
) -> tuple[str, str, int, dict[str, Any] | None] | None:
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
    for sid in l2_sorted:
        if _sort_key(sid)[0] >= 9_000_000:
            continue
        if sid in by_sid:
            return by_sid[sid][0]
    for sid in l2_sorted:
        if _sort_key(sid)[0] >= 9_000_000:
            continue
        if sid in _V7_STALE_FALLBACK_IDS:
            dire = "LONG" if float(zone_pct) < 50.0 else "SHORT"
            meta: dict[str, Any] = {
                "v7_fallback": True,
                "reasoning": f"{sid} FALLBACK: no detection conditions yet, gathering initial data",
                "confidence": "LOW",
                "risk_mode": "PYTHON_LAYER2",
            }
            return (sid, dire, 1, meta)
    return None


def _v76_rolling_5d_pnl(
    daily_rows: list[dict[str, Any]],
    *,
    as_of: date,
    current_day_pnl: float,
) -> float:
    by_date: dict[str, float] = {}
    for row in daily_rows:
        if not isinstance(row, dict):
            continue
        ds = str(row.get("date", ""))[:10]
        if not ds:
            continue
        try:
            d0 = date.fromisoformat(ds)
        except ValueError:
            continue
        if d0 > as_of:
            continue
        try:
            by_date[ds] = float(row.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            continue
    as_s = as_of.isoformat()
    by_date[as_s] = float(current_day_pnl)
    dates_sorted = sorted(by_date.keys())
    last5 = dates_sorted[-5:]
    return sum(by_date[d] for d in last5)


def _detect_period_mode(
    balance: float,
    job_id: str,
    as_of: date | None,
    completed_buf: list[dict[str, Any]],
) -> tuple[str, float, float]:
    if as_of is None:
        return "NEUTRAL", 50.0, 0.0
    as_s = as_of.isoformat()
    wr10 = 0.5
    if len(completed_buf) >= 5:
        recent_10 = completed_buf[-10:]
        wins = sum(1 for t in recent_10 if str(t.get("outcome", "")).strip().upper() == "WIN")
        wr10 = wins / float(len(recent_10))
    else:
        try:
            from regime_manager import calculate_rolling_winrate, get_recent_trades

            recent_10 = get_recent_trades(10, job_id, as_of_date=as_of)
            wr10 = calculate_rolling_winrate(recent_10)
        except Exception:  # noqa: BLE001
            wr10 = 0.5

    pnl5d = 0.0
    if completed_buf:
        start = as_of - timedelta(days=5)
        for r in completed_buf:
            ds = str(r.get("date", ""))[:10]
            try:
                d0 = date.fromisoformat(ds)
            except ValueError:
                continue
            if start < d0 <= as_of:
                try:
                    pnl5d += float(r.get("pnl_dollars", 0) or 0)
                except (TypeError, ValueError):
                    pass

    bal = float(balance or STARTING_CAPITAL)
    wr_pct = round(wr10 * 100.0, 1)
    if wr10 >= 0.45 and pnl5d > 0:
        return "GOOD", wr_pct, pnl5d
    if wr10 < 0.35 or pnl5d < -(0.02 * bal):
        return "BAD", wr_pct, pnl5d
    return "NEUTRAL", wr_pct, pnl5d


def _fragile_bad_period_skip(strat_id: str, period_mode: str) -> tuple[bool, str]:
    sid = str(strat_id or "").strip().upper()
    pm = str(period_mode or "NEUTRAL").strip().upper()
    if pm != "BAD" or sid not in FRAGILE_STRATEGIES:
        return False, ""
    return True, f"[BAD PERIOD] FRAGILE strategy {sid} paused — bad period mode active"


def detect_trailing_regime(macro_bias_adjusted: str, trend_strength: float, macro_rate_diff: float) -> str:
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


def resolve_trailing_regime(
    macro_bias: str,
    trend_strength: float,
    rate_differential: float,
    *,
    timeframe: str = "",
    log_fn: LogFn | None = None,
) -> str:
    mb = str(macro_bias or "").strip().upper()
    tf_lc = (timeframe or "").strip().lower()
    if mb == "STRONG_TAILWIND":
        if log_fn:
            log_fn(
                "[TRAIL OVERRIDE] STRONG_TAILWIND detected — forcing TRENDING mode",
                level="info",
            )
        return "TRENDING"
    if tf_lc == "4h":
        return "CHOPPY"
    return detect_trailing_regime(macro_bias, trend_strength, rate_differential)


def _atr_series_wilder(past: pd.DataFrame, period: int) -> pd.Series:
    h = pd.to_numeric(past["High"], errors="coerce")
    l = pd.to_numeric(past["Low"], errors="coerce")
    c = pd.to_numeric(past["Close"], errors="coerce")
    prev = c.shift(1)
    tr = (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _trade_condition_snapshot_fields(
    analysis_date: str,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
) -> dict[str, Any]:
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


def _v76_1d_trend_strength(sym: str, direction: str, strat_id: str, analysis_date: str) -> float:
    try:
        tr = apply_trend_filter(
            sym.strip().upper(),
            str(direction or "LONG").strip().upper(),
            str(strat_id or "").strip().upper(),
            as_of_date=analysis_date.strip()[:10],
        )
        td = tr.get("trend") if isinstance(tr, dict) else {}
        if not isinstance(td, dict):
            td = {}
        return float(td.get("strength", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _momentum_neutral_ranging_skip(
    *,
    sym: str,
    strat_id: str,
    macro_bias: str,
    analysis_date: str,
    past: pd.DataFrame | None,
    ind: dict[str, Any],
    direction: str = "LONG",
    timeframe: str = "",
    log_fn: LogFn | None = None,
) -> tuple[bool, str]:
    sid = str(strat_id or "").strip().upper()
    if sid not in MOMENTUM_STRATEGIES:
        return False, ""
    if str(macro_bias or "").strip().upper() != "NEUTRAL":
        return False, ""
    snap = _trade_condition_snapshot_fields(analysis_date, past, ind)
    mp = str(snap.get("market_phase", "")).strip().upper()
    vr = str(snap.get("volatility_regime", "")).strip().upper()
    ts_1d = _v76_1d_trend_strength(sym, direction, sid, analysis_date)

    if mp == "RANGING" or vr == "LOW_VOL":
        if ts_1d > 0.55:
            if log_fn:
                log_fn(
                    f"[NEUTRAL+RANGING OVERRIDE] trend_strength {ts_1d:.2f} > 0.55, allowing {sid}",
                    level="info",
                )
            return False, ""
        return (
            True,
            f"[NEUTRAL+RANGING SKIP] {sid} {sym.strip().upper()}: NEUTRAL macro + "
            f"{mp or 'UNKNOWN'} market / {vr or 'UNKNOWN'} vol — momentum strategy skipped",
        )
    return False, ""


def validate_stop_loss(entry: float, stop: float, direction: str, timeframe: str) -> float:
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


def _v75_buffer_stop_price(normal_stop: float, entry: float, direction: str, atr: float) -> float:
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
    log_fn: LogFn | None = None,
) -> tuple[bool, str, dict[str, Any]]:
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
        return (
            False,
            f"[LOW ATR SKIP] {sym_u} {tf_u}: ATR {cur_atr:.5f} < 50% of avg {avg_atr:.5f}",
            meta,
        )

    if cur_atr > 0:
        if d == "LONG" and body < -(1.5 * cur_atr):
            meta["body_skipped"] = True
            return (False, f"[BODY SKIP] {sym_u} {tf_u} LONG: body {body:.5f} < -1.5*ATR", meta)
        if d == "SHORT" and body > (1.5 * cur_atr):
            meta["body_skipped"] = True
            return (False, f"[BODY SKIP] {sym_u} {tf_u} SHORT: body {body:.5f} > 1.5*ATR", meta)

    ns = validate_stop_loss(float(entry), float(normal_stop), d, tf_u)
    meta["normal_stop_price"] = round(ns, 5)
    buf = _v75_buffer_stop_price(ns, float(entry), d, cur_atr)
    meta["buffer_stop_price"] = round(buf, 5)
    meta["buffer_stop_used"] = abs(buf - ns) > 1e-9
    return True, "", meta


def enforce_rules(
    ai: dict[str, Any],
    timeframe: str,
    price: float,
    ticker: str,
    *,
    rsi: float | None = None,
) -> dict[str, Any]:
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

    if ai.get("skip_trade"):
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
                    ai["skip_reason"] = f"R01 SHORT requires RSI >= 60, got {rsi_f}"
                    return ai
                if direction == "LONG" and rsi_f > 40.0:
                    ai["skip_trade"] = True
                    ai["skip_reason"] = f"R01 LONG requires RSI <= 40, got {rsi_f}"
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

    if stop and entry and stop != entry:
        stop_pct = abs(entry - stop) / entry * 100
        mn = float(MIN_STOP_PCT.get(tf, 0.3))
        mx = float(MAX_STOP_PCT.get(tf, 1.5))
        mult = 1 if direction == "LONG" else -1
        if stop_pct < mn or stop_pct > mx:
            new_stop = entry * (1 - mult * (mn if stop_pct < mn else mx) / 100)
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
    risk_dollars = STARTING_CAPITAL * risk_pct
    stop_dist = abs(entry - float(ai.get("stop_loss", entry * 0.99) or 0))
    if stop_dist > 0:
        pos_size = risk_dollars / stop_dist
        if pos_size * entry < 1500:
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
    if ai.get("skip_trade"):
        return
    tf = (timeframe or "").strip().lower()
    entry = float(ai.get("entry", price) or price)
    direction = str(ai.get("direction", "NONE")).strip().upper()
    stop = float(ai.get("stop_loss", 0) or 0)
    if stop and entry and stop != entry:
        stop_pct = abs(entry - stop) / entry * 100
        mn = float(MIN_STOP_PCT.get(tf, 0.3))
        mx = float(MAX_STOP_PCT.get(tf, 1.5))
        mult = 1 if direction == "LONG" else -1
        if stop_pct < mn or stop_pct > mx:
            new_stop = entry * (1 - mult * (mn if stop_pct < mn else mx) / 100)
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
        if pos_size * entry < 1500:
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
    log_fn: LogFn | None = None,
) -> None:
    if not macro_bt or ai.get("skip_trade"):
        return
    pre = str(ai.get("confidence", "LOW")).strip().upper()
    if pre not in ("HIGH", "MEDIUM", "LOW"):
        pre = "LOW"
    new_c = apply_macro_confidence_adjustment(pre, macro_bt)
    if new_c != pre:
        ai["confidence_pre_upgrade"] = pre
        ai["confidence"] = new_c
        if log_fn:
            log_fn(f"Macro upgrade: {sym} {pre} -> {new_c} confidence", level="info")
        _v73_refresh_risk_fields_from_ai_confidence(ai, tf_key, float(price), sym)


def _strategy_confluence_multiplier(strategy_count: int, *, triple_tf_agreement: bool = False) -> float:
    _ = triple_tf_agreement
    if strategy_count >= 3:
        return 1.5
    if strategy_count == 2:
        return 1.25
    return 1.0


def detect_perfect_storm() -> tuple[bool, int]:
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


def _apply_chrono_risk_tp_multipliers(
    ai: dict[str, Any],
    *,
    price: float,
    chrono_risk_mult: float,
    chrono_tp_mult: float,
) -> None:
    if chrono_risk_mult != 1.0:
        try:
            mrd = float(ai.get("_max_risk_dollars", 0) or 0)
            if mrd > 0:
                ai["_max_risk_dollars"] = round(mrd * chrono_risk_mult, 2)
        except (TypeError, ValueError):
            pass
    if chrono_tp_mult != 1.0:
        try:
            entry_f = float(ai.get("entry", price) or price)
            stop_f = float(ai.get("stop_loss", 0) or 0)
            risk = abs(entry_f - stop_f)
            if risk > 0:
                mult_dir = 1 if str(ai.get("direction", "LONG")).strip().upper() == "LONG" else -1
                for k, r_mult in (("tp1", 2.0), ("tp2", 3.0), ("tp3", 5.0)):
                    ai[k] = round(entry_f + mult_dir * risk * r_mult * chrono_tp_mult, 5)
        except (TypeError, ValueError):
            pass


def _v73_regime_row_fields(reg: dict[str, Any] | None) -> dict[str, Any]:
    r = reg or {}
    return {
        "regime": str(r.get("regime", "NORMAL")),
        "regime_size_multiplier": float(r.get("size_multiplier", 1.0) or 1.0),
        "regime_wr_10": float(r.get("wr_10", 0.5) or 0.5),
        "regime_consecutive_losses": int(r.get("consecutive_losses", 0) or 0),
    }


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


def _v74_apply_recipe_and_monday_boosts(
    ai: dict[str, Any],
    *,
    sym: str,
    strat_id: str,
    locked_confluence: int,
    tf_key: str,
    analysis_date: str,
    log_fn: LogFn | None = None,
) -> None:
    bal = float(ai.get("_balance_for_sizing") or STARTING_CAPITAL)
    ent = float(ai.get("entry", 0) or 0)
    stp = float(ai.get("stop_loss", 0) or 0)
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
        if log_fn:
            log_fn(f"[MONDAY BOOST] {sym_u} {tf_key.upper()}: 1.25x applied", level="info")
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
        if log_fn:
            log_fn(f"[RECIPE BOOST] {sym_u} {strat_id} 1.5x", level="info")
    mrd = max(25.0, min(mrd, cap_hi))
    ai["_max_risk_dollars"] = round(mrd, 2)
    ai["_position_size"] = round(mrd / sd, 2)
    ai["_leveraged_exposure"] = round((mrd / sd) * ent, 2)


def _v75_apply_macro_event_and_combo_boosts(
    ai: dict[str, Any],
    *,
    sym: str,
    strat_id: str,
    locked_confluence: int,
    tf_key: str,
    trend_strength: float,
    rate_diff: float,
    period_mode: str,
    log_fn: LogFn | None = None,
) -> None:
    bal = float(ai.get("_balance_for_sizing") or STARTING_CAPITAL)
    ent = float(ai.get("entry", 0) or 0)
    stp = float(ai.get("stop_loss", 0) or 0)
    sd = abs(ent - stp)
    if sd <= 0 or not math.isfinite(sd):
        return
    mrd = float(ai.get("_max_risk_dollars", 0) or 0)
    if mrd <= 0:
        return

    cap_hi = bal * float(RISK_BY_CONFIDENCE["HIGH"]) * 1.5
    macro_cap = bal * 0.08
    sym_u = sym.strip().upper()
    sid_u = strat_id.strip().upper()
    mb = str(ai.get("macro_bias", "")).strip().upper()
    conf = str(ai.get("confidence", "")).strip().upper()
    ts = float(trend_strength or 0)

    ai["period_mode"] = str(period_mode or "NEUTRAL").strip().upper()
    ai["macro_event_boost_applied"] = False
    ai["combination_boost_applied"] = 1.0

    is_macro_event = (
        mb == "STRONG_TAILWIND"
        and conf == "MEDIUM"
        and ts > 0.60
        and int(locked_confluence) >= 2
    )
    if is_macro_event:
        mrd = min(mrd * 2.0, macro_cap)
        ai["macro_event_boost_applied"] = True
        if log_fn:
            log_fn(f"[MACRO EVENT BOOST] {sym_u} {sid_u}: 2.0x", level="info")

    combo_mult = 1.0
    for tick, sid, min_trades, boost in PROVEN_COMBINATIONS:
        if sym_u != tick or sid_u != sid:
            continue
        st = COMBO_TRADE_STATS.get((sym_u, sid_u), {"count": 0, "wins": 0})
        if int(st.get("count", 0) or 0) < int(min_trades):
            break
        wr = float(st.get("wins", 0) or 0) / max(1, int(st.get("count", 0) or 0))
        if wr >= 0.40:
            combo_mult = float(boost)
            if log_fn:
                log_fn(f"[COMBO BOOST] {sym_u} {sid_u} {combo_mult:.1f}x", level="info")
        break

    if combo_mult > 1.0:
        hard_cap = macro_cap if ai.get("macro_event_boost_applied") else cap_hi
        mrd = min(mrd * combo_mult, hard_cap)
        ai["combination_boost_applied"] = combo_mult

    mrd = max(25.0, min(mrd, macro_cap if ai.get("macro_event_boost_applied") else cap_hi))
    ai["_max_risk_dollars"] = round(mrd, 2)
    ai["_position_size"] = round(mrd / sd, 2)
    ai["_leveraged_exposure"] = round((mrd / sd) * ent, 2)


def _skip_row(
    *,
    skip_reason: str,
    ai: dict[str, Any],
    regime_ctx: dict[str, Any] | None,
    cal: dict[str, Any],
) -> dict[str, Any]:
    out = dict(ai)
    out["skip_trade"] = True
    out["skipped"] = True
    out["skip_reason"] = skip_reason
    out["calendar_action"] = str(cal.get("action", "CLEAR"))
    out["calendar_reason"] = str(cal.get("reason", ""))
    out.update(_v73_regime_row_fields(regime_ctx))
    return out


def python_layer2_live_plan(
    *,
    sym: str,
    timeframe: str,
    analysis_date: str,
    tf_key: str,
    price: float,
    zone_pct: float,
    zone_label: str,
    is_exotic: bool,
    layer2: list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]],
    rsi_live: float = 50.0,
    chrono_risk_mult: float = 1.0,
    chrono_tp_mult: float = 1.0,
    regime_ctx: dict[str, Any] | None = None,
    chrono_balance: float | None = None,
    chrono_day_pnl: float = 0.0,
    period_mode: str = "NEUTRAL",
    past: pd.DataFrame | None = None,
    ind: dict[str, Any] | None = None,
    log_fn: LogFn | None = None,
) -> dict[str, Any] | None:
    """
    Layer-2 decision path for live (no forward simulation / no pandas_ta).
    Returns trade plan dict or skip dict.
    """
    if not layer2:
        return None

    best = layer2[0] if len(layer2) == 1 else max(layer2, key=lambda x: int(x[2]))
    strat_id = str(best[0]).strip().upper()
    ok_frag, rs_frag = _fragile_bad_period_skip(strat_id, period_mode)
    if ok_frag:
        return _skip_row(
            skip_reason=rs_frag,
            ai={"strategy_id": strat_id, "period_mode": period_mode},
            regime_ctx=regime_ctx,
            cal={"action": "CLEAR", "reason": ""},
        )

    meta = best[3] if len(best) > 3 else None
    direction = str(best[1]).strip().upper()
    if direction == "BOTH":
        direction = "LONG" if float(zone_pct) < 50 else "SHORT"
    if direction not in ("LONG", "SHORT"):
        return None

    if isinstance(meta, dict) and meta.get("v72_untested"):
        direction = str(meta.get("direction", direction)).strip().upper()
        entry = round(float(meta.get("entry", price)), 5)
        stop = round(float(meta["stop_loss"]), 5)
        tp1 = round(float(meta["tp1"]), 5)
        tp2 = round(float(meta["tp2"]), 5)
        tp3 = round(float(meta.get("tp3", tp2)), 5)
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

    ai: dict[str, Any] = {
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
        "zone_pct": zone_pct,
        "confluences": [str(x[0]) for x in layer2],
        "signals_used": [strat_id],
        "reasoning": f"Python-forced {strat_id}",
        "rr_ratio": "1:2.0",
        "tp_target": "TP1",
    }
    if isinstance(meta, dict) and meta.get("v7_fallback"):
        ai["reasoning"] = str(meta.get("reasoning") or ai["reasoning"])

    scan_d_macro: date | None = None
    try:
        scan_d_macro = date.fromisoformat(analysis_date.strip()[:10])
    except (TypeError, ValueError):
        scan_d_macro = None

    macro_bt = get_macro_bias(sym, direction, as_of_date=scan_d_macro)
    macro_bt = align_macro_bias_with_price(
        sym,
        direction,
        macro_bt,
        as_of_date=scan_d_macro,
        price_df=past if past is not None and not getattr(past, "empty", True) else None,
        log_fn=log_fn,
    )
    ai.update(macro_result_fields(macro_bt))
    ai["_balance_for_sizing"] = float(chrono_balance or STARTING_CAPITAL)

    ai = enforce_rules(ai, tf_key, float(price), sym, rsi=float(rsi_live))
    _v73_post_enforce_macro_confidence(ai, macro_bt, tf_key=tf_key, price=float(price), sym=sym, log_fn=log_fn)

    now_utc = datetime.now(timezone.utc)
    cal = check_calendar_risk(sym, now_utc)
    if str(cal.get("action", "")).upper() == "BLOCK":
        return _skip_row(
            skip_reason=f"calendar BLOCK: {cal.get('reason', '')}",
            ai=ai,
            regime_ctx=regime_ctx,
            cal=cal,
        )

    if ai.get("skip_trade"):
        return _skip_row(
            skip_reason=str(ai.get("skip_reason") or "enforce_rules"),
            ai=ai,
            regime_ctx=regime_ctx,
            cal=cal,
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
        log_fn=log_fn,
    )
    if not ok_v75:
        ai2 = dict(ai)
        ai2.update(v75_meta)
        return _skip_row(skip_reason=skip_v75, ai=ai2, regime_ctx=regime_ctx, cal=cal)

    normal_stop_v75 = float(v75_meta["normal_stop_price"])
    ai["stop_loss"] = normal_stop_v75

    sym_xu = sym.strip().upper()
    if strat_id == "M03_RSI_MOMENTUM_CONTINUATION" and sym_xu not in M03_ALLOWED_TICKERS:
        return _skip_row(
            skip_reason=f"[M03 BLOCKED] {sym_xu} not in allowed ticker list",
            ai=ai,
            regime_ctx=regime_ctx,
            cal=cal,
        )

    ok_mom, rs_mom = _momentum_neutral_ranging_skip(
        sym=sym,
        strat_id=strat_id,
        macro_bias=str(ai.get("macro_bias", "")),
        analysis_date=analysis_date,
        past=past,
        ind=ind or {},
        direction=direction,
        timeframe=timeframe,
        log_fn=log_fn,
    )
    if ok_mom:
        return _skip_row(skip_reason=rs_mom, ai=ai, regime_ctx=regime_ctx, cal=cal)

    trend_result = apply_trend_filter(sym, direction, strat_id, as_of_date=analysis_date.strip()[:10])
    if trend_result.get("action") == "BLOCK":
        return _skip_row(
            skip_reason=str(trend_result.get("reason") or "trend block"),
            ai=ai,
            regime_ctx=regime_ctx,
            cal=cal,
        )
    trend_mult = float(trend_result.get("size_multiplier", 1.0) or 1.0)

    sym_u = sym.upper()
    dir_u = direction.strip().upper()
    if sym_u in JPY_STORM_PAIRS:
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

    raw_sids = CHRONO_DAY_PREFILTER_SIDS.get((sym_u, dir_u), set())
    scount = len({s for s in raw_sids if _locked_confluence_sid(s)})
    triple = {"1w", "1d", "4h"}.issubset(CHRONO_SYMDIR_TFS.get((sym_u, dir_u), set()))
    cf_mult = _strategy_confluence_multiplier(scount, triple_tf_agreement=triple)
    if scount >= 3 and str(ai.get("tp_target") or "TP1").strip().upper() == "TP2":
        ai["tp_target"] = "TP3"

    ai["_v74_perfect_storm_m03_jpy"] = False
    ai.pop("_jpy_risk_headroom", None)
    bal = float(chrono_balance or STARTING_CAPITAL)
    lim = max(5000.0, bal * 0.05)
    ps_ok, _nj = detect_perfect_storm()
    daily_ok = float(chrono_day_pnl) > -lim
    if (
        ps_ok
        and daily_ok
        and strat_id == "M03_RSI_MOMENTUM_CONTINUATION"
        and sym_u in JPY_STORM_PAIRS
        and str(ai.get("confidence", "")).strip().upper() == "HIGH"
    ):
        ai["_v74_perfect_storm_m03_jpy"] = True
        if log_fn:
            log_fn("[PERFECT STORM] M03 JPY sizing 2.8% base risk", level="info")
    if sym_u.endswith("JPY"):
        hr = bal * 0.20 - float(CHRONO_JPY_RISK_DAY)
        ai["_jpy_risk_headroom"] = max(0.0, hr)

    _v73_refresh_risk_fields_from_ai_confidence(ai, tf_key, float(price), sym)
    _apply_chrono_risk_tp_multipliers(ai, price=float(price), chrono_risk_mult=chrono_risk_mult, chrono_tp_mult=chrono_tp_mult)
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

    tr_pre = trend_result.get("trend") or {}
    if not isinstance(tr_pre, dict):
        tr_pre = {}
    ts_boost = float(tr_pre.get("strength", 0) or 0)
    _v74_apply_recipe_and_monday_boosts(
        ai,
        sym=sym,
        strat_id=strat_id,
        locked_confluence=int(scount),
        tf_key=tf_key,
        analysis_date=analysis_date,
        log_fn=log_fn,
    )
    _v75_apply_macro_event_and_combo_boosts(
        ai,
        sym=sym,
        strat_id=strat_id,
        locked_confluence=int(scount),
        tf_key=tf_key,
        trend_strength=ts_boost,
        rate_diff=float(ai.get("macro_rate_diff", 0) or 0),
        period_mode=period_mode,
        log_fn=log_fn,
    )

    agreed_list = sorted(CHRONO_DAY_PREFILTER_SIDS.get((sym_u, direction), set()))
    td = tr_pre
    out: dict[str, Any] = {
        "skipped": False,
        "skip_trade": False,
        "strategy_id": strat_id,
        "direction": direction,
        "entry_price": round(float(ai.get("entry", entry) or entry), 5),
        "stop_loss": round(float(ai.get("stop_loss", stop) or stop), 5),
        "tp1": float(ai.get("tp1", tp1)),
        "tp2": float(ai.get("tp2", tp2)),
        "tp3": float(ai.get("tp3", tp3)),
        "max_risk_dollars": float(ai.get("_max_risk_dollars", 0) or 0),
        "account_risk_pct": float(ai.get("_account_risk_pct", 0) or 0),
        "confidence": str(ai.get("confidence", "LOW")),
        "confidence_pre_upgrade": ai.get("confidence_pre_upgrade"),
        "macro_bias": str(ai.get("macro_bias", "")),
        "macro_bias_adjusted": ai.get("macro_bias_adjusted", ai.get("macro_bias")),
        "macro_rate_diff": float(ai.get("macro_rate_diff", 0) or 0),
        "trend_strength": ts_boost,
        "trend_size_mult": trend_mult,
        "strategy_confluence_count": int(scount),
        "strategy_confluence_mult": float(cf_mult),
        "strategies_agreed": agreed_list,
        "period_mode": str(ai.get("period_mode") or period_mode),
        "macro_event_boost_applied": bool(ai.get("macro_event_boost_applied")),
        "combination_boost_applied": ai.get("combination_boost_applied", 1.0),
        "calendar_action": str(cal.get("action", "CLEAR")),
        "calendar_reason": str(cal.get("reason", "")),
        "is_exotic": is_exotic,
        "zone_pct": zone_pct,
        "zone_label": zone_label,
        **v75_meta,
        **_v73_regime_row_fields(regime_ctx),
        **merged_macro_result_fields(ai),
    }
    return out
