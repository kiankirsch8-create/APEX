"""Tests for funded-rule shadow layers: portfolio risk cap + equity daily stop."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import continuous_backtester as cb


def _trade_ctx(
    *,
    date: str = "2024-01-02",
    ticker: str = "AUDUSD",
    timeframe: str = "1h",
    strategy_id: str = "S01",
    direction: str = "LONG",
    entry_price: float = 0.6700,
    position_size: float = 10_000.0,
    max_risk_dollars: float = 150.0,
    nights_held: float = 3.0,
    pnl_dollars: float = -80.0,
    outcome: str = "LOSS",
) -> dict[str, Any]:
    return {
        "date": date,
        "ticker": ticker,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "direction": direction,
        "entry_price": entry_price,
        "position_size": position_size,
        "max_risk_dollars": max_risk_dollars,
        "nights_held": nights_held,
        "pnl_dollars": pnl_dollars,
        "outcome": outcome,
    }


def test_funded_config_shapes() -> None:
    assert cb.SHADOW_PORTFOLIO_CAP_ENABLED is True
    assert [n for n, _ in cb.SHADOW_PORTFOLIO_CAP_CONFIGS] == [
        "pcap_2pct",
        "pcap_3pct",
        "pcap_5pct",
        "pcap_8pct",
        "pcap_3pct_corr",
    ]
    corr = dict(cb.SHADOW_PORTFOLIO_CAP_CONFIGS)["pcap_3pct_corr"]
    assert corr["correlation_group"] is True
    assert corr["max_open_risk_pct"] == 3.0

    assert cb.SHADOW_EQUITY_DAILY_STOP_ENABLED is True
    assert [n for n, _ in cb.SHADOW_EQUITY_DAILY_STOP_CONFIGS] == [
        "eqstop_3",
        "eqstop_4",
        "eqstop_5",
    ]
    assert cb.SHADOW_EQUITY_BREACH_REPORT_PCT == 5.0


def test_shadow_expire_date_and_pos_key() -> None:
    assert cb._shadow_expire_date("2024-01-02", 0) == "2024-01-02"
    assert cb._shadow_expire_date("2024-01-02", 3) == "2024-01-05"
    assert cb._shadow_pos_key("audusd", "1H", "s01") == "AUDUSD|1h|S01"


def test_portfolio_cap_blocks_when_open_risk_exceeds() -> None:
    """Open book keyed by (ticker, tf, strategy); block when sum risk > cap."""
    runner = cb._ShadowPortfolioCapRunner()
    # $10k * 2% = $200 cap on pcap_2pct
    runner.on_new_day("2024-01-02")

    r1 = runner.process_trade(
        _trade_ctx(strategy_id="S01", max_risk_dollars=150.0, pnl_dollars=-10)
    )
    r2 = runner.process_trade(
        _trade_ctx(strategy_id="S02", max_risk_dollars=150.0, pnl_dollars=-10)
    )
    r3 = runner.process_trade(
        _trade_ctx(strategy_id="S03", max_risk_dollars=150.0, pnl_dollars=-10)
    )

    assert r1["pcap_2pct"]["taken"] is True
    assert r2["pcap_2pct"]["blocked"] is True
    assert r2["pcap_2pct"]["bound"] == "overall"
    assert r3["pcap_2pct"]["blocked"] is True

    # 8% cap ($800) allows all three
    assert r1["pcap_8pct"]["taken"] is True
    assert r2["pcap_8pct"]["taken"] is True
    assert r3["pcap_8pct"]["taken"] is True

    st2 = runner.curves["pcap_2pct"]
    assert st2.trades_taken == 1
    assert st2.trades_blocked == 2
    assert len(st2.book) == 1
    assert "AUDUSD|1h|S01" in st2.book


def test_portfolio_cap_correlation_group_binds() -> None:
    """Concentrated AUD book reports corr_AUD; diversified book reports overall."""
    runner = cb._ShadowPortfolioCapRunner()
    st = runner.curves["pcap_3pct_corr"]
    st.capital = 20_000.0  # 3% = $600
    st.peak_capital = 20_000.0
    st.day_anchor = 20_000.0
    runner.on_new_day("2024-01-02")

    assert (
        runner.process_trade(
            _trade_ctx(
                ticker="AUDUSD", strategy_id="S01", max_risk_dollars=300.0, pnl_dollars=1
            )
        )["pcap_3pct_corr"]["taken"]
        is True
    )
    assert (
        runner.process_trade(
            _trade_ctx(
                ticker="AUDJPY", strategy_id="S02", max_risk_dollars=300.0, pnl_dollars=1
            )
        )["pcap_3pct_corr"]["taken"]
        is True
    )
    r = runner.process_trade(
        _trade_ctx(ticker="AUDCAD", strategy_id="S03", max_risk_dollars=50.0, pnl_dollars=1)
    )
    assert r["pcap_3pct_corr"]["blocked"] is True
    assert r["pcap_3pct_corr"]["bound"] == "corr_AUD"
    assert st.blocked_by_corr >= 1

    # Diversified across disjoint currency pairs — overall binds, no group over.
    st.book.clear()
    st.trades_blocked = 0
    st.blocked_by_overall = 0
    st.blocked_by_corr = 0
    runner.process_trade(
        _trade_ctx(ticker="EURGBP", strategy_id="E1", max_risk_dollars=300.0, pnl_dollars=1)
    )
    runner.process_trade(
        _trade_ctx(ticker="AUDNZD", strategy_id="A1", max_risk_dollars=300.0, pnl_dollars=1)
    )
    r2 = runner.process_trade(
        _trade_ctx(ticker="USDJPY", strategy_id="J1", max_risk_dollars=50.0, pnl_dollars=1)
    )
    assert r2["pcap_3pct_corr"]["blocked"] is True
    assert r2["pcap_3pct_corr"]["bound"] == "overall"
    assert st.blocked_by_overall >= 1


def test_portfolio_cap_book_expires_by_nights_held() -> None:
    runner = cb._ShadowPortfolioCapRunner()
    runner.on_new_day("2024-01-02")
    runner.process_trade(
        _trade_ctx(
            date="2024-01-02",
            strategy_id="S01",
            max_risk_dollars=150.0,
            nights_held=2,
            pnl_dollars=-20,
        )
    )
    st = runner.curves["pcap_2pct"]
    assert len(st.book) == 1
    runner.on_new_day("2024-01-03")
    assert len(st.book) == 1
    # expire_date = Jan 4 → gone at start of Jan 4
    runner.on_new_day("2024-01-04")
    assert len(st.book) == 0


def test_portfolio_cap_real_curve_untouched_via_process() -> None:
    start = float(cb.STARTING_CAPITAL)
    runner = cb._ShadowPortfolioCapRunner()
    runner.on_new_day("2024-01-02")
    runner.process_trade(_trade_ctx(max_risk_dollars=500.0, pnl_dollars=-500))
    assert cb.STARTING_CAPITAL == start
    assert "real" not in runner.curves
    assert "real_unguarded" not in runner.curves


def test_equity_stop_blocks_new_entries_on_mtm_breach() -> None:
    runner = cb._ShadowEquityDailyStopRunner()
    runner.on_new_day("2024-01-02")
    ctx = _trade_ctx(
        date="2024-01-02",
        ticker="EURUSD",
        entry_price=1.1000,
        position_size=100_000.0,
        max_risk_dollars=200.0,
        nights_held=5,
        pnl_dollars=-900.0,
        outcome="LOSS",
    )
    with patch.object(cb, "_shadow_mark_close_price", return_value=1.1000):
        out = runner.process_trade(ctx, close_prices={"EURUSD": 1.1000})
    assert out["eqstop_3"]["taken"] is True
    assert out["real_unguarded"]["taken"] is True

    # Day 2: price dumps → MTM ~ -100000*0.006 = -600 = -6% of 10k
    with patch.object(cb, "_shadow_mark_close_price", return_value=1.0940):
        runner.on_new_day("2024-01-03", close_prices={"EURUSD": 1.0940})

    st3 = runner.curves["eqstop_3"]
    assert st3.day_stopped is True
    assert "2024-01-03" in st3.equity_breach_dates
    assert "2024-01-03" in st3.equity_5pct_breach_dates

    real = runner.curves["real_unguarded"]
    assert real.day_stopped is False
    assert "2024-01-03" in real.equity_5pct_breach_dates

    ctx2 = _trade_ctx(
        date="2024-01-03",
        ticker="GBPUSD",
        strategy_id="S99",
        entry_price=1.25,
        position_size=5_000.0,
        max_risk_dollars=50.0,
        nights_held=1,
        pnl_dollars=10.0,
        outcome="WIN",
    )
    with patch.object(cb, "_shadow_mark_close_price", return_value=1.0940):
        out2 = runner.process_trade(
            ctx2, close_prices={"EURUSD": 1.0940, "GBPUSD": 1.25}
        )
    assert out2["eqstop_3"]["blocked"] is True
    assert out2["real_unguarded"]["taken"] is True
    assert "EURUSD|1h|S01" in st3.book


def test_equity_5pct_report_on_all_curves_and_real() -> None:
    runner = cb._ShadowEquityDailyStopRunner()
    summaries = runner.summaries()
    assert "real_unguarded" in summaries
    for name, _ in cb.SHADOW_EQUITY_DAILY_STOP_CONFIGS:
        assert name in summaries
        assert "equity_5pct_breach_days" in summaries[name]
        assert "equity_5pct_breach_dates" in summaries[name]
    assert summaries["real_unguarded"]["is_real_unguarded"] is True
    assert summaries["real_unguarded"]["equity_5pct_breach_days"] == 0


def test_equity_stop_does_not_force_close() -> None:
    runner = cb._ShadowEquityDailyStopRunner()
    runner.on_new_day("2024-01-02")
    with patch.object(cb, "_shadow_mark_close_price", return_value=1.10):
        runner.process_trade(
            _trade_ctx(
                date="2024-01-02",
                ticker="EURUSD",
                entry_price=1.10,
                position_size=100_000.0,
                nights_held=4,
                max_risk_dollars=100.0,
                pnl_dollars=-500,
            ),
            close_prices={"EURUSD": 1.10},
        )
    with patch.object(cb, "_shadow_mark_close_price", return_value=1.094):
        runner.on_new_day("2024-01-03", close_prices={"EURUSD": 1.094})
        runner.finalize_day("2024-01-03", close_prices={"EURUSD": 1.094})
    assert len(runner.curves["eqstop_5"].book) == 1


def test_equity_mtm_uses_provided_close_not_zero() -> None:
    book = {
        "k": cb._FundedOpenPos(
            key="k",
            ticker="EURUSD",
            timeframe="1h",
            strategy_id="S01",
            direction="LONG",
            entry_date="2024-01-02",
            expire_date="2024-01-05",
            entry_price=1.1000,
            position_size=10_000.0,
            risk_at_stop=50.0,
            pnl_dollars=-20.0,
            currencies=("EUR", "USD"),
        )
    }
    mtm, missing = cb._book_mtm(book, {"EURUSD": 1.0950})
    assert missing == 0
    assert abs(mtm - (10_000.0 * (1.0950 - 1.1000))) < 1e-6

    mtm2, missing2 = cb._book_mtm(book, {})
    assert missing2 == 1
    assert mtm2 == 0.0


def test_persistence_roundtrip() -> None:
    pcap = cb._ShadowPortfolioCapRunner()
    pcap.on_new_day("2024-01-02")
    pcap.process_trade(_trade_ctx(max_risk_dollars=100.0, nights_held=3, pnl_dollars=-5))
    chrono: dict[str, Any] = {}
    cb._persist_shadow_portfolio_cap_state(chrono, pcap)
    pcap2 = cb._ShadowPortfolioCapRunner.from_chrono(chrono)
    assert len(pcap2.curves["pcap_5pct"].book) == 1

    eq = cb._ShadowEquityDailyStopRunner()
    eq.on_new_day("2024-01-02")
    with patch.object(cb, "_shadow_mark_close_price", return_value=0.67):
        eq.process_trade(_trade_ctx(nights_held=3), close_prices={"AUDUSD": 0.67})
    cb._persist_shadow_equity_daily_stop_state(chrono, eq)
    eq2 = cb._ShadowEquityDailyStopRunner.from_chrono(chrono)
    assert len(eq2.curves["eqstop_5"].book) == 1
    assert "real_unguarded" in eq2.curves
