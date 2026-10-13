"""
Group C / PR7 — chrono scan parallelisation is intentionally NOT wired.

Eval/apply are coupled inside ``run_one_backtest`` and the day loop; proving
workers=N ≡ workers=1 is not possible without a larger refactor. This test
locks the documented invariant so a future change cannot silently add a
ThreadPool around chrono scans without updating the contract.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CB_PATH = ROOT / "continuous_backtester.py"


def _chrono_fn_source() -> str:
    tree = ast.parse(CB_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_chronological_backtest":
            return ast.get_source_segment(CB_PATH.read_text(encoding="utf-8"), node) or ""
    raise AssertionError("run_chronological_backtest not found")


def test_chrono_loop_does_not_pool_run_one_backtest() -> None:
    src = _chrono_fn_source()
    assert "run_one_backtest(" in src
    # Prefetch may use ThreadPoolExecutor; the scan call itself must stay inline.
    # Reject patterns that submit the chrono scan to a pool.
    banned = (
        "executor.submit(run_one_backtest",
        "ex.submit(run_one_backtest",
        "pool.submit(run_one_backtest",
        "executor.map(run_one_backtest",
        "ex.map(run_one_backtest",
    )
    for frag in banned:
        assert frag not in src, f"chrono must not pool scans ({frag!r})"


def test_max_workers_comment_documents_pr7_block() -> None:
    text = CB_PATH.read_text(encoding="utf-8")
    assert "PR7" in text or "Group C" in text
    assert "CHRONO_DAY_PREFILTER_SIDS" in text
    # Rolling engine still uses the pool; chrono must not claim scan parallelism.
    assert "MAX_WORKERS = int(os.environ.get(\"APEX_PARALLEL_WORKERS\"" in text
