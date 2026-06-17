"""
APEX v7.6 Live Trader — mirrors backtest v7.6 decision logic without ``continuous_backtester``.

- Decision stack in ``apex_v76_decision_logic.py`` (no ``pandas_ta`` / numba / llvmlite).
- MT5 execution via ``apex_trader.py`` only (OHLC, indicators, orders, trailing).
- Allowed third-party: MetaTrader5, pandas, numpy, yfinance, requests, anthropic (via managers).

Deploy: copy ``apex_trader_v76.py``, ``apex_v76_decision_logic.py``, ``apex_trader.py``,
``macro_manager.py``, ``prefilter_v6.py``, etc. into ``C:\\Apex``. Set ``APEX_MT5_PASSWORD``.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


def _bootstrap_apex_sys_path() -> Path:
    """
    Ensure ``C:\\Apex`` (or this script's directory) is on ``sys.path`` before any local imports.
    Fixes ModuleNotFoundError when the process cwd is not the Apex install folder (common on VPS).
    """
    here = Path(__file__).resolve().parent
    candidates: list[Path] = [here]
    for env_key in ("APEX_HOME", "APEX_ROOT", "APEX_DATA_DIR"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            candidates.append(Path(raw).expanduser().resolve())
    if os.name == "nt":
        candidates.append(Path(r"C:\Apex"))
    seen: set[str] = set()
    for root in candidates:
        try:
            root = root.resolve()
        except OSError:
            continue
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        if root.is_dir():
            root_s = str(root)
            if root_s not in sys.path:
                sys.path.insert(0, root_s)
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return here


def _load_module_from_file(name: str, path: Path) -> ModuleType:
    """Load ``name`` from an explicit ``.py`` path (VPS layout)."""
    path = path.resolve()
    if not path.is_file():
        raise ModuleNotFoundError(f"{name} not found at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _all_apex_roots() -> list[Path]:
    """Every directory that may hold flat Apex ``*.py`` modules (VPS layout)."""
    roots: list[Path] = []
    here = Path(__file__).resolve().parent
    for r in (here, Path.cwd()):
        try:
            roots.append(r.resolve())
        except OSError:
            pass
    for env_key in ("APEX_HOME", "APEX_ROOT", "APEX_DATA_DIR"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            try:
                roots.append(Path(raw).expanduser().resolve())
            except OSError:
                pass
    if os.name == "nt":
        roots.append(Path(r"C:\Apex"))
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def _find_module_py(name: str) -> Path | None:
    for root in _all_apex_roots():
        p = root / f"{name}.py"
        if p.is_file():
            return p
    return None


def _import_local_module(name: str, apex_root: Path | None = None) -> ModuleType:
    """Import a flat module from the Apex install directory (``C:\\Apex``-style layout)."""
    if name in sys.modules:
        return sys.modules[name]
    search_roots = _all_apex_roots()
    if apex_root is not None:
        ar = apex_root.resolve()
        if ar not in search_roots:
            search_roots = [ar, *search_roots]
    last_err: ModuleNotFoundError | None = None
    for root in search_roots:
        root_s = str(root)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as e:
            last_err = e
            py_path = root / f"{name}.py"
            if py_path.is_file():
                return _load_module_from_file(name, py_path)
    hint = _find_module_py(name)
    expect = hint or (apex_root or Path(__file__).resolve().parent) / f"{name}.py"
    roots_list = ", ".join(str(r) for r in search_roots[:6])
    raise ModuleNotFoundError(
        f"No module named '{name}'. Expected file like {expect}. "
        f"Searched: {roots_list}. "
        f"Copy the full APEX repo into C:\\Apex (apex_trader.py, macro_manager.py, prefilter_v6.py, etc.)."
    ) from last_err


_APEX_ROOT = _bootstrap_apex_sys_path()

# Default data dir on Windows VPS when not set (matches apex_trader / backtest JSON paths).
if os.name == "nt" and not (os.environ.get("APEX_DATA_DIR") or "").strip():
    os.environ.setdefault("APEX_DATA_DIR", str(_APEX_ROOT))

# Third-party (VPS-safe — no pandas_ta / numba).
try:
    import pandas as pd
except ImportError as e:  # noqa: BLE001
    raise ImportError("pandas is required (pip install pandas numpy yfinance)") from e

try:
    import numpy as np  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError("numpy is required (pip install numpy)") from e

try:
    import yfinance as yf  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError("yfinance is required (pip install yfinance)") from e

_macro_manager = _import_local_module("macro_manager", _APEX_ROOT)
set_backtest_mode = _macro_manager.set_backtest_mode
set_backtest_mode(False)

# v7.6 decision logic (inlined module — no continuous_backtester import).
_v76_logic = _import_local_module("apex_v76_decision_logic", _APEX_ROOT)

# MT5 helpers only — do not use legacy live signal logic from apex_trader.
at = _import_local_module("apex_trader", _APEX_ROOT)

# ---------------------------------------------------------------------------
# v7.6 live configuration
# ---------------------------------------------------------------------------

STRATEGY_VERSION = "v7.6-live-mirror"
APEX_V76_MAGIC = int(os.environ.get("APEX_V76_MAGIC", "760760"))
ORDER_COMMENT_V76 = os.environ.get("APEX_V76_ORDER_COMMENT", "APEX76")
DRY_RUN = os.environ.get("APEX_DRY_RUN", "true").strip().lower() in ("1", "true", "yes")

SCAN_HOURS = at.SCAN_HOURS
TIMEFRAMES: tuple[str, ...] = ("1w", "1d", "4h")
TICKERS: list[str] = list(at.TICKERS)

V76_STATE_FILE = at.BASE_DIR / "apex_v76_live_state.json"
V76_TICKET_META = at.BASE_DIR / "apex_trader_v76_tickets.json"
V76_DECISION_LOG = at.BASE_DIR / "apex_v76_decisions.jsonl"
LIVE_TRADES_FORENSIC = at.BASE_DIR / "live_trades_forensic.json"

# Deep live log + remote API snapshot (VPS: ``APEX_DATA_DIR`` / ``C:\Apex``; Railway: set ``APEX_LIVE_V76_DIR``).
_LOG_RING_MAX = 8000
_LOG_RING_TRIM = 6000
_live_log_lock = threading.Lock()
LIVE_V76_LOG_RING: list[str] = []
LIVE_V76_STATUS: dict[str, Any] = {
    "version": STRATEGY_VERSION,
    "status": "idle",
    "dry_run": DRY_RUN,
    "magic": APEX_V76_MAGIC,
    "updated_at": None,
    "last_scan_slot": None,
    "balance": None,
    "equity": None,
    "daily_pnl": None,
    "period_mode": None,
    "circuit_halt_until": None,
    "open_positions": [],
    "last_scan_summary": {},
    "recent_log_tail": [],
}


def live_v76_data_dir() -> Path:
    """Directory for ``apex_v76_live.log`` and ``apex_v76_live_status.json``."""
    raw = os.environ.get("APEX_LIVE_V76_DIR") or os.environ.get("APEX_DATA_DIR") or str(at.BASE_DIR)
    return Path(raw).resolve()


def live_v76_log_path() -> Path:
    return live_v76_data_dir() / "apex_v76_live.log"


def live_v76_status_path() -> Path:
    return live_v76_data_dir() / "apex_v76_live_status.json"


def _fmt_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    return " ".join(parts)


def live_log(level: str, msg: str, **fields: Any) -> None:
    """Append to ``apex_v76_live.log``, in-memory ring, and ``apex_log.txt``."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    extra = _fmt_fields(fields) if fields else ""
    line = f"{ts} | {level.upper():7} | {msg}" + (f" | {extra}" if extra else "")
    with _live_log_lock:
        LIVE_V76_LOG_RING.append(line)
        if len(LIVE_V76_LOG_RING) > _LOG_RING_MAX:
            LIVE_V76_LOG_RING[:] = LIVE_V76_LOG_RING[-_LOG_RING_TRIM:]
        LIVE_V76_STATUS["recent_log_tail"] = LIVE_V76_LOG_RING[-80:]
    try:
        live_v76_data_dir().mkdir(parents=True, exist_ok=True)
        with open(live_v76_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        at.log_msg(f"[v76-live] log file write failed: {e}", "warning")
    lvl = level.lower()
    if lvl not in ("info", "warning", "error", "critical"):
        lvl = "info"
    at.log_msg(f"[v76-live] {msg}" + (f" | {extra}" if extra else ""), lvl)


def publish_live_status(mt5: Any | None = None, **extra: Any) -> None:
    """Write ``LIVE_V76_STATUS`` to disk for ``GET /api/live/status`` (file or in-process)."""
    snap = collect_live_status(mt5)
    with _live_log_lock:
        LIVE_V76_STATUS.update(snap)
        LIVE_V76_STATUS.update(extra)
        LIVE_V76_STATUS["updated_at"] = datetime.now(timezone.utc).isoformat()
        LIVE_V76_STATUS["dry_run"] = DRY_RUN
        LIVE_V76_STATUS["recent_log_tail"] = LIVE_V76_LOG_RING[-80:]
        payload = dict(LIVE_V76_STATUS)
    try:
        live_v76_data_dir().mkdir(parents=True, exist_ok=True)
        at._save(live_v76_status_path(), payload)
    except OSError as e:
        live_log("warning", "status file write failed", error=str(e))


def collect_live_status(mt5: Any | None = None) -> dict[str, Any]:
    """Balance, equity, open APEX v76 positions with unrealized P&L."""
    st = load_v76_state()
    out: dict[str, Any] = {
        "version": STRATEGY_VERSION,
        "dry_run": DRY_RUN,
        "magic": APEX_V76_MAGIC,
        "last_scan_slot": st.get("last_scan_slot"),
        "circuit_halt_until": st.get("circuit_halt_until"),
        "period_mode": st.get("last_period_mode"),
        "balance": st.get("last_balance"),
        "equity": st.get("last_equity"),
        "daily_pnl": st.get("last_daily_pnl"),
        "day_anchor": st.get("day_anchor"),
        "log_file": str(live_v76_log_path()),
        "status_file": str(live_v76_status_path()),
    }
    positions_out: list[dict[str, Any]] = []
    if mt5 is not None:
        try:
            ai = mt5.account_info()
            if ai is not None:
                out["balance"] = float(ai.balance)
                out["equity"] = float(ai.equity)
                out["margin"] = float(getattr(ai, "margin", 0) or 0)
                out["currency"] = str(getattr(ai, "currency", "") or "")
            old_magic = at.APEX_MAGIC
            try:
                at.APEX_MAGIC = APEX_V76_MAGIC
                for p in at.open_apex_positions(mt5):
                    tick = mt5.symbol_info_tick(p.symbol)
                    bid = float(tick.bid) if tick else 0.0
                    ask = float(tick.ask) if tick else 0.0
                    d = "LONG" if int(p.type) == 0 else "SHORT"
                    px = bid if d == "LONG" else ask
                    meta = ticket_meta_v76_load().get(str(int(p.ticket)), {})
                    positions_out.append(
                        {
                            "ticket": int(p.ticket),
                            "symbol": str(p.symbol),
                            "ticker": str(meta.get("ticker", "")),
                            "timeframe": str(meta.get("tf", "")),
                            "strategy_id": str(meta.get("strategy", "")),
                            "direction": d,
                            "volume": float(p.volume),
                            "entry": float(p.price_open),
                            "current_price": px,
                            "sl": float(p.sl or 0),
                            "profit": float(p.profit),
                            "swap": float(getattr(p, "swap", 0) or 0),
                            "trail_regime": str(meta.get("trail_regime", "")),
                            "tp1": float(meta.get("tp1", 0) or 0),
                            "tp2": float(meta.get("tp2", 0) or 0),
                            "tp3": float(meta.get("tp3", 0) or 0),
                            "hit_tp1": bool(meta.get("hit_tp1")),
                            "hit_tp2": bool(meta.get("hit_tp2")),
                            "macro_bias": str(meta.get("macro_bias", "")),
                            "confidence": str(meta.get("confidence", "")),
                        }
                    )
            finally:
                at.APEX_MAGIC = old_magic
        except Exception as e:  # noqa: BLE001
            out["mt5_error"] = str(e)
    out["open_positions"] = positions_out
    out["open_count"] = len(positions_out)
    return out


def tail_live_log_file(max_lines: int) -> list[str]:
    path = live_v76_log_path()
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-max_lines:]]
    except OSError:
        return []


