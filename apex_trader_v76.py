"""
APEX v7.6 Live Trader — 1:1 mirror of ``continuous_backtester.py`` decision logic.

- Reuses backtest modules/functions (macro, trend, regime, calendar, prefilter, sizing, trailing).
- Execution via MetaTrader 5 (optional DRY_RUN logs decisions without orders).
- Does NOT modify the backtest engine or intelligence modules.

Deploy: place this file in ``C:\\Apex`` next to ``continuous_backtester.py``, ``macro_manager.py``,
``apex_trader.py``, etc. Set ``APEX_MT5_PASSWORD``. Optional: ``APEX_HOME=C:\\Apex``.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
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
        f"Copy the full APEX repo (same as Railway) into C:\\Apex — "
        f"continuous_backtester.py and macro_manager.py must be present."
    ) from last_err


# Modules ``continuous_backtester`` imports at load time (preload so VPS path issues surface early).
_BACKTEST_DEPENDENCY_MODULES: tuple[str, ...] = (
    "utils",
    "strategies_v5_data",
    "prefilter_v6",
    "intelligence_fetch_cached",
    "calendar_manager",
    "trend_manager",
    "regime_manager",
    "macro_manager",
)


def _preload_backtest_dependencies() -> None:
    for mod_name in _BACKTEST_DEPENDENCY_MODULES:
        _import_local_module(mod_name)


def _import_continuous_backtester_module() -> ModuleType:
    _preload_backtest_dependencies()
    for root in _all_apex_roots():
        root_s = str(root)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
        py_path = root / "continuous_backtester.py"
        if py_path.is_file():
            if "continuous_backtester" in sys.modules:
                return sys.modules["continuous_backtester"]
            try:
                return importlib.import_module("continuous_backtester")
            except ModuleNotFoundError:
                return _load_module_from_file("continuous_backtester", py_path)
    missing = [m for m in ("continuous_backtester", *_BACKTEST_DEPENDENCY_MODULES) if not _find_module_py(m)]
    roots_hint = ", ".join(str(r) for r in _all_apex_roots())
    raise ModuleNotFoundError(
        "Cannot load backtest engine on VPS. Missing modules: "
        + ", ".join(missing)
        + f". Install dir(s) checked: {roots_hint}. "
        "Download the latest main branch from GitHub into C:\\Apex "
        "(must include continuous_backtester.py)."
    )


_APEX_ROOT = _bootstrap_apex_sys_path()

# Default data dir on Windows VPS when not set (matches apex_trader / backtest JSON paths).
if os.name == "nt" and not (os.environ.get("APEX_DATA_DIR") or "").strip():
    os.environ.setdefault("APEX_DATA_DIR", str(_APEX_ROOT))

# Third-party (same stack as apex_trader / continuous_backtester).
try:
    import pandas as pd  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError(
        "pandas is required. Install: pip install pandas numpy yfinance pandas-ta anthropic"
    ) from e

try:
    import numpy as np  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError("numpy is required (pip install numpy)") from e

try:
    import yfinance as yf  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError("yfinance is required (pip install yfinance)") from e

try:
    import pandas_ta  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError(
        "pandas-ta is required by continuous_backtester (pip install pandas-ta)"
    ) from e

try:
    from anthropic import Anthropic  # noqa: F401
except ImportError as e:  # noqa: BLE001
    raise ImportError(
        "anthropic is required by continuous_backtester (pip install anthropic)"
    ) from e

# Live mode for macro_manager (must be set before importing continuous_backtester).
_macro_manager = _import_local_module("macro_manager", _APEX_ROOT)
set_backtest_mode = _macro_manager.set_backtest_mode

set_backtest_mode(False)

cb = _import_continuous_backtester_module()

set_backtest_mode(False)

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
    trailing_5d = cb._v76_rolling_5d_pnl(daily_rows, as_of=scan_d, current_day_pnl=day_pnl)
    cap = float(balance or cb.STARTING_CAPITAL)
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
    saved = cb.CHRONO_COMPLETED_TRADES_BUFFER
    try:
        cb.CHRONO_COMPLETED_TRADES_BUFFER = buf
        pm, _, _ = cb._detect_period_mode(balance, "live", scan_d)
        return pm
    finally:
        cb.CHRONO_COMPLETED_TRADES_BUFFER = saved


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
        if sid not in cb.LOCKED_STRATEGY_IDS or not cb._v75_backtest_strategy_allowed(sid):
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
            cb.CHRONO_DAY_PREFILTER_SIDS[(sym_u, "LONG")].add(sid)
            cb.CHRONO_DAY_PREFILTER_SIDS[(sym_u, "SHORT")].add(sid)
        elif dr in ("LONG", "SHORT"):
            cb.CHRONO_DAY_PREFILTER_SIDS[(sym_u, dr)].add(sid)
        cb.CHRONO_SYMDIR_TFS[(sym_u, dr if dr in ("LONG", "SHORT") else "LONG")].add(tf_key)
        if sym_u in cb.JPY_STORM_PAIRS:
            cb.CHRONO_JPY_PAIRS_SIGNALLED.add(sym_u)


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
    is_exotic = sym in cb.EXOTIC_REDUCE
    if not layer2_locked:
        return ScanSkip(reason="No LOCKED Layer-2 candidate after filters")

    picked = cb._layer2_tuple_for_deterministic_pick(sym, tf_key, zone_pct, layer2_locked)
    if picked is None:
        return ScanSkip(reason="No Layer 2 pick after deterministic ordering")

    d_pick = str(picked[1]).strip().upper()
    if d_pick == "BOTH":
        d_pick = "LONG" if float(zone_pct) < 50.0 else "SHORT"
    if d_pick not in ("LONG", "SHORT"):
        return ScanSkip(reason=f"Invalid direction for {picked[0]}")

    if tf_key == "4h" and str(picked[0]).strip().upper() == "M02_MACD_ZERO_CROSS":
        return ScanSkip(reason="M02 blocked on 4h timeframe")

    # Build a one-row future frame so we can call backtest Layer-2 executor, then strip sim outcome.
    try:
        last_row = past.iloc[-1:].copy()
        fut_stub = last_row.copy()
    except Exception:  # noqa: BLE001
        return ScanSkip(reason="Cannot build forward stub from OHLC")

    res = cb._python_forced_layer2_trade(
        sym=sym,
        timeframe=timeframe,
        analysis_date=analysis_date,
        tf_key=tf_key,
        price=float(price),
        zone_pct=zone_pct,
        zone_label=zone_label,
        is_exotic=is_exotic,
        future=fut_stub,
        layer2=[picked],
        rsi_live=float(ind.get("rsi", 50) or 50),
        chrono_risk_mult=chrono_risk_mult,
        chrono_tp_mult=chrono_tp_mult,
        chrono_job=True,
        calendar_risk=cb.cached_calendar_historical(sym, date.fromisoformat(analysis_date[:10])),
        regime_ctx=regime_ctx,
        chrono_balance=balance,
        chrono_day_pnl=day_pnl,
        period_mode=period_mode,
        atr_ref=float(ind.get("atr", 0) or 0) or None,
        past=past,
        ind=ind,
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
    trail_reg = cb._resolve_trailing_regime(mb, ts, rd, timeframe=timeframe)
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
            "dry_run": DRY_RUN,
        },
    )
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

    qualifies, qualifying, _reason = cb._v7_python_prefilter_bundle(
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

    layer2 = [q for q in qualifying if str(q[0]).strip().upper() not in cb.LAYER1_STRATEGY_IDS]
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
        }
        meta.update(cb.merged_macro_result_fields(plan.ai))
        res = at.order_send_live(mt5, broker_sym, plan.direction, plan.stop_loss, plan.risk_usd, meta)
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


def manage_trailing_v76(mt5: Any) -> None:
    """Trailing with deep logging (before/after each pass)."""
    _log_positions_trailing_phase(mt5, phase="BEFORE")
    old_magic = at.APEX_MAGIC
    old_meta_path = at.TICKET_META_FILE
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        at.TICKET_META_FILE = V76_TICKET_META
        at.manage_trailing_live(mt5)
    finally:
        at.APEX_MAGIC = old_magic
        at.TICKET_META_FILE = old_meta_path
    _log_positions_trailing_phase(mt5, phase="AFTER")
    publish_live_status(mt5, status="running")


def run_full_scan_v76() -> None:
    cb._v72_load_strategy_status(log_startup=True)
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
        locked_count=len(cb.LOCKED_STRATEGY_IDS),
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
    regime_ctx = cb.cached_regime(job_id, scan_d)
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

    cb.CHRONO_DAY_PREFILTER_SIDS.clear()
    cb.CHRONO_SYMDIR_TFS.clear()
    cb.CHRONO_JPY_PAIRS_SIGNALLED.clear()
    cb.CHRONO_JPY_STORM_SNAPSHOT.clear()
    cb.CHRONO_JPY_RISK_DAY = 0.0

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
            qualifies, qualifying, pre_reason = cb._v7_python_prefilter_bundle(
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
        locked_count=len(cb.LOCKED_STRATEGY_IDS),
    )
    publish_live_status(None, status="starting")
    if not DRY_RUN:
        at.emit_startup_diagnostics()

    while True:
        try:
            mt5 = None if DRY_RUN else at.ensure_mt5()
            if not DRY_RUN and not mt5:
                log_v76("MT5 reconnect in 30s", "warning")
                time.sleep(30)
                continue

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
