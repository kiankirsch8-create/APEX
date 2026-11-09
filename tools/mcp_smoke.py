#!/usr/bin/env python3
"""Read-only live smoke test for apex_mcp against a running MT5 MCP server.

Does NOT place or modify orders. Requires:
  MCP_URL        (default http://127.0.0.1:22346/mcp)
  MT5_MCP_KEY    Bearer token

Usage:
  python3 tools/mcp_smoke.py
  python3 tools/mcp_smoke.py --symbol EURUSD
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apex_mcp as mt5  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only apex_mcp smoke against live MCP")
    p.add_argument("--symbol", default="EURUSD")
    args = p.parse_args()

    url = os.environ.get("MCP_URL", "http://127.0.0.1:22346/mcp")
    key = os.environ.get("MT5_MCP_KEY", "")
    print(f"MCP_URL={url} key_set={bool(key)}")

    if not mt5.initialize():
        print("FAIL initialize", mt5.last_error())
        return 1
    if not mt5.login():
        print("FAIL login", mt5.last_error())
        return 1

    ti = mt5.terminal_info()
    print("terminal_info", None if ti is None else ti.__dict__)
    if ti is None or not getattr(ti, "connected", False):
        print("FAIL terminal not connected")
        mt5.shutdown()
        return 2

    ai = mt5.account_info()
    print("account_info", None if ai is None else {k: getattr(ai, k) for k in ("login", "balance", "equity", "margin_free", "currency")})
    if ai is None:
        print("FAIL account_info")
        mt5.shutdown()
        return 3

    sym = args.symbol.upper()
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)
    info = mt5.symbol_info(sym)
    print("tick", None if tick is None else tick.__dict__)
    print(
        "symbol_info",
        None
        if info is None
        else {
            k: getattr(info, k)
            for k in (
                "digits",
                "point",
                "trade_stops_level",
                "volume_min",
                "volume_step",
                "trade_tick_value",
                "trade_contract_size",
                "filling_mode",
            )
        },
    )

    positions = mt5.positions_get() or ()
    print(f"positions_get count={len(positions)}")
    for p0 in positions[:5]:
        print(
            " ",
            {
                "ticket": p0.ticket,
                "symbol": p0.symbol,
                "type": p0.type,
                "volume": p0.volume,
                "comment": p0.comment,
                "magic": p0.magic,
                "profit": p0.profit,
            },
        )

    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H4, 0, 5)
    print(f"copy_rates_from_pos H4 n={0 if rates is None else len(rates)}")
    if rates:
        print(" ", json.dumps(rates[-1], default=str))

    mt5.shutdown()
    print("PASS smoke (read-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
