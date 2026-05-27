"""
APEX v7.3 Live — MT5 execution aligned with Railway backtest intelligence (May 2026).

Deploy to Windows VPS as ``C:\\Apex\\apex_trader.py`` (or set ``APEX_DATA_DIR``).

- Scans 8× daily at fixed UTC hours (no APScheduler tight loop).
- Uses ``prefilter_v6.python_prefilter`` + pandas/numpy TA columns (no ``pandas_ta`` / numba).
- Six locked strategies only; macro + calendar + regime + trend + confluence sizing stack.
- Simplified cooldowns vs legacy script; crash-safe main loop + MT5 reconnect.

**Never store MT5 password in this file.** Use ``APEX_MT5_PASSWORD``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from calendar_manager import check_calendar_risk
from macro_manager import (
    align_macro_bias_with_price,
    apply_macro_confidence_adjustment,
    get_macro_bias,
    macro_result_fields,
    set_backtest_mode,
)
from prefilter_v6 import python_prefilter
from regime_manager import get_regime_cached
from trend_manager import apply_trend_filter

set_backtest_mode(False)

# ---------------------------------------------------------------------------
# Paths (default Windows: C:\\Apex — case-insensitive on NTFS)
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    if os.name == "nt":
        default = r"C:\Apex"
    else:
        default = str(Path.home() / "Apex")
    return Path(os.environ.get("APEX_DATA_DIR", default)).resolve()


BASE_DIR = _base_dir()
LIVE_STATE_FILE = BASE_DIR / "live_state.json"
LIVE_TRADES_LOG = BASE_DIR / "live_trades_log.txt"
LOG_FILE = BASE_DIR / "apex_log.txt"
TICKET_META_FILE = BASE_DIR / "apex_trader_tickets.json"

MT5_LOGIN = int(os.environ.get("APEX_MT5_LOGIN", "107356886"))
MT5_SERVER = os.environ.get("APEX_MT5_SERVER", "MetaQuotes-Demo")
STARTING_BALANCE = float(os.environ.get("APEX_STARTING_BALANCE", "100000"))

SCAN_HOURS = [0, 3, 7, 9, 12, 15, 18, 21]

TICKERS: list[str] = [
    "AUDUSD", "AUDJPY", "CADJPY", "CHFJPY", "EURAUD", "EURCHF",
    "EURGBP", "EURNZD", "EURUSD", "GBPAUD", "GBPCAD", "GBPCHF",
    "GBPJPY", "GBPNZD", "GBPUSD", "NZDCAD", "NZDJPY", "NZDUSD",
    "USDCAD", "USDCHF", "USDJPY", "USDMXN", "USDNOK", "USDSEK",
    "USDZAR", "QQQ",
]

TIMEFRAMES: tuple[str, ...] = ("1w", "1d")

LIVE_STRATEGIES: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "R01_EXTREME_ZONE_REVERSION",
        "SMC05_EQUAL_HL_HUNT",
        "M02_MACD_ZERO_CROSS",
        "T07_MA_RIBBON_ALIGNMENT",
        "M03_RSI_MOMENTUM_CONTINUATION",
    }
)

# Base risk fractions (balance %) after macro confidence upgrade — matches backtest RISK_BY_CONFIDENCE style.
RISK_FRAC_BY_CONFIDENCE: dict[str, float] = {"LOW": 0.005, "MEDIUM": 0.010, "HIGH": 0.017}

APEX_MAGIC = 107356887
ORDER_COMMENT = "APEX"

EXOTIC_PAIRS = frozenset({"USDMXN", "USDZAR", "USDNOK", "USDSEK"})

_log_lock = threading.Lock()
_logger: logging.Logger | None = None


def log_msg(msg: str, level: str = "info") -> None:
    global _logger
    if _logger is None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        _logger = logging.getLogger("apex_trader")
        _logger.setLevel(logging.INFO)
        _logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        _logger.addHandler(fh)
        _logger.addHandler(sh)
    with _log_lock:
        getattr(_logger, level.lower(), _logger.info)(msg)


def _load(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log_msg(f"[state] load {path.name}: {e}", "warning")
    return default


def _save(path: Path, data: Any) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
    except OSError as e:
        log_msg(f"[state] save {path.name}: {e}", "error")


def load_live_state() -> dict[str, Any]:
    d = _load(
        LIVE_STATE_FILE,
        {
            "last_scan_slot": "",
            "last_trade_open": {},
            "last_trade_closed": {},
            "loss_consec": {},
            "loss_last_date": {},
            "day_key": "",
            "day_anchor": None,
            "anchor_equity": None,
            "halted_dd": False,
        },
    )
    return d if isinstance(d, dict) else {}


def save_live_state(d: dict[str, Any]) -> None:
    _save(LIVE_STATE_FILE, d)


def append_trade_log(line: str) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIVE_TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError as e:
        log_msg(f"[log] live_trades_log: {e}", "warning")


def ticket_meta_load() -> dict[str, Any]:
    return _load(TICKET_META_FILE, {})


def ticket_meta_save(d: dict[str, Any]) -> None:
    _save(TICKET_META_FILE, d)


# ---------------------------------------------------------------------------
# OHLCV + indicators (pandas/numpy only — no pandas_ta / numba on Windows VPS)
# ---------------------------------------------------------------------------


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _rsi_series(s: pd.Series, period: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0.0)
    lo = (-d.clip(upper=0.0))
    ag = g.ewm(alpha=1.0 / period, adjust=False).mean()
    al = lo.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = ag / al.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _true_range(h: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    prev = c.shift(1)
    return (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)


def _atr_wilder(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    tr = _true_range(h, l, c)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _adx_wilder(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    up = h.diff()
    dn = -l.diff()
    pdm = ((up > dn) & (up > 0)) * up
    mdm = ((dn > up) & (dn > 0)) * dn
    tr = _true_range(h, l, c)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    pdi = 100.0 * (pdm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan))
    mdi = 100.0 * (mdm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = (100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).fillna(0.0)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


def _append_indicators_numpy(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append columns using the same names ``python_prefilter`` / backtester expect
    (RSI_14, MACD_*, EMA_*, ATRr_14, ADX_14, BBL_/BBM_/BBU_*).
    """
    out = df.copy()
    c = out["Close"].astype(float)
    h = out["High"].astype(float)
    l = out["Low"].astype(float)
    out["EMA_20"] = _ema(c, 20)
    out["EMA_50"] = _ema(c, 50)
    out["EMA_200"] = _ema(c, 200)
    out["RSI_14"] = _rsi_series(c, 14)
    macd_line = _ema(c, 12) - _ema(c, 26)
    out["MACD_12_26_9"] = macd_line
    out["MACDs_12_26_9"] = _ema(macd_line, 9)
    out["MACDh_12_26_9"] = out["MACD_12_26_9"] - out["MACDs_12_26_9"]
    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std(ddof=0)
    out["BBM_20_2.0_2.0"] = mid
    out["BBU_20_2.0_2.0"] = mid + 2.0 * sd
    out["BBL_20_2.0_2.0"] = mid - 2.0 * sd
    out["ATRr_14"] = _atr_wilder(h, l, c, 14)
    out["ADX_14"] = _adx_wilder(h, l, c, 14)
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