def get_live_logs_api(max_lines: int = 300) -> dict[str, Any]:
    n = max(1, min(int(max_lines), 10000))
    with _live_log_lock:
        ring = list(LIVE_V76_LOG_RING[-n:])
    file_lines = tail_live_log_file(n)
    lines = file_lines if len(file_lines) >= len(ring) else ring
    if len(file_lines) > len(lines):
        lines = file_lines
    return {
        "lines": lines,
        "count": len(lines),
        "log_path": str(live_v76_log_path()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_live_status_api() -> dict[str, Any]:
    path = live_v76_status_path()
    if path.is_file():
        data = at._load(path, default=None)
        if isinstance(data, dict):
            with _live_log_lock:
                LIVE_V76_STATUS.update(data)
            return data
    with _live_log_lock:
        return dict(LIVE_V76_STATUS)


def _sizing_fields_from_ai(ai: dict[str, Any]) -> dict[str, Any]:
    return {
        "confidence": ai.get("confidence"),
        "confidence_pre_upgrade": ai.get("confidence_pre_upgrade"),
        "account_risk_pct": ai.get("account_risk_pct"),
        "max_risk_dollars": ai.get("max_risk_dollars"),
        "strategy_confluence_count": ai.get("strategy_confluence_count"),
        "strategy_confluence_mult": ai.get("strategy_confluence_mult"),
        "trend_size_mult": ai.get("trend_size_mult"),
        "regime": ai.get("regime"),
        "regime_size_multiplier": ai.get("regime_size_multiplier"),
        "calendar_action": ai.get("calendar_action"),
        "macro_bias": ai.get("macro_bias"),
        "macro_bias_adjusted": ai.get("macro_bias_adjusted"),
        "macro_rate_diff": ai.get("macro_rate_diff"),
        "macro_event_boost_applied": ai.get("macro_event_boost_applied"),
        "combination_boost_applied": ai.get("combination_boost_applied"),
        "period_mode": ai.get("period_mode"),
        "v74_perfect_storm": ai.get("_v74_perfect_storm_m03_jpy"),
    }

# Phase risk/tp multipliers — same as ``_chrono_v71_phases`` (1w / 1d / 4h).
TF_PHASE_MULT: dict[str, tuple[float, float]] = {
    "1w": (1.0, 1.0),
    "1d": (0.85, 0.85),
    "4h": (0.70, 0.70),
    "1h": (0.85, 0.85),
}


@dataclass
class ScanSkip:
    skipped: bool = True
    reason: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradePlan:
    skipped: bool = False
    sym: str = ""
    timeframe: str = ""
    tf_key: str = ""
    strategy_id: str = ""
    direction: str = ""
    entry: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    trail_regime: str = "CHOPPY"
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    lot_size: float = 0.0
    ai: dict[str, Any] = field(default_factory=dict)
    log_fields: dict[str, Any] = field(default_factory=dict)


def log_v76(msg: str, level: str = "info") -> None:
    live_log(level, msg)


def load_v76_state() -> dict[str, Any]:
    d = at._load(
        V76_STATE_FILE,
        {
            "last_scan_slot": "",
            "circuit_halt_until": "",
            "completed_trades": [],
            "daily_pnl": [],
            "day_key": "",
            "day_anchor": None,
        },
    )
    return d if isinstance(d, dict) else {}


def save_v76_state(d: dict[str, Any]) -> None:
    at._save(V76_STATE_FILE, d)


def append_decision_log(row: dict[str, Any]) -> None:
    try:
        at.BASE_DIR.mkdir(parents=True, exist_ok=True)
        with open(V76_DECISION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        log_v76(f"decision log: {e}", "warning")


def ticket_meta_v76_load() -> dict[str, Any]:
    return at._load(V76_TICKET_META, {})


def ticket_meta_v76_save(d: dict[str, Any]) -> None:
    at._save(V76_TICKET_META, d)


def load_live_trades_forensic() -> list[dict[str, Any]]:
    raw = at._load(LIVE_TRADES_FORENSIC, [])
    return list(raw) if isinstance(raw, list) else []


def append_live_trade_forensic(record: dict[str, Any]) -> None:
    """Append one closed-trade row to ``live_trades_forensic.json`` (JSON array)."""
    rows = load_live_trades_forensic()
    rows.append(record)
    at._save(LIVE_TRADES_FORENSIC, rows)


def _pip_size_for_ticker(ticker: str) -> float:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and "JPY" in t:
        return 0.01
    if len(t) == 6:
        return 0.0001
    return 0.0001


def _pips_moved(direction: str, entry: float, exit_price: float, ticker: str) -> float:
    pip = _pip_size_for_ticker(ticker)
    if pip <= 0 or not math.isfinite(entry) or not math.isfinite(exit_price):
        return 0.0
    if str(direction or "").strip().upper() == "LONG":
        return round((exit_price - entry) / pip, 1)
    return round((entry - exit_price) / pip, 1)


def _classify_live_exit_reason(meta: dict[str, Any], deals: list[Any], mt5m: Any) -> str:
    hit1 = bool(meta.get("hit_tp1"))
    hit2 = bool(meta.get("hit_tp2"))
    hit3 = bool(meta.get("hit_tp3_partial") or meta.get("hit_tp3_full"))

    last_out = None
    for d in deals:
        if int(getattr(d, "entry", -1)) == mt5m.DEAL_ENTRY_OUT:
            last_out = d

    reason_code = int(getattr(last_out, "reason", -1)) if last_out is not None else -1
    if reason_code == mt5m.DEAL_REASON_CLIENT:
        return "MANUAL"
    if reason_code == mt5m.DEAL_REASON_TP:
        if hit3:
            return "TP3"
        if hit2:
            return "TP2"
        if hit1:
            return "TP1"
        return "TP3"
    if reason_code == mt5m.DEAL_REASON_SL:
        if hit1 or hit2:
            return "TRAIL"
        return "SL"
    if hit3:
        return "TP3"
    if hit2:
        return "TP2"
    if hit1:
        return "TP1"
    return "MANUAL"


def _forensic_record_from_close(mt5: Any, ticket: int, meta: dict[str, Any]) -> dict[str, Any]:
    import MetaTrader5 as mt5m

    t0 = datetime.now(timezone.utc) - timedelta(days=90)
    now = datetime.now(timezone.utc)
    deals = [
        d
        for d in (mt5.history_deals_get(t0.replace(tzinfo=None), datetime.utcnow(), position=ticket) or [])
        if int(getattr(d, "magic", 0) or 0) == APEX_V76_MAGIC
    ]

    entry_price = float(meta.get("entry_fill", 0) or 0)
    exit_price = entry_price
    pnl = 0.0
    volume = 0.0
    open_ts: int | None = None
    close_ts: int | None = None

    for d in deals:
        ent = int(getattr(d, "entry", -1))
        if ent == mt5m.DEAL_ENTRY_IN:
            open_ts = int(d.time) if open_ts is None else min(open_ts, int(d.time))
            if entry_price <= 0:
                entry_price = float(d.price)
            volume = max(volume, float(d.volume))
        elif ent == mt5m.DEAL_ENTRY_OUT:
            pnl += float(d.profit)
            exit_price = float(d.price)
            close_ts = int(d.time)

    direction = str(meta.get("direction", "")).strip().upper()
    ticker = str(meta.get("ticker", "")).strip().upper()
    if not ticker:
        ticker = at.position_forex_base6(str(meta.get("symbol", ""))) or str(meta.get("symbol", ""))

    hold_hours = 0.0
    if open_ts is not None and close_ts is not None and close_ts >= open_ts:
        hold_hours = round((close_ts - open_ts) / 3600.0, 2)

    close_date = (
        datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if close_ts is not None
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    exit_reason = _classify_live_exit_reason(meta, deals, mt5m)
    outcome = "WIN" if pnl > 0 else "LOSS"

    return {
        "date": close_date,
        "ticker": ticker,
        "direction": direction,
        "strategy_id": str(meta.get("strategy", meta.get("strategy_id", ""))).strip().upper(),
        "confidence": str(meta.get("confidence", "")).strip().upper(),
        "macro_bias": str(meta.get("macro_bias", "")).strip().upper(),
        "period_mode": str(meta.get("period_mode", "")).strip().upper(),
        "st_boost_tier": str(meta.get("st_boost_tier", "NONE")).strip().upper(),
        "trail_regime": str(meta.get("trail_regime", meta.get("trail_market_regime", "CHOPPY"))).strip().upper(),
        "entry_price": round(entry_price, 5),
        "exit_price": round(exit_price, 5),
        "pnl_dollars": round(pnl, 2),
        "pnl_pips": _pips_moved(direction, entry_price, exit_price, ticker),
        "outcome": outcome,
        "position_size": round(volume, 2) if volume > 0 else round(float(meta.get("position_size", 0) or 0), 2),
        "max_risk_dollars": round(
            float(meta.get("final_risk_usd", meta.get("max_risk_dollars", 0)) or 0),
            2,
        ),
        "hold_time_hours": hold_hours,
        "exit_reason": exit_reason,
        "pyramid_trade": bool(meta.get("pyramid_trade")),
    }


def _finalize_closed_positions_v76(mt5: Any, *, prior_meta: dict[str, Any] | None = None) -> None:
    """Detect closed v76 tickets, append forensic rows, prune ticket meta."""
    if mt5 is None:
        return
    try:
        if not mt5.terminal_info():
            return
    except Exception:  # noqa: BLE001
        return

    open_ids = {
        int(p.ticket)
        for p in (mt5.positions_get() or [])
        if int(getattr(p, "magic", 0) or 0) == APEX_V76_MAGIC
    }
    current_meta = ticket_meta_v76_load()
    to_log: dict[str, dict[str, Any]] = {}

    for src in (prior_meta, current_meta):
        if not isinstance(src, dict):
            continue
        for k, m in src.items():
            if not isinstance(m, dict):
                continue
            try:
                tid = int(k)
            except (TypeError, ValueError):
                continue
            if tid in open_ids:
                continue
            to_log[k] = m

    if not to_log:
        return

    changed = False
    for k, m in to_log.items():
        try:
            tid = int(k)
        except (TypeError, ValueError):
            continue
        record = _forensic_record_from_close(mt5, tid, m)
        append_live_trade_forensic(record)
        live_log(
            "info",
            "[FORENSIC] position closed",
            ticket=tid,
            ticker=record.get("ticker"),
            outcome=record.get("outcome"),
            exit_reason=record.get("exit_reason"),
            pnl_dollars=record.get("pnl_dollars"),
        )
        if k in current_meta:
            del current_meta[k]
            changed = True

    if changed:
        ticket_meta_v76_save(current_meta)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_halt_until(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def _circuit_breaker_live(
    st: dict[str, Any],
    scan_d: date,
    balance: float,
    day_pnl: float,
) -> tuple[bool, str]:
    """Mirror ``_v76_circuit_breaker_check`` with persisted halt (survives restarts)."""
    halt_raw = str(st.get("circuit_halt_until") or "").strip()
    halt_dt = _parse_halt_until(halt_raw)
    scan_dt = datetime.combine(scan_d, datetime.min.time())
    if halt_dt is not None and scan_dt < halt_dt:
        return True, f"[HALTED] Trading suspended until {halt_dt.isoformat(sep=' ', timespec='minutes')}"

    daily_rows = st.get("daily_pnl") if isinstance(st.get("daily_pnl"), list) else []
    trailing_5d = _v76_logic._v76_rolling_5d_pnl(daily_rows, as_of=scan_d, current_day_pnl=day_pnl)
    cap = float(balance or _v76_logic.STARTING_CAPITAL)
    if cap <= 0:
        return False, ""
    pct = trailing_5d / cap
    if pct > -0.08:
        if halt_dt is not None and scan_dt >= halt_dt:
            st.pop("circuit_halt_until", None)
            save_v76_state(st)
        return False, ""

    halt_until = scan_dt + timedelta(hours=48)
    st["circuit_halt_until"] = halt_until.isoformat()
    save_v76_state(st)
    pct_disp = round(pct * 100.0, 2)
    msg = (
        f"[CIRCUIT BREAKER] 5-day P&L is {pct_disp}% — HALTING all trading for 48 hours. "
        f"Resumes at {halt_until.isoformat(sep=' ', timespec='minutes')}"
    )
    live_log("warning", msg)
    return True, msg


def _live_period_mode(st: dict[str, Any], balance: float, scan_d: date) -> str:
    """Use closed-trade buffer in v76 state (same math as ``_detect_period_mode``)."""
    buf: list[dict[str, Any]] = []
    for r in st.get("completed_trades") or []:
        if not isinstance(r, dict) or r.get("skipped"):
            continue
        if str(r.get("outcome", "")).strip().upper() not in ("WIN", "LOSS"):
            continue
        ds = str(r.get("date", ""))[:10]
        if ds and ds <= scan_d.isoformat():
            buf.append(r)
    pm, _, _ = _v76_logic._detect_period_mode(balance, "live", scan_d, buf)
    return pm


def _locked_layer2_only(
    layer2: list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]],
    *,
    tf_key: str,
) -> list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]]:
    """Live executes LOCKED strategies only (per design)."""
    tf_l = tf_key.strip().lower()
    out: list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]] = []
    for row in layer2:
        sid = str(row[0]).strip().upper()
        if sid not in _v76_logic.LOCKED_STRATEGY_IDS or not _v76_logic._v75_backtest_strategy_allowed(sid):
            continue
        if tf_l == "4h" and sid == "M02_MACD_ZERO_CROSS":
            continue
        out.append(row)
    return out


