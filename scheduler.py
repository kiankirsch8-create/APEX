"""APEX scheduler — runs the daily pipeline.

  python scheduler.py --run-now    # one-shot (no daemon)
  python scheduler.py              # blocks and runs daily at 07:00 local time
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import schedule

import analyzer
import screener_big_players
import screener_small_caps
from scorer import calculate, enforce_portfolio_cap
from utils import RESULTS_DIR, env, log, save_json, today_str, utcnow_iso, load_json

DEFAULT_BUDGET = float(env("DEFAULT_BUDGET_USD") or 10_000)


# ---------------------------------------------------------------------------
async def run_daily_apex(
    total_budget_usd: float | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Execute the full APEX scan-analyze-score pipeline once.

    Returns the aggregated result dict.

    Parameters
    ----------
    total_budget_usd : float | None
        Override for the user's total investment budget. Falls back to the
        config-stored value or the ``DEFAULT_BUDGET_USD`` env var.
    persist : bool, default True
        If True (CLI / cron), the payload is also written to
        ``results/latest.json`` and ``results/daily_picks_<DATE>.json``.
        Set to False when the caller manages storage itself (e.g. the API
        server's in-memory ``LATEST_RESULTS`` global on Railway, where the
        filesystem is reset on every deploy).
    """
    budget = float(total_budget_usd if total_budget_usd is not None else _read_budget())
    log(f"APEX scan starting — budget=${budget:,.0f}")
    started = time.time()

    small_task = asyncio.create_task(screener_small_caps.scan(top_n=3))
    big_task = asyncio.create_task(screener_big_players.scan(top_n=2))

    try:
        small_caps, big_players = await asyncio.gather(small_task, big_task)
    except Exception as e:  # noqa: BLE001
        log(f"Screeners failed: {e}", "error")
        small_caps, big_players = [], []

    candidates = (small_caps or []) + (big_players or [])
    log(f"Screeners returned {len(candidates)} candidates")

    # Analyze in parallel but bounded
    sem = asyncio.Semaphore(3)

    async def _analyze(c: dict) -> dict | None:
        async with sem:
            try:
                report = await analyzer.analyze_stock(
                    ticker=c["ticker"],
                    section=c["section"],
                    triggered_signals=c.get("triggered_signals", []),
                    market_data=c,
                    total_budget_usd=budget,
                )
                return calculate(report)
            except Exception as e:  # noqa: BLE001
                log(f"Analyze failed for {c.get('ticker')}: {e}", "error")
                return None

    raw_results = await asyncio.gather(*[_analyze(c) for c in candidates])
    results = [r for r in raw_results if r]

    # Sort by probability descending
    results.sort(key=lambda x: x.get("final_probability_percentage", 0), reverse=True)

    # Enforce portfolio-level 35% cap
    results = enforce_portfolio_cap(results, max_total_pct=35.0)

    payload = _build_payload(results, budget, started)
    if persist:
        _persist(payload)
    log(f"APEX complete: {len(results)} picks in {payload['duration_seconds']:.1f}s")
    return payload


# ---------------------------------------------------------------------------
def _build_payload(results: list[dict], budget: float, started: float) -> dict:
    date_str = today_str()
    small = [r for r in results if r.get("section") == "SMALL_CAP"]
    big = [r for r in results if r.get("section") == "BIG_PLAYER"]
    return {
        "date": date_str,
        "generated_at": utcnow_iso(),
        "duration_seconds": round(time.time() - started, 2),
        "total_budget_usd": budget,
        "total_picks": len(results),
        "small_cap_count": len(small),
        "big_player_count": len(big),
        "small_cap_picks": small,
        "big_player_picks": big,
        "top_pick": results[0] if results else None,
        "all_picks": results,
    }


def _persist(payload: dict) -> None:
    save_json(RESULTS_DIR / f"daily_picks_{payload['date']}.json", payload)
    save_json(RESULTS_DIR / "latest.json", payload)


def _read_budget() -> float:
    cfg = load_json(RESULTS_DIR.parent / "config" / "budget.json", default={})
    if isinstance(cfg, dict) and cfg.get("total_budget_usd"):
        return float(cfg["total_budget_usd"])
    return DEFAULT_BUDGET


# ---------------------------------------------------------------------------
def _schedule_loop() -> None:
    log("APEX scheduler started — daily run at 07:00 local time.")

    def _job() -> None:
        try:
            asyncio.run(run_daily_apex())
        except Exception as e:  # noqa: BLE001
            log(f"Scheduled run failed: {e}", "error")

    schedule.every().day.at("07:00").do(_job)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="APEX daily scan scheduler")
    parser.add_argument("--run-now", action="store_true", help="Run pipeline once and exit")
    parser.add_argument("--budget", type=float, default=None, help="Override total budget USD")
    args = parser.parse_args()

    if args.run_now:
        asyncio.run(run_daily_apex(args.budget))
        return

    _schedule_loop()


if __name__ == "__main__":
    main()
