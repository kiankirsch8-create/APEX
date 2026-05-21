"""MetaTrader 5 live execution bridge for APEX forex-style signals.

This module connects to an MT5 terminal, enforces account risk limits, sends
market orders, and manages trailing stops for positions tagged with comment
``APEX``. Import it from your scan pipeline **after** a signal is produced and
call :func:`emit_apex_signal_for_mt5` instead of (or in addition to) persisting
only to JSON — the scan / backtest code itself can stay unchanged apart from
that single hook.

**Security:** never commit account passwords. Set ``APEX_MT5_PASSWORD`` in the
environment. Login and server default to the demo values you provided but can
be overridden with ``APEX_MT5_LOGIN`` and ``APEX_MT5_SERVER``.

**Platform:** the official ``MetaTrader5`` Python wheel targets Windows with a
local MT5 terminal installed. Use ``pip install MetaTrader5`` there. On Linux
servers this module will no-op until MT5 is available.

Schedulers (hourly status, 15-minute trailing) start when you call
:func:`start_mt5_background_services` (typically from your app lifespan or a
dedicated process entrypoint).
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from utils import DATA_DIR, log

# ---------------------------------------------------------------------------
# Credentials & constants (override via environment in production)
# ---------------------------------------------------------------------------

DEFAULT_MT5_LOGIN: int = 107356886
DEFAULT_MT5_SERVER: str = "MetaQuotes-Demo"
APEX_MAGIC: int = 107356887  # distinct from login; identifies APEX orders
ORDER_COMMENT: str = "APEX"

DAILY_LOSS_LIMIT_USD: float = 4000.0
TOTAL_LOSS_LIMIT_USD: float = 8000.0
PHASE1_PROFIT_USD: float = 10000.0

RISK_STATE_FILE: Path = DATA_DIR / "apex_mt5_risk_state.json"
TICKET_STATE_FILE: Path = DATA_DIR / "apex_mt5_ticket_state.json"

_mt5_module: Any = None
_mt5_lock = threading.RLock()
_services_started = False
_status_thread: threading.Thread | None = None
_trailing_thread: threading.Thread | None = None

_trading_halted_daily = False
_trading_halted_total = False


def _lazy_mt5() -> Any:
    global _mt5_module
    if _mt5_module is None:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]

        _mt5_module = mt5
    return _mt5_module


def _env_int(name: str, default: int) -> int:
    import os

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    import os

    v = os.environ.get(name)
    return default if v is None or not str(v).strip() else str(v).strip()


def _mt5_password() -> str:
    import os

    pw = os.environ.get("APEX_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD")
    return (pw or "").strip()


def connect_mt5() -> bool:
    """Initialize the MT5 terminal and log in to the configured account."""
    try:
        mt5 = _lazy_mt5()
    except ImportError as e:
        log(f"[MT5] MetaTrader5 package not installed: {e}", level="error")
        return False
    pw = _mt5_password()
    if not pw:
        log("[MT5] APEX_MT5_PASSWORD (or MT5_PASSWORD) is not set — cannot connect", level="warning")
        return False

    login = _env_int("APEX_MT5_LOGIN", DEFAULT_MT5_LOGIN)
    server = _env_str("APEX_MT5_SERVER", DEFAULT_MT5_SERVER)

    import os

    path = os.environ.get("APEX_MT5_PATH") or os.environ.get("MT5_PATH")
    kwargs: dict[str, Any] = {
        "login": login,
        "password": pw,
        "server": server,
    }
    if path:
        kwargs["path"] = path

    if not mt5.initialize(**kwargs):
        err = mt5.last_error()
        log(f"[MT5] initialize failed: {err}", level="error")
        return False

    if not mt5.login(login, password=pw, server=server):
        err = mt5.last_error()
        log(f"[MT5] login failed: {err}", level="error")
        mt5.shutdown()
        return False

    log(f"[MT5] Connected login={login} server={server}", level="info")
    _ensure_risk_anchor()
    return True


def shutdown_mt5() -> None:
    try:
        mt5 = _lazy_mt5()
        mt5.shutdown()
    except Exception as e:  # noqa: BLE001
        log(f"[MT5] shutdown: {e}", level="warning")


# ---------------------------------------------------------------------------
# Risk state (daily / lifetime vs anchors)
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_risk_anchor() -> None:
    """Seed anchor equity and start-of-day marker on first successful connect."""
    mt5 = _lazy_mt5()
    ai = mt5.account_info()
    if ai is None:
        return
    eq = float(ai.equity)
    state = _load_json(RISK_STATE_FILE, {})
    changed = False
    if state.get("anchor_equity") is None:
        state["anchor_equity"] = eq
        changed = True
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("equity_day_key") != day or state.get("equity_day_start") is None:
        state["equity_day_key"] = day
        state["equity_day_start"] = eq
        changed = True
    if changed:
        _save_json(RISK_STATE_FILE, state)


def _rollover_day_if_needed() -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = _load_json(RISK_STATE_FILE, {})
    if state.get("equity_day_key") != day:
        mt5 = _lazy_mt5()
        ai = mt5.account_info()
        eq = float(ai.equity) if ai is not None else float(state.get("equity_day_start", 0.0))
        state["equity_day_key"] = day
        state["equity_day_start"] = eq
        global _trading_halted_daily
        _trading_halted_daily = False
        _save_json(RISK_STATE_FILE, state)
        log(f"[MT5] New UTC day {day} — reset daily loss gate (equity_day_start={eq:.2f})", level="info")


def _risk_snapshot() -> tuple[float, float, float, float]:
    """Return (equity, daily_pnl, total_pnl, floating)."""
    mt5 = _lazy_mt5()
    ai = mt5.account_info()
    if ai is None:
        return 0.0, 0.0, 0.0, 0.0
    equity = float(ai.equity)
    floating = float(ai.profit)
    state = _load_json(RISK_STATE_FILE, {})
    anchor = float(state.get("anchor_equity", equity))
    day_start = float(state.get("equity_day_start", equity))
    total_pnl = equity - anchor
    daily_pnl = equity - day_start
    return equity, daily_pnl, total_pnl, floating


def _update_risk_gates() -> None:
    """Set halt flags and Phase-1 message from configured USD thresholds."""
    global _trading_halted_daily, _trading_halted_total
    _rollover_day_if_needed()
    _, daily_pnl, total_pnl, _ = _risk_snapshot()
    state = _load_json(RISK_STATE_FILE, {})

    if daily_pnl <= -DAILY_LOSS_LIMIT_USD:
        if not _trading_halted_daily:
            log(
                f"[MT5] Daily loss limit reached (daily PnL {daily_pnl:.2f} <= -{DAILY_LOSS_LIMIT_USD:.0f}) — "
                "new trades disabled until next UTC day",
                level="error",
            )
        _trading_halted_daily = True

    if total_pnl <= -TOTAL_LOSS_LIMIT_USD:
        if not _trading_halted_total:
            log(
                f"[MT5] Total loss limit reached (total PnL {total_pnl:.2f} <= -{TOTAL_LOSS_LIMIT_USD:.0f}) — "
                "all new trading disabled until manual reset of risk state file",
                level="error",
            )
        _trading_halted_total = True

    if total_pnl >= PHASE1_PROFIT_USD and not state.get("phase1_complete_printed"):
        print(f"Phase 1 complete — equity profit vs anchor >= ${PHASE1_PROFIT_USD:,.0f} (total PnL {total_pnl:.2f})")
        log(
            f"[MT5] Phase 1 complete — total PnL {total_pnl:.2f} >= {PHASE1_PROFIT_USD:.0f}",
            level="info",
        )
        state["phase1_complete_printed"] = True
        _save_json(RISK_STATE_FILE, state)


def trading_allowed() -> tuple[bool, str]:
    """Return whether new risk checks allow opening another trade."""
    _update_risk_gates()
    if _trading_halted_total:
        return False, "halted_total_loss"
    if _trading_halted_daily:
        return False, "halted_daily_loss"
    return True, ""


def reset_trading_halts(*, reset_anchor: bool = False) -> None:
    """Operator escape hatch: clear halt flags (and optionally re-anchor equity)."""
    global _trading_halted_daily, _trading_halted_total
    _trading_halted_daily = False
    _trading_halted_total = False
    state = _load_json(RISK_STATE_FILE, {})
    if reset_anchor:
        mt5 = _lazy_mt5()
        ai = mt5.account_info()
        if ai is not None:
            state["anchor_equity"] = float(ai.equity)
            state["equity_day_start"] = float(ai.equity)
            state["equity_day_key"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.pop("phase1_complete_printed", None)
    _save_json(RISK_STATE_FILE, state)
    log("[MT5] Risk halts cleared (reset_anchor=%s)" % reset_anchor, level="info")


# ---------------------------------------------------------------------------
# Ticket metadata for trailing (comment is only ``APEX`` on the order)
# ---------------------------------------------------------------------------


def _ticket_state() -> dict[str, Any]:
    return _load_json(TICKET_STATE_FILE, {})


def _set_ticket_state(ticket: int, meta: dict[str, Any]) -> None:
    st = _ticket_state()
    st[str(ticket)] = meta
    _save_json(TICKET_STATE_FILE, st)


def _remove_ticket_state(ticket: int) -> None:
    st = _ticket_state()
    st.pop(str(ticket), None)
    _save_json(TICKET_STATE_FILE, st)


# ---------------------------------------------------------------------------
# Symbol / volume helpers
# ---------------------------------------------------------------------------


def _resolve_symbol(mt5: Any, symbol: str) -> str | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    if mt5.symbol_select(sym, True):
        return sym
    # Common broker suffix fallbacks
    for suf in (".", "m", ".a", "pro"):
        cand = f"{sym}{suf}"
        if mt5.symbol_select(cand, True):
            return cand
    log(f"[MT5] Symbol not found in terminal: {sym}", level="error")
    return None


def _filling_mode(mt5: Any, sym: str) -> int:
    info = mt5.symbol_info(sym)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm & mt5.ORDER_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if fm & mt5.ORDER_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _normalize_volume(mt5: Any, sym: str, volume: float) -> float:
    info = mt5.symbol_info(sym)
    if info is None:
        return round(volume, 2)
    step = float(info.volume_step or 0.01) or 0.01
    vmin = float(info.volume_min or 0.01)
    vmax = float(info.volume_max or 100.0)
    steps = math.floor(volume / step + 1e-9)
    v = max(vmin, min(vmax, steps * step))
    return round(v, int(max(0, -math.floor(math.log10(step)))))


def _money_per_lot_to_sl(mt5: Any, sym: str, entry: float, sl: float) -> float | None:
    """Estimate loss in account currency for 1.0 lot if SL is hit (absolute)."""
    info = mt5.symbol_info(sym)
    if info is None:
        return None
    tick_size = float(info.trade_tick_size or info.point or 0.0)
    tick_value = float(info.trade_tick_value or 0.0)
    if tick_size <= 0 or tick_value <= 0:
        return None
    ticks = abs(entry - sl) / tick_size
    return ticks * tick_value


def _volume_for_risk(mt5: Any, sym: str, entry: float, sl: float, risk_dollars: float) -> float:
    mpl = _money_per_lot_to_sl(mt5, sym, entry, sl)
    if mpl is None or mpl <= 0:
        log("[MT5] Could not derive money-per-lot; using volume_min", level="warning")
        info = mt5.symbol_info(sym)
        return float(info.volume_min) if info else 0.01
    vol = risk_dollars / mpl
    return _normalize_volume(mt5, sym, vol)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_trade(
    symbol: str,
    direction: str,
    stop_loss: float,
    tp1: float,
    risk_dollars: float,
    tp2: float | None = None,
) -> dict[str, Any]:
    """Send a market order with stop loss; size lots from ``risk_dollars`` and SL distance.

    TP2 is optional metadata for :func:`manage_trailing_stops` (not sent as broker TP).
    """
    mt5 = _lazy_mt5()
    ok, reason = trading_allowed()
    if not ok:
        msg = f"[MT5] execute_trade blocked ({reason})"
        log(msg, level="warning")
        return {"ok": False, "error": reason}

    sym = _resolve_symbol(mt5, symbol)
    if sym is None:
        return {"ok": False, "error": "symbol_not_found"}

    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return {"ok": False, "error": "no_tick"}

    d = (direction or "").strip().upper()
    if d not in ("LONG", "SHORT"):
        return {"ok": False, "error": "bad_direction"}

    entry = float(tick.ask if d == "LONG" else tick.bid)
    sl = float(stop_loss)
    r = abs(entry - sl)
    if r <= 0:
        return {"ok": False, "error": "zero_stop_distance"}

    vol = _volume_for_risk(mt5, sym, entry, sl, float(risk_dollars))
    if vol <= 0:
        return {"ok": False, "error": "volume_zero"}

    order_type = mt5.ORDER_TYPE_BUY if d == "LONG" else mt5.ORDER_TYPE_SELL
    price = float(tick.ask if d == "LONG" else tick.bid)
    filling = _filling_mode(mt5, sym)

    request: dict[str, Any] = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": vol,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": 0.0,
        "deviation": 25,
        "magic": APEX_MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    with _mt5_lock:
        result = mt5.order_send(request)

    if result is None:
        log("[MT5] order_send returned None", level="error")
        return {"ok": False, "error": "order_send_none"}

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"[MT5] order_send failed retcode={result.retcode} comment={result.comment}", level="error")
        return {"ok": False, "error": f"retcode_{result.retcode}", "comment": result.comment}

    # Find opened position ticket
    ticket: int | None = None
    time.sleep(0.15)
    positions = mt5.positions_get(symbol=sym) or []
    for p in positions:
        if int(getattr(p, "magic", 0) or 0) == APEX_MAGIC and (p.comment or "") == ORDER_COMMENT:
            ticket = int(p.ticket)
            entry = float(p.price_open)
            sl = float(p.sl)
            r = abs(entry - sl)
            break

    if ticket is not None:
        meta = {
            "symbol": sym,
            "direction": d,
            "entry": entry,
            "sl_initial": sl,
            "r": r,
            "tp1": float(tp1),
            "tp2": float(tp2) if tp2 is not None else None,
            "hit_tp1": False,
            "hit_tp2": False,
        }
        _set_ticket_state(ticket, meta)

    log(
        f"[MT5] execute_trade ok sym={sym} dir={d} vol={vol} entry={entry:.5f} sl={sl:.5f} "
        f"tp1={tp1} risk=${risk_dollars:.2f} ticket={ticket}",
        level="info",
    )
    return {
        "ok": True,
        "symbol": sym,
        "volume": vol,
        "entry": entry,
        "sl": sl,
        "ticket": ticket,
        "retcode": int(result.retcode),
    }


def manage_trailing_stops() -> None:
    """For all open ``APEX`` positions, move SL to BE+0.75R after TP1, then to 1.5R after TP2."""
    try:
        mt5 = _lazy_mt5()
    except Exception as e:  # noqa: BLE001
        log(f"[MT5] manage_trailing_stops import: {e}", level="debug")
        return
    if not mt5.terminal_info():
        return

    state = _ticket_state()
    positions = mt5.positions_get() or []

    for pos in positions:
        if int(getattr(pos, "magic", 0) or 0) != APEX_MAGIC:
            continue
        if (pos.comment or "").strip() != ORDER_COMMENT:
            continue
        ticket = int(pos.ticket)
        meta = state.get(str(ticket))
        if not isinstance(meta, dict):
            continue

        sym = str(meta.get("symbol") or pos.symbol)
        d = str(meta.get("direction") or "").upper()
        entry = float(meta.get("entry", pos.price_open))
        r = float(meta.get("r", 0.0) or abs(entry - float(pos.sl or entry)))
        if r <= 0:
            continue
        tp1 = float(meta["tp1"])
        tp2_raw = meta.get("tp2")
        tp2 = float(tp2_raw) if tp2_raw is not None else None

        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            continue

        cur_sl = float(pos.sl or 0.0)
        hit_tp1 = bool(meta.get("hit_tp1"))
        hit_tp2 = bool(meta.get("hit_tp2"))

        bid = float(tick.bid)
        ask = float(tick.ask)

        new_sl: float | None = None
        mark_tp1 = False
        mark_tp2 = False
        if d == "LONG":
            if not hit_tp1 and bid >= tp1:
                new_sl = entry + 0.75 * r
                mark_tp1 = True
            elif hit_tp1 and tp2 is not None and not hit_tp2 and bid >= tp2:
                new_sl = entry + 1.5 * r
                mark_tp2 = True
        else:  # SHORT
            if not hit_tp1 and ask <= tp1:
                new_sl = entry - 0.75 * r
                mark_tp1 = True
            elif hit_tp1 and tp2 is not None and not hit_tp2 and ask <= tp2:
                new_sl = entry - 1.5 * r
                mark_tp2 = True

        if new_sl is None:
            _set_ticket_state(ticket, meta)
            continue

        # Do not loosen stop
        if d == "LONG" and new_sl <= cur_sl:
            _set_ticket_state(ticket, meta)
            continue
        if d == "SHORT" and new_sl >= cur_sl and cur_sl > 0:
            _set_ticket_state(ticket, meta)
            continue

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": sym,
            "position": ticket,
            "sl": float(new_sl),
            "tp": float(pos.tp) if pos.tp else 0.0,
        }
        with _mt5_lock:
            res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            if mark_tp1:
                meta["hit_tp1"] = True
            if mark_tp2:
                meta["hit_tp2"] = True
            log(f"[MT5] Trailing update ticket={ticket} new_sl={new_sl:.5f}", level="info")
            _set_ticket_state(ticket, meta)
        else:
            err = getattr(res, "comment", "") if res else ""
            log(f"[MT5] Trailing update failed ticket={ticket} err={err}", level="warning")


def print_status() -> None:
    """Log balance, equity, daily/total PnL, and open APEX positions."""
    try:
        mt5 = _lazy_mt5()
    except Exception as e:  # noqa: BLE001
        print(f"[MT5] status unavailable: {e}")
        return
    ai = mt5.account_info()
    if ai is None:
        print("[MT5] account_info unavailable")
        return
    equity, daily_pnl, total_pnl, floating = _risk_snapshot()
    balance = float(ai.balance)
    print(
        f"[MT5] balance={balance:.2f} equity={equity:.2f} floating={floating:.2f} "
        f"daily_pnl={daily_pnl:.2f} total_pnl={total_pnl:.2f}"
    )
    positions = mt5.positions_get() or []
    rows = [p for p in positions if int(getattr(p, "magic", 0) or 0) == APEX_MAGIC]
    if not rows:
        print("[MT5] open APEX positions: none")
        return
    print(f"[MT5] open APEX positions: {len(rows)}")
    for p in rows:
        print(
            f"  ticket={p.ticket} sym={p.symbol} vol={p.volume} type={p.type} "
            f"price_open={p.price_open:.5f} sl={p.sl:.5f} profit={p.profit:.2f} comment={p.comment!r}"
        )


def emit_apex_signal_for_mt5(
    trade: dict[str, Any],
    *,
    risk_dollars: float | None = None,
) -> dict[str, Any]:
    """Translate an APEX scan / backtest row (``ai``-shaped dict) into :func:`execute_trade`.

    Expected keys (any subset may be nested under ``ai``):

    - ``ticker`` / ``symbol``
    - ``direction`` — LONG / SHORT
    - ``stop_loss`` / ``sl``
    - ``tp1``
    - ``tp2`` (optional)
    - ``skip_trade`` — if truthy, no order is sent
    - ``_max_risk_dollars`` or ``risk_dollars`` for sizing

    This is the integration point: call once at the end of your scan when you
    would otherwise only append JSON.
    """
    src = trade.get("ai") if isinstance(trade.get("ai"), dict) else trade
    if not isinstance(src, dict):
        return {"ok": False, "error": "not_a_dict"}

    if src.get("skip_trade"):
        return {"ok": False, "error": "skip_trade"}

    sym = str(src.get("ticker") or src.get("symbol") or trade.get("ticker") or "").strip()
    direction = str(src.get("direction") or "").strip().upper()
    try:
        sl = float(src.get("stop_loss") or src.get("sl") or 0.0)
        tp1 = float(src.get("tp1") or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_prices"}

    tp2_v = src.get("tp2")
    try:
        tp2 = float(tp2_v) if tp2_v is not None else None
    except (TypeError, ValueError):
        tp2 = None

    rd = risk_dollars
    if rd is None:
        try:
            rd = float(src.get("_max_risk_dollars") or src.get("risk_dollars") or 0.0)
        except (TypeError, ValueError):
            rd = 0.0
    if rd is None or rd <= 0:
        return {"ok": False, "error": "risk_dollars_missing"}

    if not sym or direction not in ("LONG", "SHORT") or sl <= 0 or tp1 <= 0:
        return {"ok": False, "error": "incomplete_signal"}

    return execute_trade(sym, direction, sl, tp1, rd, tp2=tp2)


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


def _loop_every(name: str, interval_sec: float, fn: Callable[[], None]) -> None:
    log(f"[MT5] background loop '{name}' started (every {interval_sec}s)", level="info")
    while True:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"[MT5] {name} error: {e}", level="error")
        time.sleep(interval_sec)


def start_mt5_background_services(*, connect: bool = True) -> bool:
    """Start hourly :func:`print_status` and 15-minute :func:`manage_trailing_stops` threads."""
    global _services_started, _status_thread, _trailing_thread
    if _services_started:
        return True
    if connect and not connect_mt5():
        return False

    _status_thread = threading.Thread(
        target=_loop_every,
        args=("print_status", 3600.0, print_status),
        daemon=True,
        name="mt5_status_hourly",
    )
    _trailing_thread = threading.Thread(
        target=_loop_every,
        args=("manage_trailing_stops", 900.0, manage_trailing_stops),
        daemon=True,
        name="mt5_trailing_15m",
    )
    _status_thread.start()
    _trailing_thread.start()
    _services_started = True
    return True


def main() -> None:
    """CLI entry: connect, print status once, then hourly status + 15m trailing."""
    if not connect_mt5():
        raise SystemExit(2)
    print_status()
    start_mt5_background_services(connect=False)
    # Keep main thread alive
    while True:
        time.sleep(60.0)


if __name__ == "__main__":
    main()
