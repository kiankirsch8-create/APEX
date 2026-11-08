"""Score strategies across chronological blocks.

Out of sample is structural: every block is scored by a new strategy instance
built from the same parameter dict. Block results are not passed back into
the constructor, and the dict is not updated between blocks.

    python -m scalping.run --demo
    python -m scalping.run --strategy bollinger_reversion --pair EURUSD
    python -m scalping.run --strategy all --out scalping_data/report.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from scalping import __version__
from scalping.config import Block, ScalpingConfig, load_config
from scalping.costs import CostModel
from scalping.data.store import DataNotReady, load_market
from scalping.engine import FxBook, run_backtest
from scalping.instruments import Instrument, get_instrument
from scalping.market import MarketData
from scalping.metrics import block_returns_from_cells, consistency, json_ready, summarize_result
from scalping.strategies import all_strategies, get_strategy
from scalping.synthetic import synthetic_eurusd


@dataclass
class Job:
    block_id: str
    pair: str
    market: MarketData
    instrument: Instrument
    eurusd: MarketData | None = None


def run_oos(
    strategy_cls,
    params: dict | None,
    jobs: list[Job],
    costs: CostModel,
    initial_equity: float,
    *,
    include_trades: bool = False,
) -> dict:
    """Score ``jobs`` with one frozen parameter set.

    ``params`` is deep-copied into every instance. Nothing in this function
    writes block returns back into those parameters.
    """
    frozen = copy.deepcopy(params or {})
    signature = json.dumps(frozen, sort_keys=True, default=str)
    cells = []
    for job in jobs:
        if json.dumps(frozen, sort_keys=True, default=str) != signature:
            raise RuntimeError("OOS violation: shared parameters changed during the run")
        strategy = strategy_cls(**copy.deepcopy(frozen))
        fx = None
        if job.instrument.symbol != "EURUSD":
            fx = FxBook(job.instrument, job.eurusd)
        result = run_backtest(
            job.market,
            strategy,
            job.instrument,
            costs,
            fx=fx,
            initial_equity=initial_equity,
        )
        summary = summarize_result(result)
        cell = {
            "strategy": strategy_cls.name,
            "block": job.block_id,
            "pair": job.pair,
            "params": strategy.parameters(),
            "warnings": list(result.warnings),
            "bars": result.bars_processed,
            "ticks": result.ticks_processed,
            **summary,
        }
        if include_trades:
            cell["trades"] = [trade.as_dict() for trade in result.trades]
        cells.append(cell)
    if json.dumps(frozen, sort_keys=True, default=str) != signature:
        raise RuntimeError("OOS violation: shared parameters changed during the run")
    block_order = list(dict.fromkeys(job.block_id for job in jobs))
    returns = block_returns_from_cells(cells, block_order)
    return {
        "scalping_version": __version__,
        "oos": True,
        "oos_note": (
            "Parameters are fixed before any block is scored. "
            "Each block gets a new strategy instance, and results are not fed back."
        ),
        "strategy": strategy_cls.name,
        "params": strategy_cls(**copy.deepcopy(frozen)).parameters(),
        "account_currency": costs.account_currency,
        "cost_model": costs.as_dict(),
        "costs_embedded_in_net": ["spread", "slippage", "commission"],
        "blocks": cells,
        "aggregate": {
            **consistency(returns),
            "block_returns": returns,
            "block_order": block_order,
            "trade_count": int(sum(cell["trade_count"] for cell in cells)),
            "net_pnl": float(sum(cell["net_pnl"] for cell in cells)),
        },
    }


def format_table(report: dict) -> str:
    header = (
        f"{'strategy':<22} {'block':<10} {'pair':<8} {'trades':>7} "
        f"{'win%':>7} {'pf':>7} {'exp':>10} {'ret%':>8} {'dd%':>7} {'sharpe':>7}"
    )
    lines = [header, "-" * len(header)]
    for cell in report["blocks"]:
        pf = cell["profit_factor"]
        pf_text = "inf" if cell.get("profit_factor_infinite") or pf is None else f"{pf:.2f}"
        lines.append(
            f"{cell['strategy']:<22} {cell['block']:<10} {cell['pair']:<8} "
            f"{cell['trade_count']:>7d} {100 * cell['win_rate']:>7.1f} {pf_text:>7} "
            f"{cell['expectancy']:>10.2f} {100 * cell['net_return']:>8.2f} "
            f"{100 * cell['max_drawdown']:>7.2f} {cell['sharpe']:>7.2f}"
        )
    agg = report["aggregate"]
    lines.append("-" * len(header))
    lines.append(
        f"consistency {agg['consistency_score']:.3f}  "
        f"positive blocks {agg['positive_block_fraction']:.0%}  "
        f"mean return {agg['return_mean']:.4f}  "
        f"std {agg['return_std']:.4f}  "
        f"trades {agg['trade_count']}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scalping.run")
    parser.add_argument("--strategy", default="all", help="Strategy name, or 'all'")
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--block", action="append", dest="blocks")
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=None, help="JSON output path")
    parser.add_argument("--demo", action="store_true", help="Score the reference strategies on synthetic EURUSD")
    parser.add_argument("--trades", action="store_true", help="Include every trade in the JSON")
    parser.add_argument("--slippage-points", type=float, default=None)
    parser.add_argument("--commission", type=float, default=None)
    parser.add_argument("--equity", type=float, default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    costs = _costs_from(cfg, args)
    equity = float(args.equity if args.equity is not None else cfg.initial_equity)
    names = [cls.name for cls in all_strategies()] if args.strategy == "all" else [args.strategy]
    reports = []
    try:
        if args.demo:
            jobs = [_demo_job()]
            for name in names:
                reports.append(
                    run_oos(get_strategy(name), {}, jobs, costs, equity, include_trades=args.trades)
                )
        else:
            pairs = tuple(p.upper() for p in args.pairs) if args.pairs else cfg.pairs
            blocks = tuple(cfg.block(b) for b in args.blocks) if args.blocks else cfg.blocks
            data_dir = Path(args.data_dir or cfg.data_dir)
            jobs = _load_jobs(pairs, blocks, data_dir)
            for name in names:
                reports.append(
                    run_oos(get_strategy(name), {}, jobs, costs, equity, include_trades=args.trades)
                )
    except DataNotReady as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = reports[0] if len(reports) == 1 else {"scalping_version": __version__, "strategies": reports}
    text = json.dumps(json_ready(payload), indent=2)
    for report in reports:
        print(format_table(report))
        print()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(text)
    return 0


def _demo_job() -> Job:
    market = synthetic_eurusd()
    return Job(
        block_id="demo",
        pair="EURUSD",
        market=market,
        instrument=get_instrument("EURUSD"),
    )


def _load_jobs(pairs: tuple[str, ...], blocks: tuple[Block, ...], data_dir: Path) -> list[Job]:
    cache: dict[tuple[str, str], MarketData] = {}

    def cached(pair: str, block: Block) -> MarketData:
        key = (pair, block.id)
        if key not in cache:
            cache[key] = load_market(data_dir, pair, block.start, block.end)
        return cache[key]

    jobs: list[Job] = []
    for block in blocks:
        eurusd = cached("EURUSD", block) if any(pair != "EURUSD" for pair in pairs) else None
        for pair in pairs:
            jobs.append(
                Job(
                    block_id=block.id,
                    pair=pair,
                    market=cached(pair, block),
                    instrument=get_instrument(pair),
                    eurusd=None if pair == "EURUSD" else eurusd,
                )
            )
    return jobs


def _costs_from(cfg: ScalpingConfig, args) -> CostModel:
    return CostModel(
        commission_per_lot_side=(
            args.commission if args.commission is not None else cfg.costs.commission_per_lot_side
        ),
        commission_currency=cfg.costs.commission_currency,
        slippage_points=(
            args.slippage_points if args.slippage_points is not None else cfg.costs.slippage_points
        ),
        account_currency=cfg.costs.account_currency,
    )


if __name__ == "__main__":
    sys.exit(main())