def yf_ticker(sym: str) -> str:
    s = (sym or "").strip().upper()
    if s == "QQQ":
        return "QQQ"
    if len(s) == 6 and s.isalpha():
        return f"{s}=X"
    return s


def fetch_past_for_prefilter(sym: str, tf_key: str) -> pd.DataFrame | None:
    """Enough history for weekly/daily indicators + swings."""
    t = yf_ticker(sym)
    try:
        if tf_key == "1w":
            df = yf.download(t, period="max", interval="1wk", progress=False, auto_adjust=False)
        else:
            df = yf.download(t, period="5y", interval="1d", progress=False, auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        log_msg(f"[yf] {sym} {tf_key}: {e}", "warning")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).title() for c in df.columns})
    if "Adj Close" in df.columns and "Close" not in df.columns:
        df = df.rename(columns={"Adj Close": "Close"})
    df = df.dropna(subset=["Close"])
    min_rows = 35 if tf_key == "1w" else 55
    if len(df) < min_rows:
        return None
    return df


def build_prefilter_inputs(past: pd.DataFrame, sym: str) -> tuple[dict[str, Any], float, float] | None:
    """Return (ind dict, zone_pct, price) for python_prefilter."""
    try:
        past = _append_indicators_numpy(past)
    except Exception as e:  # noqa: BLE001
        log_msg(f"[ta] {sym}: {e}", "warning")
        return None

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

    return ind, zone_pct, price


def run_python_prefilter_live(sym: str, tf_key: str) -> tuple[list[tuple[str, str, int]], float, dict[str, Any], float, str]:
    """
    Returns filtered live strategies:
    ``(rows, price, ind, zone_pct, analysis_date)`` where rows are (sid, dir, score).
    """
    past = fetch_past_for_prefilter(sym, tf_key)
    if past is None:
        return [], 0.0, {}, 0.0, datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pack = build_prefilter_inputs(past, sym)
    if pack is None:
        return [], 0.0, {}, 0.0, datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ind, zone_pct, price = pack
    analysis_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok, rows, _reason = python_prefilter(
        sym,
        tf_key,
        float(price),
        ind,
        float(zone_pct),
        analysis_date=analysis_date,
        past=past,
    )
    if not ok:
        return [], float(price), ind, float(zone_pct), analysis_date
    out: list[tuple[str, str, int]] = []
    for sid, dr, sc in rows:
        su = str(sid).strip().upper()
        if su in LIVE_STRATEGIES:
            out.append((su, str(dr).strip().upper(), int(sc)))
    return out, float(price), ind, float(zone_pct), analysis_date


# ---------------------------------------------------------------------------
# Stops / targets (2R / 3R / 5R off entry–stop risk, same as backtest forward sim)
# ---------------------------------------------------------------------------

JPY_STORM_PAIRS: frozenset[str] = frozenset(
    {"USDJPY", "CADJPY", "AUDJPY", "GBPJPY", "NZDJPY", "CHFJPY"},
)

CONFLUENCE_COUNTING_LIVE: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "R01_EXTREME_ZONE_REVERSION",
        "SMC05_EQUAL_HL_HUNT",
        "M02_MACD_ZERO_CROSS",
        "T07_MA_RIBBON_ALIGNMENT",
        "M03_RSI_MOMENTUM_CONTINUATION",
        "B09_RSI_MOMENTUM_BREAK",
        "T10_200EMA_BOUNCE",
    },
)

