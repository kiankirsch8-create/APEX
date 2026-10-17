#!/usr/bin/env python3
"""Manual one-shot tester for the live private engine (apex_trader_v76_private).

Additive only — does not change trading logic or the scheduled main loop.
Reuses the engine's real startup (env MT5 login, state load, PROFILE=ftmo,
guardrails, demo gate).

Modes:
  default           Run exactly one entry scan cycle now (bypass SCAN_HOURS wait).
  --plumbing-test   Place ONE minimal-size market order with SL on a DEMO account,
                    confirm broker SL, then close it immediately. Skips strategy.

Examples (VPS, MT5 open, env set):
  py tools/scan_once.py --plumbing-test
  py tools/scan_once.py
  py tools/scan_once.py --dry-run-hint   # print DRY_RUN / PROFILE / login context only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Engine import performs the real bootstrap (path, PROFILE, CFG, MT5 helpers).
import apex_trader_v76_private as eng  # noqa: E402
import ftmo_demo_gate as gate  # noqa: E402


PLUMBING_SYMBOL_DEFAULT = "EURUSD"
PLUMBING_COMMENT = "APEX76P_PROBE"


def _print(msg: str = "", **fields: Any) -> None:
    if fields:
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and v != "")
        print(f"{msg} | {extra}" if extra else msg, flush=True)
    else:
        print(msg, flush=True)


def _startup_banner() -> dict[str, Any]:
    """Mirror main_loop_v76 header: state load, profile, halt, sizing rebuild."""
    st = eng.load_v76_state()
    halt_raw = str(st.get("circuit_halt_until") or "").strip()
    demo_clean = False
    try:
        demo_clean = bool(gate.is_demo_clean())
    except Exception:  # noqa: BLE001
        demo_clean = False

    login = os.environ.get("APEX_MT5_LOGIN") or os.environ.get("MT5_LOGIN") or ""
    server = os.environ.get("APEX_MT5_SERVER") or os.environ.get("MT5_SERVER") or ""
    ctx = {
        "profile": eng.PROFILE,
        "dry_run": eng.DRY_RUN,
        "magic": eng.APEX_V76_MAGIC,
        "login_env": login or None,
        "server_env": server or None,
        "circuit_halt_until": halt_raw or None,
        "ftmo_demo_clean": demo_clean,
        "challenge_login_blocked": not demo_clean,
    }
    eng.live_log(
        "info",
        "[SCAN_ONCE] starting",
        version=eng.STRATEGY_VERSION,
        **ctx,
    )
    if halt_raw:
        eng.live_log(
            "warning",
            "[HALT] reloaded circuit_halt_until from state (crash-safe)",
            circuit_halt_until=halt_raw,
        )
    eng.publish_live_status(None, status="scan_once_starting", circuit_halt_until=halt_raw or None)
    eng.rebuild_live_sizing_health_from_forensic()
    if not eng.DRY_RUN:
        eng.at.emit_startup_diagnostics()
    _print("[SCAN_ONCE] context", **ctx)
    return st


def _collect_decision_rows(start_offset: int) -> list[dict[str, Any]]:
    """Read decision-log lines appended after ``start_offset`` bytes."""
    path = eng.V76_DECISION_LOG
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(max(0, int(start_offset)))
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as e:
        _print(f"[SCAN_ONCE] decision log read failed: {e}")
    return rows


def _summarize_scan(rows: list[dict[str, Any]], summary: dict[str, Any] | None) -> None:
    _print()
    _print("=== SCAN_ONCE decisions ===")
    if not rows:
        _print("(no decision-log rows appended this cycle — see live log for SCAN CHECK skips)")
    taken = 0
    rejected = 0
    other = 0
    for row in rows:
        action = str(row.get("action") or "").strip().upper()
        ticker = str(row.get("ticker") or row.get("sym") or "?").upper()
        tf = str(row.get("timeframe") or row.get("tf") or "")
        sid = str(row.get("strategy_id") or "")
        if action in ("ORDER", "DRY_RUN", "TAKE"):
            taken += 1
            _print(
                f"  TAKEN   {ticker:8} {tf:3} {sid:28}",
                action=action,
                direction=row.get("direction"),
                risk_usd=row.get("risk_usd") or row.get("final_risk_usd"),
                lot=row.get("lot_size"),
                ticket=row.get("ticket"),
            )
        elif action in ("SKIP", "HALT", "ORDER_FAIL"):
            rejected += 1
            reason = row.get("skip_reason") or row.get("error") or row.get("reason") or action
            _print(
                f"  REJECT  {ticker:8} {tf:3} {sid:28}",
                reason=str(reason)[:120],
            )
        else:
            other += 1
            _print(f"  OTHER   {ticker:8} {tf:3}", action=action)

    _print()
    _print("=== SCAN_ONCE summary ===")
    if summary:
        _print(
            "engine summary",
            placed=summary.get("placed"),
            skipped=summary.get("skipped"),
            cells_checked=summary.get("cells_checked"),
            cells_signalled=summary.get("cells_signalled"),
            period_mode=summary.get("period_mode"),
            dry_run=eng.DRY_RUN,
        )
    _print(
        "decision-log tally",
        taken=taken,
        rejected=rejected,
        other=other,
        dry_run=eng.DRY_RUN,
        profile=eng.PROFILE,
    )


def run_one_scan() -> int:
    """Bypass SCAN_HOURS — invoke the same ``run_full_scan_v76`` the scheduler uses."""
    st = _startup_banner()
    log_path = eng.V76_DECISION_LOG
    start_offset = log_path.stat().st_size if log_path.is_file() else 0

    mt5 = None if eng.DRY_RUN else eng.at.ensure_mt5()
    if not eng.DRY_RUN and not mt5:
        _print("[SCAN_ONCE] ERROR: MT5 unavailable (ensure terminal is open + APEX_MT5_* set)")
        return 2

    if mt5:
        if not eng._adopt_open_positions(mt5):
            _print("[SCAN_ONCE] warning: adopt pass deferred (terminal not ready)")

    _print(
        "[SCAN_ONCE] running one full entry scan now (bypassing SCAN_HOURS)",
        dry_run=eng.DRY_RUN,
        profile=eng.PROFILE,
    )
    eng.run_full_scan_v76()

    st2 = eng.load_v76_state()
    summary = st2.get("last_scan_summary") if isinstance(st2.get("last_scan_summary"), dict) else {}
    rows = _collect_decision_rows(start_offset)
    _summarize_scan(rows, summary)
    return 0


def _assert_plumbing_demo_only(mt5: Any) -> tuple[int, str]:
    """
    Hard refuse challenge login and non-demo accounts for --plumbing-test.

    Challenge login 511443540 is refused even if a demo-clean unlock exists —
    plumbing must never touch the FTMO challenge account.
    """
    ai = mt5.account_info()
    if ai is None:
        raise SystemExit("[PLUMBING] REFUSED: account_info unavailable")
    login = int(getattr(ai, "login", 0) or 0)
    server = str(getattr(ai, "server", "") or "")
    if login == int(gate.FTMO_CHALLENGE_LOGIN):
        raise SystemExit(
            f"[PLUMBING] REFUSED: FTMO challenge login {gate.FTMO_CHALLENGE_LOGIN} "
            "— use a DEMO account only"
        )
    # Also refuse if env points at challenge before connect quirks.
    env_login = os.environ.get("APEX_MT5_LOGIN") or os.environ.get("MT5_LOGIN") or ""
    try:
        if env_login and int(str(env_login).strip()) == int(gate.FTMO_CHALLENGE_LOGIN):
            raise SystemExit(
                f"[PLUMBING] REFUSED: APEX_MT5_LOGIN is challenge {gate.FTMO_CHALLENGE_LOGIN}"
            )
    except ValueError:
        pass

    demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
    trade_mode = int(getattr(ai, "trade_mode", -1))
    server_demo = "demo" in server.lower()
    # Must be clearly a demo: MT5 trade_mode == DEMO (server name alone is not enough).
    if trade_mode != demo_mode:
        raise SystemExit(
            f"[PLUMBING] REFUSED: account not clearly DEMO "
            f"(login={login} server={server!r} trade_mode={trade_mode}, "
            f"expected ACCOUNT_TRADE_MODE_DEMO={demo_mode}, server_looks_demo={server_demo})"
        )
    return login, server


def run_plumbing_test(*, symbol: str = PLUMBING_SYMBOL_DEFAULT) -> int:
    """Skip strategy — open min-lot market order with SL, confirm, close."""
    if eng.DRY_RUN:
        _print(
            "[PLUMBING] REFUSED: APEX_V76_PRIVATE_DRY_RUN is true — "
            "set it to false for a real demo plumbing test"
        )
        return 2

    _startup_banner()
    mt5 = eng.at.ensure_mt5()
    if not mt5:
        _print("[PLUMBING] ERROR: MT5 unavailable")
        return 2

    login, server = _assert_plumbing_demo_only(mt5)
    _print("[PLUMBING] demo account OK", login=login, server=server, magic=eng.APEX_V76_MAGIC)

    broker = eng.at.resolve_sym(mt5, symbol)
    if not broker:
        _print(f"[PLUMBING] ERROR: cannot resolve symbol {symbol}")
        return 2
    if not mt5.symbol_select(broker, True):
        _print(f"[PLUMBING] ERROR: symbol_select failed for {broker}")
        return 2

    info = mt5.symbol_info(broker)
    tick = mt5.symbol_info_tick(broker)
    if info is None or tick is None:
        _print("[PLUMBING] ERROR: missing symbol_info / tick")
        return 2

    vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
    point = float(getattr(info, "point", 1e-5) or 1e-5)
    stops_lvl = int(getattr(info, "trade_stops_level", 0) or 0)
    # LONG with SL below bid — clear of stops level.
    entry = float(tick.ask)
    sl_dist = max(stops_lvl + 10, 20) * point
    sl = eng.at.mt5_round_price(mt5, broker, float(tick.bid) - sl_dist)
    sl = eng.at.clamp_sl_buy_live(mt5, broker, float(tick.bid), sl)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": broker,
        "volume": vmin,
        "type": mt5.ORDER_TYPE_BUY,
        "price": entry,
        "sl": float(sl),
        "tp": 0.0,
        "deviation": 30,
        "magic": int(eng.APEX_V76_MAGIC),
        "comment": PLUMBING_COMMENT[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": eng.at.fill_mode(mt5, broker),
    }
    _print(
        "[PLUMBING] sending min-lot market BUY with SL",
        symbol=broker,
        volume=vmin,
        entry=round(entry, 5),
        sl=round(float(sl), 5),
        magic=eng.APEX_V76_MAGIC,
    )
    res = mt5.order_send(req)
    if res is None or int(getattr(res, "retcode", -1)) != int(mt5.TRADE_RETCODE_DONE):
        _print(
            "[PLUMBING] ERROR: order_send failed",
            retcode=getattr(res, "retcode", None),
            comment=getattr(res, "comment", None),
            last_error=mt5.last_error(),
        )
        return 3

    time.sleep(0.35)
    ticket: int | None = None
    fill_price = float(getattr(res, "price", 0) or entry)
    pos_sl = 0.0
    pos = None
    candidates: list[Any] = []
    for p in mt5.positions_get(symbol=broker) or []:
        if int(getattr(p, "magic", 0) or 0) != int(eng.APEX_V76_MAGIC):
            continue
        candidates.append(p)
    # Prefer exact plumbing comment, else newest magic match on symbol.
    tagged = [
        p
        for p in candidates
        if PLUMBING_COMMENT[:8] in str(getattr(p, "comment", "") or "")
    ]
    pool = tagged or candidates
    if pool:
        pos = max(pool, key=lambda pp: (int(getattr(pp, "time", 0) or 0), int(pp.ticket)))
        ticket = int(pos.ticket)
        fill_price = float(pos.price_open)
        pos_sl = float(pos.sl or 0)

    if ticket is None or pos is None:
        _print("[PLUMBING] ERROR: order reported DONE but position not found")
        return 3

    if pos_sl <= 0:
        _print(
            "[PLUMBING] ERROR: position opened but SL not attached",
            ticket=ticket,
            fill=fill_price,
            sl=pos_sl,
        )
        # Still attempt close so we don't leave an unmanaged probe.
        eng.at.close_position_at_market(mt5, pos)
        return 4

    _print(
        "[PLUMBING] OPEN OK — broker accepted order with SL",
        ticket=ticket,
        symbol=broker,
        volume=vmin,
        fill_price=round(fill_price, 5),
        sl=round(pos_sl, 5),
        login=login,
        server=server,
    )

    closed = eng.at.close_position_at_market(mt5, pos)
    if not closed:
        _print("[PLUMBING] ERROR: failed to close probe ticket", ticket=ticket)
        return 5

    # Confirm gone
    time.sleep(0.25)
    still = False
    for p in mt5.positions_get(ticket=ticket) or []:
        if int(p.ticket) == ticket:
            still = True
            break
    if still:
        _print("[PLUMBING] ERROR: ticket still open after close", ticket=ticket)
        return 5

    _print(
        "[PLUMBING] CLOSE OK — probe complete",
        ticket=ticket,
        fill_price=round(fill_price, 5),
        sl=round(pos_sl, 5),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-shot private-engine scan / demo plumbing tester (additive).",
    )
    ap.add_argument(
        "--plumbing-test",
        action="store_true",
        help="Skip strategy; open+close one min-lot DEMO trade with SL attached",
    )
    ap.add_argument(
        "--symbol",
        default=PLUMBING_SYMBOL_DEFAULT,
        help=f"Symbol for --plumbing-test (default {PLUMBING_SYMBOL_DEFAULT})",
    )
    ap.add_argument(
        "--dry-run-hint",
        action="store_true",
        help="Print PROFILE / DRY_RUN / env login context and exit (no scan, no orders)",
    )
    args = ap.parse_args(argv)

    if args.dry_run_hint:
        _startup_banner()
        return 0

    if args.plumbing_test:
        try:
            return run_plumbing_test(symbol=str(args.symbol or PLUMBING_SYMBOL_DEFAULT))
        except SystemExit as e:
            _print(str(e))
            return 2

    return run_one_scan()


if __name__ == "__main__":
    raise SystemExit(main())
