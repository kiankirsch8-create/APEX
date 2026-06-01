"""
APEX v7.6 Live Trader — 1:1 mirror of ``continuous_backtester.py`` decision logic.

- Reuses backtest modules/functions (macro, trend, regime, calendar, prefilter, sizing, trailing).
- Execution via MetaTrader 5 (optional DRY_RUN logs decisions without orders).
- Does NOT modify the backtest engine or intelligence modules.

Deploy: copy to VPS (e.g. ``C:\\Apex\\apex_trader_v76.py``). Set ``APEX_MT5_PASSWORD``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# Live mode for macro_manager (must be set before importing continuous_backtester).
from macro_manager import set_backtest_mode

set_backtest_mode(False)

import continuous_backtester as cb  # noqa: E402  — backtest sets mode True on import; reset below

set_backtest_mode(False)

import apex_trader as at  # noqa: E402 — MT5 helpers only; trading logic is v76 + cb

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
    at.log_msg(f"[v76] {msg}", level)


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
    log_v76(msg, "warning")
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
        return ScanSkip(
            reason=str(res.get("skip_reason") or "skipped"),
            fields={k: res.get(k) for k in (
                "strategy_id", "macro_bias", "period_mode", "halt_active", "trend_strength",
            )},
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
        return ScanSkip(reason=f"Insufficient OHLC for {sym} {tf}")

    built = at.build_prefilter_inputs(past, sym)
    if built is None:
        return ScanSkip(reason=f"Indicator build failed {sym} {tf}")
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
    if not qualifies:
        return ScanSkip(reason=f"PreFilter: {_reason}")

    layer2 = [q for q in qualifying if str(q[0]).strip().upper() not in cb.LAYER1_STRATEGY_IDS]
    layer2_locked = _locked_layer2_only(layer2, tf_key=tf_key)
    if not layer2_locked:
        return ScanSkip(reason="No LOCKED strategies in Layer-2 qualifiers")

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
        log_v76(
            f"DRY_RUN {plan.sym} {plan.timeframe} {plan.strategy_id} {plan.direction} "
            f"risk=${plan.risk_usd:.0f} lots={lots:.2f} trail={plan.trail_regime}",
            "info",
        )
        return {"ok": True, "dry_run": True, "volume": lots, "entry": entry}

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
        row = dict(plan.log_fields)
        row["mt5_retcode"] = res.get("retcode") if isinstance(res, dict) else None
        row["action"] = "ORDER" if res.get("ok") else "ORDER_FAIL"
        row["lot_size"] = res.get("volume")
        row["magic_number"] = APEX_V76_MAGIC
        append_decision_log(row)
        return res
    finally:
        at.APEX_MAGIC = old_magic
        at.ORDER_COMMENT = old_comment


def manage_trailing_v76(mt5: Any) -> None:
    """Delegate to apex_trader trailing with v76 magic + ticket file."""
    old_magic = at.APEX_MAGIC
    old_meta_path = at.TICKET_META_FILE
    try:
        at.APEX_MAGIC = APEX_V76_MAGIC
        at.TICKET_META_FILE = V76_TICKET_META
        at.manage_trailing_live(mt5)
    finally:
        at.APEX_MAGIC = old_magic
        at.TICKET_META_FILE = old_meta_path


def run_full_scan_v76() -> None:
    cb._v72_load_strategy_status(log_startup=True)
    st = load_v76_state()
    scan_d = datetime.now(timezone.utc).date()
    analysis_date = scan_d.isoformat()

    halted, halt_reason = _circuit_breaker_live(
        st,
        scan_d,
        float(st.get("last_balance") or at.STARTING_BALANCE),
        0.0,
    )
    if halted:
        log_v76(halt_reason, "warning")
        append_decision_log(
            {"date": analysis_date, "action": "HALT", "skip_reason": halt_reason, "halt_active": True},
        )
        return

    mt5 = None if DRY_RUN else at.ensure_mt5()
    if not DRY_RUN and not mt5:
        log_v76("MT5 unavailable", "error")
        return

    balance = float(st.get("last_balance") or at.STARTING_BALANCE)
    equity = balance
    if mt5:
        ai = mt5.account_info()
        if ai is not None:
            balance = float(ai.balance)
            equity = float(ai.equity)
    st["last_balance"] = balance

    now = datetime.now(timezone.utc)
    dk = now.strftime("%Y-%m-%d")
    if st.get("day_key") != dk:
        st["day_key"] = dk
        st["day_anchor"] = equity
    day_anchor = float(st.get("day_anchor") or equity)
    day_pnl = equity - day_anchor

    period_mode = _live_period_mode(st, balance, scan_d)
    log_v76(f"period_mode={period_mode} balance={balance:.2f} day_pnl={day_pnl:.2f}", "info")

    job_id = os.environ.get("APEX_JOB_ID", "live")
    regime_ctx = cb.cached_regime(job_id, scan_d)

    # Reset scan-day confluence accumulators (mirror chrono day start).
    cb.CHRONO_DAY_PREFILTER_SIDS.clear()
    cb.CHRONO_SYMDIR_TFS.clear()
    cb.CHRONO_JPY_PAIRS_SIGNALLED.clear()
    cb.CHRONO_JPY_STORM_SNAPSHOT.clear()
    cb.CHRONO_JPY_RISK_DAY = 0.0

    scan_cells: list[tuple[str, str]] = []
    for sym in TICKERS:
        for tf in TIMEFRAMES:
            past = at.fetch_past_for_prefilter(sym, tf.strip().lower())
            if past is None or past.empty:
                continue
            built = at.build_prefilter_inputs(past, sym)
            if built is None:
                continue
            ind, price, zone_pct = built
            qualifies, qualifying, _ = cb._v7_python_prefilter_bundle(
                sym,
                tf.strip().lower(),
                float(price),
                ind,
                float(zone_pct),
                analysis_date=analysis_date,
                past=past,
            )
            if qualifies:
                _accumulate_live_prefilter(sym, tf.strip().lower(), qualifying)
                scan_cells.append((sym, tf))

    placed = 0
    skipped = 0
    if mt5:
        old_magic = at.APEX_MAGIC
        try:
            at.APEX_MAGIC = APEX_V76_MAGIC
            at.resolve_apex_hedged_same_pair(mt5)
            if len(at.open_apex_positions(mt5)) >= 15:
                log_v76("max 15 open positions", "warning")
                return
        finally:
            at.APEX_MAGIC = old_magic

    for sym, tf in scan_cells:
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
        bs = at.resolve_sym(mt5, sym) if mt5 else sym
        if not bs and not DRY_RUN:
            skipped += 1
            continue

        if mt5:
            ok_opp, rs_opp = at.account_symbol_direction_conflict(mt5, bs, plan.direction)
            if ok_opp:
                skipped += 1
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

    save_v76_state(st)
    log_v76(
        f"Scan complete {STRATEGY_VERSION} DRY_RUN={DRY_RUN}: placed={placed} skipped={skipped}",
        "info",
    )


def main_loop_v76() -> None:
    st = load_v76_state()
    last_slot = str(st.get("last_scan_slot") or "")
    log_v76(
        f"Starting {STRATEGY_VERSION} magic={APEX_V76_MAGIC} DRY_RUN={DRY_RUN} "
        f"locked={len(cb.LOCKED_STRATEGY_IDS)}",
        "info",
    )
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
                    manage_trailing_v76(mt5)
                except Exception as te:  # noqa: BLE001
                    log_v76(f"trailing: {te}", "warning")
            time.sleep(60)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            log_v76(f"recover: {e}", "critical")
            time.sleep(60)


if __name__ == "__main__":
    main_loop_v76()