def _regime_tp_levels(
    direction: str,
    entry: float,
    stop: float,
    trail_regime: str,
) -> tuple[float, float, float]:
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return entry, entry, entry
    mult1, mult2, mult3 = (2.0, 4.0, 7.0) if trail_regime == "TRENDING" else (1.5, 3.0, 5.0)
    sign = 1.0 if direction == "LONG" else -1.0
    return (
        round(entry + sign * risk * mult1, 5),
        round(entry + sign * risk * mult2, 5),
        round(entry + sign * risk * mult3, 5),
    )


def _accumulate_live_prefilter(
    sym: str,
    tf_key: str,
    qualifying: list[tuple[str, str, int, dict[str, Any] | None]],
) -> None:
    """Mirror ``_chrono_accumulate_prefilter_signals`` for confluence counting."""
    sym_u = sym.strip().upper()
    for row in qualifying:
        if len(row) < 2:
            continue
        sid = str(row[0]).strip().upper()
        dr = str(row[1]).strip().upper()
        if dr == "BOTH":
            _v76_logic.CHRONO_DAY_PREFILTER_SIDS[(sym_u, "LONG")].add(sid)
            _v76_logic.CHRONO_DAY_PREFILTER_SIDS[(sym_u, "SHORT")].add(sid)
        elif dr in ("LONG", "SHORT"):
            _v76_logic.CHRONO_DAY_PREFILTER_SIDS[(sym_u, dr)].add(sid)
        _v76_logic.CHRONO_SYMDIR_TFS[(sym_u, dr if dr in ("LONG", "SHORT") else "LONG")].add(tf_key)
        if sym_u in _v76_logic.JPY_STORM_PAIRS:
            _v76_logic.CHRONO_JPY_PAIRS_SIGNALLED.add(sym_u)


