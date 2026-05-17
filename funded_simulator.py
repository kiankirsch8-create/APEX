"""Funded challenge simulator — replays persisted rolling backtest trades."""
from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from utils import DATA_DIR, load_json, save_json

FUNDED_RESULTS_DIR = DATA_DIR / "funded_results"
FUNDED_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

FIRM_CONFIGS: dict[str, dict[str, Any]] = {
    "stella_one_step": {
        "name": "Stella One Step",
        "profit_target_pct": 10.0,
        "daily_loss_pct": 4.0,
        "max_drawdown_pct": 8.0,
        "trailing_drawdown": True,
        "leverage": 50,
    }
}


def get_funded_file(job_id: str) -> Path:
    return FUNDED_RESULTS_DIR / f"funded_{job_id}.json"


def funded_stop_flag_path(job_id: str) -> Path:
    return FUNDED_RESULTS_DIR / f"funded_stop_{job_id}.flag"


def _load_backtest_results() -> list[dict[str, Any]]:
    try:
        from continuous_backtester import load_all_results

        return load_all_results()
    except Exception:  # noqa: BLE001
        return []


def _save_funded(job_id: str, data: dict[str, Any]) -> None:
    save_json(get_funded_file(job_id), data)


def load_funded(job_id: str) -> dict[str, Any] | None:
    fp = get_funded_file(job_id)
    if not fp.is_file():
        return None
    return load_json(fp, default=None)


