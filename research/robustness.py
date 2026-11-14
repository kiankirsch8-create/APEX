"""Robustness gate: does a build-period candidate survive small changes? (2018-2021 only)

    python -m research.robustness --idea A004 --strategy a004_name@M15

Runs on the build period:
  * the strategy exactly as it passed (base),
  * every numeric parameter at x0.9 and x1.1 (integers move by at least 1), one at a time,
  * the base with +0.2 pip extra slippage on market/stop fills.

Gate (all required):
  * every perturbed variant keeps net > 0 pips/trade,
  * the median variant keeps >= 50% of the base net,
  * the +0.2 pip slippage run keeps net > 0.

Skipped parameters: lots, max_spread_points, booleans, timeframe_minutes, strings.
A collapse when a lookback moves from 20 to 22 means the "edge" was a fitted coincidence.
Logged in the ledger as period "robust". The validator only takes candidates that passed it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ["SCALPING_PLUGIN_DIRS"] = str(ROOT / "strategies")

import pandas as pd  # noqa: E402

from research import ledger  # noqa: E402
from research.evaluate import CONFIGS, check_data, source_sha  # noqa: E402
from scalping.config import load_config  # noqa: E402
from scalping.run import _load_jobs, run_oos  # noqa: E402
from scalping.strategies import get_strategy  # noqa: E402
from scalping.tools_diagnose import enrich  # noqa: E402

SKIP = {"lots", "max_spread_points", "timeframe_minutes"}


def variants(params: dict) -> list[tuple[str, dict]]:
    out = [("base", dict(params))]
    for key, value in params.items():
        if key in SKIP or isinstance(value, bool) or not isinstance(value, (int, float)) or value == 0:
            continue
        for factor, tag in ((0.9, "-10%"), (1.1, "+10%")):
            if isinstance(value, int):
                new = int(round(value * factor))
                if new == value:
                    new = value + (1 if factor > 1 else -1)
                if new <= 0:
                    continue
            else:
                new = round(value * factor, 6)
            out.append((f"{key} {tag} ({value}->{new})", {**params, key: new}))
    return out


def _net_pips(name: str, cells: list[dict]) -> tuple[float, int, float]:
    rows = []
    for c in cells:
        for t in c.get("trades", []):
            rows.append({"strategy": name, "block": c["block"], **t})
    if not rows:
        return 0.0, 0, 0.0
    df = pd.DataFrame(rows)
    df = df.rename(columns={"slippage_entry": "slip_entry", "slippage_exit": "slip_exit"})
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True, format="ISO8601")
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, format="ISO8601")
    if "financing" not in df:
        df["financing"] = 0.0
    df = enrich(df)
    pos_q = sum(c["net_return"] > 0 for c in cells) / max(len(cells), 1)
    return float(df["net_pips"].mean()), len(df), pos_q


def run(idea: str, name: str, only_blocks: list[str] | None = None) -> int:
    cfg = load_config(CONFIGS["build"])
    if only_blocks:  # smoke tests only; a real gate must use every build block
        cfg = replace(cfg, blocks=tuple(b for b in cfg.blocks if b.id in only_blocks))
    raw = json.loads(CONFIGS["build"].read_text())
    missing = check_data(raw)
    if missing:
        print(f"ERROR: build data missing: {missing}", file=sys.stderr)
        return 2
    cls = get_strategy(name)
    base_params = {k: v for k, v in cls().parameters().items() if k != "timeframe_minutes"}
    runs = variants(base_params)
    slip_costs = replace(cfg.costs, slippage_points=cfg.costs.slippage_points + 2.0)
    cells: dict[str, list] = {label: [] for label, _ in runs}
    cells["base +0.2 pip slippage"] = []
    for block in cfg.blocks:                       # one quarter in memory at a time
        jobs = _load_jobs(cfg.pairs, (block,), REPO / cfg.data_dir)
        for label, params in runs:
            rep = run_oos(cls, params, jobs, cfg.costs, cfg.initial_equity, include_trades=True)
            cells[label] += rep["blocks"]
        rep = run_oos(cls, base_params, jobs, slip_costs, cfg.initial_equity, include_trades=True)
        cells["base +0.2 pip slippage"] += rep["blocks"]
        print(f"  {block.id} done ({len(runs) + 1} runs)", flush=True)

    table = {label: dict(zip(("net_pips", "trades", "pos_quarters"), _net_pips(name, c))) for label, c in cells.items()}
    base = table["base"]["net_pips"]
    perturbed = [v["net_pips"] for k, v in table.items() if k not in ("base", "base +0.2 pip slippage")]
    median = statistics.median(perturbed) if perturbed else base
    checks = {
        "all perturbed net > 0": all(p > 0 for p in perturbed),
        "median >= 50% of base": base > 0 and median >= 0.5 * base,
        "+0.2 pip slippage net > 0": table["base +0.2 pip slippage"]["net_pips"] > 0,
    }
    verdict = "pass" if all(checks.values()) else "fail"
    print(f"\n{'variant':44} {'net pips':>9} {'trades':>7} {'posQ':>5}")
    for label, v in table.items():
        print(f"{label:44} {v['net_pips']:+9.3f} {v['trades']:>7} {100 * v['pos_quarters']:4.0f}%")
    print(f"\nROBUSTNESS {verdict.upper()}: " + ", ".join(f"{k}: {'ok' if ok else 'FAIL'}" for k, ok in checks.items()))
    if only_blocks:
        print("(smoke run: not written to results or ledger)")
        return 0 if verdict == "pass" else 1
    out = ROOT / "results" / f"{idea}__{name.replace('@', '_at_')}__robust.json"
    out.write_text(json.dumps({"idea": idea, "strategy": name, "period": "robust", "verdict": verdict,
                               "checks": checks, "table": table, "source_sha": source_sha(name)}, indent=1) + "\n")
    ledger.append({"kind": "eval", "idea": idea, "strategy": name, "period": "robust", "verdict": verdict,
                   "checks": checks, "source_sha": source_sha(name), "params": base_params,
                   "metrics": {"net_pips": base, "median_variant_net": median,
                               "worst_variant_net": min(perturbed) if perturbed else base,
                               "slippage_net": table["base +0.2 pip slippage"]["net_pips"],
                               "edge_pips": 0.0, "edge_t": 0.0, "trades": table["base"]["trades"]}})
    ledger.render()
    return 0 if verdict == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.robustness")
    ap.add_argument("--idea", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--smoke-blocks", default="", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.idea not in ledger.ideas():
        ap.error(f"unknown idea {a.idea}")
    blocks = [b for b in a.smoke_blocks.split(",") if b]
    if blocks:
        print("SMOKE RUN on", blocks, "- not logged")
    return run(a.idea, a.strategy, blocks or None)


if __name__ == "__main__":
    sys.exit(main())