M03_ALLOWED_TICKERS_LIVE: frozenset[str] = frozenset(
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

NEUTRAL_MACRO_BLOCKED_LIVE: frozenset[str] = frozenset(
    {
        "M03_RSI_MOMENTUM_CONTINUATION",
        "T08_DONCHIAN_BREAKOUT",
        "T04_ADX_TREND_ENTRY",
        "V02_ATR_EXPANSION_ENTRY",
        "M06_PRICE_ACCELERATION",
        "B10_WEEKLY_RANGE_BREAK",
    },
)


def detect_market_regime(macro_bias: str, trend_strength: float, rate_diff: float) -> str:
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


def detect_perfect_storm_live(snaps: dict[str, dict[str, Any]]) -> tuple[bool, int]:
    firing = sorted(s for s in JPY_STORM_PAIRS if s in snaps)
    if len(firing) < 3:
        return False, len(firing)
    rows = [snaps[s] for s in firing if isinstance(snaps.get(s), dict)]
    if len(rows) < 3:
        return False, len(rows)
    biases = {str(x.get("trend_bias", "NEUTRAL")).strip().upper() for x in rows}
    if biases != {"LONG"} and biases != {"SHORT"}:
        return False, len(rows)
    for x in rows:
        if str(x.get("macro_bias", "")).strip().upper() != "STRONG_TAILWIND":
            return False, len(rows)
        if str(x.get("confidence", "")).strip().upper() != "HIGH":
            return False, len(rows)
        if float(x.get("trend_strength", 0) or 0) <= 0.65:
            return False, len(rows)
        if float(x.get("rate_differential", 0) or 0) <= 2.5:
            return False, len(rows)
    return True, len(rows)


def regime_tp_prices(direction: str, entry: float, sl: float, regime: str) -> tuple[float, float, float]:
    d = direction.strip().upper()
    risk = abs(float(entry) - float(sl))
    if risk <= 0:
        risk = abs(float(entry)) * 0.005
    mult1, mult2, mult3 = (2.0, 4.0, 7.0) if regime == "TRENDING" else (1.5, 3.0, 5.0)
    m = 1.0 if d == "LONG" else -1.0
    return (
        round(entry + m * risk * mult1, 5),
        round(entry + m * risk * mult2, 5),
        round(entry + m * risk * mult3, 5),
    )


def stop_tp_bundle(strategy_id: str, direction: str, entry: float, atr: float) -> tuple[float, float, float, float]:
    """Return (sl, tp1, tp2, tp3). T01/R01 use legacy live multipliers; others 1.5 ATR stop."""
    d = direction.strip().upper()
    mult = 1.0 if d == "LONG" else -1.0
    sid = strategy_id.strip().upper()
    if sid == "T01_EMA_PULLBACK":
        sl = entry - mult * 1.5 * atr
        tp1 = entry + mult * 3.0 * atr
        tp2 = entry + mult * 4.5 * atr
    elif sid == "R01_EXTREME_ZONE_REVERSION":
        sl = entry - mult * 2.0 * atr
        tp1 = entry + mult * 4.0 * atr
        tp2 = entry + mult * 6.0 * atr
    else:
        sl = entry - mult * 1.5 * atr
        tp1 = entry + mult * 3.5 * atr
        tp2 = entry + mult * 5.5 * atr
    r = abs(entry - sl)
    if r <= 0:
        r = abs(entry) * 0.005
    tp3 = entry + mult * 5.0 * r
    return round(sl, 5), round(tp1, 5), round(tp2, 5), round(tp3, 5)


def rr_ok(entry: float, sl: float, tp1: float, direction: str) -> bool:
    risk = abs(entry - sl)
    if risk <= 0:
        return False
    rew = abs(tp1 - entry)
    return rew / risk >= 2.0 - 1e-9


# ---------------------------------------------------------------------------
# Risk helpers
# ---------------------------------------------------------------------------


def confluence_multiplier(n: int) -> float:
    if n >= 3:
        return 1.5
    if n == 2:
        return 1.25
    return 1.0


def get_currencies(ticker: str) -> list[str]:
    t = (ticker or "").strip().upper()
    return [t[:3], t[3:]] if len(t) == 6 and t.isalpha() else []


def open_position_currency_maps(mt5: Any) -> tuple[set[str], set[str]]:
    """Currencies with net long / net short exposure from open APEX positions."""
    long_c: set[str] = set()
    short_c: set[str] = set()
    for p in mt5.positions_get() or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_MAGIC:
            continue
        raw = str(p.symbol).replace(".", "").upper()
        base = raw[:6] if len(raw) >= 6 and raw[:6].isalpha() else raw
        ccys = get_currencies(base)
        if len(ccys) != 2:
            continue
        a, b = ccys[0], ccys[1]
        typ = int(getattr(p, "type", 0) or 0)
        # MT5: POSITION_TYPE_BUY = 0
        if typ == 0:
            long_c.add(a)
            short_c.add(b)
        else:
            short_c.add(a)
            long_c.add(b)
    return long_c, short_c


def currency_direction_conflict(sym: str, direction: str, mt5: Any) -> tuple[bool, str]:
    """Block if new trade leg fights an open currency leg (live spec)."""
    long_c, short_c = open_position_currency_maps(mt5)
    d = direction.strip().upper()
    cc = get_currencies(sym)
    if len(cc) != 2:
        return False, ""
    a, b = cc[0], cc[1]
    if d == "LONG":
        if a in short_c:
            return True, f"conflict: {a} short from open exposure"
        if b in long_c:
            return True, f"conflict: {b} long from open exposure"
    else:
        if a in long_c:
            return True, f"conflict: {a} long from open exposure"
        if b in short_c:
            return True, f"conflict: {b} short from open exposure"
    return False, ""


def is_first_friday_nfp(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def nfp_blocks_symbol(sym: str, today: date) -> bool:
    if not is_first_friday_nfp(today):
        return False
    s = sym.strip().upper()
    if "USD" in s:
        return True
    return False


def loss_streak_block(st: dict[str, Any], sym: str, tf: str, direction: str, today: date) -> tuple[bool, str]:
    lk = f"{sym.strip().upper()}_{tf.lower()}_{direction.strip().upper()}"
    consec = int((st.get("loss_consec") or {}).get(lk, 0) or 0)
    if consec < 3:
        return False, ""
    ld = str((st.get("loss_last_date") or {}).get(lk, "") or "")[:10]
    if not ld:
        return False, ""
    try:
        d0 = date.fromisoformat(ld)
    except ValueError:
        return False, ""
    if (today - d0).days < 21:
        return True, "extended_loss_21d"
    return False, ""


def closed_cooldown_block(st: dict[str, Any], sym: str, tf: str, today: date) -> tuple[bool, str]:
    """2-day cooldown after last *close* on this ticker+timeframe."""
    key = f"{sym.strip().upper()}_{tf.lower()}"
    last = str((st.get("last_trade_closed") or {}).get(key, "") or "")[:10]
    if not last:
        return False, ""
    try:
        d0 = date.fromisoformat(last)
    except ValueError:
        return False, ""
    if (today - d0).days < 2:
        return True, "sym_tf_2d_cooldown"
    return False, ""


def finalize_closed_positions(mt5: Any, st: dict[str, Any]) -> dict[str, Any]:
    """When a ticket leaves ``positions_get``, apply loss streak + ``last_trade_closed`` once."""
    try:
        import MetaTrader5 as mt5m

        open_ids = {int(p.ticket) for p in (mt5.positions_get() or []) if int(getattr(p, "magic", 0) or 0) == APEX_MAGIC}
        meta = ticket_meta_load()
        changed = False
        lc = dict(st.get("loss_consec") or {})
        ll = dict(st.get("loss_last_date") or {})
        ltc = dict(st.get("last_trade_closed") or {})
        today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t0 = datetime.now(timezone.utc) - timedelta(days=14)

        for k, m in list(meta.items()):
            if not isinstance(m, dict):
                continue
            try:
                tid = int(k)
            except ValueError:
                continue
            if tid in open_ids:
                continue
            prof = 0.0
            deals = mt5.history_deals_get(t0.replace(tzinfo=None), datetime.utcnow(), position=tid) or []
            for d in deals:
                if int(getattr(d, "magic", 0) or 0) != APEX_MAGIC:
                    continue
                if d.entry == mt5m.DEAL_ENTRY_OUT:
                    prof += float(d.profit)
            sym = str(m.get("ticker", "")).upper()
            tf = str(m.get("tf", "")).lower()
            dr = str(m.get("direction", "")).upper()
            lk = f"{sym}_{tf}_{dr}"
            lc[lk] = lc.get(lk, 0) + 1 if prof < 0 else 0
            ll[lk] = today_s
            ltc[f"{sym}_{tf}"] = today_s
            del meta[k]
            changed = True

        if changed:
            st["loss_consec"] = lc
            st["loss_last_date"] = ll
            st["last_trade_closed"] = ltc
            ticket_meta_save(meta)
    except Exception as e:  # noqa: BLE001
        log_msg(f"[finalize] {e}", "warning")
    return st


def update_closed_trades_and_losses(mt5: Any, st: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible name — delegates to ``finalize_closed_positions``."""
    return finalize_closed_positions(mt5, st)


# ---------------------------------------------------------------------------
# MT5 (preserved structure from prior apex_trader)
# ---------------------------------------------------------------------------


def mt5_connect() -> Any | None:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        log_msg("[MT5] pip install MetaTrader5 (Windows)", "error")
        return None
    pw = (os.environ.get("APEX_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD") or "").strip()
    if not pw:
        log_msg("[MT5] Set APEX_MT5_PASSWORD", "error")
        return None
    path = os.environ.get("APEX_MT5_PATH") or os.environ.get("MT5_PATH")
    kw: dict[str, Any] = {"login": MT5_LOGIN, "password": pw, "server": MT5_SERVER}
    if path:
        kw["path"] = path
    if not mt5.initialize(**kw) or not mt5.login(MT5_LOGIN, password=pw, server=MT5_SERVER):
        log_msg(f"[MT5] connect fail {mt5.last_error()}", "error")
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
        return None
    return mt5


_mt5_holder: dict[str, Any] = {"m": None}


def ensure_mt5() -> Any | None:
    m = _mt5_holder.get("m")
    if m is not None:
        try:
            if m.terminal_info():
                return m
        except Exception:  # noqa: BLE001
            pass
        try:
            m.shutdown()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(1)
    nm = mt5_connect()
    _mt5_holder["m"] = nm
    return nm


def resolve_sym(mt5: Any, s: str) -> str | None:
    u = s.strip().upper()
    if mt5.symbol_select(u, True):
        return u
    for suf in (".", "m", ".a"):
        if mt5.symbol_select(u + suf, True):
            return u + suf
    return None


def fill_mode(mt5: Any, sym: str) -> int:
    info = mt5.symbol_info(sym)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = int(info.filling_mode)
    if fm & mt5.ORDER_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if fm & mt5.ORDER_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def mpl_sl(mt5: Any, sym: str, entry: float, sl: float, d: str) -> float | None:
    info = mt5.symbol_info(sym)
    if info is None:
        return None
    ts, tv = float(info.trade_tick_size or info.point or 0), float(info.trade_tick_value or 0)
    if ts <= 0 or tv <= 0:
        return None
    ticks = (sl - entry) / ts if d == "LONG" else (entry - sl) / ts
    return abs(float(ticks) * tv)


def norm_vol(mt5: Any, sym: str, v: float) -> float:
    info = mt5.symbol_info(sym)
    if info is None:
        return round(v, 2)
    step = float(info.volume_step or 0.01) or 0.01
    vmin, vmax = float(info.volume_min or 0.01), float(info.volume_max or 100.0)
    steps = math.floor(v / step + 1e-9)
    v2 = max(vmin, min(vmax, steps * step))
    return round(v2, int(max(0, -math.floor(math.log10(step)))))


def mt5_round_price(mt5: Any, sym: str, price: float) -> float:
    info = mt5.symbol_info(sym)
    if info is None:
        return round(float(price), 5)
    dg = int(getattr(info, "digits", 5) or 5)
    return round(float(price), dg)


def clamp_sl_buy_live(mt5: Any, sym: str, bid: float, want_sl: float) -> float:
    """BUY: stop must stay strictly below bid (freeze / stops level)."""
    info = mt5.symbol_info(sym)
    if info is None:
        return want_sl
    pt = float(info.point or 1e-5)
    lvl = int(getattr(info, "trade_stops_level", 0) or 0)
    dist = (lvl + 1) * pt
    max_sl = bid - dist
    return min(float(want_sl), max_sl)


def clamp_sl_sell_live(mt5: Any, sym: str, ask: float, want_sl: float) -> float:
    """SELL: stop must stay strictly above ask."""
    info = mt5.symbol_info(sym)
    if info is None:
        return want_sl
    pt = float(info.point or 1e-5)
    lvl = int(getattr(info, "trade_stops_level", 0) or 0)
    dist = (lvl + 1) * pt
    min_sl = ask + dist
    return max(float(want_sl), min_sl)


def open_apex_positions(mt5: Any) -> list[Any]:
    out: list[Any] = []
    for p in mt5.positions_get() or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_MAGIC:
            continue
        if (p.comment or "").strip() != ORDER_COMMENT:
            continue
        out.append(p)
    return out


def close_all_apex(mt5: Any) -> None:
    import MetaTrader5 as mt5

    for p in open_apex_positions(mt5):
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        typ = mt5.ORDER_TYPE_SELL if int(p.type) == 0 else mt5.ORDER_TYPE_BUY
        price = float(tick.bid if typ == mt5.ORDER_TYPE_SELL else tick.ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": typ,
            "position": int(p.ticket),
            "price": price,
            "deviation": 25,
            "magic": APEX_MAGIC,
            "comment": ORDER_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": fill_mode(mt5, p.symbol),
        }
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            log_msg(f"[close_all] fail ticket={p.ticket} {r}", "error")


def order_send_live(
    mt5: Any,
    sym: str,
    d: str,
    sl: float,
    risk_usd: float,
    meta: dict[str, Any],
) -> dict[str, Any]:
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return {"ok": False, "error": "no_tick"}
    entry = float(tick.ask if d == "LONG" else tick.bid)
    mpl = mpl_sl(mt5, sym, entry, sl, d)
    if mpl is None or mpl <= 0:
        return {"ok": False, "error": "mpl"}
    vol = norm_vol(mt5, sym, risk_usd / mpl)
    typ = mt5.ORDER_TYPE_BUY if d == "LONG" else mt5.ORDER_TYPE_SELL
    price = float(tick.ask if d == "LONG" else tick.bid)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": vol,
        "type": typ,
        "price": price,
        "sl": float(sl),
        "tp": 0.0,
        "deviation": 25,
        "magic": APEX_MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill_mode(mt5, sym),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        return {"ok": False, "error": getattr(res, "comment", str(res))}
    time.sleep(0.25)
    ticket = None
    candidates: list[Any] = []
    for p in mt5.positions_get(symbol=sym) or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_MAGIC:
            continue
        pc = (p.comment or "").strip()
        if pc and pc != ORDER_COMMENT:
            continue
        candidates.append(p)
    if candidates:
        ticket = int(
            max(candidates, key=lambda pp: (int(getattr(pp, "time", 0) or 0), int(pp.ticket))).ticket
        )
    if ticket is None:
        log_msg(f"[ORDER] filled but no APEX position row for {sym} (check magic/comment)", "error")
        return {"ok": False, "error": "ticket_resolve"}
    meta = dict(meta)
    meta["ticket"] = ticket
    meta["entry_fill"] = entry
    meta["r"] = abs(entry - float(sl))
    meta["tp1"] = float(meta.get("tp1", 0) or 0)
    meta["tp2"] = float(meta.get("tp2", 0) or 0)
    meta["tp3"] = float(meta.get("tp3", 0) or 0)
    tm = ticket_meta_load()
    if ticket:
        tm[str(ticket)] = {k: (float(v) if isinstance(v, (float, int)) else v) for k, v in meta.items()}
        ticket_meta_save(tm)
    return {"ok": True, "ticket": ticket, "volume": vol, "entry": entry}


# ---------------------------------------------------------------------------
# Trailing (Part 7): TP1 BE, TP2 lock at TP1, TP3 partial 50% + trail remainder
# ---------------------------------------------------------------------------


def manage_trailing_live(mt5: Any) -> None:
    import MetaTrader5 as mt5

    if not mt5.terminal_info():
        return
    meta = ticket_meta_load()
    raw = mt5.positions_get() or []
    managed: list[Any] = []
    for pos in raw:
        if int(getattr(pos, "magic", 0) or 0) != APEX_MAGIC:
            continue
        k = str(int(pos.ticket))
        if not isinstance(meta.get(k), dict):
            continue
        managed.append(pos)

    log_msg(f"[TRAIL CHECK] Running trailing stop update for {len(managed)} open positions", "info")

    for pos in managed:
        k = str(int(pos.ticket))
        m = meta[k]
        d = str(m.get("direction", "")).upper()
        entry = float(m.get("entry_fill", pos.price_open))
        tp1 = float(m.get("tp1", 0) or 0)
        tp2 = float(m.get("tp2", 0) or 0)
        tp3 = float(m.get("tp3", 0) or 0)
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            log_msg(f"[TRAIL] {pos.symbol} ticket#{pos.ticket} no tick — skip", "warning")
            continue
        bid, ask = float(tick.bid), float(tick.ask)
        cur_sl = float(pos.sl or 0.0)
        vol = float(pos.volume)
        px = bid if d == "LONG" else ask

        hit1 = bool(m.get("hit_tp1"))
        hit2 = bool(m.get("hit_tp2"))
        hit3p = bool(m.get("hit_tp3_partial"))
        hit3full = bool(m.get("hit_tp3_full"))
        treg = str(m.get("trail_regime", "CHOPPY")).upper()
        atru = float(m.get("atr_live", 0) or 0) or abs(entry) * 0.001
        nsl: float | None = None
        advance_hit1 = False
        advance_hit2 = False

        tp1_reached = (d == "LONG" and tp1 > 0 and bid >= tp1) or (d == "SHORT" and tp1 > 0 and ask <= tp1)
        tp1_hit_label = "YES" if (hit1 or tp1_reached) else "NO"

        cm = (pos.comment or "").strip()
        log_msg(
            f"[TRAIL] {pos.symbol} ticket#{pos.ticket} entry={entry:.5f} TP1={tp1:.5f} current={px:.5f} "
            f"TP1_hit={tp1_hit_label} comment={cm!r}",
            "info",
        )

        def _partial_close(vol_close: float, otyp: int, px2: float, label: str) -> bool:
            v = norm_vol(mt5, pos.symbol, vol_close)
            if v <= 0 or v >= vol:
                return False
            cr = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos.symbol,
                    "volume": v,
                    "type": otyp,
                    "position": int(pos.ticket),
                    "price": px2,
                    "deviation": 25,
                    "magic": APEX_MAGIC,
                    "comment": ORDER_COMMENT,
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": fill_mode(mt5, pos.symbol),
                }
            )
            if cr and cr.retcode == mt5.TRADE_RETCODE_DONE:
                log_msg(f"[PARTIAL] ticket={pos.ticket} {label} vol={v}", "info")
                return True
            rc = getattr(cr, "retcode", None) if cr else None
            log_msg(
                f"[PARTIAL MT5] ticket={pos.ticket} {label} retcode={rc} "
                f"comment={getattr(cr, 'comment', '')!r}",
                "warning",
            )
            return False

        if d == "LONG":
            px = bid
            if not hit1 and tp1 > 0 and px >= tp1:
                nsl = entry
                advance_hit1 = True
                if treg == "CHOPPY" and not m.get("tp1_partial_done"):
                    if _partial_close(vol * 0.25, mt5.ORDER_TYPE_SELL, bid, "CHOPPY 25% @TP1"):
                        m["tp1_partial_done"] = True
            elif hit1 and not hit2 and tp2 > 0 and px >= tp2:
                nsl = tp1
                advance_hit2 = True
                if not m.get("tp2_touch_logged"):
                    m["tp2_touch_logged"] = True
                    if treg == "TRENDING":
                        _partial_close(vol * 0.20, mt5.ORDER_TYPE_SELL, bid, "TREND 20% @TP2")
                    elif treg == "CHOPPY":
                        _partial_close(vol * 0.40, mt5.ORDER_TYPE_SELL, bid, "CHOP 40% @TP2")
            elif hit2 and treg == "CHOPPY" and not hit3full and tp3 > 0 and px >= tp3:
                if _partial_close(vol * 1.0, mt5.ORDER_TYPE_SELL, bid, "CHOPPY 100% @TP3"):
                    m["hit_tp3_full"] = True
            elif hit2 and treg == "TRENDING" and not hit3p and tp3 > 0 and px >= tp3:
                if _partial_close(vol * 0.30, mt5.ORDER_TYPE_SELL, bid, "TREND 30% @TP3"):
                    m["hit_tp3_partial"] = True
                    m["peak_hi"] = max(float(m.get("peak_hi", entry)), px)
                ph = float(m.get("peak_hi", px))
                ph = max(ph, px)
                m["peak_hi"] = ph
                trail = ph - 2.0 * atru
                nsl = max(cur_sl, trail) if cur_sl > 0 else trail
            elif hit2 and treg == "TRENDING" and hit3p and tp3 > 0:
                ph = max(float(m.get("peak_hi", px)), px)
                m["peak_hi"] = ph
                trail = ph - 2.0 * atru
                if trail > cur_sl:
                    nsl = trail
            if treg == "CHOPPY" and hit1 and not hit3full and (rates := mt5.copy_rates_from_pos(pos.symbol, mt5.TIMEFRAME_H4, 0, 4)) is not None:
                if len(rates) >= 2:
                    prev_low = float(rates[-2]["low"])
                    prop = prev_low - 0.5 * atru
                    if prop > cur_sl and (not hit2 or hit2):
                        nsl2 = max(cur_sl, prop)
                        if nsl is None or nsl2 > (nsl or cur_sl):
                            nsl = nsl2
        else:
            px = ask
            if not hit1 and tp1 > 0 and px <= tp1:
                nsl = entry
                advance_hit1 = True
                if treg == "CHOPPY" and not m.get("tp1_partial_done"):
                    if _partial_close(vol * 0.25, mt5.ORDER_TYPE_BUY, ask, "CHOPPY 25% @TP1"):
                        m["tp1_partial_done"] = True
            elif hit1 and not hit2 and tp2 > 0 and px <= tp2:
                nsl = tp1
                advance_hit2 = True
                if not m.get("tp2_touch_logged"):
                    m["tp2_touch_logged"] = True
                    if treg == "TRENDING":
                        _partial_close(vol * 0.20, mt5.ORDER_TYPE_BUY, ask, "TREND 20% @TP2")
                    elif treg == "CHOPPY":
                        _partial_close(vol * 0.40, mt5.ORDER_TYPE_BUY, ask, "CHOP 40% @TP2")
            elif hit2 and treg == "CHOPPY" and not hit3full and tp3 > 0 and px <= tp3:
                if _partial_close(vol * 1.0, mt5.ORDER_TYPE_BUY, ask, "CHOPPY 100% @TP3"):
                    m["hit_tp3_full"] = True
            elif hit2 and treg == "TRENDING" and not hit3p and tp3 > 0 and px <= tp3:
                if _partial_close(vol * 0.30, mt5.ORDER_TYPE_BUY, ask, "TREND 30% @TP3"):
                    m["hit_tp3_partial"] = True
                    m["peak_lo"] = min(float(m.get("peak_lo", entry)), px)
                pl = float(m.get("peak_lo", px))
                pl = min(pl, px)
                m["peak_lo"] = pl
                trail = pl + 2.0 * atru
                nsl = min(cur_sl, trail) if cur_sl > 0 else trail
            elif hit2 and treg == "TRENDING" and hit3p and tp3 > 0:
                pl = min(float(m.get("peak_lo", px)), px)
                m["peak_lo"] = pl
                trail = pl + 2.0 * atru
                if cur_sl <= 0 or trail < cur_sl:
                    nsl = trail
            if treg == "CHOPPY" and hit1 and not hit3full and (rates := mt5.copy_rates_from_pos(pos.symbol, mt5.TIMEFRAME_H4, 0, 4)) is not None:
                if len(rates) >= 2:
                    prev_high = float(rates[-2]["high"])
                    prop = prev_high + 0.5 * atru
                    if cur_sl <= 0 or prop < cur_sl:
                        nsl2 = min(cur_sl, prop) if cur_sl > 0 else prop
                        if nsl is None or nsl2 < (nsl if nsl is not None else prop + 1):
                            nsl = nsl2

        if nsl is not None:
            if d == "LONG":
                nsl = clamp_sl_buy_live(mt5, pos.symbol, bid, float(nsl))
            else:
                nsl = clamp_sl_sell_live(mt5, pos.symbol, ask, float(nsl))
            nsl = mt5_round_price(mt5, pos.symbol, float(nsl))
            if d == "LONG" and nsl >= bid:
                log_msg(
                    f"[TRAIL] {pos.symbol} ticket#{pos.ticket} clamped SL {nsl:.5f} still >= bid {bid:.5f} — skip modify",
                    "warning",
                )
                meta[k] = m
                continue
            if d == "SHORT" and nsl <= ask:
                log_msg(
                    f"[TRAIL] {pos.symbol} ticket#{pos.ticket} clamped SL {nsl:.5f} still <= ask {ask:.5f} — skip modify",
                    "warning",
                )
                meta[k] = m
                continue
            si = mt5.symbol_info(pos.symbol)
            pt = float(si.point or 1e-5) if si is not None else 1e-5
            if abs(nsl - cur_sl) < pt * 0.5 and cur_sl > 0:
                log_msg(
                    f"[TRAIL] {pos.symbol} ticket#{pos.ticket} SL unchanged (within point) cur={cur_sl:.5f}",
                    "info",
                )
                meta[k] = m
                continue

            res = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": pos.symbol,
                    "position": int(pos.ticket),
                    "sl": float(nsl),
                    "tp": float(pos.tp or 0.0),
                }
            )
            rc = getattr(res, "retcode", None) if res else None
            cc = getattr(res, "comment", "") if res else ""
            deal = getattr(res, "deal", 0) if res else 0
            ord_ = getattr(res, "order", 0) if res else 0
            lvl = "info" if res and rc == mt5.TRADE_RETCODE_DONE else "warning"
            log_msg(
                f"[TRAIL MT5] {pos.symbol} ticket#{pos.ticket} SLTP retcode={rc} comment={cc!r} "
                f"deal={deal} order={ord_} request_sl={nsl:.5f} cur_sl={cur_sl:.5f}",
                lvl,
            )
            if res and rc == mt5.TRADE_RETCODE_DONE:
                log_msg(f"[TRAIL] ticket={pos.ticket} new_sl={nsl:.5f}", "info")
                if advance_hit1:
                    m["hit_tp1"] = True
                if advance_hit2:
                    m["hit_tp2"] = True
        meta[k] = m
    ticket_meta_save(meta)


# ---------------------------------------------------------------------------
# Sizing stack (Part 5)
# ---------------------------------------------------------------------------


def pick_signal_for_sym_tf(rows: list[tuple[str, str, int]]) -> tuple[str, str, int] | None:
    if not rows:
        return None
    prio = {
        "T01_EMA_PULLBACK": 0,
        "R01_EXTREME_ZONE_REVERSION": 1,
        "SMC05_EQUAL_HL_HUNT": 2,
        "T07_MA_RIBBON_ALIGNMENT": 3,
        "M02_MACD_ZERO_CROSS": 4,
        "M03_RSI_MOMENTUM_CONTINUATION": 5,
    }
    return sorted(rows, key=lambda x: (-int(x[2]), prio.get(x[0], 99), x[0]))[0]


def run_full_scan() -> None:
    import MetaTrader5 as mt5

    st = load_live_state()
    if st.get("halted_dd"):
        log_msg("[SCAN] halted — total drawdown pause active", "warning")
        return

    m = ensure_mt5()
    if not m:
        log_msg("[SCAN] MT5 unavailable", "error")
        return

    ai = m.account_info()
    if ai is None:
        log_msg("[SCAN] no account_info", "error")
        return
    balance = float(ai.balance)
    equity = float(ai.equity)
    if st.get("anchor_equity") is None:
        st["anchor_equity"] = equity
    anchor = float(st.get("anchor_equity") or equity)

    now = datetime.now(timezone.utc)
    today = now.date()
    dk = now.strftime("%Y-%m-%d")
    if st.get("day_key") != dk:
        st["day_key"] = dk
        st["day_anchor"] = equity
        st["jpy_risk_usd_day"] = 0.0
        save_live_state(st)
    day_anchor = float(st.get("day_anchor") or equity)
    daily_pnl = equity - day_anchor
    dd_frac = (anchor - equity) / anchor if anchor > 0 else 0.0

    daily_limit = max(5000.0, balance * 0.05)
    if daily_pnl <= -daily_limit:
        log_msg(f"[RISK] daily loss limit hit ({daily_pnl:.2f} <= -{daily_limit:.2f})", "warning")
        return
    if dd_frac >= 0.20:
        log_msg("[RISK] -20% drawdown — closing all and halting new trades", "critical")
        close_all_apex(m)
        st["halted_dd"] = True
        save_live_state(st)
        return

    st = update_closed_trades_and_losses(m, st)
    save_live_state(st)

    if len(open_apex_positions(m)) >= 15:
        log_msg("[SCAN] max 15 open positions", "warning")
        return

    job_id = os.environ.get("APEX_JOB_ID", "live")
    regime = get_regime_cached(job_id)
    reg_m = float(regime.get("size_multiplier", 1.0) or 1.0)

    # Pass 1 — confluence: distinct LIVE strategies per (sym, direction) this scan
    agree: dict[tuple[str, str], set[str]] = defaultdict(set)
    scan_cells: list[tuple[str, str, list[tuple[str, str, int]], float, dict[str, Any], float, str]] = []
    for sym in TICKERS:
        for tf in TIMEFRAMES:
            rows, price, ind, zone_pct, ad = run_python_prefilter_live(sym, tf)
            if not rows:
                continue
            scan_cells.append((sym, tf, rows, price, ind, zone_pct, ad))
            for sid, dr, _sc in rows:
                if dr == "BOTH":
                    agree[(sym.upper(), "LONG")].add(sid)
                    agree[(sym.upper(), "SHORT")].add(sid)
                elif dr in ("LONG", "SHORT"):
                    agree[(sym.upper(), dr)].add(sid)

    storm_snap: dict[str, dict[str, Any]] = {}
    for sym, tf, rows, price, ind, zone_pct, ad in scan_cells:
        su = sym.upper()
        if su not in JPY_STORM_PAIRS:
            continue
        sig2 = pick_signal_for_sym_tf(rows)
        if sig2 is None:
            continue
        sid2, dr2, _sc2 = sig2
        if dr2 not in ("LONG", "SHORT"):
            continue
        macro2 = get_macro_bias(sym, dr2)
        try:
            ad_st = date.fromisoformat(str(ad)[:10])
        except (TypeError, ValueError):
            ad_st = today
        macro2 = align_macro_bias_with_price(
            sym, dr2, macro2, as_of_date=ad_st, price_df=None, log_fn=log_msg
        )
        conf2 = apply_macro_confidence_adjustment("MEDIUM", macro2)
        tr2 = apply_trend_filter(sym, dr2, sid2, as_of_date=ad)
        td2 = tr2.get("trend") or {}
        if not isinstance(td2, dict):
            td2 = {}
        storm_snap[su] = {
            "macro_bias": str(macro2.get("bias", "")),
            "confidence": str(conf2).strip().upper(),
            "trend_strength": float(td2.get("strength", 0) or 0),
            "trend_bias": str(td2.get("direction_bias", "NEUTRAL")).strip().upper(),
            "rate_differential": float(macro2.get("rate_differential", 0) or 0),
        }
    perfect_ok, storm_ct = detect_perfect_storm_live(storm_snap)

    placed = 0
    skipped = 0
    lines_out: list[str] = []

    for sym, tf, rows, price, ind, zone_pct, ad in scan_cells:
        sig = pick_signal_for_sym_tf(rows)
        if sig is None:
            continue
        sid, direction, _score = sig
        if direction not in ("LONG", "SHORT"):
            skipped += 1
            continue

        if nfp_blocks_symbol(sym, today):
            msg = f"SKIP: {sym} {tf} {direction} — NFP first Friday (USD)"
            lines_out.append(msg)
            skipped += 1
            continue

        ok_ls, rs_ls = loss_streak_block(st, sym, tf, direction, today)
        if ok_ls:
            lines_out.append(f"SKIP: {sym} {tf} {direction} — {rs_ls}")
            skipped += 1
            continue

        ok_c, rs_c = closed_cooldown_block(st, sym, tf, today)
        if ok_c:
            lines_out.append(f"SKIP: {sym} {tf} {direction} — {rs_c}")
            skipped += 1
            continue

        ok_cc, rs_cc = currency_direction_conflict(sym, direction, m)
        if ok_cc:
            lines_out.append(f"SKIP: {sym} {tf} {direction} — {rs_cc}")
            skipped += 1
            continue

        now_utc = datetime.now(timezone.utc)
        cal = check_calendar_risk(sym, now_utc)
        if str(cal.get("action", "")).upper() == "BLOCK":
            lines_out.append(f"SKIP: {sym} {tf} {direction} — calendar BLOCK")
            skipped += 1
            continue
        cal_pen = 0.5 if str(cal.get("action", "")).upper() == "REDUCE" else 1.0

        try:
            ad_live = date.fromisoformat(str(ad)[:10])
        except (TypeError, ValueError):
            ad_live = today
        macro = get_macro_bias(sym, direction)
        macro = align_macro_bias_with_price(
            sym, direction, macro, as_of_date=ad_live, price_df=None, log_fn=log_msg
        )
        conf0 = "MEDIUM"
        conf = apply_macro_confidence_adjustment(conf0, macro)
        cpu = conf0 if conf0 != conf else None

        if sid == "M03_RSI_MOMENTUM_CONTINUATION" and sym.upper() not in M03_ALLOWED_TICKERS_LIVE:
            lines_out.append(f"SKIP: {sym} {tf} M03 — [M03 BLOCKED] {sym.upper()} not in allowed ticker list")
            skipped += 1
            continue

        if str(macro.get("bias", "")).strip().upper() == "NEUTRAL" and sid in NEUTRAL_MACRO_BLOCKED_LIVE:
            lines_out.append(
                f"SKIP: {sym} {tf} {sid} — [NEUTRAL BLOCK] momentum strategy not allowed in NEUTRAL macro"
            )
            skipped += 1
            continue

        if sid == "M03_RSI_MOMENTUM_CONTINUATION" and conf != "HIGH":
            lines_out.append(f"SKIP: {sym} {tf} M03 — need HIGH after macro ({conf})")
            skipped += 1
            continue

        tr = apply_trend_filter(sym, direction, sid, as_of_date=ad)
        if tr.get("action") == "BLOCK":
            lines_out.append(f"SKIP: {sym} {tf} {direction} — {tr.get('reason', 'trend')}")
            skipped += 1
            continue
        trend_m = float(tr.get("size_multiplier", 1.0) or 1.0)

        td_w = tr.get("trend") or {}
        if not isinstance(td_w, dict):
            td_w = {}
        ts_wd = float(td_w.get("strength", 0) or 0)
        try:
            wd_scan = date.fromisoformat(str(ad)[:10]).weekday()
        except (TypeError, ValueError):
            wd_scan = today.weekday()
        if tf.strip().lower() == "1w" and wd_scan in (2, 3, 4) and ts_wd <= 0.75:
            lines_out.append(
                f"SKIP: {sym} {tf} {direction} — [1W TIMING] mid-week requires trend_strength > 0.75 "
                f"(current {ts_wd:.2f})"
            )
            skipped += 1
            continue

        raw_agree = agree.get((sym.upper(), direction), set())
        n_agree = len(raw_agree & CONFLUENCE_COUNTING_LIVE)
        cf_m = confluence_multiplier(n_agree)

        base_risk = balance * float(RISK_FRAC_BY_CONFIDENCE.get(conf, 0.01))
        if (
            perfect_ok
            and daily_pnl > -daily_limit
            and sid == "M03_RSI_MOMENTUM_CONTINUATION"
            and sym.upper() in JPY_STORM_PAIRS
            and conf == "HIGH"
        ):
            base_risk = balance * 0.028
            log_msg(f"[PERFECT STORM] {storm_ct} JPY pairs aligned — M03 sizing: 2.8% base risk", "info")
        macro_m = float(macro.get("size_multiplier", 1.0) or 1.0)
        raw = base_risk * cal_pen * macro_m * trend_m * cf_m * reg_m
        cap_hi = balance * RISK_FRAC_BY_CONFIDENCE["HIGH"] * 1.5
        final_risk = max(25.0, min(raw, cap_hi))
        final_risk = min(final_risk, balance * 0.055)
        jpy_used = float(st.get("jpy_risk_usd_day", 0) or 0)
        if sym.upper().endswith("JPY"):
            headroom = max(0.0, balance * 0.20 - jpy_used)
            final_risk = min(final_risk, headroom)

        mb_adj = str(macro.get("bias", "")).strip().upper()
        if tf.strip().lower() == "1w" and ad_live.weekday() == 0 and mb_adj in ("STRONG_TAILWIND", "TAILWIND"):
            final_risk = min(final_risk * 1.25, cap_hi)
            log_msg(
                f"[MONDAY BOOST] {sym.upper()} {tf.upper()} {direction}: 1.25x Monday multiplier applied",
                "info",
            )
        rec_sid = {
            "M03_RSI_MOMENTUM_CONTINUATION",
            "B09_RSI_MOMENTUM_BREAK",
            "T01_EMA_PULLBACK",
        }
        if (
            sym.upper() in ("AUDJPY", "USDMXN")
            and mb_adj == "STRONG_TAILWIND"
            and conf == "MEDIUM"
            and 2 <= n_agree <= 4
            and sid in rec_sid
        ):
            cap5 = balance * 0.05
            final_risk = min(final_risk * 1.5, cap5, cap_hi)
            log_msg(
                f"[RECIPE BOOST] {sym.upper()} {sid} STRONG_TAILWIND MEDIUM conf:{n_agree} — 1.5x boost",
                "info",
            )
        final_risk = max(25.0, min(final_risk, cap_hi))

        atrv = float(ind.get("atr", price * 0.01) or (price * 0.01))
        sl, _t1, _t2, _t3 = stop_tp_bundle(sid, direction, float(price), atrv)
        entry = float(price)
        td0 = tr.get("trend") or {}
        if not isinstance(td0, dict):
            td0 = {}
        ts0 = float(td0.get("strength", 0) or 0)
        mb0 = str(macro.get("bias", ""))
        rd0 = float(macro.get("rate_differential", 0) or 0)
        trail_reg = detect_trailing_regime(mb0, ts0, rd0)
        tp1, tp2, tp3 = regime_tp_prices(direction, entry, sl, trail_reg)
        if not rr_ok(entry, sl, tp1, direction):
            lines_out.append(f"SKIP: {sym} {tf} {sid} — R/R")
            skipped += 1
            continue

        bs = resolve_sym(m, sym)
        if not bs:
            lines_out.append(f"SKIP: {sym} — symbol resolve")
            skipped += 1
            continue

        meta = {
            "ticker": sym.upper(),
            "tf": tf,
            "strategy": sid,
            "direction": direction,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "confidence": conf,
            "reasoning": "python_prefilter_v6",
            "calendar_action": str(cal.get("action", "CLEAR")),
            "calendar_reason": str(cal.get("reason", "")),
            "regime": str(regime.get("regime", "NORMAL")),
            "regime_size_multiplier": reg_m,
            "regime_wr_10": float(regime.get("wr_10", 0.5) or 0.5),
            "regime_consecutive_losses": int(regime.get("consecutive_losses", 0) or 0),
            "base_risk_usd": round(base_risk, 2),
            "final_risk_usd": round(final_risk, 2),
            "macro_mult": macro_m,
            "trend_mult": trend_m,
            "confluence_mult": cf_m,
            "confluence_count": n_agree,
            "trail_regime": trail_reg,
            "atr_live": atrv,
            "macro_bias": mb0,
            "trend_strength": ts0,
            "macro_rate_diff": rd0,
        }
        meta.update(macro_result_fields(macro))
        if cpu:
            meta["confidence_pre_upgrade"] = cpu

        sig_line = (
            f"SIGNAL: {sym} {tf} {direction} | {sid} | {conf} | {macro_m:.2f}x macro | "
            f"{trend_m:.2f}x trend | {cf_m:.2f}x confluence | risk ${final_risk:.0f}"
        )
        lines_out.append(sig_line)

        res = order_send_live(m, bs, direction, sl, final_risk, meta)
        if not res.get("ok"):
            lines_out.append(f"FAIL: {sym} {tf} {sid} — {res.get('error')}")
            skipped += 1
            continue

        placed += 1
        if sym.upper().endswith("JPY"):
            st["jpy_risk_usd_day"] = float(st.get("jpy_risk_usd_day", 0) or 0) + float(final_risk)
        otk = st.setdefault("last_trade_open", {})
        otk[f"{sym.upper()}_{tf.lower()}"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_live_state(st)
        lines_out.append(
            f"PLACED: ticket#{res.get('ticket')} entry:{res.get('entry'):.5f} sl:{sl:.5f} tp1:{tp1:.5f} "
            f"lots:{res.get('volume')}"
        )
        append_trade_log(f"{now.isoformat()} | {sig_line} | PLACED ticket={res.get('ticket')}")

    manage_trailing_live(m)

    # Status banner (Part 9)
    next_h = _next_scan_hour(now)
    print("═" * 46)
    print(f"APEX v7.3 LIVE — {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Balance:     ${balance:,.2f}")
    print(f"Daily P&L:   {daily_pnl:+,.2f}  (limit -${daily_limit:,.0f})")
    print(f"Open trades: {len(open_apex_positions(m))}")
    print(f"Next scan:   {next_h:02d}:00 UTC")
    print("═" * 46)
    for ln in lines_out:
        print(ln)
    print(f"Scan complete. {placed} placed, {skipped} skipped. Next: {next_h:02d}:00 UTC")
    save_live_state(st)


def _next_scan_hour(now: datetime) -> int:
    h = now.hour
    for x in SCAN_HOURS:
        if x > h:
            return x
    return SCAN_HOURS[0]


def next_scan_datetime_utc(now: datetime | None = None) -> datetime:
    """Earliest future UTC time at minute 0 whose hour is in ``SCAN_HOURS`` (strictly after ``now``)."""
    t = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    cand = t.replace(minute=0, second=0, microsecond=0)
    if cand <= t:
        cand = cand + timedelta(hours=1)
    for _ in range(24 * 14):
        if cand.hour in SCAN_HOURS:
            return cand
        cand = cand + timedelta(hours=1)
    return cand


def emit_startup_diagnostics() -> None:
    """
    Log within a few seconds of process start (does not wait for SCAN_HOURS):
    (1) password env present, (2) MT5 account + balance or failure, (3) next scan UTC.
    """
    pw = (os.environ.get("APEX_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD") or "").strip()
    pw_ok = bool(pw)
    line1 = f"[STARTUP] APEX_MT5_PASSWORD / MT5_PASSWORD: {'set (non-empty)' if pw_ok else 'MISSING — export APEX_MT5_PASSWORD'}"
    print(line1)
    log_msg(line1, "info" if pw_ok else "warning")

    mt5 = ensure_mt5()
    if not mt5:
        line2 = "[STARTUP] MT5: NOT connected (see apex_log.txt for details)"
        print(line2)
        log_msg(line2, "error")
    else:
        try:
            ai = mt5.account_info()
            if ai is None:
                line2 = "[STARTUP] MT5: connected but account_info() returned None"
            else:
                line2 = (
                    f"[STARTUP] MT5: OK | login={int(ai.login)} | server={getattr(ai, 'server', '')!s} "
                    f"| balance={float(ai.balance):,.2f} {ai.currency}"
                )
        except Exception as e:  # noqa: BLE001
            line2 = f"[STARTUP] MT5: connected but account_info failed: {e}"
        print(line2)
        log_msg(line2, "info" if "OK" in line2 else "warning")

    nxt = next_scan_datetime_utc(datetime.now(timezone.utc))
    line3 = f"[STARTUP] Next scan (UTC): {nxt.strftime('%Y-%m-%d %H:%M')} (hours {SCAN_HOURS})"
    print(line3)
    log_msg(line3, "info")


def print_status_quick(mt5: Any) -> None:
    st = load_live_state()
    ai = mt5.account_info()
    if ai is None:
        return
    eq = float(ai.equity)
    if st.get("day_anchor") is not None:
        dp = eq - float(st["day_anchor"])
    else:
        dp = 0.0
    log_msg(f"[STATUS] eq={eq:.2f} daily_pnl={dp:.2f} halted={st.get('halted_dd')}", "info")


def main_loop() -> None:
    st = load_live_state()
    last_slot = str(st.get("last_scan_slot") or "")
    emit_startup_diagnostics()

    while True:
        try:
            mt5 = ensure_mt5()
            if not mt5:
                log_msg("[MT5] reconnect in 30s…", "warning")
                time.sleep(30)
                continue

            now = datetime.now(timezone.utc)
            ran_scan = False
            if now.minute == 0 and now.hour in SCAN_HOURS:
                slot = f"{now:%Y-%m-%d}-{now.hour:02d}"
                if slot != last_slot:
                    last_slot = slot
                    st = load_live_state()
                    st["last_scan_slot"] = slot
                    save_live_state(st)
                    run_full_scan()
                    print_status_quick(mt5)
                    ran_scan = True
            if not ran_scan:
                try:
                    manage_trailing_live(mt5)
                except Exception as te:  # noqa: BLE001
                    log_msg(f"[TRAIL] manage_trailing_live: {te}", "warning")

            time.sleep(60)
        except KeyboardInterrupt:
            print("Stopped by user")
            break
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e} — restarting in 60 seconds")
            log_msg(f"[recover] {e}", "critical")
            time.sleep(60)


if __name__ == "__main__":
    main_loop()
