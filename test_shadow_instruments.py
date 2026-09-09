"""Tests for unified shadow instrument evaluation."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import continuous_backtester as cb
import shadow_instruments as si


def test_hard_block_bypassed_in_shadow_eval() -> None:
    si.enter_shadow_eval("AUDUSD", shadow_class="blocked_fx")
    try:
        assert cb._hard_block_skip_reason("AUDUSD", "T01") is None
        assert si.trade_fields_for_row("AUDUSD")["shadow_class"] == "blocked_fx"
    finally:
        si.exit_shadow_eval()
    # After exit_shadow_eval the in-scan bypass is gone, but a completed shadow
    # trade row must still pass the chrono post-scan gate.
    assert cb._hard_block_skip_reason("AUDUSD", "T01") is not None
    assert (
        cb._hard_block_skip_reason("AUDUSD", "T01", shadow_trade_row=True) is None
    )


def test_hard_block_still_blocks_real_curve() -> None:
    si.exit_shadow_eval()
    assert cb._hard_block_skip_reason("AUDUSD", "T01") is not None
    assert cb._hard_block_skip_reason("AUDUSD", "T01", shadow_trade_row=False) is not None


def test_shadow_ab_histories_per_class() -> None:
    si.reset_run_state()
    cb._STRAT_PNL_HISTORY.clear()
    cb._STRAT_PNL_HISTORY["T01"] = [50.0]
    row = {
        "strategy_id": "T01",
        "confidence": "HIGH",
        "macro_bias": "NEUTRAL",
        "outcome": "WIN",
        "pnl_dollars": 120.0,
        "max_risk_dollars": 100.0,
        "ticker": "EURAUD",
    }
    si._record_ab_histories(row, "blocked_fx")
    hist = si._shadow_strat_by_class["blocked_fx"]["T01"]
    assert hist["n"] == 1
    assert hist["last3"] == [120.0]
    assert cb._STRAT_PNL_HISTORY["T01"] == [50.0]


def test_compute_summary_streams_jsonl(tmp_path: Path, monkeypatch: Any) -> None:
    job_id = "test-job-summary"
    monkeypatch.setattr(si, "DATA_DIR", tmp_path)
    jsonl = si.job_instruments_path(job_id)
    rows = [
        {
            "job_id": job_id,
            "shadow_instrument": "AUDUSD",
            "ticker": "AUDUSD",
            "date": "2022-03-01",
            "outcome": "WIN",
            "pnl_dollars": 100.0,
            "pnl_r": 1.0,
        },
        {
            "job_id": job_id,
            "shadow_instrument": "AUDUSD",
            "ticker": "AUDUSD",
            "date": "2022-06-01",
            "outcome": "LOSS",
            "pnl_dollars": -50.0,
            "pnl_r": -0.5,
        },
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    summary = si.compute_summary(job_id)
    aud = summary["by_ticker"]["AUDUSD"]
    assert aud["net_r"] == 0.5
    assert aud["estimated_dollars"]["net_pnl"] == 50.0
    assert "2022" in aud["by_year"]


def test_instrument_sizing_contracts() -> None:
    ai: dict = {"stop_loss": 99.0}
    spec = si.SHADOW_INSTRUMENTS["ES"]
    si.apply_instrument_sizing(ai, spec=spec, entry=100.0, risk_dollars=100.0)
    assert ai["_instrument_contracts"] > 0
    assert ai["_max_risk_dollars"] == 100.0


def test_cfd_financing_sign_long_pays_short_receives() -> None:
    spec = dict(si.SHADOW_INSTRUMENTS["XAUUSD"])
    notional = 100_000.0
    nights = 10.0
    long_net, _, long_costs = si.apply_instrument_costs(
        spec=spec,
        direction="LONG",
        contracts=1.0,
        entry=2000.0,
        notional=notional,
        raw_pct=0.01,
        nights_held=nights,
    )
    short_net, _, short_costs = si.apply_instrument_costs(
        spec=spec,
        direction="SHORT",
        contracts=1.0,
        entry=2000.0,
        notional=notional,
        raw_pct=0.01,
        nights_held=nights,
    )
    assert long_costs["swap_amount"] < 0
    assert short_costs["swap_amount"] > 0
    assert abs(short_costs["swap_amount"] - abs(long_costs["swap_amount"]) * 0.5) < 0.02
    assert long_net < short_net


def test_fx_pair_candidates_majors_only() -> None:
    pairs = si._fx_pair_candidates()
    assert len(pairs) == 110
    assert pairs == sorted(pairs)
    assert all(
        pair[:3] in si.SHADOW_FX_EXTRA_CURRENCIES
        and pair[3:] in si.SHADOW_FX_EXTRA_CURRENCIES
        for pair in pairs
    )
    assert "USDMXN" not in pairs


def test_extra_fx_disabled_by_default(monkeypatch: Any) -> None:
    assert si.SHADOW_EXTRA_FX_ENABLED is False
    monkeypatch.setattr(si, "PART1_DATA_EXCLUDED_FX", frozenset())
    probe_calls: list[str] = []

    def fake_probe(
        pair: str,
        start_d: date,
        end_d: date,
        *,
        yf_download_fn: Any,
        hourly_ok: bool,
        cache: dict[str, dict[str, Any]],
    ) -> tuple[bool, str, bool]:
        probe_calls.append(pair)
        return True, "ok", True

    monkeypatch.setattr(si, "_probe_fx_ohlc_cached", fake_probe)
    monkeypatch.setattr(
        si,
        "_test_non_forex_ohlc",
        lambda *a, **k: (False, "skip", False),
    )
    monkeypatch.setattr(si, "time", type("T", (), {"sleep": staticmethod(lambda _: None)})())
    discovery = si.init_shadow_universe(
        start_date="2021-01-01",
        end_date="2025-01-01",
        blocked_pairs=frozenset({"AUDUSD"}),
        excluded_pairs=frozenset(),
        real_chrono_tickers=frozenset(),
        yf_download_fn=lambda *a, **k: None,
        hourly_earliest_fn=lambda: date(2020, 1, 1),
        enabled=True,
    )
    assert discovery["extra_fx_enabled"] is False
    assert discovery["extra_fx_loaded"] == 0
    assert probe_calls == ["AUDUSD"]
    assert discovery["reason_by_ticker"]["AUDUSD"] == "BLOCKED_PAIR"


def test_extra_fx_cap_stops_probing_when_enabled(monkeypatch: Any) -> None:
    monkeypatch.setattr(si, "SHADOW_EXTRA_FX_ENABLED", True)
    monkeypatch.setattr(si, "SHADOW_MAX_EXTRA_FX", 2)
    monkeypatch.setattr(si, "PART1_DATA_EXCLUDED_FX", frozenset())
    probe_calls: list[str] = []

    def fake_probe(
        pair: str,
        start_d: date,
        end_d: date,
        *,
        yf_download_fn: Any,
        hourly_ok: bool,
        cache: dict[str, dict[str, Any]],
    ) -> tuple[bool, str, bool]:
        probe_calls.append(pair)
        return True, "ok", True

    monkeypatch.setattr(si, "_probe_fx_ohlc_cached", fake_probe)
    monkeypatch.setattr(
        si,
        "_test_non_forex_ohlc",
        lambda *a, **k: (False, "skip", False),
    )
    monkeypatch.setattr(si, "time", type("T", (), {"sleep": staticmethod(lambda _: None)})())
    discovery = si.init_shadow_universe(
        start_date="2021-01-01",
        end_date="2025-01-01",
        blocked_pairs=frozenset(),
        excluded_pairs=frozenset(),
        real_chrono_tickers=frozenset(),
        yf_download_fn=lambda *a, **k: None,
        hourly_earliest_fn=lambda: date(2020, 1, 1),
        enabled=True,
    )
    assert discovery["extra_fx_loaded"] == 2
    assert discovery["extra_fx_untested"] == 110 - 2
    assert len(probe_calls) == 2


def test_rebuild_histories_sorts_by_close_ts(tmp_path: Path, monkeypatch: Any) -> None:
    job_id = "sort-job"
    monkeypatch.setattr(si, "DATA_DIR", tmp_path)
    jsonl = si.job_instruments_path(job_id)
    rows = [
        {
            "job_id": job_id,
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "outcome": "WIN",
            "pnl_dollars": 10.0,
            "date": "2022-01-01",
            "ticker": "AAAAAA",
            "close_ts": 1641024000,
            "shadow_class": "blocked_fx",
        },
        {
            "job_id": job_id,
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "outcome": "LOSS",
            "pnl_dollars": -5.0,
            "date": "2022-01-01",
            "ticker": "ZZZZZZ",
            "shadow_class": "blocked_fx",
        },
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    si.rebuild_histories(job_id)
    hist = si._shadow_strat_by_class["blocked_fx"]["T01"]
    assert hist["last3"] == [-5.0, 10.0]


def test_persist_dual_writes_blocked_pairs_file(tmp_path: Path, monkeypatch: Any) -> None:
    job_id = "dual-write"
    monkeypatch.setattr(si, "DATA_DIR", tmp_path)
    si._shadow_universe["class_by_ticker"] = {"AUDUSD": "blocked_fx", "AUDCAD": "blocked_fx"}
    si._shadow_universe["reason_by_ticker"] = {
        "AUDUSD": "BLOCKED_PAIR",
        "AUDCAD": "EXCLUDED_PAIR",
    }
    si.persist_shadow_trade(
        {
            "ticker": "AUDUSD",
            "outcome": "WIN",
            "pnl_dollars": 10.0,
            "max_risk_dollars": 10.0,
            "strategy_id": "T01",
        },
        job_id=job_id,
    )
    si.persist_shadow_trade(
        {
            "ticker": "AUDCAD",
            "outcome": "LOSS",
            "pnl_dollars": -5.0,
            "max_risk_dollars": 10.0,
            "strategy_id": "T01",
        },
        job_id=job_id,
    )
    inst = si.job_instruments_path(job_id).read_text(encoding="utf-8").strip().splitlines()
    blocked = si.job_blocked_pairs_path(job_id).read_text(encoding="utf-8").strip().splitlines()
    assert len(inst) == 2
    assert len(blocked) == 2
    reasons = {json.loads(line)["shadow_reason"] for line in blocked}
    assert reasons == {"BLOCKED_PAIR", "EXCLUDED_PAIR"}


def test_assert_blocked_pairs_have_trades(tmp_path: Path, monkeypatch: Any) -> None:
    job_id = "assert-job"
    monkeypatch.setattr(si, "DATA_DIR", tmp_path)
    discovery = {"loaded_tickers": ["AUDUSD", "USDCAD", "AUDCAD"]}
    missing = si.assert_blocked_pairs_have_trades(
        job_id,
        blocked_pairs=frozenset({"AUDUSD", "USDCAD"}),
        discovery=discovery,
    )
    assert missing == ["AUDUSD", "USDCAD"]
    si.job_instruments_path(job_id).write_text(
        json.dumps(
            {
                "ticker": "AUDUSD",
                "shadow_instrument": "AUDUSD",
                "outcome": "WIN",
                "pnl_dollars": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    missing2 = si.assert_blocked_pairs_have_trades(
        job_id,
        blocked_pairs=frozenset({"AUDUSD", "USDCAD"}),
        discovery=discovery,
    )
    assert missing2 == ["USDCAD"]


def test_real_curve_tickers_exclude_blocked() -> None:
    real = cb._real_chrono_forex_tickers()
    assert "AUDUSD" not in real
    assert "EURUSD" in real


def test_shadow_flag_off() -> None:
    assert cb._shadow_eval_active("AUDUSD", chrono_yfinance=False) is False