def build_trade_plan_v76(
    *,
    sym: str,
    timeframe: str,
    analysis_date: str,
    balance: float,
    day_pnl: float,
    period_mode: str,
    regime_ctx: dict[str, Any],
    layer2_locked: list[tuple[str, str, int] | tuple[str, str, int, dict[str, Any] | None]],
    past: pd.DataFrame,
    ind: dict[str, Any],
    price: float,
    zone_pct: float,
    zone_label: str,
    chrono_risk_mult: float,
    chrono_tp_mult: float,
) -> ScanSkip | TradePlan:
    """
  Run the Layer-2 Python path from the backtest (steps 23 in pipeline map) without forward simulation.
  """
    tf_key = timeframe.strip().lower()
    is_exotic = sym in _v76_logic.EXOTIC_REDUCE
    if not layer2_locked:
        return ScanSkip(reason="No LOCKED Layer-2 candidate after filters")

    picked = _v76_logic._layer2_tuple_for_deterministic_pick(sym, tf_key, zone_pct, layer2_locked)
    if picked is None:
        return ScanSkip(reason="No Layer 2 pick after deterministic ordering")

    d_pick = str(picked[1]).strip().upper()
    if d_pick == "BOTH":
        d_pick = "LONG" if float(zone_pct) < 50.0 else "SHORT"
    if d_pick not in ("LONG", "SHORT"):
        return ScanSkip(reason=f"Invalid direction for {picked[0]}")

    if tf_key == "4h" and str(picked[0]).strip().upper() == "M02_MACD_ZERO_CROSS":
        return ScanSkip(reason="M02 blocked on 4h timeframe")

    res = _v76_logic.python_layer2_live_plan(
        sym=sym,
        timeframe=timeframe,
        analysis_date=analysis_date,
        tf_key=tf_key,
        price=float(price),
        zone_pct=zone_pct,
        zone_label=zone_label,
        is_exotic=is_exotic,
        layer2=[picked],
        rsi_live=float(ind.get("rsi", 50) or 50),
        chrono_risk_mult=chrono_risk_mult,
        chrono_tp_mult=chrono_tp_mult,
        regime_ctx=regime_ctx,
        chrono_balance=balance,
        chrono_day_pnl=day_pnl,
        period_mode=period_mode,
        past=past,
        ind=ind,
        log_fn=live_log,
    )
    if res is None:
        return ScanSkip(reason="Layer 2 produced no result")
    if res.get("skipped") or res.get("skip_trade"):
        skip_fields = {k: res.get(k) for k in (
            "strategy_id", "macro_bias", "macro_bias_adjusted", "period_mode", "halt_active",
            "trend_strength", "regime", "confidence", "strategy_confluence_count",
            "skip_reason",
        )}
        skip_fields.update(_sizing_fields_from_ai(res))
        return ScanSkip(
            reason=str(res.get("skip_reason") or "skipped"),
            fields=skip_fields,
        )

    entry = float(res.get("entry_price", price) or price)
    stop = float(res.get("stop_loss", 0) or 0)
    direction = str(res.get("direction", d_pick)).strip().upper()
    mb = str(res.get("macro_bias", "") or "")
    ts = float(res.get("trend_strength", 0) or 0)
    rd = float(res.get("macro_rate_diff", 0) or 0)
    trail_reg = _v76_logic.resolve_trailing_regime(mb, ts, rd, timeframe=timeframe, log_fn=live_log)
    tp1, tp2, tp3 = _regime_tp_levels(direction, entry, stop, trail_reg)

    risk_usd = float(res.get("max_risk_dollars", 0) or 0)
    risk_pct = float(res.get("account_risk_pct", 0) or 0)
    macro_event = bool(res.get("macro_event_boost_applied"))

    plan = TradePlan(
        sym=sym.upper(),
        timeframe=timeframe,
        tf_key=tf_key,
        strategy_id=str(res.get("strategy_id", picked[0])).strip().upper(),
        direction=direction,
        entry=entry,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        trail_regime=trail_reg,
        risk_usd=risk_usd,
        risk_pct=risk_pct,
        ai=dict(res),
        log_fields={
            "date": analysis_date,
            "ticker": sym.upper(),
            "timeframe": timeframe,
            "strategy_id": res.get("strategy_id"),
            "direction": direction,
            "macro_bias": mb,
            "macro_bias_adjusted": res.get("macro_bias_adjusted", mb),
            "trend_strength": ts,
            "regime": res.get("regime"),
            "confidence": res.get("confidence"),
            "strategy_confluence_count": res.get("strategy_confluence_count"),
            "period_mode": res.get("period_mode", period_mode),
            "macro_event_boost_applied": macro_event,
            "is_macro_event": macro_event,
            "trail_market_regime": trail_reg,
            "final_risk_pct": risk_pct,
            "max_risk_dollars": risk_usd,
            "entry_price": entry,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "combination_boost_applied": res.get("combination_boost_applied"),
            "st_boost_tier": res.get("st_boost_tier", "NONE"),
            "dry_run": DRY_RUN,
        },
    )
    tier, score = _st_pyramid_entry_fields(plan)
    plan.ai["st_boost_tier"] = tier
    plan.ai["st_layer2_score"] = score
    plan.log_fields["st_boost_tier"] = tier
    plan.log_fields["st_layer2_score"] = score
    return plan


def evaluate_cell_v76(
    sym: str,
    tf: str,
    *,
    st: dict[str, Any],
    balance: float,
    day_pnl: float,
    period_mode: str,
    regime_ctx: dict[str, Any],
    scan_d: date,
) -> ScanSkip | TradePlan | None:
    """Full prefilter + Layer-2 plan for one symbol/timeframe."""
    tf_key = tf.strip().lower()
    if tf_key == "4h":
        pass  # allowed
    analysis_date = scan_d.isoformat()

    past = at.fetch_past_for_prefilter(sym, tf_key)
    if past is None or past.empty or len(past) < 40:
        return ScanSkip(
            reason=f"Insufficient OHLC for {sym} {tf}",
            fields={"ticker": sym.upper(), "timeframe": tf, "bars": len(past) if past is not None else 0},
        )

    built = at.build_prefilter_inputs(past, sym)
    if built is None:
        return ScanSkip(reason=f"Indicator build failed {sym} {tf}", fields={"ticker": sym.upper(), "timeframe": tf})
    ind, price, zone_pct = built
    zone_label = "EQUILIBRIUM"
    if zone_pct >= 66:
        zone_label = "PREMIUM"
    elif zone_pct <= 33:
        zone_label = "DISCOUNT"

    qualifies, qualifying, _reason = _v76_logic._v7_python_prefilter_bundle(
        sym,
        tf_key,
        float(price),
        ind,
        float(zone_pct),
        analysis_date=analysis_date,
        past=past,
    )
    for row in qualifying:
        if len(row) < 2:
            continue
        live_log(
            "info",
            "[SIGNAL] prefilter qualified",
            ticker=sym.upper(),
            timeframe=tf,
            strategy_id=str(row[0]).strip().upper(),
            direction=str(row[1]).strip().upper(),
            score=int(row[2]) if len(row) > 2 else 0,
            price=round(float(price), 5),
            zone_pct=round(float(zone_pct), 1),
        )

    if not qualifies:
        return ScanSkip(
            reason=f"PreFilter: {_reason}",
            fields={"ticker": sym.upper(), "timeframe": tf, "price": round(float(price), 5)},
        )

    layer2 = [q for q in qualifying if str(q[0]).strip().upper() not in _v76_logic.LAYER1_STRATEGY_IDS]
    layer2_locked = _locked_layer2_only(layer2, tf_key=tf_key)
    locked_ids = [str(r[0]).strip().upper() for r in layer2_locked]
    live_log(
        "info",
        "[SCAN] layer2 locked candidates",
        ticker=sym.upper(),
        timeframe=tf,
        locked_strategies=",".join(locked_ids) or "none",
        period_mode=period_mode,
    )
    if not layer2_locked:
        return ScanSkip(
            reason="No LOCKED strategies in Layer-2 qualifiers",
            fields={"ticker": sym.upper(), "timeframe": tf, "qualified": ",".join(
                str(q[0]).strip().upper() for q in qualifying
            )},
        )

    risk_m, tp_m = TF_PHASE_MULT.get(tf_key, (1.0, 1.0))
    return build_trade_plan_v76(
        sym=sym,
        timeframe=tf,
        analysis_date=analysis_date,
        balance=balance,
        day_pnl=day_pnl,
        period_mode=period_mode,
        regime_ctx=regime_ctx,
        layer2_locked=layer2_locked,
        past=past,
        ind=ind,
        price=float(price),
        zone_pct=float(zone_pct),
        zone_label=zone_label,
        chrono_risk_mult=risk_m,
        chrono_tp_mult=tp_m,
    )


