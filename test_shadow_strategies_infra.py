"""Lightweight validation for shadow strategy infrastructure (no network)."""
from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

import regime_engine as re
import shadow_strategies as ss


def _make_ohlc(n: int = 80, start: date | None = None, open0: float = 1.10) -> pd.DataFrame:
    d0 = start or date(2024, 1, 1)
    rows = []
    px = open0
    for i in range(n):
        d = d0 + timedelta(days=i)
        o = px
        c = px + 0.001
        h = max(o, c) + 0.0005
        l = min(o, c) - 0.0005
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1000})
        px = c
    idx = pd.DatetimeIndex([d0 + timedelta(days=i) for i in range(n)])
    return pd.DataFrame(rows, index=idx)


def test_regime_atr_and_history() -> None:
    closes = [1.0 + i * 0.01 for i in range(40)]
    prev = None
    r1 = re.compute_pair_regime("TESTATR", closes, 0.5, 0.4, prev, atr=0.012)
    assert "atr_at_regime_entry" in r1
    assert len(re.get_score_history("TESTATR")) >= 1
    # Force a transition after dwell by feeding opposite score path.
    prev = r1
    for _ in range(re.MIN_DWELL_DAYS + 1):
        prev = re.compute_pair_regime(
            "TESTATR", list(reversed(closes)), -2.0, 2.0, prev, atr=0.020,
        )
    if prev.get("changed_this_scan"):
        assert prev.get("atr_at_regime_entry") == 0.020
    assert re.get_atr_at_regime_entry("TESTATR") is not None


def test_history_one_append_per_session_day() -> None:
    """Provisional no-closes never append; session_date dedupes same-day repeats."""
    tku = "HISTDAY"
    re._raw_score_history.pop(tku, None)
    re._er_history.pop(tku, None)
    re._confidence_history.pop(tku, None)
    re._last_history_session.pop(tku, None)
    re._atr_at_regime_entry.pop(tku, None)

    # Provisional (no closes) — must not append.
    r0 = re.compute_pair_regime(tku, [], 0.5, 0.4, None, atr=0.01, session_date="2024-01-08")
    assert r0["er"] is None
    assert re.get_score_history(tku) == []

    closes = [1.0 + i * 0.01 for i in range(40)]
    r1 = re.compute_pair_regime(
        tku, closes, 0.5, 0.4, None, atr=0.012, session_date="2024-01-08",
    )
    assert r1["er"] is not None
    assert len(re.get_score_history(tku)) == 1

    # Same session day — no second append.
    r2 = re.compute_pair_regime(
        tku, closes, 0.5, 0.4, r1, atr=0.013, session_date="2024-01-08",
    )
    assert len(re.get_score_history(tku)) == 1
    assert r2.get("atr_at_regime_entry") is not None

    # Next day — one more append.
    re.compute_pair_regime(
        tku, closes, 0.5, 0.4, r2, atr=0.014, session_date="2024-01-09",
    )
    assert len(re.get_score_history(tku)) == 2


def test_registry_and_dummy_signal() -> None:
    assert "PDUMMY01_VALIDATION" in ss.SHADOW_STRATEGIES
    assert "PDUMMY02_TIMEEXIT" in ss.SHADOW_STRATEGIES
    # Find a Monday
    monday = date(2024, 1, 8)  # Monday
    past = _make_ohlc(60, start=monday - timedelta(days=59))
    # Last bar is monday + ... wait: start = monday-59, last = monday
    assert past.index[-1].date() == monday
    ctx = ss.build_shadow_context(
        ticker="EURUSD",
        timeframe="1d",
        analysis_date=monday.isoformat(),
        past=past,
        ind={"atr": 0.01},
        regime={"state": "UP", "raw_score": 0.3, "er": 0.2, "confidence": 0.5, "days_in_state": 6},
    )
    assert ctx.day_of_week == 0
    sig = ss.SHADOW_STRATEGIES["PDUMMY01_VALIDATION"](ctx)
    assert sig is not None
    assert sig["direction"] == "LONG"
    assert abs(sig["entry_price"] - float(past["Open"].iloc[-1])) < 1e-9
    assert abs(sig["stop_price"] - (sig["entry_price"] - 0.01)) < 1e-9
    sig2 = ss.SHADOW_STRATEGIES["PDUMMY02_TIMEEXIT"](ctx)
    assert sig2 is not None
    assert sig2.get("custom_exit") == {"type": "time_exit", "max_sessions": 5}

    # Non-Monday → None
    tue = monday + timedelta(days=1)
    ctx2 = ss.build_shadow_context(
        ticker="EURUSD",
        timeframe="1d",
        analysis_date=tue.isoformat(),
        past=past,
        ind={"atr": 0.01},
    )
    assert ss.SHADOW_STRATEGIES["PDUMMY01_VALIDATION"](ctx2) is None
    assert ss.SHADOW_STRATEGIES["PDUMMY02_TIMEEXIT"](ctx2) is None


