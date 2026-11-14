"""Developer gate: verify a research strategy before it may be backtested for the record.

    python -m research.check --idea B001 --strategy b001_london_sweep@M5

Checks (on one build quarter, default 2019Q2):
  1. it loads, runs without exceptions and places trades (0 trades = warning, not a pass);
  2. deterministic: two full runs give identical trades;
  3. no lookahead: a run on the first ~60% of the quarter gives exactly the same trades
     (entries, sides, prices, exits) for every trade that closed before the cut;
  4. static scan of the source: no file I/O, wall clock, randomness, network or env access.

A pass is logged (kind "check") for the exact source hash. `research.evaluate --period build`
refuses to run a strategy without one. Failures are printed with the reason; fix and re-run
(checks are not strikes).
"""

from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ["SCALPING_PLUGIN_DIRS"] = str(ROOT / "strategies")

from research import ledger  # noqa: E402
from research.evaluate import CONFIGS, source_sha  # noqa: E402
from scalping.config import load_config  # noqa: E402
from scalping.data.store import load_market  # noqa: E402
from scalping.engine import run_backtest  # noqa: E402
from scalping.instruments import get_instrument  # noqa: E402
from scalping.strategies import get_strategy  # noqa: E402

FORBIDDEN = {
    r"\bopen\(": "file access",
    r"\bPath\(": "file paths",
    r"datetime\.now|datetime\.utcnow|date\.today|time\.time\(|time\.monotonic": "wall clock",
    r"\brandom\b|np\.random": "randomness",
    r"\brequests\b|urllib|socket|http": "network",
    r"os\.environ|getenv": "environment",
    r"read_parquet|read_csv|load_market": "reading data outside ctx.window",
    r"\._market|\._end\b|_view\(": "private harness internals (can expose future bars)",
}


def _key(t) -> tuple:
    return (t.side, t.entry_time, round(t.entry_price, 6), t.exit_time, round(t.exit_price, 6), t.exit_reason)


def static_scan(name: str) -> list[str]:
    cls = get_strategy(name)
    files = {inspect.getsourcefile(cls)}
    if getattr(cls, "inner_cls", None) is not None:
        files.add(inspect.getsourcefile(cls.inner_cls))
    problems = []
    for f in files:
        if "research/strategies" not in f.replace("\\", "/"):
            continue  # harness strategies are trusted
        text = Path(f).read_text()
        for pattern, what in FORBIDDEN.items():
            if re.search(pattern, text):
                problems.append(f"{Path(f).name}: uses {what} ({pattern})")
    return problems


def check(idea: str, name: str, block_id: str = "2019Q2", log: bool = True) -> int:
    cfg = load_config(CONFIGS["build"])
    block = cfg.block(block_id)
    pair = cfg.pairs[0]
    data_dir = REPO / cfg.data_dir
    cls = get_strategy(name)
    inst = get_instrument(pair)
    problems = static_scan(name)

    full = load_market(data_dir, pair, block.start, block.end)
    try:
        a = run_backtest(full, cls(), inst, cfg.costs, flatten_end=False)
        b = run_backtest(full, cls(), inst, cfg.costs, flatten_end=False)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"crashed: {type(exc).__name__}: {exc}")
        a = b = None
    if a is not None:
        if [_key(t) for t in a.trades] != [_key(t) for t in b.trades]:
            problems.append("not deterministic: two identical runs gave different trades")
        cut = block.start + (block.end - block.start) * 0.6
        cut = cut.replace(hour=0, minute=0, second=0, microsecond=0)
        part = load_market(data_dir, pair, block.start, cut)
        c = run_backtest(part, cls(), inst, cfg.costs, flatten_end=False)
        cut_iso = (cut - timedelta(minutes=1)).isoformat()
        before_full = [_key(t) for t in a.trades if t.exit_time < cut_iso]
        before_part = [_key(t) for t in c.trades if t.exit_time < cut_iso]
        if before_full != before_part:
            n = sum(1 for x, y in zip(before_full, before_part) if x != y) + abs(len(before_full) - len(before_part))
            problems.append(f"LOOKAHEAD: {n} trades before the cut changed when later data was removed")
        if len(a.trades) == 0:
            problems.append("no trades on the check quarter (cannot verify; loosen or pick --block)")
        print(f"{name}: {len(a.trades)} trades on {block_id}, {len(before_full)} closed before the cut")
    verdict = "fail" if problems else "pass"
    for p in problems:
        print("  PROBLEM:", p)
    print(f"CHECK {verdict.upper()}  source {source_sha(name)}")
    if log:
        ledger.append({"kind": "check", "idea": idea, "strategy": name, "source_sha": source_sha(name),
                       "verdict": verdict, "problems": problems, "block": block_id})
    return 0 if verdict == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.check")
    ap.add_argument("--idea", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--block", default="2019Q2")
    a = ap.parse_args(argv)
    if a.idea not in ledger.ideas():
        ap.error(f"unknown idea {a.idea}")
    return check(a.idea, a.strategy, a.block)


if __name__ == "__main__":
    sys.exit(main())