def lot_size_from_risk(mt5: Any, sym: str, entry: float, sl: float, risk_usd: float) -> float:
    mpl = at.mpl_sl(mt5, sym, entry, sl, "LONG" if entry > sl else "SHORT")
    if mpl is None or mpl <= 0:
        return 0.0
    return at.norm_vol(mt5, sym, risk_usd / mpl)


def order_send_v76(
    mt5: Any,
    broker_sym: str,
    plan: TradePlan,
) -> dict[str, Any]:
    """Open position (or dry-run log only)."""
    if DRY_RUN:
        tick = mt5.symbol_info_tick(broker_sym) if mt5 else None
        entry = float(tick.ask if plan.direction == "LONG" else tick.bid) if tick else plan.entry
        lots = 0.0
        if mt5 and tick:
            lots = lot_size_from_risk(mt5, broker_sym, entry, plan.stop_loss, plan.risk_usd)
        row = dict(plan.log_fields)
        row.update(
            {
                "action": "DRY_RUN",
                "lot_size": round(lots, 2),
                "entry_price_live": entry,
                "magic_number": APEX_V76_MAGIC,
            }
        )
        dry_meta: dict[str, Any] = {
            "macro_bias": plan.ai.get("macro_bias"),
            "sl": plan.stop_loss,
            "r": abs(entry - plan.stop_loss),
            "final_risk_usd": plan.risk_usd,
        }
        _init_pyramid_tracking(dry_meta, plan, entry)
        if dry_meta.get("pyramid"):
            row["pyramid_trade"] = True
            row["pyramid_candidate"] = dry_meta.get("pyramid")
        append_decision_log(row)
        live_log(
            "info",
            "[TRADE] DRY_RUN would open",
            ticker=plan.sym,
            timeframe=plan.timeframe,
            strategy_id=plan.strategy_id,
            direction=plan.direction,
            lot_size=round(lots, 2),
            risk_usd=round(plan.risk_usd, 2),
            final_risk_pct=plan.risk_pct,
            stop=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp3=plan.tp3,
            trail_regime=plan.trail_regime,
            **_sizing_fields_from_ai(plan.ai),
        )
        return {"ok": True, "dry_run": True, "volume": lots, "entry": entry, "retcode": "DRY_RUN"}

    old_magic = at.APEX_MAGIC
    old_comment = at.ORDER_COMMENT
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        at.ORDER_COMMENT = ORDER_COMMENT_V76
        meta = {
            "ticker": plan.sym,
            "tf": plan.timeframe,
            "strategy": plan.strategy_id,
            "direction": plan.direction,
            "sl": plan.stop_loss,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "tp3": plan.tp3,
            "trail_regime": plan.trail_regime,
            "confidence": plan.ai.get("confidence"),
            "macro_bias": plan.ai.get("macro_bias"),
            "macro_bias_adjusted": plan.ai.get("macro_bias_adjusted"),
            "trend_strength": plan.ai.get("trend_strength"),
            "period_mode": plan.ai.get("period_mode"),
            "macro_event_boost_applied": plan.ai.get("macro_event_boost_applied"),
            "strategy_confluence_count": plan.ai.get("strategy_confluence_count"),
            "final_risk_usd": plan.risk_usd,
            "final_risk_pct": plan.risk_pct,
            "atr_live": float(plan.ai.get("entry_atr", 0) or plan.ai.get("atr", 0) or 0),
            "macro_rate_diff": plan.ai.get("macro_rate_diff"),
            "st_boost_tier": plan.ai.get("st_boost_tier", "NONE"),
            "st_layer2_score": int(plan.ai.get("st_layer2_score", 0) or 0),
        }
        meta.update(_macro_manager.merged_macro_result_fields(plan.ai))
        res = at.order_send_live(mt5, broker_sym, plan.direction, plan.stop_loss, plan.risk_usd, meta)
        if res.get("ok") and res.get("ticket"):
            k = str(int(res["ticket"]))
            tm = ticket_meta_v76_load()
            if isinstance(tm.get(k), dict):
                _init_pyramid_tracking(
                    tm[k],
                    plan,
                    float(res.get("entry", plan.entry) or plan.entry),
                )
                ticket_meta_v76_save(tm)
        lots = float(res.get("volume", 0) or 0)
        retcode = res.get("retcode") if isinstance(res, dict) else None
        row = dict(plan.log_fields)
        row["mt5_retcode"] = retcode
        row["action"] = "ORDER" if res.get("ok") else "ORDER_FAIL"
        row["lot_size"] = lots
        row["magic_number"] = APEX_V76_MAGIC
        append_decision_log(row)
        live_log(
            "info" if res.get("ok") else "warning",
            "[TRADE] MT5 order result",
            ticker=plan.sym,
            timeframe=plan.timeframe,
            strategy_id=plan.strategy_id,
            direction=plan.direction,
            broker_symbol=broker_sym,
            lot_size=round(lots, 2),
            risk_usd=round(plan.risk_usd, 2),
            final_risk_pct=plan.risk_pct,
            stop=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp3=plan.tp3,
            trail_regime=plan.trail_regime,
            mt5_retcode=retcode,
            ok=bool(res.get("ok")),
            error=res.get("error"),
            ticket=res.get("ticket"),
            **_sizing_fields_from_ai(plan.ai),
        )
        return res
    finally:
        at.APEX_MAGIC = old_magic
        at.ORDER_COMMENT = old_comment


def _log_positions_trailing_phase(mt5: Any, *, phase: str) -> None:
    """Log open v76 positions: price vs TP ladder and stop (before/after trail pass)."""
    meta = ticket_meta_v76_load()
    old_magic = at.APEX_MAGIC
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        positions = at.open_apex_positions(mt5)
    finally:
        at.APEX_MAGIC = old_magic
    if not positions:
        live_log("info", f"[TRAIL] {phase} — no open v76 positions")
        return
    live_log("info", f"[TRAIL] {phase} — {len(positions)} position(s)")
    for pos in positions:
        k = str(int(pos.ticket))
        m = meta.get(k) if isinstance(meta.get(k), dict) else {}
        d = "LONG" if int(pos.type) == 0 else "SHORT"
        tick = mt5.symbol_info_tick(pos.symbol)
        bid = float(tick.bid) if tick else 0.0
        ask = float(tick.ask) if tick else 0.0
        px = bid if d == "LONG" else ask
        tp1 = float(m.get("tp1", 0) or 0)
        tp2 = float(m.get("tp2", 0) or 0)
        tp3 = float(m.get("tp3", 0) or 0)
        live_log(
            "info",
            f"[TRAIL] {phase} position",
            ticket=int(pos.ticket),
            symbol=str(pos.symbol),
            strategy_id=str(m.get("strategy", "")),
            timeframe=str(m.get("tf", "")),
            direction=d,
            volume=float(pos.volume),
            entry=float(pos.price_open),
            current_price=round(px, 5),
            sl=float(pos.sl or 0),
            profit=round(float(pos.profit), 2),
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            tp1_hit=bool(m.get("hit_tp1")),
            tp2_hit=bool(m.get("hit_tp2")),
            tp3_partial=bool(m.get("hit_tp3_partial")),
            trail_regime=str(m.get("trail_regime", "")),
            price_vs_tp1="HIT" if (d == "LONG" and tp1 > 0 and px >= tp1) or (d == "SHORT" and tp1 > 0 and px <= tp1) else "no",
            price_vs_tp2="HIT" if (d == "LONG" and tp2 > 0 and px >= tp2) or (d == "SHORT" and tp2 > 0 and px <= tp2) else "no",
            price_vs_tp3="HIT" if (d == "LONG" and tp3 > 0 and px >= tp3) or (d == "SHORT" and tp3 > 0 and px <= tp3) else "no",
        )


