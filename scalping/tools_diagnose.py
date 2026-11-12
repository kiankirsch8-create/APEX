"""Trade-level diagnostics: where does a strategy's result come from?

Reads per-block reports written with ``--trades`` and splits every trade into

    edge  = mid-to-mid price move in the trade's direction (pips, before any cost)
    costs = half spread at entry + half spread at exit + slippage both ways + commission

so ``net = edge - costs``. Then slices edge by exit reason, session, side,
year and holding time, and adds a t-statistic for the edge.

    python -m scalping.tools_diagnose results/scalping/trades scalping/findings/sectionA_diagnostics.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PIP = 1e-4  # EURUSD
SESSIONS = [  # UTC entry hour -> label
    ("asia 22-07", lambda h: (h >= 22) | (h < 7)),
    ("london 07-12", lambda h: (h >= 7) & (h < 12)),
    ("overlap 12-16", lambda h: (h >= 12) & (h < 16)),
    ("ny 16-22", lambda h: (h >= 16) & (h < 22)),
]


def load(src: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(src.glob("*.json")):
        report = json.loads(path.read_text())
        for strat in report.get("strategies", [report]):
            for cell in strat["blocks"]:
                for t in cell.get("trades", []):
                    rows.append((strat["strategy"], cell["block"], t["side"], t["lots"], t["entry_time"],
                                 t["exit_time"], t["entry_price"], t["exit_price"], t["exit_reason"],
                                 t["spread_entry"], t["spread_exit"], t["slippage_entry"], t["slippage_exit"],
                                 t["commission"], t["net_pnl"]))
    df = pd.DataFrame(rows, columns=["strategy", "block", "side", "lots", "entry_time", "exit_time", "entry_price",
                                     "exit_price", "exit_reason", "spread_entry", "spread_exit", "slip_entry",
                                     "slip_exit", "commission", "net_pnl"])
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True, format="ISO8601")
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, format="ISO8601")
    return df


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    long = df["side"].astype(str).str.lower().str.startswith("b")
    sign = np.where(long, 1.0, -1.0)
    # filled long: entry = ask + slip, exit = bid - slip ; short mirrored
    mid_in = df["entry_price"] - sign * (df["slip_entry"] + df["spread_entry"] / 2)
    mid_out = df["exit_price"] + sign * (df["slip_exit"] + df["spread_exit"] / 2)
    df["edge_pips"] = sign * (mid_out - mid_in) / PIP
    df["spread_pips"] = (df["spread_entry"] + df["spread_exit"]) / 2 / PIP
    df["slip_pips"] = (df["slip_entry"] + df["slip_exit"]) / PIP
    # commission is in EUR; one pip on `lots` is 10*lots USD = 10*lots/EURUSD EUR
    df["comm_pips"] = df["commission"] * df["exit_price"] / (10.0 * df["lots"])
    df["net_pips"] = df["edge_pips"] - df["spread_pips"] - df["slip_pips"] - df["comm_pips"]
    df["side_label"] = np.where(long, "long", "short")
    df["hold_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60
    df["year"] = df["entry_time"].dt.year
    hour = df["entry_time"].dt.hour
    df["session"] = ""
    for label, test in SESSIONS:
        df.loc[test(hour), "session"] = label
    df["timeframe"] = df["strategy"].str.split("@").str[1].fillna("M1")
    df["base"] = df["strategy"].str.split("@").str[0]
    return df


def edge_stats(g: pd.DataFrame) -> dict:
    n = len(g)
    e = g["edge_pips"]
    sd = float(e.std(ddof=1)) if n > 1 else float("nan")
    return {
        "trades": n,
        "edge_pips": round(float(e.mean()), 3),
        "edge_t": round(float(e.mean() / (sd / math.sqrt(n))), 2) if n > 1 and sd > 0 else None,
        "net_pips": round(float(g["net_pips"].mean()), 3),
        "win_rate": round(float((g["net_pips"] > 0).mean()), 3),
    }


def diagnose(df: pd.DataFrame) -> dict:
    out = {}
    for name, g in df.groupby("strategy", sort=True):
        wins, losses = g.loc[g["net_pips"] > 0, "net_pips"], g.loc[g["net_pips"] <= 0, "net_pips"]
        avg_w, avg_l = float(wins.mean()) if len(wins) else 0.0, float(-losses.mean()) if len(losses) else 0.0
        rec = {
            **edge_stats(g),
            "costs_pips": {
                "spread": round(float(g["spread_pips"].mean()), 3),
                "slippage": round(float(g["slip_pips"].mean()), 3),
                "commission": round(float(g["comm_pips"].mean()), 3),
                "total": round(float((g["spread_pips"] + g["slip_pips"] + g["comm_pips"]).mean()), 3),
            },
            "avg_win_pips": round(avg_w, 2),
            "avg_loss_pips": round(avg_l, 2),
            "breakeven_win_rate": round(avg_l / (avg_w + avg_l), 3) if avg_w + avg_l else None,
            "edge_abs_move_pips": round(float(g["edge_pips"].abs().mean()), 2),
            "cost_share_of_move": round(float((g["spread_pips"] + g["slip_pips"] + g["comm_pips"]).mean()
                                              / g["edge_pips"].abs().mean()), 3),
            "hold_minutes_median": round(float(g["hold_min"].median()), 1),
            "trades_per_quarter": round(len(g) / g["block"].nunique(), 1),
            "by_exit_reason": {k: {**edge_stats(v), "share": round(len(v) / len(g), 3)}
                               for k, v in g.groupby("exit_reason")},
            "by_side": {k: edge_stats(v) for k, v in g.groupby("side_label")},
            "by_session": {k: edge_stats(v) for k, v in g.groupby("session")},
            "by_year": {str(k): edge_stats(v) for k, v in g.groupby("year")},
            "by_hold": {k: edge_stats(v) for k, v in g.groupby(
                pd.cut(g["hold_min"], [0, 5, 30, 120, 480, 1e9],
                       labels=["<5m", "5-30m", "30m-2h", "2-8h", ">8h"]), observed=True)},
        }
        out[name] = rec
    return out


def main(src: str, out: str) -> int:
    df = enrich(load(Path(src)))
    result = {"meta": {"pair": "EURUSD", "trades": len(df), "blocks": int(df["block"].nunique()),
                       "period": f"{df['entry_time'].min():%Y-%m-%d} to {df['entry_time'].max():%Y-%m-%d}",
                       "pip": PIP, "notes": "edge = mid-to-mid move in trade direction, before all costs; "
                                            "net = edge - spread - slippage - commission (EUR->pips at the trade exit rate)"},
              "strategies": diagnose(df)}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, indent=1) + "\n")
    print(f"{'strategy':28} {'trades':>7} {'edge':>6} {'t':>6} {'cost':>5} {'net':>6} {'win%':>5} {'BE%':>5} {'|move|':>6} {'hold':>6}")
    for k, r in result["strategies"].items():
        print(f"{k:28} {r['trades']:>7} {r['edge_pips']:6.2f} {r['edge_t'] or 0:6.1f} {r['costs_pips']['total']:5.2f} "
              f"{r['net_pips']:6.2f} {100 * r['win_rate']:5.1f} {100 * (r['breakeven_win_rate'] or 0):5.1f} "
              f"{r['edge_abs_move_pips']:6.1f} {r['hold_minutes_median']:6.0f}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