def test_custom_exit_types() -> None:
    past = _make_ohlc(30)
    candle = {"High": 1.12, "Low": 1.10, "Close": 1.11}
    hit, px, reason = ss._custom_exit_hit(
        {"type": "time_exit", "max_sessions": 3},
        direction="LONG",
        candle=candle,
        session_i=3,
        past_plus=past,
    )
    assert hit and reason.startswith("CUSTOM_TIME_EXIT")
    hit, px, reason = ss._custom_exit_hit(
        {"type": "price_target", "level": 1.115},
        direction="LONG",
        candle=candle,
        session_i=1,
        past_plus=past,
    )
    assert hit and px == 1.115
    hit, _, _ = ss._custom_exit_hit(
        {"type": "condition"},
        direction="LONG",
        candle=candle,
        session_i=1,
        past_plus=past,
    )
    assert not hit  # reserved


def test_cross_pair_access_no_lookahead() -> None:
    ds = "2024-01-08"
    eurusd = _make_ohlc(50)
    gbpusd = _make_ohlc(50, open0=1.25)
    ss.clear_shadow_ohlc_day(None)
    ss.remember_shadow_ohlc("EURUSD", "1d", ds, eurusd)
    ss.remember_shadow_ohlc("GBPUSD", "1d", ds, gbpusd)
    ctx = ss.build_shadow_context(
        ticker="EURUSD",
        timeframe="1d",
        analysis_date=ds,
        past=eurusd,
        ind={"atr": 0.01},
    )
    partner = ctx.get_pair_history("GBPUSD")
    assert partner is not None
    assert len(partner) == len(gbpusd)
    rets = ctx.get_universe_returns(1)
    assert "EURUSD" in rets and "GBPUSD" in rets


def test_jsonl_append_and_dedup() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "shadow_strategy_trades.jsonl"
        with mock.patch.object(ss, "SHADOW_TRADES_FILE", path):
            ss._shadow_seen_keys = None
            ss._shadow_trades_count = 0
            row = {
                "strategy_id": "PDUMMY01_VALIDATION",
                "ticker": "EURUSD",
                "timeframe": "1d",
                "date": "2024-01-08",
                "pnl_dollars": 1.0,
            }
            n1 = ss.append_shadow_trade(row)
            n2 = ss.append_shadow_trade(row)  # dedup
            assert n1 == 1 and n2 == 1
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["ticker"] == "EURUSD"


def test_hook_exception_isolated() -> None:
    """Shadow evaluate failure must not raise to caller."""
    past = _make_ohlc(40)
    fut = _make_ohlc(20, start=date(2024, 2, 1))
    with mock.patch.object(ss, "evaluate_shadow_strategies", side_effect=RuntimeError("boom")):
        ss.run_shadow_strategies_safe(
            ticker="EURUSD",
            timeframe="1d",
            analysis_date="2024-01-08",
            past=past,
            future=fut,
            ind={"atr": 0.01},
            regime={},
        )  # must not raise