def _st_pyramid_entry_fields(plan: TradePlan) -> tuple[str, int]:
    """ST tier + Layer 2 score for pyramid tracking (reuses plan ai or recomputes)."""
    ai = plan.ai
    mb = str(ai.get("macro_bias", "")).strip().upper()
    if mb != "STRONG_TAILWIND":
        return "NONE", 0
    tier = str(ai.get("st_boost_tier", "")).strip().upper()
    score = int(ai.get("st_layer2_score", 0) or 0)
    if tier and tier != "NONE":
        return tier, score
    ts = float(ai.get("trend_strength", 0) or 0)
    rd = float(ai.get("macro_rate_diff", 0) or 0)
    scount = int(ai.get("strategy_confluence_count", 0) or 0)
    layer2 = _macro_manager.compute_st_layer2_score(
        plan.sym,
        plan.direction,
        ts,
        rd,
        as_of_date=date.today(),
    )
    score = int(layer2.get("st_layer2_score", 0) or 0)
    eff = score + (1 if scount >= 2 else 0)
    if eff >= 4:
        tier = "FULL_GOLDEN"
    elif eff >= 3:
        tier = "ENHANCED"
    elif eff >= 1:
        tier = "STANDARD"
    else:
        tier = "BASE"
    return tier, score


def _init_pyramid_tracking(meta: dict[str, Any], plan: TradePlan, entry_fill: float) -> None:
    """Record STRONG_TAILWIND open for staged pyramid-in (Step 1)."""
    mb = str(meta.get("macro_bias") or plan.ai.get("macro_bias", "")).strip().upper()
    if mb != "STRONG_TAILWIND":
        return
    tier, score = _st_pyramid_entry_fields(plan)
    meta["st_boost_tier"] = tier
    meta["st_layer2_score"] = score
    entry = float(entry_fill or plan.entry)
    sl = float(meta.get("sl", plan.stop_loss) or plan.stop_loss)
    sl_dist = float(meta.get("r", abs(entry - sl)) or abs(entry - sl))
    if sl_dist <= 0:
        sl_dist = abs(entry - sl) or abs(entry) * 0.005
    mrd = float(meta.get("final_risk_usd", plan.risk_usd) or plan.risk_usd)
    meta["pyramid"] = {
        "entry_price": entry,
        "symbol": plan.sym,
        "direction": plan.direction,
        "initial_tier": tier,
        "initial_mrd": mrd,
        "entry_layer2_score": score,
        "sl_distance": sl_dist,
        "pyramid_added": False,
    }
    meta["pyramid_trade"] = True


def _check_pyramid_eligible(meta: dict[str, Any], current_price: float, current_score: int) -> bool:
    """
    Add-in eligible when:
    1. Trade is at 0.75R+ profit (proven, not chasing)
    2. Not already pyramided
    3. Original tier was STANDARD or BASE (room to scale up)
    4. Current Layer 2 score improved AND reached ENHANCED threshold (>=3)
    """
    if meta.get("pyramid_added"):
        return False
    if meta.get("initial_tier") not in ("STANDARD", "BASE"):
        return False
    entry = float(meta.get("entry_price", 0) or 0)
    sl_dist = float(meta.get("sl_distance", 0) or 0)
    if sl_dist <= 0 or entry <= 0:
        return False
    direction = str(meta.get("direction", "")).strip().upper()
    if direction == "LONG":
        profit_r = (current_price - entry) / sl_dist
    else:
        profit_r = (entry - current_price) / sl_dist
    if profit_r < 0.75:
        return False
    if current_score <= int(meta.get("entry_layer2_score", 0) or 0) or current_score < 3:
        return False
    return True


def _execute_pyramid_add_mrd(meta: dict[str, Any], current_capital: float) -> float:
    """Step 3 — 50% of original risk, hard-capped at 2.5% total capital exposure."""
    add_mrd = float(meta.get("initial_mrd", 0) or 0) * 0.50
    max_total = float(current_capital) * 0.025
    initial = float(meta.get("initial_mrd", 0) or 0)
    return min(add_mrd, max(0.0, max_total - initial))


def _pyramid_profit_r(meta: dict[str, Any], current_price: float) -> float:
    entry = float(meta.get("entry_price", 0) or 0)
    sl_dist = float(meta.get("sl_distance", 0) or 0)
    if sl_dist <= 0:
        return 0.0
    direction = str(meta.get("direction", "")).strip().upper()
    if direction == "LONG":
        return (current_price - entry) / sl_dist
    return (entry - current_price) / sl_dist


def _pyramid_move_sl_breakeven(mt5: Any, pos: Any, entry_fill: float) -> bool:
    """Move original position stop to breakeven before pyramid add-in."""
    import MetaTrader5 as mt5m

    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    d = "LONG" if int(getattr(pos, "type", 0) or 0) == 0 else "SHORT"
    bid, ask = float(tick.bid), float(tick.ask)
    nsl = at.mt5_round_price(mt5, pos.symbol, float(entry_fill))
    if d == "LONG":
        nsl = at.clamp_sl_buy_live(mt5, pos.symbol, bid, nsl)
        if nsl >= bid:
            return False
    else:
        nsl = at.clamp_sl_sell_live(mt5, pos.symbol, ask, nsl)
        if nsl <= ask:
            return False
    res = mt5.order_send(
        {
            "action": mt5m.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": int(pos.ticket),
            "sl": float(nsl),
            "tp": float(pos.tp or 0.0),
        }
    )
    return bool(res and res.retcode == mt5m.TRADE_RETCODE_DONE)


def _pyramid_send_add_order(
    mt5: Any,
    broker_sym: str,
    direction: str,
    sl: float,
    add_mrd: float,
) -> dict[str, Any]:
    """Send pyramid add-in market order at computed risk dollars."""
    import MetaTrader5 as mt5m

    tick = mt5.symbol_info_tick(broker_sym)
    if tick is None:
        return {"ok": False, "error": "no_tick"}
    d = direction.strip().upper()
    entry = float(tick.ask if d == "LONG" else tick.bid)
    mpl = at.mpl_sl(mt5, broker_sym, entry, sl, d)
    if mpl is None or mpl <= 0:
        return {"ok": False, "error": "mpl"}
    vol = at.norm_vol(mt5, broker_sym, add_mrd / mpl)
    if vol <= 0:
        return {"ok": False, "error": "zero_volume"}
    typ = mt5m.ORDER_TYPE_BUY if d == "LONG" else mt5m.ORDER_TYPE_SELL
    price = float(tick.ask if d == "LONG" else tick.bid)
    old_magic = at.APEX_MAGIC
    old_comment = at.ORDER_COMMENT
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        at.ORDER_COMMENT = ORDER_COMMENT_V76
        res = mt5.order_send(
            {
                "action": mt5m.TRADE_ACTION_DEAL,
                "symbol": broker_sym,
                "volume": vol,
                "type": typ,
                "price": price,
                "sl": float(sl),
                "tp": 0.0,
                "deviation": 25,
                "magic": APEX_V76_MAGIC,
                "comment": ORDER_COMMENT_V76,
                "type_time": mt5m.ORDER_TIME_GTC,
                "type_filling": at.fill_mode(mt5, broker_sym),
            }
        )
    finally:
        at.APEX_MAGIC = old_magic
        at.ORDER_COMMENT = old_comment
    if res is None or res.retcode != mt5m.TRADE_RETCODE_DONE:
        return {"ok": False, "error": getattr(res, "comment", str(res))}
    time.sleep(0.25)
    ticket = None
    for p in mt5.positions_get(symbol=broker_sym) or []:
        if int(getattr(p, "magic", 0) or 0) != APEX_V76_MAGIC:
            continue
        if int(p.ticket) == int(getattr(res, "position", 0) or 0):
            ticket = int(p.ticket)
            break
    if ticket is None:
        candidates = [
            p
            for p in (mt5.positions_get(symbol=broker_sym) or [])
            if int(getattr(p, "magic", 0) or 0) == APEX_V76_MAGIC
        ]
        if candidates:
            ticket = int(max(candidates, key=lambda pp: int(pp.ticket)).ticket)
    return {"ok": True, "ticket": ticket, "volume": vol, "entry": entry}


