"""
APEX live MT5 trader — FTMO-style soft limits, Layer 1 (Claude gate) + Layer 2 Python,
v7.1-style filters, trailing, persistence.

Place or symlink at ``C:\\APEX\\apex_trader.py`` or set ``APEX_DATA_DIR`` to your data folder.

**Never put your MT5 password in this file.** Use environment variable ``APEX_MT5_PASSWORD``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from calendar_manager import check_calendar_risk
from macro_manager import (
    apply_macro_confidence_adjustment,
    get_macro_bias,
    macro_result_fields,
    set_backtest_mode,
    weekly_macro_summary_lines,
)
from regime_manager import get_regime_cached

set_backtest_mode(False)

CURRENT_JOB_ID = os.environ.get("APEX_JOB_ID", "live")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    if os.name == "nt":
        default = r"C:\APEX"
    else:
        default = str(Path.home() / "APEX")
    return Path(os.environ.get("APEX_DATA_DIR", default)).resolve()


BASE_DIR = _base_dir()
LOG_FILE = BASE_DIR / "apex_log.txt"
COOLDOWN_FILE = BASE_DIR / "cooldowns.json"
LOSS_FILE = BASE_DIR / "loss_tracking.json"
STATE_FILE = BASE_DIR / "apex_trader_state.json"
TICKET_META_FILE = BASE_DIR / "apex_trader_tickets.json"
STRATEGY_COUNTS_FILE = BASE_DIR / "strategy_trade_counts.json"

MT5_LOGIN = int(os.environ.get("APEX_MT5_LOGIN", "107356886"))
MT5_SERVER = os.environ.get("APEX_MT5_SERVER", "MetaQuotes-Demo")
STARTING_BALANCE = float(os.environ.get("APEX_STARTING_BALANCE", "100000"))

DAILY_SOFT_LIMIT = 4000.0
TOTAL_SOFT_LIMIT = 8000.0
PHASE1_PROFIT = 10000.0

APEX_MAGIC = 107356887
ORDER_COMMENT = "APEX"

CONF_RISK_USD: dict[str, float] = {"HIGH": 1500.0, "MEDIUM": 1000.0, "LOW": 300.0}
TF_RISK_MULT: dict[str, float] = {"1w": 1.0, "1d": 0.85, "4h": 0.70}

# FIX 27 — live MT5: only elite strategies (Railway backtests all 68).
LIVE_ALLOWED_STRATEGIES: frozenset[str] = frozenset(
    {
        "T01_EMA_PULLBACK",
        "R01_EXTREME_ZONE_REVERSION",
        "SMC05_EQUAL_HL_HUNT",
        "M07_VOLUME_SURGE_MOMENTUM",
        "M03_RSI_MOMENTUM_CONTINUATION",
        "T07_MA_RIBBON_ALIGNMENT",
        "SMC01_SR_FLIP",
    }
)


def live_elite_strategy_ok(strategy_id: str, confidence: str) -> tuple[bool, str]:
    sid = (strategy_id or "").strip().upper()
    conf_u = (confidence or "").strip().upper()
    if sid not in LIVE_ALLOWED_STRATEGIES:
        return False, f"LIVE SKIP: {sid} not in elite set"
    if sid == "M03_RSI_MOMENTUM_CONTINUATION" and conf_u != "HIGH":
        return False, "LIVE SKIP: M03 not HIGH confidence"
    return True, ""


EXOTIC_PAIRS = frozenset({"USDMXN", "USDZAR", "USDNOK", "USDSEK"})

TICKERS: tuple[str, ...] = (
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
    "EURGBP", "EURJPY", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    "GBPJPY", "GBPNZD", "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDJPY", "AUDCAD", "AUDNZD", "NZDCAD", "NZDJPY", "CADJPY", "CHFJPY",
    "USDMXN", "USDZAR", "USDNOK", "USDSEK", "QQQ",
)

_MIN_TRAIL_LOCK_USD = 25.0

_log_lock = threading.Lock()
_logger: logging.Logger | None = None


def _ensure_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("apex_trader")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.addHandler(sh)
    _logger = lg
    return lg


def log_msg(msg: str, level: str = "info") -> None:
    lg = _ensure_logger()
    with _log_lock:
        getattr(lg, level.lower(), lg.info)(msg)


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


@dataclass
class CooldownState:
    last_trade: dict[str, str] = field(default_factory=dict)
    traded_day: str = ""
    traded_tickers: list[str] = field(default_factory=list)


@dataclass
class LossState:
    consec: dict[str, int] = field(default_factory=dict)
    last_outcome_date: dict[str, str] = field(default_factory=dict)


def load_cooldown_state() -> CooldownState:
    raw = _load(COOLDOWN_FILE, {})
    st = CooldownState()
    if isinstance(raw, dict):
        st.last_trade = {str(k): str(v) for k, v in (raw.get("last_trade") or {}).items()}
        st.traded_day = str(raw.get("traded_day") or "")
        st.traded_tickers = [str(x).upper() for x in (raw.get("traded_tickers") or [])]
    return st


def save_cooldown_state(st: CooldownState) -> None:
    _save(
        COOLDOWN_FILE,
        {"last_trade": st.last_trade, "traded_day": st.traded_day, "traded_tickers": st.traded_tickers},
    )


def load_loss_state() -> LossState:
    raw = _load(LOSS_FILE, {})
    st = LossState()
    if isinstance(raw, dict):
        st.consec = {str(k): int(v) for k, v in (raw.get("consec") or {}).items()}
        st.last_outcome_date = {str(k): str(v) for k, v in (raw.get("last_outcome_date") or {}).items()}
    return st


def save_loss_state(st: LossState) -> None:
    _save(LOSS_FILE, {"consec": st.consec, "last_outcome_date": st.last_outcome_date})


def load_strategy_counts() -> dict[str, int]:
    raw = _load(STRATEGY_COUNTS_FILE, {})
    return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def save_strategy_counts(c: dict[str, int]) -> None:
    _save(STRATEGY_COUNTS_FILE, c)


def load_ftmo_state() -> dict[str, Any]:
    return _load(
        STATE_FILE,
        {"trading_day_dates": [], "anchor_equity": None, "day_anchor": None, "day_key": ""},
    )


def save_ftmo_state(d: dict[str, Any]) -> None:
    _save(STATE_FILE, d)


def yahoo_ticker(sym: str) -> str:
    s = sym.strip().upper()
    if s == "QQQ":
        return "QQQ"
    if len(s) == 6 and s.isalpha():
        return f"{s}=X"
    return s


def fetch_df(sym: str, interval: str, period: str) -> Any:
    import pandas as pd
    import yfinance as yf

    df = yf.download(yahoo_ticker(sym), period=period, interval=interval, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    if "adj close" in df.columns and "close" not in df.columns:
        df.rename(columns={"adj close": "close"}, inplace=True)
    return df.dropna(subset=["close"])


def ema(s: Any, span: int) -> Any:
    return s.ewm(span=span, adjust=False).mean()


def rsi_series(s: Any, period: int = 14) -> Any:
    d = s.diff()
    g = d.clip(lower=0.0)
    l = (-d.clip(upper=0.0))
    ag = g.ewm(alpha=1.0 / period, adjust=False).mean()
    al = l.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = ag / al.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(h: Any, l: Any, c: Any) -> Any:
    prev = c.shift(1)
    return (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)


def atr_wilder(h: Any, l: Any, c: Any, period: int = 14) -> Any:
    return true_range(h, l, c).ewm(alpha=1.0 / period, adjust=False).mean()


def adx_wilder(h: Any, l: Any, c: Any, period: int = 14) -> Any:
    import numpy as np
    import pandas as pd

    up = h.diff()
    dn = -l.diff()
    pdm = ((up > dn) & (up > 0)) * up
    mdm = ((dn > up) & (dn > 0)) * dn
    tr = true_range(h, l, c)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    pdi = 100.0 * (pdm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan))
    mdi = 100.0 * (mdm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = (100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).fillna(0.0)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


def zone_pct_52w(c: Any, h: Any, l: Any, lb: int) -> float:
    wh = h.rolling(lb, min_periods=max(20, lb // 10)).max()
    wl = l.rolling(lb, min_periods=max(20, lb // 10)).min()
    hi, lo, cl = float(wh.iloc[-1]), float(wl.iloc[-1]), float(c.iloc[-1])
    if not math.isfinite(hi) or not math.isfinite(lo) or hi <= lo:
        return 50.0
    return float(max(0.0, min(100.0, (cl - lo) / (hi - lo) * 100.0)))


def build_indicators(sym: str, interval: str) -> dict[str, Any] | None:
    try:
        period = "730d" if interval == "1wk" else "400d" if interval == "1d" else "120d"
        df = fetch_df(sym, interval, period)
        if df.empty or len(df) < 60:
            return None
        c = df["close"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
        r = rsi_series(c, 14)
        atrv = atr_wilder(h, l, c, 14)
        adxv = adx_wilder(h, l, c, 14)
        zp = zone_pct_52w(c, h, l, 260 if interval == "1wk" else 252)
        return {
            "close": float(c.iloc[-1]),
            "ema20": float(e20.iloc[-1]),
            "ema50": float(e50.iloc[-1]),
            "ema200": float(e200.iloc[-1]),
            "rsi": float(r.iloc[-1]),
            "rsi_prev": float(r.iloc[-2]) if len(r) > 1 else float(r.iloc[-1]),
            "atr": float(atrv.iloc[-1]),
            "adx": float(adxv.iloc[-1]),
            "zone_pct": float(zp),
            "_df": df,
        }
    except Exception as e:  # noqa: BLE001
        log_msg(f"[yf] {sym} {interval}: {e}", "warning")
        return None


def rr_ok(entry: float, sl: float, tp1: float) -> bool:
    risk = abs(entry - sl)
    return risk > 0 and abs(tp1 - entry) / risk >= 2.0 - 1e-9


def layer1_t01(d: str, ind: dict[str, Any], zp: float) -> bool:
    e20, e50, e200 = ind["ema20"], ind["ema50"], ind["ema200"]
    cl, atrv = ind["close"], ind["atr"]
    dist = abs(cl - e20)
    if d == "LONG":
        return e20 > e50 > e200 and zp < 60 and dist <= 2.5 * atrv
    return e20 < e50 < e200 and zp > 40 and dist <= 2.5 * atrv


def layer1_t01_sl_tp(d: str, entry: float, atrv: float) -> tuple[float, float, float]:
    if d == "LONG":
        return entry - 1.5 * atrv, entry + 3.0 * atrv, entry + 4.5 * atrv
    return entry + 1.5 * atrv, entry - 3.0 * atrv, entry - 4.5 * atrv


def layer1_r01(d: str, zp: float, rsi_v: float, adx_v: float) -> bool:
    if d == "LONG":
        return zp <= 20 and rsi_v <= 40 and adx_v <= 45
    return zp >= 80 and rsi_v >= 60 and adx_v <= 45


def layer1_r01_sl_tp(d: str, entry: float, atrv: float) -> tuple[float, float, float]:
    if d == "LONG":
        return entry - 2.0 * atrv, entry + 4.0 * atrv, entry + 6.0 * atrv
    return entry + 2.0 * atrv, entry - 4.0 * atrv, entry - 6.0 * atrv


def layer2_t02(df: Any) -> str | None:
    if len(df) < 4:
        return None
    e20 = ema(df["close"].astype(float), 20)
    e50 = ema(df["close"].astype(float), 50)
    for k in (-1, -2, -3):
        if float(e20.iloc[k - 1]) <= float(e50.iloc[k - 1]) and float(e20.iloc[k]) > float(e50.iloc[k]):
            return "LONG"
        if float(e20.iloc[k - 1]) >= float(e50.iloc[k - 1]) and float(e20.iloc[k]) < float(e50.iloc[k]):
            return "SHORT"
    return None


def layer2_t04(ind: dict[str, Any]) -> str | None:
    if ind["adx"] <= 25:
        return None
    c, e20, e50, e200 = ind["close"], ind["ema20"], ind["ema50"], ind["ema200"]
    if c > e200 and e20 > e50:
        return "LONG"
    if c < e200 and e20 < e50:
        return "SHORT"
    return None


def layer2_m03(ind: dict[str, Any]) -> str | None:
    r0, r1 = ind["rsi"], ind["rsi_prev"]
    if r0 > 60 and r0 > r1:
        return "LONG"
    if r0 < 40 and r0 < r1:
        return "SHORT"
    return None


def layer2_smc01(df: Any) -> str | None:
    if len(df) < 25:
        return None
    low = df["low"].astype(float)
    high = df["high"].astype(float)
    close = df["close"].astype(float)
    wl = low.iloc[-20:-3].min()
    wh = high.iloc[-20:-3].max()
    if low.iloc[-4] <= wl * 0.9995 and close.iloc[-1] > wl * 1.001:
        return "LONG"
    if high.iloc[-4] >= wh * 1.0005 and close.iloc[-1] < wh * 0.999:
        return "SHORT"
    return None


def layer2_sl_tp(entry: float, atrv: float, d: str) -> tuple[float, float, float]:
    if d == "LONG":
        return entry - 1.5 * atrv, entry + 3.5 * atrv, entry + 5.5 * atrv
    return entry + 1.5 * atrv, entry - 3.5 * atrv, entry - 5.5 * atrv


def ema200_ok(d: str, ind: dict[str, Any]) -> bool:
    c, e200 = ind["close"], ind["ema200"]
    return (c >= e200) if d == "LONG" else (c <= e200)


def gbpusd_short_block(sym: str, tf: str, d: str, zp: float) -> bool:
    return sym == "GBPUSD" and tf == "1d" and d == "SHORT" and zp < 80


def nzdusd_w1_short_block(sym: str, tf: str, d: str, zp: float) -> bool:
    return sym == "NZDUSD" and tf == "1w" and d == "SHORT" and zp < 35


def get_currencies(ticker: str) -> list[str]:
    t = (ticker or "").strip().upper()
    return [t[:3], t[3:]] if len(t) == 6 and t.isalpha() else []


def cooldown_ok(
    cd: CooldownState, loss: LossState, sym: str, tf: str, d: str, today: date
) -> tuple[bool, str]:
    su = sym.upper()
    if cd.traded_day == today.isoformat() and su in cd.traded_tickers:
        return False, "same_day_dedup"
    key = f"{su}_{tf.lower()}"
    last = cd.last_trade.get(key)
    if last:
        try:
            d0 = datetime.strptime(last[:10], "%Y-%m-%d").date()
            if tf == "1w" and (today - d0).days < 7:
                return False, "weekly_cooldown_7d"
            if tf == "1d" and (today - d0).days < 2:
                return False, "daily_cooldown_2d"
        except ValueError:
            pass
    lk = f"{su}_{tf.lower()}_{d.upper()}"
    if loss.consec.get(lk, 0) >= 3:
        ld = loss.last_outcome_date.get(lk)
        if ld:
            try:
                d0 = datetime.strptime(ld[:10], "%Y-%m-%d").date()
                if (today - d0).days < 21:
                    return False, "extended_loss_21d"
            except ValueError:
                pass
    return True, ""


def currency_cap_ok(sym: str, counts: dict[str, int]) -> tuple[bool, str]:
    su = sym.upper()
    for ccy in get_currencies(su):
        if su in EXOTIC_PAIRS and ccy == "USD":
            continue
        if counts.get(ccy, 0) >= 2:
            return False, f"currency_cap_{ccy}"
    return True, ""


def open_ccy_counts(mt5: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in mt5.positions_get() or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_MAGIC:
            continue
        raw = str(p.symbol).replace(".", "").upper()
        base = raw[:6] if len(raw) >= 6 and raw[:6].isalpha() else raw
        for ccy in get_currencies(base):
            if base[:6] in EXOTIC_PAIRS and ccy == "USD":
                continue
            out[ccy] = out.get(ccy, 0) + 1
    return out


def claude_gate(sym: str, tf: str, d: str, ind: dict[str, Any], zp: float) -> tuple[str, str, str]:
    import json as _json

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return "EXECUTE", "HIGH", "no_api_key_fallback"
    prompt = f"""You are APEX, an expert forex trader. Evaluate this trade setup:
Ticker: {sym}
Timeframe: {tf}
Direction: {d}
Current price: {ind['close']:.5f}
EMA20: {ind['ema20']:.5f} EMA50: {ind['ema50']:.5f} EMA200: {ind['ema200']:.5f}
Zone position: {zp:.1f}% (0=52-week low, 100=52-week high)
RSI: {ind['rsi']:.2f}
ATR: {ind['atr']:.6f}
Respond in JSON only:
{{
"verdict": "EXECUTE" or "SKIP",
"confidence": "HIGH" or "MEDIUM" or "LOW",
"reasoning": "one sentence"
}}"""
    try:
        import anthropic

        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}]
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not m:
            return "EXECUTE", "HIGH", "parse_fail_fallback"
        data = _json.loads(m.group(0))
        v = str(data.get("verdict", "SKIP")).upper()
        c = str(data.get("confidence", "MEDIUM")).upper()
        r = str(data.get("reasoning", "")).strip()
        if v not in ("EXECUTE", "SKIP"):
            v = "EXECUTE"
        if c not in ("HIGH", "MEDIUM", "LOW"):
            c = "HIGH"
        return v, c, r
    except Exception as e:  # noqa: BLE001
        log_msg(f"[Claude] {e} — fallback", "warning")
        return "EXECUTE", "HIGH", f"api_error:{e}"


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


def mpl_sl(mt5: Any, sym: str, entry: float, sl: float) -> float | None:
    info = mt5.symbol_info(sym)
    if info is None:
        return None
    ts, tv = float(info.trade_tick_size or info.point or 0), float(info.trade_tick_value or 0)
    if ts <= 0 or tv <= 0:
        return None
    return abs(entry - sl) / ts * tv


def norm_vol(mt5: Any, sym: str, v: float) -> float:
    info = mt5.symbol_info(sym)
    if info is None:
        return round(v, 2)
    step = float(info.volume_step or 0.01) or 0.01
    vmin, vmax = float(info.volume_min or 0.01), float(info.volume_max or 100.0)
    steps = math.floor(v / step + 1e-9)
    v2 = max(vmin, min(vmax, steps * step))
    return round(v2, int(max(0, -math.floor(math.log10(step)))))


def pnl_snapshot(mt5: Any) -> tuple[float, float, float, float]:
    st = load_ftmo_state()
    ai = mt5.account_info()
    if ai is None:
        return 0.0, 0.0, 0.0, 0.0
    eq = float(ai.equity)
    anchor = float(st["anchor_equity"]) if st.get("anchor_equity") is not None else STARTING_BALANCE
    dk = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day_key") != dk or st.get("day_anchor") is None:
        st["day_key"] = dk
        st["day_anchor"] = eq
        save_ftmo_state(st)
    da = float(st.get("day_anchor", eq))
    return eq, eq - da, eq - anchor, float(ai.balance)


def _regime_with_ftmo_override(reg: dict[str, Any], mt5: Any) -> dict[str, Any]:
    """Force CRISIS sizing when within $500 of FTMO soft stops (Prompt 2)."""
    rd = dict(reg)
    if not mt5:
        return rd
    _eq, daily, total, _bal = pnl_snapshot(mt5)
    if daily < -3500.0 or total < -7500.0:
        log_msg(
            "[REGIME OVERRIDE] FTMO limit approaching — forced CRISIS 25% size",
            "warning",
        )
        rd["regime"] = "CRISIS_FTMO"
        rd["size_multiplier"] = 0.25
        rd["note"] = "FTMO soft limit within $500 — forced CRISIS 25% size"
    return rd


def limits_ok(mt5: Any) -> tuple[bool, str]:
    eq, daily, total, _ = pnl_snapshot(mt5)
    if total <= -TOTAL_SOFT_LIMIT:
        return False, "total_loss_gate"
    if daily <= -DAILY_SOFT_LIMIT:
        return False, "daily_loss_gate"
    if total >= PHASE1_PROFIT:
        msg = f"Phase 1 complete — total PnL {total:.2f} >= ${PHASE1_PROFIT:,.0f}"
        print(msg)
        log_msg(msg, "info")
    return True, ""


def ticket_meta_load() -> dict[str, Any]:
    return _load(TICKET_META_FILE, {})


def ticket_meta_save(d: dict[str, Any]) -> None:
    _save(TICKET_META_FILE, d)


def order_send(
    mt5: Any, sym: str, d: str, sl: float, risk_usd: float, meta: dict[str, Any]
) -> dict[str, Any]:
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return {"ok": False, "error": "no_tick"}
    entry = float(tick.ask if d == "LONG" else tick.bid)
    mpl = mpl_sl(mt5, sym, entry, sl)
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
    time.sleep(0.2)
    ticket = None
    for p in mt5.positions_get(symbol=sym) or []:
        if int(getattr(p, "magic", 0) or 0) == APEX_MAGIC:
            ticket = int(p.ticket)
            break
    meta = dict(meta)
    meta["ticket"] = ticket
    meta["entry_fill"] = entry
    meta["r"] = abs(entry - float(sl))
    tm = ticket_meta_load()
    if ticket:
        tm[str(ticket)] = {k: v for k, v in meta.items()}
        ticket_meta_save(tm)
    return {"ok": True, "ticket": ticket, "volume": vol, "entry": entry}


def select_layer2(sym: str, tf: str, ind: dict[str, Any], df: Any) -> tuple[str | None, str | None]:
    counts = load_strategy_counts()
    cands: list[tuple[str, Callable[[], str | None]]] = [
        ("T02_EMA_CROSSOVER", lambda: layer2_t02(df)),
        ("T04_ADX_TREND_ENTRY", lambda: layer2_t04(ind)),
        ("M03_RSI_MOMENTUM_CONTINUATION", lambda: layer2_m03(ind)),
        ("SMC01_SR_FLIP", lambda: layer2_smc01(df)),
    ]

    def sk(it: tuple[str, Callable[[], str | None]]) -> tuple[int, int, str]:
        sid = it[0]
        return (0 if counts.get(sid, 0) == 0 else 1, counts.get(sid, 0), sid)

    for sid, fn in sorted(cands, key=sk):
        if tf == "1w" and sid == "T04_ADX_TREND_ENTRY":
            continue
        dr = fn()
        if dr and ema200_ok(dr, ind):
            return sid, dr
    return None, None


def scan_one(mt5: Any, sym: str, tf: str, cd: CooldownState, loss: LossState, regime: dict[str, Any]) -> None:
    today = datetime.now(timezone.utc).date()
    if cd.traded_day != today.isoformat():
        cd.traded_day = today.isoformat()
        cd.traded_tickers = []
        save_cooldown_state(cd)

    interval = {"1w": "1wk", "1d": "1d", "4h": "4h"}[tf]
    ind = build_indicators(sym, interval)
    if ind is None:
        log_msg(f"[SKIP] {sym} {tf}: yfinance", "info")
        return
    df = ind.pop("_df")
    zp = float(ind["zone_pct"])

    if cd.traded_day == today.isoformat() and sym.upper() in cd.traded_tickers:
        log_msg(f"[SKIP] {sym} {tf}: same_day_dedup", "info")
        return

    if not limits_ok(mt5)[0]:
        log_msg(f"[SKIP] {sym} {tf}: {limits_ok(mt5)[1]}", "warning")
        return

    cc = open_ccy_counts(mt5)
    if not currency_cap_ok(sym, cc)[0]:
        log_msg(f"[SKIP] {sym} {tf}: {currency_cap_ok(sym, cc)[1]}", "info")
        return

    now_utc = datetime.now(timezone.utc)
    cal_risk = check_calendar_risk(sym, now_utc)
    if cal_risk.get("action") == "BLOCK":
        log_msg(f"[CALENDAR BLOCK] {sym} — {cal_risk.get('reason', '')}", "info")
        return
    if cal_risk.get("action") in ("REDUCE", "WATCH"):
        log_msg(f"[CALENDAR {cal_risk.get('action')}] {sym} — {cal_risk.get('reason', '')}", "info")

    # Layer 1
    for strat, dire in (
        ("T01_EMA_PULLBACK", "LONG"),
        ("T01_EMA_PULLBACK", "SHORT"),
        ("R01_EXTREME_ZONE_REVERSION", "LONG"),
        ("R01_EXTREME_ZONE_REVERSION", "SHORT"),
    ):
        if strat.startswith("T01") and not layer1_t01(dire, ind, zp):
            continue
        if strat.startswith("R01") and not layer1_r01(dire, zp, ind["rsi"], ind["adx"]):
            continue
        if nzdusd_w1_short_block(sym, tf, dire, zp) or gbpusd_short_block(sym, tf, dire, zp):
            log_msg(f"[SKIP] {sym} {tf} {strat} {dire}: zone_rule", "info")
            continue
        ok2, r2 = cooldown_ok(cd, loss, sym, tf, dire, today)
        if not ok2:
            log_msg(f"[SKIP] {sym} {tf} {dire}: {r2}", "info")
            continue
        if strat.startswith("T01"):
            sl, tp1, tp2 = layer1_t01_sl_tp(dire, ind["close"], ind["atr"])
        else:
            sl, tp1, tp2 = layer1_r01_sl_tp(dire, ind["close"], ind["atr"])
        ent = float(ind["close"])
        if not rr_ok(ent, sl, tp1):
            log_msg(f"[SKIP] {sym} {tf} {strat}: rr", "info")
            continue
        v, c_cl, rs = claude_gate(sym, tf, dire, ind, zp)
        log_msg(f"[Claude] {sym} {tf} {strat} {dire} {v} {c_cl} {rs}")
        if v != "EXECUTE":
            continue
        conf = c_cl if c_cl in CONF_RISK_USD else "HIGH"
        macro = get_macro_bias(sym, dire)
        log_msg(
            f"[MACRO] {sym} {dire} — {macro['bias']} (score {macro['composite_score']:+.2f}) "
            f"rate_diff={macro['rate_differential']:+.2f}% {macro['price_trend']}",
            "info",
        )
        conf_pre = conf
        conf = apply_macro_confidence_adjustment(conf, macro)
        ok_elite, elite_rs = live_elite_strategy_ok(strat, conf)
        if not ok_elite:
            log_msg(elite_rs, "info")
            continue
        risk = CONF_RISK_USD[conf] * TF_RISK_MULT.get(tf, 1.0)
        cpu = conf_pre if conf_pre != conf and conf_pre in ("HIGH", "MEDIUM", "LOW") else None
        exec_trade(
            mt5,
            sym,
            tf,
            strat,
            dire,
            sl,
            tp1,
            tp2,
            risk,
            cd,
            rs,
            conf,
            cal_risk,
            regime,
            macro,
            confidence_pre_upgrade=cpu,
        )
        return

    # Layer 2
    sid, dire = select_layer2(sym, tf, ind, df)
    if not sid:
        log_msg(f"[SKIP] {sym} {tf}: no_layer2", "info")
        return
    if not ema200_ok(dire, ind) or gbpusd_short_block(sym, tf, dire, zp) or nzdusd_w1_short_block(sym, tf, dire, zp):
        log_msg(f"[SKIP] {sym} {tf} {sid}: filter", "info")
        return
    ok3, r3 = cooldown_ok(cd, loss, sym, tf, dire, today)
    if not ok3:
        log_msg(f"[SKIP] {sym} {tf} {sid}: {r3}", "info")
        return
    sl, tp1, tp2 = layer2_sl_tp(float(ind["close"]), ind["atr"], dire)
    ent = float(ind["close"])
    if not rr_ok(ent, sl, tp1):
        log_msg(f"[SKIP] {sym} {tf} {sid}: rr", "info")
        return
    macro = get_macro_bias(sym, dire)
    log_msg(
        f"[MACRO] {sym} {dire} — {macro['bias']} (score {macro['composite_score']:+.2f}) "
        f"rate_diff={macro['rate_differential']:+.2f}% {macro['price_trend']}",
        "info",
    )
    conf_pre = "LOW"
    conf = apply_macro_confidence_adjustment(conf_pre, macro)
    ok_elite, elite_rs = live_elite_strategy_ok(sid, conf)
    if not ok_elite:
        log_msg(elite_rs, "info")
        return
    risk = CONF_RISK_USD[conf] * TF_RISK_MULT.get(tf, 1.0)
    cpu = conf_pre if conf_pre != conf else None
    exec_trade(
        mt5,
        sym,
        tf,
        sid,
        dire,
        sl,
        tp1,
        tp2,
        risk,
        cd,
        "layer2",
        conf,
        cal_risk,
        regime,
        macro,
        confidence_pre_upgrade=cpu,
    )


def exec_trade(
    mt5: Any,
    sym: str,
    tf: str,
    strat: str,
    dire: str,
    sl: float,
    tp1: float,
    tp2: float,
    risk: float,
    cd: CooldownState,
    reasoning: str,
    conf: str,
    calendar_risk: dict[str, Any] | None = None,
    regime_risk: dict[str, Any] | None = None,
    macro_bias: dict[str, Any] | None = None,
    confidence_pre_upgrade: str | None = None,
) -> None:
    if not limits_ok(mt5)[0]:
        return
    if not currency_cap_ok(sym, open_ccy_counts(mt5))[0]:
        return
    bs = resolve_sym(mt5, sym)
    if not bs:
        return
    cr = calendar_risk or {
        "action": "CLEAR",
        "reason": "No high-impact events within 48h",
        "size_multiplier": 1.0,
    }
    rg = regime_risk or {
        "regime": "NORMAL",
        "size_multiplier": 1.0,
        "wr_10": 0.5,
        "wr_20": 0.5,
        "consecutive_losses": 0,
    }
    cal_m = float(cr.get("size_multiplier", 1.0) or 0.0)
    reg_m = float(rg.get("size_multiplier", 1.0) or 0.0)
    mb = macro_bias if isinstance(macro_bias, dict) and macro_bias else {
        "bias": "NEUTRAL",
        "composite_score": 0.0,
        "rate_differential": 0.0,
        "price_trend": "RANGING",
        "sentiment_score": 0.0,
        "confidence_upgrade": 0,
        "size_multiplier": 1.0,
    }
    mm = float(mb.get("size_multiplier", 1.0) or 0.0)
    raw_risk = float(risk) * max(0.0, cal_m) * max(0.0, reg_m) * max(0.0, mm)
    max_allowed = float(CONF_RISK_USD["HIGH"]) * 1.5
    risk_eff = max(25.0, min(raw_risk, max_allowed))
    meta = {
        "ticker": sym.upper(),
        "tf": tf,
        "strategy": strat,
        "direction": dire,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "confidence": conf,
        "reasoning": reasoning,
        "calendar_action": str(cr.get("action", "CLEAR")),
        "calendar_reason": str(cr.get("reason", "")),
        "regime": str(rg.get("regime", "NORMAL")),
        "regime_size_multiplier": reg_m,
        "regime_wr_10": float(rg.get("wr_10", 0.5) or 0.5),
        "regime_consecutive_losses": int(rg.get("consecutive_losses", 0) or 0),
    }
    meta.update(macro_result_fields(mb))
    if confidence_pre_upgrade is not None:
        pre_u = str(confidence_pre_upgrade).strip().upper()
        if pre_u in ("HIGH", "MEDIUM", "LOW") and pre_u != str(conf).strip().upper():
            meta["confidence_pre_upgrade"] = pre_u
    res = order_send(mt5, bs, dire, sl, risk_eff, meta)
    if not res.get("ok"):
        log_msg(f"[FAIL] {sym} {tf} {strat}: {res}", "error")
        return
    cnt = load_strategy_counts()
    cnt[strat] = cnt.get(strat, 0) + 1
    save_strategy_counts(cnt)
    cd.traded_tickers.append(sym.upper())
    cd.last_trade[f"{sym.upper()}_{tf.lower()}"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_cooldown_state(cd)
    log_msg(
        f"[TRADE] {sym} {dire} {tf} {strat} entry={res.get('entry')} SL={sl:.5f} TP1={tp1:.5f} "
        f"lot={res.get('volume')} risk_usd={risk_eff:.2f} (base={risk:.2f}) claude={reasoning!r} "
        f"regime={rg.get('regime')}",
        "info",
    )


def pnl_at_sl(entry: float, nsl: float, d: str, vol: float, mt5: Any, sym: str) -> float:
    info = mt5.symbol_info(sym)
    if info is None:
        return 0.0
    ts, tv = float(info.trade_tick_size or info.point or 0), float(info.trade_tick_value or 0)
    if ts <= 0 or tv <= 0:
        return 0.0
    ticks = (nsl - entry) / ts if d == "LONG" else (entry - nsl) / ts
    return ticks * tv * vol


def manage_trailing(mt5: Any) -> None:
    """TP1 @ entry±1.0R → SL at entry±0.75R; TP2 @ entry±1.5R → SL at entry±1.5R; $25 min locked P&L."""
    if not mt5.terminal_info():
        return
    meta = ticket_meta_load()
    for pos in mt5.positions_get() or []:
        if int(getattr(pos, "magic", 0) or 0) != APEX_MAGIC or (pos.comment or "").strip() != ORDER_COMMENT:
            continue
        k = str(int(pos.ticket))
        m = meta.get(k)
        if not isinstance(m, dict):
            continue
        d = str(m["direction"]).upper()
        entry = float(m.get("entry_fill", pos.price_open))
        r = float(m.get("r") or abs(entry - float(pos.sl or entry)))
        if r <= 0:
            continue
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            continue
        bid, ask, vol = float(tick.bid), float(tick.ask), float(pos.volume)
        cur_sl = float(pos.sl or 0.0)
        hit1 = bool(m.get("hit_tp1"))
        hit2 = bool(m.get("hit_tp2"))
        nsl: float | None = None
        tag = ""

        if d == "LONG":
            if not hit1 and bid >= entry + 1.0 * r:
                cand = entry + 0.75 * r
                if pnl_at_sl(entry, cand, d, vol, mt5, pos.symbol) >= _MIN_TRAIL_LOCK_USD and cand > cur_sl:
                    nsl, tag, m["hit_tp1"] = cand, "tp1_lock_0.75R", True
            elif hit1 and not hit2 and bid >= entry + 1.5 * r:
                cand = entry + 1.5 * r
                if pnl_at_sl(entry, cand, d, vol, mt5, pos.symbol) >= _MIN_TRAIL_LOCK_USD and cand > cur_sl:
                    nsl, tag, m["hit_tp2"] = cand, "tp2_lock_1.5R", True
        else:
            if not hit1 and ask <= entry - 1.0 * r:
                cand = entry - 0.75 * r
                if pnl_at_sl(entry, cand, d, vol, mt5, pos.symbol) >= _MIN_TRAIL_LOCK_USD and (cur_sl <= 0 or cand < cur_sl):
                    nsl, tag, m["hit_tp1"] = cand, "tp1_lock_0.75R", True
            elif hit1 and not hit2 and ask <= entry - 1.5 * r:
                cand = entry - 1.5 * r
                if pnl_at_sl(entry, cand, d, vol, mt5, pos.symbol) >= _MIN_TRAIL_LOCK_USD and (cur_sl <= 0 or cand < cur_sl):
                    nsl, tag, m["hit_tp2"] = cand, "tp2_lock_1.5R", True

        if nsl is None:
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
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            log_msg(f"[TRAIL] ticket={pos.ticket} {tag} new_sl={nsl:.5f}", "info")
        meta[k] = m
    ticket_meta_save(meta)


def update_loss_from_deals(mt5: Any) -> None:
    try:
        import MetaTrader5 as mt5m
        from collections import defaultdict

        t0 = datetime.now(timezone.utc) - timedelta(days=60)
        deals = mt5.history_deals_get(t0.replace(tzinfo=None), datetime.utcnow())
        if not deals:
            return
        profit_by_pos: dict[int, float] = defaultdict(float)
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != APEX_MAGIC:
                continue
            pid = int(getattr(d, "position_id", 0) or 0)
            if pid and d.entry == mt5m.DEAL_ENTRY_OUT:
                profit_by_pos[pid] += float(d.profit)
        if not profit_by_pos:
            return
        loss = load_loss_state()
        meta = ticket_meta_load()
        ch = False
        for pid, prof in profit_by_pos.items():
            m = meta.pop(str(pid), None)
            if not isinstance(m, dict):
                continue
            sym = str(m.get("ticker", "")).upper()
            tf = str(m.get("tf", "")).lower()
            dr = str(m.get("direction", "")).upper()
            lk = f"{sym}_{tf}_{dr}"
            ds = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            loss.consec[lk] = loss.consec.get(lk, 0) + 1 if prof < 0 else 0
            loss.last_outcome_date[lk] = ds
            ch = True
        if ch:
            save_loss_state(loss)
            ticket_meta_save(meta)
    except Exception as e:  # noqa: BLE001
        log_msg(f"[loss] {e}", "warning")


def print_status(mt5: Any) -> None:
    eq, daily, total, bal = pnl_snapshot(mt5)
    print(
        f"[STATUS] balance={bal:.2f} equity={eq:.2f} daily_pnl={daily:.2f} total_pnl={total:.2f}\n"
        f"  headroom daily_soft: {DAILY_SOFT_LIMIT + daily:.2f}  total_soft: {TOTAL_SOFT_LIMIT + total:.2f}\n"
        f"  to profit target: {PHASE1_PROFIT - total:.2f}\n"
    )
    st = load_ftmo_state()
    days = len(st.get("trading_day_dates") or [])
    print(f"  trading_days_logged: {days}")
    log_msg(f"[STATUS] bal={bal} eq={eq} daily={daily} total={total} days={days}", "info")
    for p in mt5.positions_get() or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_MAGIC:
            continue
        tk = mt5.symbol_info_tick(p.symbol)
        px = float(tk.bid) if tk else float(p.price_current)
        print(f"  OPEN {p.symbol} ticket={p.ticket} open={p.price_open:.5f} now={px:.5f} pnl={p.profit:.2f} sl={p.sl}")


_mt5: dict[str, Any] = {"m": None}


def ensure_mt5() -> Any | None:
    m = _mt5.get("m")
    if m is not None and m.terminal_info():
        return m
    try:
        if m is not None:
            m.shutdown()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1)
    nm = mt5_connect()
    _mt5["m"] = nm
    return nm


def daily_job() -> None:
    if datetime.now(timezone.utc).weekday() >= 5:
        return
    try:
        mt5 = ensure_mt5()
        if not mt5:
            return
        cd, loss = load_cooldown_state(), load_loss_state()
        st = load_ftmo_state()
        t = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        td = list(st.get("trading_day_dates") or [])
        if t not in td:
            td.append(t)
            st["trading_day_dates"] = td
            save_ftmo_state(st)
        base_reg = get_regime_cached(CURRENT_JOB_ID)
        reg = _regime_with_ftmo_override(base_reg, mt5)
        log_msg(
            f"[REGIME] {reg['regime']} — WR10={reg['wr_10']:.0%} WR20={reg['wr_20']:.0%} "
            f"Streak={reg['consecutive_losses']} losses Size={reg['size_multiplier']}x",
            "info",
        )
        for sym in TICKERS:
            try:
                for tf in ("1d", "4h"):
                    scan_one(mt5, sym, tf, cd, loss, reg)
            except Exception as e:  # noqa: BLE001
                log_msg(f"[scan] {sym}: {e}", "error")
    except Exception as e:  # noqa: BLE001
        log_msg(f"[daily_job] {e}", "error")


def weekly_job() -> None:
    try:
        if datetime.now(timezone.utc).weekday() == 0:
            log_msg("[WEEKLY MACRO SUMMARY]", "info")
            for ln in weekly_macro_summary_lines(TICKERS):
                log_msg(ln, "info")
        mt5 = ensure_mt5()
        if not mt5:
            return
        cd, loss = load_cooldown_state(), load_loss_state()
        base_reg = get_regime_cached(CURRENT_JOB_ID)
        reg = _regime_with_ftmo_override(base_reg, mt5)
        log_msg(
            f"[REGIME] {reg['regime']} — WR10={reg['wr_10']:.0%} WR20={reg['wr_20']:.0%} "
            f"Streak={reg['consecutive_losses']} losses Size={reg['size_multiplier']}x",
            "info",
        )
        for sym in TICKERS:
            try:
                scan_one(mt5, sym, "1w", cd, loss, reg)
            except Exception as e:  # noqa: BLE001
                log_msg(f"[w] {sym}: {e}", "error")
    except Exception as e:  # noqa: BLE001
        log_msg(f"[weekly_job] {e}", "error")


def hourly_job() -> None:
    try:
        mt5 = ensure_mt5()
        if mt5:
            print_status(mt5)
            update_loss_from_deals(mt5)
            rb = get_regime_cached(CURRENT_JOB_ID)
            reg = _regime_with_ftmo_override(rb, mt5)
            log_msg(
                f"REGIME: {reg['regime']} | Size: {reg['size_multiplier']}x | "
                f"WR(10): {reg['wr_10']:.0%} | WR(20): {reg['wr_20']:.0%} | "
                f"Streak: {reg['consecutive_losses']} consecutive losses",
                "info",
            )
    except Exception as e:  # noqa: BLE001
        log_msg(f"[hourly] {e}", "error")


def trail_job() -> None:
    try:
        mt5 = ensure_mt5()
        if mt5:
            manage_trailing(mt5)
    except Exception as e:  # noqa: BLE001
        log_msg(f"[trail] {e}", "error")


def reconnect_job() -> None:
    ensure_mt5()


def main() -> None:
    log_msg("[APEX] apex_trader — set APEX_MT5_PASSWORD and ANTHROPIC_API_KEY", "info")
    mt5 = ensure_mt5()
    if mt5:
        ai = mt5.account_info()
        st = load_ftmo_state()
        if ai and st.get("anchor_equity") is None:
            st["anchor_equity"] = float(ai.equity)
            save_ftmo_state(st)

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sch = BlockingScheduler(timezone="UTC")
    sch.add_job(hourly_job, CronTrigger(minute=0), id="hourly")
    sch.add_job(trail_job, CronTrigger(minute="*/15"), id="trail")
    sch.add_job(reconnect_job, CronTrigger(minute="*/1"), id="ping")
    sch.add_job(daily_job, CronTrigger(day_of_week="mon-fri", hour=13, minute=0), id="daily")
    sch.add_job(weekly_job, CronTrigger(day_of_week="mon", hour=0, minute=0), id="weekly")
    threading.Timer(3.0, hourly_job).start()
    sch.start()


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:  # noqa: BLE001
            log_msg(f"[recover] {e}", "critical")
            time.sleep(60)
