"""Merge per-block scalping reports and break results into edge vs costs.

    python -m scalping.tools_summarize results/scalping/full_v2 results/scalping/sectionA_full_summary.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ORDER = [b["id"] for b in json.loads(Path("scalping/config_histdata.json").read_text())["blocks"]]


def main(src: str, out: str) -> int:
    cells = defaultdict(dict)
    params = {}
    for path in sorted(Path(src).glob("*.json")):
        report = json.loads(path.read_text())
        for strat in report.get("strategies", [report]):
            params[strat["strategy"]] = strat["params"]
            for cell in strat["blocks"]:
                cells[strat["strategy"]][cell["block"]] = cell

    summary = {}
    years = sorted({b[:4] for b in ORDER})
    head = f"{'strategy':22} " + " ".join(f"{y:>7}" for y in years) + f" {'total':>8} {'pos q':>6}"
    print("net return % per year, summed from quarterly blocks (after all costs)")
    print(head)
    for name in sorted(cells):
        row = cells[name]
        rets = [100 * sum(row[b]["net_return"] for b in ORDER if b in row and b[:4] == y) for y in years]
        trades = sum(c["trade_count"] for c in row.values())
        gross = sum(c["gross_pnl"] for c in row.values())
        comm = sum(c["commission"] for c in row.values())
        net = sum(c["net_pnl"] for c in row.values())
        lots = params[name].get("lots", 0.1)
        # EURUSD: 1 pip on 0.1 lot ~ $1 ~ EUR 0.9; report per-trade figures in EUR and approx pips
        pip_eur = 10.0 * lots * 0.9
        summary[name] = {
            "params": params[name],
            "blocks": {b: {k: row[b][k] for k in ("net_return", "trade_count", "win_rate", "profit_factor",
                                                  "max_drawdown", "gross_pnl", "commission", "net_pnl",
                                                  "spread_entry_mean", "warnings")} for b in ORDER if b in row},
            "trades": trades,
            "positive_blocks": sum(1 for b in ORDER if b in row and row[b]["net_return"] > 0),
            "gross_pnl_eur": gross,
            "commission_eur": comm,
            "net_pnl_eur": net,
            "gross_per_trade_pips": gross / trades / pip_eur if trades else 0.0,
            "commission_per_trade_pips": comm / trades / pip_eur if trades else 0.0,
            "net_per_trade_pips": net / trades / pip_eur if trades else 0.0,
        }
        print(f"{name:22} " + " ".join(f"{r:7.2f}" for r in rets)
              + f" {100 * net / 100000:8.1f} {summary[name]['positive_blocks']:>2}/{len(row)}")

    print("\nper trade, whole period (approx pips; gross already includes spread + slippage)")
    print(f"{'strategy':22} {'trades':>7} {'win%':>6} {'gross':>7} {'comm':>6} {'net':>7}")
    for name, s in sorted(summary.items()):
        wins = sum(c["win_rate"] * c["trade_count"] for c in s["blocks"].values())
        print(f"{name:22} {s['trades']:>7} {100 * wins / max(s['trades'], 1):6.1f} "
              f"{s['gross_per_trade_pips']:7.2f} {s['commission_per_trade_pips']:6.2f} {s['net_per_trade_pips']:7.2f}")
    Path(out).write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