def _current_st_layer2_score(sym_u: str, direction: str, meta: dict[str, Any]) -> int:
    """Recompute Layer 2 score during trail cycle (macro fields cached on ticket meta)."""
    ts = float(meta.get("trend_strength", 0) or 0)
    rd = float(meta.get("macro_rate_diff", 0) or 0)
    layer2 = _macro_manager.compute_st_layer2_score(
        sym_u,
        direction,
        ts,
        rd,
        as_of_date=date.today(),
    )
    return int(layer2.get("st_layer2_score", 0) or 0)


def _process_pyramid_in_v76(mt5: Any) -> None:
    """Trail-cycle pyramid-in check (Steps 2–3)."""
    if DRY_RUN or mt5 is None:
        return
    try:
        if not mt5.terminal_info():
            return
    except Exception:  # noqa: BLE001
        return

    ai = mt5.account_info()
    current_capital = float(ai.balance) if ai is not None else float(at.STARTING_BALANCE)

    meta = ticket_meta_v76_load()
    old_magic = at.APEX_MAGIC
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        positions = at.open_apex_positions(mt5) or []
    finally:
        at.APEX_MAGIC = old_magic

    changed = False
    for pos in positions:
        k = str(int(pos.ticket))
        m = meta.get(k)
        if not isinstance(m, dict):
            continue
        if m.get("is_pyramid_add"):
            continue
        pyr = m.get("pyramid")
        if not isinstance(pyr, dict):
            continue
        if pyr.get("pyramid_added"):
            continue

        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            continue
        d = str(m.get("direction", "")).strip().upper()
        px = float(tick.bid if d == "LONG" else tick.ask)
        sym_u = str(
            pyr.get("symbol")
            or m.get("ticker")
            or at.position_forex_base6(str(pos.symbol))
            or pos.symbol
        ).strip().upper()
        current_score = _current_st_layer2_score(sym_u, d, m)
        if not _check_pyramid_eligible(pyr, px, current_score):
            continue

        add_mrd = _execute_pyramid_add_mrd(pyr, current_capital)
        if add_mrd < 25.0:
            continue

        profit_r = _pyramid_profit_r(pyr, px)
        entry_fill = float(m.get("entry_fill", pyr.get("entry_price", pos.price_open)))
        if not _pyramid_move_sl_breakeven(mt5, pos, entry_fill):
            live_log(
                "warning",
                "[PYRAMID-IN] breakeven SL move failed — skip add",
                ticket=int(pos.ticket),
                symbol=sym_u,
            )
            continue

        sl = float(pos.sl or m.get("sl", 0) or 0)
        add_res = _pyramid_send_add_order(mt5, str(pos.symbol), d, sl, add_mrd)
        if not add_res.get("ok"):
            live_log(
                "warning",
                "[PYRAMID-IN] add order failed",
                ticket=int(pos.ticket),
                symbol=sym_u,
                error=add_res.get("error"),
            )
            continue

        pyr["pyramid_added"] = True
        m["pyramid"] = pyr
        m["pyramid_trade"] = True
        m["final_risk_usd"] = round(float(pyr.get("initial_mrd", 0) or 0) + add_mrd, 2)
        m["hit_tp1"] = True

        add_ticket = add_res.get("ticket")
        if add_ticket:
            add_k = str(int(add_ticket))
            add_meta = {
                "ticker": sym_u,
                "symbol": str(pos.symbol),
                "direction": d,
                "strategy": m.get("strategy"),
                "tf": m.get("tf"),
                "entry_fill": float(add_res.get("entry", px)),
                "sl": sl,
                "final_risk_usd": round(add_mrd, 2),
                "pyramid_trade": True,
                "is_pyramid_add": True,
                "pyramid_parent_ticket": int(pos.ticket),
                "macro_bias": m.get("macro_bias"),
                "st_boost_tier": m.get("st_boost_tier"),
                "trail_regime": m.get("trail_regime", "CHOPPY"),
            }
            meta[add_k] = add_meta

        live_log(
            "info",
            f"[PYRAMID-IN] {sym_u}: +${add_mrd:.0f} to position "
            f"(tier {pyr.get('initial_tier')}, score {pyr.get('entry_layer2_score')}→{current_score}, "
            f"at {profit_r:.2f}R). Original SL→breakeven.",
            ticket=int(pos.ticket),
            add_mrd=round(add_mrd, 2),
            pyramid_trade=True,
        )
        meta[k] = m
        changed = True

    if changed:
        ticket_meta_v76_save(meta)


def _meta_from_adopted_position(pos: Any) -> dict[str, Any]:
    """Minimal ticket meta from an open MT5 position (prior trader / restart adoption)."""
    ticket = int(pos.ticket)
    d = "LONG" if int(getattr(pos, "type", 0) or 0) == 0 else "SHORT"
    entry = float(pos.price_open)
    sl = float(pos.sl or 0.0)
    if sl <= 0:
        risk = abs(entry) * 0.01
        sl = entry - risk if d == "LONG" else entry + risk
    else:
        risk = abs(entry - sl)
    if risk <= 0:
        risk = abs(entry) * 0.01
    trail_reg = "CHOPPY"
    tp1, tp2, tp3 = _regime_tp_levels(d, entry, sl, trail_reg)
    broker_tp = float(pos.tp or 0.0)
    if broker_tp > 0:
        tp3 = broker_tp
    ticker = at.position_forex_base6(str(pos.symbol)) or str(pos.symbol)
    return {
        "ticket": ticket,
        "entry_fill": entry,
        "direction": d,
        "sl": sl,
        "r": risk,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "trail_regime": trail_reg,
        "ticker": ticker,
        "symbol": str(pos.symbol),
        "adopted_on_startup": True,
        "st_boost_tier": "NONE",
        "hit_tp1": False,
        "hit_tp2": False,
        "hit_tp3_partial": False,
    }


def _adopt_open_positions(mt5: Any) -> None:
    """Register open v76-magic MT5 tickets in ``apex_trader_v76_tickets.json`` on startup."""
    if mt5 is None:
        return
    try:
        if not mt5.terminal_info():
            return
    except Exception:  # noqa: BLE001
        return

    raw = mt5.positions_get() or []
    meta = ticket_meta_v76_load()
    updated = False
    for pos in raw:
        if int(getattr(pos, "magic", 0) or 0) != APEX_V76_MAGIC:
            continue
        ticket = int(pos.ticket)
        key = str(ticket)
        if isinstance(meta.get(key), dict):
            continue
        meta[key] = _meta_from_adopted_position(pos)
        updated = True
        live_log("info", f"[ADOPT] Ticket {ticket} {pos.symbol} adopted from MT5 on startup")

    if updated:
        ticket_meta_v76_save(meta)


def manage_trailing_v76(mt5: Any) -> None:
    """Trailing with deep logging (before/after each pass)."""
    _log_positions_trailing_phase(mt5, phase="BEFORE")
    pre_meta = ticket_meta_v76_load()
    old_magic = at.APEX_MAGIC
    old_meta_path = at.TICKET_META_FILE
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        at.TICKET_META_FILE = V76_TICKET_META
        at.manage_trailing_live(mt5)
    finally:
        at.APEX_MAGIC = old_magic
        at.TICKET_META_FILE = old_meta_path
    _process_pyramid_in_v76(mt5)
    _finalize_closed_positions_v76(mt5, prior_meta=pre_meta)
    _log_positions_trailing_phase(mt5, phase="AFTER")
    publish_live_status(mt5, status="running")


