"""CLI for the Dukascopy cache.

    python -m scalping.data.pipeline download
    python -m scalping.data.pipeline download --pair EURUSD --block 2018-19
    python -m scalping.data.pipeline status

This talks to Dukascopy. Run it on a workstation, not in CI.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from scalping.config import load_config
from scalping.data.download import FetchPolicy, run_exit_code, sync_symbol
from scalping.data.store import iter_days, missing_days


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scalping.data.pipeline")
    parser.add_argument("command", choices=("download", "status"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--block", action="append", dest="blocks")
    parser.add_argument(
        "--backend",
        choices=("bi5", "library"),
        default="bi5",
        help="bi5 is the historical hour files. library calls dukascopy-python fetch_ticks.",
    )
    parser.add_argument("--start", default=None, help="UTC date YYYY-MM-DD, overrides blocks")
    parser.add_argument("--end", default=None, help="UTC date YYYY-MM-DD, exclusive")
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds to wait between hourly Dukascopy requests (default 0.3)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Extra attempts after a 503 or read timeout (default 6)",
    )
    parser.add_argument(
        "--max-backoff",
        type=float,
        default=None,
        help="Cap on the exponential retry wait, in seconds (default 60)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg.data_dir
    pairs = tuple(p.upper() for p in args.pairs) if args.pairs else cfg.pairs
    if args.start and args.end:
        windows = [("custom", _date(args.start), _date(args.end))]
    else:
        chosen = cfg.blocks
        if args.blocks:
            chosen = tuple(cfg.block(b) for b in args.blocks)
        windows = [(b.id, b.start, b.end) for b in chosen]

    if args.command == "status":
        return _status(pairs, windows, data_dir)
    policy = FetchPolicy(
        request_delay=cfg.request_delay if args.delay is None else args.delay,
        max_retries=cfg.max_retries if args.max_retries is None else args.max_retries,
        max_backoff=cfg.max_backoff if args.max_backoff is None else args.max_backoff,
    )
    reports = []
    for pair in pairs:
        for block_id, start, end in windows:
            logging.info("sync %s %s %s -> %s backend=%s", pair, block_id, start.date(), end.date(), args.backend)
            report = sync_symbol(pair, start, end, data_dir, backend=args.backend, policy=policy)
            reports.append(report)
            print(
                f"{pair} {block_id}: fetched={report.fetched_hours} "
                f"cached={report.cached_hours} empty={report.empty_hours} "
                f"missed={len(report.missed_hours)} "
                f"tick_days={report.tick_days_written} bar_days={report.bar_days_written} "
                f"backend={report.backend}"
            )
            for note in report.notes:
                print(f"  {note}")
            for hour in report.missed_hours:
                print(f"  missed {hour}")
    missed = [hour for report in reports for hour in report.missed_hours]
    if missed:
        print(f"missed {len(missed)} hour(s); re-run the same command to fill the gaps")
    return run_exit_code(reports)


def _status(pairs, windows, data_dir) -> int:
    from pathlib import Path

    root = Path(data_dir)
    for pair in pairs:
        for block_id, start, end in windows:
            days = list(iter_days(start, end))
            missing = missing_days(root, pair, start, end, "ticks")
            print(
                f"{pair} {block_id}: days={len(days)} tick_days_ready={len(days) - len(missing)} "
                f"missing={len(missing)}"
            )
    return 0


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