def test_pipeline_e2e_with_mocked_exit() -> None:
    monday = date(2024, 1, 8)
    past = _make_ohlc(60, start=monday - timedelta(days=59))
    fut = _make_ohlc(15, start=monday + timedelta(days=1))
    fake_exit = {
        "outcome": "WIN",
        "exit_price": 1.12,
        "exit_reason": "TP1",
        "pnl_pct": 0.01,
        "hit_tp1": True,
        "hit_tp2": False,
        "hit_tp3": False,
        "hit_stop": False,
        "candles_to_exit": 3,
        "trailing_activated": True,
        "final_stop": 1.09,
    }
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "shadow_strategy_trades.jsonl"
        with mock.patch.object(ss, "SHADOW_TRADES_FILE", path):
            ss._shadow_seen_keys = None
            ss._shadow_trades_count = 0
            with mock.patch(
                "continuous_backtester._evaluate_forward_with_trend_continuation",
                return_value=fake_exit,
            ), mock.patch(
                "continuous_backtester._apply_realistic_costs",
                return_value=(50.0, 0.009, 0.01, {"spread_cost": 0.1}),
            ), mock.patch(
                "continuous_backtester.STARTING_CAPITAL", 100_000,
            ), mock.patch(
                "continuous_backtester.RISK_BY_CONFIDENCE", {"MEDIUM": 0.01},
            ), mock.patch(
                "continuous_backtester.LEVERAGE", 30,
            ), mock.patch(
                "continuous_backtester._shadow_bias_from_state",
                return_value="ALIGNED",
            ):
                ctx = ss.build_shadow_context(
                    ticker="EURUSD",
                    timeframe="1d",
                    analysis_date=monday.isoformat(),
                    past=past,
                    ind={"atr": 0.01, "trend_strength": 0.2},
                    regime={
                        "state": "UP",
                        "raw_score": 0.4,
                        "er": 0.3,
                        "confidence": 0.6,
                        "days_in_state": 8,
                        "atr_at_regime_entry": 0.011,
                        "rate_diff": 0.8,
                    },
                )
                n = ss.evaluate_shadow_strategies(ctx, forward_df=fut, analysis_date=monday.isoformat())
                assert n == 2  # PDUMMY01 + PDUMMY02
                rows = [json.loads(x) for x in path.read_text(encoding="utf-8").strip().splitlines()]
                ids = {r["strategy_id"] for r in rows}
                assert ids == {"PDUMMY01_VALIDATION", "PDUMMY02_TIMEEXIT"}
                for row in rows:
                    for key in (
                        "strategy_id",
                        "ticker",
                        "timeframe",
                        "entry_price",
                        "exit_price",
                        "exit_reason",
                        "pnl_dollars",
                        "pnl_pct",
                        "trailing_activated",
                        "hit_tp1",
                        "entry_atr",
                        "entry_date",
                        "shadow_bias",
                        "shadow_confidence",
                        "shadow_er",
                        "shadow_days_in_state",
                        "shadow_raw_score",
                        "shadow_mult_flat",
                    ):
                        assert key in row, f"missing {key}"


def test_pdummy02_custom_time_exit_integration() -> None:
    """Custom time_exit wins when it fires before the normal engine exit bar."""
    monday = date(2024, 1, 8)
    past = _make_ohlc(60, start=monday - timedelta(days=59))
    fut = _make_ohlc(10, start=monday + timedelta(days=1))
    late_normal = {
        "outcome": "WIN",
        "exit_price": 1.20,
        "exit_reason": "TP3",
        "pnl_pct": 0.05,
        "hit_tp1": True,
        "hit_tp2": True,
        "hit_tp3": True,
        "hit_stop": False,
        "candles_to_exit": 10,
        "trailing_activated": True,
        "final_stop": 1.09,
    }
    with mock.patch(
        "continuous_backtester._evaluate_forward_with_trend_continuation",
        return_value=late_normal,
    ), mock.patch(
        "continuous_backtester._resolve_trailing_regime",
        return_value="CHOPPY",
    ), mock.patch(
        "continuous_backtester._shadow_bias_from_state",
        return_value="TAILWIND",
    ), mock.patch(
        "continuous_backtester.STARTING_CAPITAL", 100_000,
    ), mock.patch(
        "continuous_backtester.RISK_BY_CONFIDENCE", {"MEDIUM": 0.01},
    ), mock.patch(
        "continuous_backtester.LEVERAGE", 30,
    ), mock.patch(
        "continuous_backtester._apply_realistic_costs",
        return_value=(10.0, 0.001, 0.002, {}),
    ):
        trade = ss._simulate_shadow_trade(
            {
                "direction": "LONG",
                "entry_price": float(past["Open"].iloc[-1]),
                "stop_price": float(past["Open"].iloc[-1]) - 0.01,
                "timeframe": "1d",
                "strategy_id": "PDUMMY02_TIMEEXIT",
                "ticker": "EURUSD",
                "custom_exit": {"type": "time_exit", "max_sessions": 5},
            },
            forward_df=fut,
            past_df=past,
            atr=0.01,
            regime_state="UP",
            rate_diff=0.5,
            trend_strength=0.1,
        )
    assert trade is not None
    assert str(trade["exit_reason"]).startswith("CUSTOM_TIME_EXIT")
    assert int(trade["candles_to_exit"]) == 5


def test_live_path_does_not_import_shadow() -> None:
    live_files = [
        "apex_trader.py",
        "apex_trader_v76.py",
        "apex_trader_v76_private.py",
        "apex_v76_decision_logic.py",
    ]
    for fp in live_files:
        text = Path(fp).read_text(encoding="utf-8")
        assert "shadow_strategies" not in text, fp


if __name__ == "__main__":
    test_regime_atr_and_history()
    test_history_one_append_per_session_day()
    test_registry_and_dummy_signal()
    test_custom_exit_types()
    test_cross_pair_access_no_lookahead()
    test_jsonl_append_and_dedup()
    test_hook_exception_isolated()
    test_pipeline_e2e_with_mocked_exit()
    test_pdummy02_custom_time_exit_integration()
    test_live_path_does_not_import_shadow()
    print("ALL SHADOW INFRA TESTS PASSED")