def run_full_scan_v76() -> None:
    _v76_logic.v72_load_strategy_status(at.BASE_DIR, log_fn=live_log)
    st = load_v76_state()
    scan_d = datetime.now(timezone.utc).date()
    analysis_date = scan_d.isoformat()
    slot = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")

    LIVE_V76_STATUS["status"] = "scanning"
    live_log(
        "info",
        "[SCAN CYCLE] start",
        date=analysis_date,
        slot=slot,
        dry_run=DRY_RUN,
        locked_count=len(_v76_logic.LOCKED_STRATEGY_IDS),
    )

    halted, halt_reason = _circuit_breaker_live(
        st,
        scan_d,
        float(st.get("last_balance") or at.STARTING_BALANCE),
        0.0,
    )
    if halted:
        live_log("warning", "[SCAN CYCLE] halted", reason=halt_reason, halt_active=True)
        append_decision_log(
            {"date": analysis_date, "action": "HALT", "skip_reason": halt_reason, "halt_active": True},
        )
        publish_live_status(None, status="halted", halt_reason=halt_reason)
        return

    mt5 = None if DRY_RUN else at.ensure_mt5()
    if not DRY_RUN and not mt5:
        live_log("error", "[SCAN CYCLE] MT5 unavailable")
        publish_live_status(None, status="error", error="mt5_unavailable")
        return

    balance = float(st.get("last_balance") or at.STARTING_BALANCE)
    equity = balance
    if mt5:
        ai = mt5.account_info()
        if ai is not None:
            balance = float(ai.balance)
            equity = float(ai.equity)
    st["last_balance"] = balance
    st["last_equity"] = equity

    now = datetime.now(timezone.utc)
    dk = now.strftime("%Y-%m-%d")
    if st.get("day_key") != dk:
        st["day_key"] = dk
        st["day_anchor"] = equity
    day_anchor = float(st.get("day_anchor") or equity)
    day_pnl = equity - day_anchor
    st["last_daily_pnl"] = round(day_pnl, 2)

    period_mode = _live_period_mode(st, balance, scan_d)
    st["last_period_mode"] = period_mode
    job_id = os.environ.get("APEX_JOB_ID", "live")
    regime_ctx = _v76_logic.cached_regime(job_id, scan_d)
    live_log(
        "info",
        "[SCAN CYCLE] context",
        period_mode=period_mode,
        balance=round(balance, 2),
        equity=round(equity, 2),
        day_pnl=round(day_pnl, 2),
        regime=regime_ctx.get("regime"),
        regime_wr10=regime_ctx.get("wr_10"),
    )

    _v76_logic.CHRONO_DAY_PREFILTER_SIDS.clear()
    _v76_logic.CHRONO_SYMDIR_TFS.clear()
    _v76_logic.CHRONO_JPY_PAIRS_SIGNALLED.clear()
    _v76_logic.CHRONO_JPY_STORM_SNAPSHOT.clear()
    _v76_logic.CHRONO_JPY_RISK_DAY = 0.0

    scan_cells: list[tuple[str, str]] = []
    checked = 0
    for sym in TICKERS:
        for tf in TIMEFRAMES:
            checked += 1
            tf_key = tf.strip().lower()
            live_log("info", "[SCAN CHECK] ticker", ticker=sym.upper(), timeframe=tf)
            past = at.fetch_past_for_prefilter(sym, tf_key)
            if past is None or past.empty:
                live_log("info", "[SCAN CHECK] skip", ticker=sym.upper(), timeframe=tf, reason="no_ohlc")
                continue
            built = at.build_prefilter_inputs(past, sym)
            if built is None:
                live_log("info", "[SCAN CHECK] skip", ticker=sym.upper(), timeframe=tf, reason="indicators_failed")
                continue
            ind, price, zone_pct = built
            qualifies, qualifying, pre_reason = _v76_logic._v7_python_prefilter_bundle(
                sym,
                tf_key,
                float(price),
                ind,
                float(zone_pct),
                analysis_date=analysis_date,
                past=past,
            )
            if qualifies:
                _accumulate_live_prefilter(sym, tf_key, qualifying)
                scan_cells.append((sym, tf))
            else:
                live_log(
                    "info",
                    "[SCAN CHECK] no prefilter qualify",
                    ticker=sym.upper(),
                    timeframe=tf,
                    reason=pre_reason,
                    price=round(float(price), 5),
                )

    live_log(
        "info",
        "[SCAN CYCLE] prefilter pass complete",
        cells_checked=checked,
        cells_with_signals=len(scan_cells),
    )

    placed = 0
    skipped = 0
    if mt5:
        old_magic = at.APEX_MAGIC
        try:
            at.APEX_MAGIC = APEX_V76_MAGIC
            at.resolve_apex_hedged_same_pair(mt5)
            if len(at.open_apex_positions(mt5)) >= 15:
                live_log("warning", "[SCAN CYCLE] max 15 open positions — abort entries")
                publish_live_status(mt5, status="running", scan_aborted="max_positions")
                save_v76_state(st)
                return
        finally:
            at.APEX_MAGIC = old_magic

    for sym, tf in scan_cells:
        live_log("info", "[SCAN] evaluate entry", ticker=sym.upper(), timeframe=tf)
        result = evaluate_cell_v76(
            sym,
            tf,
            st=st,
            balance=balance,
            day_pnl=day_pnl,
            period_mode=period_mode,
            regime_ctx=regime_ctx,
            scan_d=scan_d,
        )
        if result is None:
            continue
        if isinstance(result, ScanSkip):
            skipped += 1
            live_log(
                "info",
                "[SKIP] trade blocked",
                ticker=sym.upper(),
                timeframe=tf,
                skip_reason=result.reason,
                **result.fields,
            )
            append_decision_log(
                {
                    "date": analysis_date,
                    "ticker": sym.upper(),
                    "timeframe": tf,
                    "action": "SKIP",
                    "skip_reason": result.reason,
                    **result.fields,
                },
            )
            continue

        plan: TradePlan = result
        live_log(
            "info",
            "[TAKE] trade plan approved",
            ticker=plan.sym,
            timeframe=plan.timeframe,
            strategy_id=plan.strategy_id,
            direction=plan.direction,
            macro_bias=plan.log_fields.get("macro_bias"),
            trend_strength=plan.log_fields.get("trend_strength"),
            regime=plan.log_fields.get("regime"),
            confidence=plan.log_fields.get("confidence"),
            confluence=plan.log_fields.get("strategy_confluence_count"),
            period_mode=plan.log_fields.get("period_mode"),
            trail_regime=plan.trail_regime,
            risk_usd=round(plan.risk_usd, 2),
            **_sizing_fields_from_ai(plan.ai),
        )
        bs = at.resolve_sym(mt5, sym) if mt5 else sym
        if not bs and not DRY_RUN:
            skipped += 1
            live_log("warning", "[SKIP] broker symbol resolve failed", ticker=sym.upper())
            continue

        if mt5:
            ok_opp, rs_opp = at.account_symbol_direction_conflict(mt5, bs, plan.direction)
            if ok_opp:
                skipped += 1
                live_log("info", "[SKIP] conflict", ticker=sym.upper(), timeframe=tf, skip_reason=rs_opp)
                append_decision_log(
                    {
                        "date": analysis_date,
                        "action": "SKIP",
                        "skip_reason": rs_opp,
                        **plan.log_fields,
                    },
                )
                continue

        out = order_send_v76(mt5, bs or sym, plan)
        if out.get("ok"):
            placed += 1
        else:
            skipped += 1

    if mt5:
        manage_trailing_v76(mt5)
    elif DRY_RUN:
        publish_live_status(None, status="dry_run")

    st["last_scan_slot"] = slot
    summary = {
        "placed": placed,
        "skipped": skipped,
        "cells_checked": checked,
        "cells_signalled": len(scan_cells),
        "period_mode": period_mode,
    }
    st["last_scan_summary"] = summary
    save_v76_state(st)
    live_log(
        "info",
        "[SCAN CYCLE] complete",
        placed=placed,
        skipped=skipped,
        dry_run=DRY_RUN,
        **summary,
    )
    publish_live_status(
        mt5,
        status="idle",
        last_scan_summary=summary,
        period_mode=period_mode,
    )


def main_loop_v76() -> None:
    st = load_v76_state()
    last_slot = str(st.get("last_scan_slot") or "")
    live_log(
        "info",
        "APEX v76 live trader starting",
        version=STRATEGY_VERSION,
        magic=APEX_V76_MAGIC,
        dry_run=DRY_RUN,
        log_file=str(live_v76_log_path()),
        locked_count=len(_v76_logic.LOCKED_STRATEGY_IDS),
    )
    publish_live_status(None, status="starting")
    if not DRY_RUN:
        at.emit_startup_diagnostics()

    adopted_positions = False
    while True:
        try:
            mt5 = None if DRY_RUN else at.ensure_mt5()
            if not DRY_RUN and not mt5:
                log_v76("MT5 reconnect in 30s", "warning")
                time.sleep(30)
                continue

            if not DRY_RUN and mt5 and not adopted_positions:
                _adopt_open_positions(mt5)
                adopted_positions = True

            now = datetime.now(timezone.utc)
            ran = False
            if now.minute == 0 and now.hour in SCAN_HOURS:
                slot = f"{now:%Y-%m-%d}-{now.hour:02d}"
                if slot != last_slot:
                    last_slot = slot
                    st = load_v76_state()
                    st["last_scan_slot"] = slot
                    save_v76_state(st)
                    run_full_scan_v76()
                    if mt5:
                        at.print_status_quick(mt5)
                    ran = True
            if not ran and mt5:
                try:
                    live_log("info", "[TRAIL CYCLE] periodic trailing pass")
                    manage_trailing_v76(mt5)
                except Exception as te:  # noqa: BLE001
                    live_log("warning", "[TRAIL CYCLE] error", error=str(te))
            elif not ran and DRY_RUN:
                publish_live_status(None, status="idle")
            time.sleep(60)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            log_v76(f"recover: {e}", "critical")
            time.sleep(60)


if __name__ == "__main__":
    main_loop_v76()
