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
import pick_history
import screener_big_players
import screener_small_caps
from scorer import calculate, enforce_portfolio_cap
from utils import RESULTS_DIR, env, log, save_json, today_str, utcnow_iso, load_json

DEFAULT_BUDGET = float(env("DEFAULT_BUDGET_USD") or 10_000)


def _is_short_like(report: dict) -> bool:
    ar = (report.get("apex_rating") or "").upper()
    if ar in {"SHORT", "STRONG SHORT"}:
        return True
    tier = (report.get("conviction_tier") or "").upper()
    return "TIER_S" in tier


def _infer_diversity_bucket(report: dict) -> str:
    b = (report.get("sector_bucket") or "").upper()
    if "BIOTECH" in b or "PHARMA" in b:
        return "biotech"
    if "CHINA" in b:
        return "china"
    if any(k in b for k in ("TECH", "SOFTWARE", "SEMICONDUCTOR")):
        return "tech"
    name = (report.get("company_name") or "").lower()
    if any(x in name for x in ("therapeut", "biopharma", "pharma", "oncology", "gene therapy")):
        return "biotech"
    if "china" in name or "adr)" in name:
        return "china"
    if any(x in name for x in (" semiconductor", " software", "technology", " cloud ", " saas")):
        return "tech"
    return "other"


def _apply_correlation_caps(reports: list[dict]) -> list[dict]:
    """Enforce diversification caps; restore top filtered names if fewer than MIN picks."""
    MIN_PICKS = 4
    if not reports:
        return reports
    if len(reports) <= 1:
        return reports
    shorts = [r for r in reports if _is_short_like(r)]
    longs = [r for r in reports if not _is_short_like(r)]
    longs_sorted = sorted(
        longs,
        key=lambda x: x.get("final_probability_percentage", 0),
        reverse=True,
    )
    caps = {"biotech": 3, "china": 2, "tech": 3}
    counts = {k: 0 for k in caps}
    kept_longs: list[dict] = []
    skipped_longs: list[dict] = []
    for r in longs_sorted:
        bucket = _infer_diversity_bucket(r)
        if bucket in caps and counts[bucket] >= caps[bucket]:
            log(
                f"[Correlation] SKIP {r.get('ticker')}: {bucket} bucket at daily cap "
                f"(sector_bucket={r.get('sector_bucket')})"
            )
            skipped_longs.append(r)
            continue
        if bucket in counts:
            counts[bucket] += 1
        kept_longs.append(r)
    merged = shorts + kept_longs
    merged.sort(key=lambda x: x.get("final_probability_percentage", 0), reverse=True)
    skipped_longs.sort(key=lambda x: x.get("final_probability_percentage", 0), reverse=True)
    while len(merged) < MIN_PICKS and skipped_longs:
        restore = skipped_longs.pop(0)
        log(
            f"[Correlation] RESTORE {restore.get('ticker')} (prob "
            f"{restore.get('final_probability_percentage')}) to reach minimum {MIN_PICKS} picks"
        )
        merged.append(restore)
        merged.sort(key=lambda x: x.get("final_probability_percentage", 0), reverse=True)
    if not any(_is_short_like(r) for r in merged):
        log("[Correlation] NOTE: no SHORT / bearish-tier name in today's basket")
    return merged


def _candidate_blocked_ma_signal(c: dict) -> bool:
    for s in c.get("triggered_signals") or []:
        if isinstance(s, dict) and (s.get("name") or "") == "M&A_TAKE_PRIVATE_ANNOUNCED":
            return True
    return False


# ---------------------------------------------------------------------------
async def run_daily_apex(total_budget_usd: float | None = None) -> dict[str, Any]:
    """Execute the full APEX scan-analyze-score pipeline once.

    Returns the aggregated result dict that is also persisted to disk under
    ``latest.json`` and ``daily_picks_<DATE>.json`` under RESULTS_DIR (e.g. ``/data`` on Railway).
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
    candidates = await pick_history.filter_recent_exclusions(candidates)
    ma_ok: list[dict[str, Any]] = []
    for c in candidates:
        if _candidate_blocked_ma_signal(c):
            log(f"[Scheduler] EXCLUDE {c.get('ticker')}: M&A_TAKE_PRIVATE_ANNOUNCED signal")
            continue
        ma_ok.append(c)
    candidates = ma_ok
    log(
        f"Screeners returned {len((small_caps or []) + (big_players or []))} candidates → "
        f"{len(candidates)} after 14d duplicate + M&A signal filter"
    )

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
    results: list[dict[str, Any]] = []
    for c, r in zip(candidates, raw_results):
        if not r:
            continue
        si = ((c.get("snapshot") or {}).get("shortInterest") or {}).get("percentOfFloat")
        try:
            r["short_interest_pct_at_scan"] = float(si) if si is not None else None
        except (TypeError, ValueError):
            r["short_interest_pct_at_scan"] = None
        results.append(r)

    # Sort by probability descending
    results.sort(key=lambda x: x.get("final_probability_percentage", 0), reverse=True)

    results = _apply_correlation_caps(results)

    # Enforce portfolio-level 35% cap
    results = enforce_portfolio_cap(results, max_total_pct=35.0)

    payload = _build_payload(results, budget, started)
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
    cfg = load_json(RESULTS_DIR / "budget.json", default={})
    if isinstance(cfg, dict):
        if cfg.get("budget") is not None:
            try:
                return float(cfg["budget"])
            except (TypeError, ValueError):
                pass
        if cfg.get("total_budget_usd") is not None:
            try:
                return float(cfg["total_budget_usd"])
            except (TypeError, ValueError):
                pass
    leg = load_json(RESULTS_DIR.parent / "config" / "budget.json", default={})
    if isinstance(leg, dict) and leg.get("total_budget_usd") is not None:
        try:
            return float(leg["total_budget_usd"])
        except (TypeError, ValueError):
            pass
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
