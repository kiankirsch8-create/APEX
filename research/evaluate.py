"""Evaluate research strategies on the build or confirm period and log the result.

    python -m research.evaluate --idea R001 --strategy my_strat@M15 --period build
    python -m research.evaluate --idea R001 --strategy my_strat@M15 --period confirm   # validator only
    python -m research.evaluate --idea R001 --strategy my_strat@M15 --period stress    # validator only

Strategies are found in the harness registry and in ``research/strategies/*.py``.
Every run is appended to ``research/ledger.jsonl`` automatically, pass or fail.

Gates (see research/README.md for the reasoning):
  build    trades >= 200, edge t >= 3.0 + 0.3 * prior build evals of this idea,
           net >= +0.30 pips/trade, >= 60% positive quarters, worst quarter DD <= 6%,
           positive edge in >= 3 of 4 build years; max 6 build evals per idea
  confirm  trades >= 60, net > 0, edge t >= 2.0, >= 50% positive quarters,
           positive edge in >= 2 of 3 years, worst quarter DD <= 6%
  stress   confirm period with commission 5.0/lot/side and 3-point slippage: net > 0
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ["SCALPING_PLUGIN_DIRS"] = str(ROOT / "strategies")

from research import ledger  # noqa: E402
from scalping.data.store import tick_day_path  # noqa: E402
from scalping.strategies import get_strategy  # noqa: E402
from scalping.tools_diagnose import edge_stats, enrich, load  # noqa: E402

CONFIGS = {
    "build": ROOT / "config_build.json",
    "confirm": ROOT / "config_confirm.json",
    "stress": ROOT / "config_confirm_stress.json",
}
MAX_BUILD_EVALS = 6


def source_sha(name: str) -> str:
    cls = get_strategy(name)
    files = {inspect.getsourcefile(cls)}
    inner = getattr(cls, "inner_cls", None)
    if inner is not None:
        files.add(inspect.getsourcefile(inner))
    digest = hashlib.sha256()
    for f in sorted(files):
        digest.update(Path(f).read_bytes())
    return digest.hexdigest()[:16]


def check_data(config: dict) -> list[str]:
    missing = []
    data_dir = REPO / config["data_dir"]
    for block in config["blocks"]:
        start, end = date.fromisoformat(block["start"]), date.fromisoformat(block["end"])
        for day in (start + timedelta(days=3), end - timedelta(days=3)):
            if not tick_day_path(data_dir, "EURUSD", day).exists():
                missing.append(f"{block['id']} ({day})")
                break
    return missing


def gates(period: str, m: dict, prior_build_evals: int) -> dict[str, bool]:
    years_pos = sum(1 for v in m["by_year"].values() if v["edge_pips"] > 0)
    if period == "build":
        return {
            "trades>=200": m["trades"] >= 200,
            f"edge_t>={3.0 + 0.3 * prior_build_evals:.1f}": (m["edge_t"] or 0) >= 3.0 + 0.3 * prior_build_evals,
            "net>=+0.30": m["net_pips"] >= 0.30,
            "pos_quarters>=60%": m["pos_quarters"] >= 0.60,
            "worst_dd<=6%": m["worst_dd"] <= 0.06,
            "edge+ in >=3/4 years": years_pos >= 3,
        }
    if period == "confirm":
        return {
            "trades>=60": m["trades"] >= 60,
            "net>0": m["net_pips"] > 0,
            "edge_t>=2.0": (m["edge_t"] or 0) >= 2.0,
            "pos_quarters>=50%": m["pos_quarters"] >= 0.50,
            "edge+ in >=2/3 years": years_pos >= 2,
            "worst_dd<=6%": m["worst_dd"] <= 0.06,
        }
    return {"net>0 under stress": m["net_pips"] > 0}


def evaluate(names: list[str], period: str, idea: str, note: str, keep_trades: bool) -> int:
    config = json.loads(CONFIGS[period].read_text())
    missing = check_data(config)
    if missing:
        print(f"ERROR: tick data missing for {period} blocks: {', '.join(missing)}", file=sys.stderr)
        print("Run research/bootstrap.sh with the right role first.", file=sys.stderr)
        return 2
    prior = ledger.build_evals(idea)
    if period == "build" and prior + len(names) > MAX_BUILD_EVALS:
        print(f"ERROR: idea {idea} already has {prior} build evaluations; the cap is {MAX_BUILD_EVALS}. "
              "Close it and write a new idea with a genuinely different mechanism.", file=sys.stderr)
        return 3
    for n in names:
        get_strategy(n)  # fail fast on typos

    tmp = REPO / ".research_tmp" / f"{period}_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SCALPING_PLUGIN_DIRS": str(ROOT / "strategies")}
    cfg_path = tmp.parent / f"{tmp.name}_config.json"  # outside tmp: load() reads every json in it
    cfg_path.write_text(json.dumps({**config, "data_dir": str(REPO / config["data_dir"])}))
    for block in config["blocks"]:
        out = tmp / f"{block['id']}.json"
        proc = subprocess.run(
            [sys.executable, "-W", "ignore", "-m", "scalping.run", "--config", str(cfg_path), "--block", block["id"],
             "--strategy", ",".join(names), "--trades", "--out", str(out)],
            cwd=REPO, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout[-2000:], proc.stderr[-4000:], file=sys.stderr)
            print(f"ERROR: backtest failed on block {block['id']}", file=sys.stderr)
            return 4
        print(f"  {block['id']} done", flush=True)

    df = enrich(load(tmp))
    cells: dict[str, dict] = {n: {} for n in names}
    for path in sorted(tmp.glob("*Q*.json")):
        report = json.loads(path.read_text())
        for strat in report.get("strategies", [report]):  # single-strategy runs write one flat report
            for c in strat["blocks"]:
                cells[strat["strategy"]][c["block"]] = c

    status = 0
    print(f"\n{'strategy':34} {'verdict':7} {'trades':>6} {'edge':>6} {'t':>5} {'net':>6} {'posQ':>5} {'DD':>5}")
    for n in names:
        g = df[df["strategy"] == n]
        q = cells[n]
        m = {**(edge_stats(g) if len(g) else {"trades": 0, "edge_pips": 0.0, "edge_t": None, "net_pips": 0.0,
                                                "win_rate": 0.0}),
             "pos_quarters": round(sum(c["net_return"] > 0 for c in q.values()) / max(len(q), 1), 3),
             "worst_dd": round(max((c["max_drawdown"] for c in q.values()), default=0.0), 4),
             "sum_return": round(sum(c["net_return"] for c in q.values()), 4),
             "costs_pips": round(float((g["spread_pips"] + g["slip_pips"] + g["comm_pips"]).mean()), 3) if len(g) else 0,
             "by_year": {str(k): edge_stats(v) for k, v in g.groupby("year")} if len(g) else {},
             "by_session": {k: edge_stats(v) for k, v in g.groupby("session")} if len(g) else {},
             "by_side": {k: edge_stats(v) for k, v in g.groupby("side_label")} if len(g) else {}}
        checks = gates(period, m, prior)
        verdict = "pass" if all(checks.values()) else "fail"
        params = get_strategy(n)().parameters()
        result = {"idea": idea, "strategy": n, "period": period, "verdict": verdict, "checks": checks,
                  "metrics": m, "params": params, "source_sha": source_sha(n), "note": note,
                  "quarters": {b: {k: c[k] for k in ("net_return", "trade_count", "win_rate", "max_drawdown",
                                                     "profit_factor")} for b, c in q.items()}}
        out = ROOT / "results" / f"{idea}__{n.replace('@', '_at_')}__{period}.json"
        out.write_text(json.dumps(result, indent=1, default=str) + "\n")
        ledger.append({"kind": "eval", "idea": idea, "strategy": n, "period": period, "verdict": verdict,
                       "checks": checks, "params": params, "source_sha": result["source_sha"], "note": note,
                       "metrics": {k: m[k] for k in ("trades", "edge_pips", "edge_t", "net_pips", "win_rate",
                                                     "pos_quarters", "worst_dd", "sum_return", "costs_pips")}})
        if period == "build":
            prior += 1
        failed = [k for k, ok in checks.items() if not ok]
        print(f"{n:34} {verdict:7} {m['trades']:>6} {m['edge_pips']:6.2f} {m['edge_t'] or 0:5.1f} "
              f"{m['net_pips']:6.2f} {100 * m['pos_quarters']:4.0f}% {100 * m['worst_dd']:4.1f}%"
              + (f"   failed: {', '.join(failed)}" if failed else ""))
        status = status or (0 if verdict == "pass" else 1)
    ledger.render()
    if keep_trades:
        print(f"trade logs kept in {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    cfg_path.unlink(missing_ok=True)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.evaluate")
    ap.add_argument("--idea", required=True, help="ledger idea id, e.g. R001")
    ap.add_argument("--strategy", required=True, help="one or more comma-separated strategy names")
    ap.add_argument("--period", choices=list(CONFIGS), default="build")
    ap.add_argument("--note", default="", help="what this variant changes vs the previous one")
    ap.add_argument("--keep-trades", action="store_true")
    a = ap.parse_args(argv)
    if a.idea not in ledger.ideas():
        ap.error(f"unknown idea {a.idea}: register it first with `python -m research.ledger idea ...`")
    return evaluate([s.strip() for s in a.strategy.split(",") if s.strip()], a.period, a.idea, a.note, a.keep_trades)


if __name__ == "__main__":
    sys.exit(main())
