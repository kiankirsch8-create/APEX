#!/usr/bin/env python3
"""Validate FTMO demo readiness / mark clean unlock for challenge login.

Usage (on the VPS after ≥3 trading days of PROFILE=ftmo demo):

  python tools/ftmo_demo_validate.py
  python tools/ftmo_demo_validate.py --mark-clean

Does NOT switch credentials. Challenge login 511443540 stays blocked until
``--mark-clean`` succeeds AND ``APEX_FTMO_ALLOW_LIVE=1`` is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ftmo_demo_gate as gate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mark-clean",
        action="store_true",
        help="Write unlock file only if acceptance passes",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="Print current demo status JSON",
    )
    args = ap.parse_args()

    st = gate.load_demo_status()
    if args.status or not args.mark_clean:
        print(json.dumps(st, indent=2, default=str))

    ok, reasons = gate.demo_acceptance(st)
    print()
    print(f"PROFILE gate target login: {gate.FTMO_CHALLENGE_LOGIN}")
    print(f"Trading days: {len(st.get('trading_days') or [])} "
          f"(need ≥{gate.DEMO_MIN_TRADING_DAYS})")
    print(f"Worst day P&L %: {st.get('worst_day_loss_pct')}")
    print(f"Worst total DD %: {st.get('worst_total_dd_pct')}")
    events = st.get("guardrail_events") or {}
    for k in gate.REQUIRED_GUARDRAIL_EVENTS:
        n = len(events.get(k) or [])
        if k == "entry_with_sl":
            n = max(n, int(st.get("entries_with_sl") or 0))
        print(f"  guardrail {k}: {n} event(s)")

    if ok:
        print("\nACCEPTANCE: PASS — demo is clean.")
    else:
        print("\nACCEPTANCE: FAIL — do NOT go live.")
        for r in reasons:
            print(f"  - {r}")

    if args.mark_clean:
        marked, mark_reasons = gate.mark_demo_clean()
        if marked:
            print(f"\nUnlock written: {gate.demo_unlock_path()}")
            print(
                "Next (VPS only, after human review):\n"
                "  set APEX_FTMO_ALLOW_LIVE=1\n"
                f"  set APEX_MT5_LOGIN={gate.FTMO_CHALLENGE_LOGIN}\n"
                f"  set APEX_MT5_SERVER={gate.FTMO_LIVE_SERVER_DEFAULT}\n"
                "  set APEX_MT5_PASSWORD=<challenge password from env manager — never commit>\n"
                "  restart APEX-LiveTraderPrivate"
            )
            return 0
        print("\nMark-clean refused:")
        for r in mark_reasons:
            print(f"  - {r}")
        return 2

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