def _count_fail_reasons(failed_sims: list[dict[str, Any]]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for s in failed_sims:
        r = str(s.get("fail_reason", "unknown"))[:80]
        reasons[r] = reasons.get(r, 0) + 1
    return reasons


def _summarize_simulations(simulations: list[dict[str, Any]]) -> dict[str, Any]:
    if not simulations:
        return {}
    passed = [s for s in simulations if s.get("result") == "passed"]
    failed = [s for s in simulations if s.get("result") == "failed"]
    total = len(simulations)
    return {
        "total_sims": total,
        "passed": len(passed),
        "failed": len(failed),
        "pass_rate_pct": round(len(passed) / max(1, total) * 100, 1),
        "avg_days_pass": round(
            sum(s.get("days_taken", 0) for s in passed) / max(1, len(passed)),
            1,
        ),
        "avg_peak_capital": round(
            sum(float(s.get("peak_capital", 0) or 0) for s in simulations) / max(1, total),
            2,
        ),
        "fail_reasons": _count_fail_reasons(failed),
    }


def _run_single_simulation(
    sim_num: int,
    start_date: str,
    starting_balance: float,
    profit_target_pct: float,
    daily_loss_pct: float,
    max_drawdown_pct: float,
    trailing_drawdown: bool,
    trades_by_date: dict[str, list[dict[str, Any]]],
    all_dates: list[str],
) -> dict[str, Any]:
    capital = float(starting_balance)
    peak_capital = float(starting_balance)
    profit_target = starting_balance * (1.0 + profit_target_pct / 100.0)
    max_dd_floor = starting_balance * (1.0 - max_drawdown_pct / 100.0)
    daily_loss_limit = starting_balance * (daily_loss_pct / 100.0)

    daily_log: list[dict[str, Any]] = []
    status = "running"
    fail_reason = ""
    pass_date = ""

    sim_dates = [d for d in all_dates if d >= start_date]
    for date_str in sim_dates:
        day_trades = trades_by_date.get(date_str, [])
        day_pnl = 0.0
        day_loss = 0.0

        for trade in day_trades:
            raw_pnl = float(trade.get("pnl_dollars", 0) or 0)
            scale = capital / 10000.0 if starting_balance else 0.0
            pnl = raw_pnl * scale

            capital += pnl
            day_pnl += pnl
            if pnl < 0:
                day_loss += abs(pnl)

            if trailing_drawdown and capital > peak_capital:
                peak_capital = capital

            if trailing_drawdown:
                dd_floor = peak_capital * (1.0 - max_drawdown_pct / 100.0)
            else:
                dd_floor = max_dd_floor

            if capital <= dd_floor:
                status = "failed"
                fail_reason = (
                    f"Max drawdown breached on {date_str} — "
                    f"capital ${capital:.2f} below floor ${dd_floor:.2f}"
                )
                break

            if day_loss >= daily_loss_limit:
                status = "failed"
                fail_reason = (
                    f"Daily loss limit breached on {date_str} — "
                    f"lost ${day_loss:.2f} of ${daily_loss_limit:.2f} max"
                )
                break

        if status == "running" and capital >= profit_target:
            status = "passed"
            pass_date = date_str

        if trailing_drawdown and peak_capital > 0:
            dd_pct = round((peak_capital - capital) / peak_capital * 100.0, 2)
        else:
            dd_pct = round(
                (starting_balance - min(capital, starting_balance)) / starting_balance * 100.0,
                2,
            )

        daily_log.append(
            {
                "date": date_str,
                "trades": len(day_trades),
                "daily_pnl": round(day_pnl, 2),
                "capital": round(capital, 2),
                "daily_loss": round(day_loss, 2),
                "daily_loss_pct": round(day_loss / starting_balance * 100.0, 2) if starting_balance else 0.0,
                "drawdown_pct": dd_pct,
                "status": status,
            }
        )

        if status in ("failed", "passed"):
            break

    days_taken = len(daily_log)
    wins = sum(1 for d in daily_log if float(d.get("daily_pnl", 0) or 0) > 0)
    losses = sum(1 for d in daily_log if float(d.get("daily_pnl", 0) or 0) < 0)

    return {
        "sim_num": sim_num,
        "start_date": start_date,
        "result": status,
        "fail_reason": fail_reason,
        "pass_date": pass_date,
        "days_taken": days_taken,
        "start_capital": starting_balance,
        "end_capital": round(capital, 2),
        "peak_capital": round(peak_capital, 2),
        "profit_made": round(capital - starting_balance, 2),
        "profit_pct": round((capital - starting_balance) / starting_balance * 100.0, 1)
        if starting_balance
        else 0.0,
        "max_dd_reached": round(max((d.get("drawdown_pct", 0) or 0) for d in daily_log), 2) if daily_log else 0.0,
        "winning_days": wins,
        "losing_days": losses,
        "no_trade_days": max(0, days_taken - wins - losses),
        "total_trades": sum(int(d.get("trades", 0) or 0) for d in daily_log),
        "daily_log": daily_log,
    }


def run_funded_simulation(
    job_id: str,
    start_date: str,
    starting_balance: float = 10000.0,
    profit_target_pct: float = 10.0,
    daily_loss_pct: float = 4.0,
    max_drawdown_pct: float = 8.0,
    trailing_drawdown: bool = True,
    num_simulations: int = 1,
) -> None:
    """Replay completed backtest trades day-by-day; writes ``funded_{job_id}.json``."""
    all_results = _load_backtest_results()
    if not all_results:
        _save_funded(
            job_id,
            {
                "job_id": job_id,
                "status": "error",
                "error": "No backtest results found — run the engine first",
            },
        )
        return

    trades_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in all_results:
        if t.get("outcome") in ("WIN", "LOSS") and t.get("date"):
            trades_by_date[str(t["date"])].append(t)

    all_dates = sorted(trades_by_date.keys())
    if not all_dates:
        _save_funded(
            job_id,
            {
                "job_id": job_id,
                "status": "error",
                "error": "No completed trades with dates found",
            },
        )
        return

    simulations: list[dict[str, Any]] = []
    for sim_num in range(int(num_simulations)):
        if funded_stop_flag_path(job_id).is_file():
            try:
                funded_stop_flag_path(job_id).unlink(missing_ok=True)
            except OSError:
                pass
            funded_data = {
                "job_id": job_id,
                "status": "cancelled",
                "num_simulations": int(num_simulations),
                "sims_complete": sim_num,
                "simulations": simulations,
                "summary": _summarize_simulations(simulations),
            }
            _save_funded(job_id, funded_data)
            return

        if sim_num == 0:
            sim_start = start_date
        else:
            valid_starts = [d for d in all_dates if d >= "2023-01-01"]
            if not valid_starts:
                break
            sim_start = random.choice(valid_starts)

        sim_result = _run_single_simulation(
            sim_num=sim_num + 1,
            start_date=sim_start,
            starting_balance=starting_balance,
            profit_target_pct=profit_target_pct,
            daily_loss_pct=daily_loss_pct,
            max_drawdown_pct=max_drawdown_pct,
            trailing_drawdown=trailing_drawdown,
            trades_by_date=trades_by_date,
            all_dates=all_dates,
        )
        simulations.append(sim_result)

        funded_data: dict[str, Any] = {
            "job_id": job_id,
            "status": "running" if sim_num < int(num_simulations) - 1 else "complete",
            "num_simulations": int(num_simulations),
            "sims_complete": sim_num + 1,
            "starting_balance": starting_balance,
            "profit_target_pct": profit_target_pct,
            "daily_loss_pct": daily_loss_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "trailing_drawdown": trailing_drawdown,
            "simulations": simulations,
            "summary": _summarize_simulations(simulations),
        }
        _save_funded(job_id, funded_data)

    final = load_funded(job_id)
    if isinstance(final, dict) and final.get("status") not in ("cancelled", "error"):
        final["status"] = "complete"
        _save_funded(job_id, final)
