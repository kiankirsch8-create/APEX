"""Tick fills: intrabar SL/TP, spread, slippage, and commission on both sides."""

from __future__ import annotations

import pytest

from scalping.costs import CostModel, FillKind, OrderType, Side, convert_to_eur, execution_price
from scalping.engine import FxBook, run_backtest
from scalping.fills import get_simulate, simulate_bar
from scalping.instruments import get_instrument
from scalping.tests.support import Scripted, market_from_minutes
from scalping import fills
from scalping.strategies.base import OrderIntent


EURUSD = get_instrument("EURUSD")
SLIP = 0.00002  # 2 points


def _costs(commission: float = 3.5, points: float = 2.0) -> CostModel:
    return CostModel(commission_per_lot_side=commission, slippage_points=points, account_currency="EUR")


def _buy(sl: float | None = None, tp: float | None = None, lots: float = 0.10) -> OrderIntent:
    return OrderIntent(type=OrderType.MARKET, side=Side.BUY, lots=lots, sl=sl, tp=tp, tag="probe")


def test_market_buy_uses_ask_plus_slippage_and_charges_both_sides():
    # Bar 0 is the signal. Bar 1 has two quotes so entry and the end-of-data
    # flatten are different ticks.
    market = market_from_minutes(
        [
            [(1.20000, 1.20020)],
            [(1.10000, 1.10020), (1.10010, 1.10030)],
        ]
    )
    result = run_backtest(market, Scripted({0: [_buy(lots=1.0)]}), EURUSD, _costs())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(1.10022)
    assert trade.entry_price != pytest.approx(1.20022)
    assert trade.exit_reason == "eod"
    assert trade.exit_price == pytest.approx(1.10010 - SLIP)
    assert trade.spread_entry == pytest.approx(0.00020)
    assert trade.slippage_entry == pytest.approx(SLIP)
    assert trade.slippage_exit == pytest.approx(SLIP)
    assert trade.commission > 0.0
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission)
    assert trade.entry_time < trade.exit_time
    # Commission is per lot per side, converted at the worse EURUSD side.
    assert trade.commission == pytest.approx(3.5 / 1.10000 + 3.5 / 1.10010)


def test_wider_spread_worsens_the_market_buy():
    tight = market_from_minutes([[(1.10000, 1.10010)], [(1.10000, 1.10010), (1.10000, 1.10010)]])
    wide = market_from_minutes([[(1.10000, 1.10040)], [(1.10000, 1.10040), (1.10000, 1.10040)]])
    plan = {0: [_buy(lots=0.10)]}
    tight_px = run_backtest(tight, Scripted(plan), EURUSD, _costs()).trades[0].entry_price
    wide_px = run_backtest(wide, Scripted(plan), EURUSD, _costs()).trades[0].entry_price
    assert wide_px - tight_px == pytest.approx(0.00030)


def test_stop_loss_fills_intrabar_not_at_the_bar_close():
    # Tick 2 would look like a winner if the engine only saw the minute close.
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [
                (1.10000, 1.10020),
                (1.09900, 1.09920),
                (1.10300, 1.10320),
            ],
        ]
    )
    sl = 1.09950
    result = run_backtest(market, Scripted({0: [_buy(sl=sl, lots=0.10)]}), EURUSD, _costs())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "sl"
    assert trade.exit_price == pytest.approx(1.09900 - SLIP)
    assert trade.exit_price < sl
    assert trade.net_pnl < 0.0
    assert trade.exit_time < "2020-01-06T10:01:02"
    assert trade.commission > 0.0
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission)


def test_stop_loss_touch_is_level_minus_slippage():
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.10000, 1.10020), (1.09950, 1.09970), (1.10080, 1.10100)],
        ]
    )
    result = run_backtest(market, Scripted({0: [_buy(sl=1.09950)]}), EURUSD, _costs())
    trade = result.trades[0]
    assert trade.exit_reason == "sl"
    assert trade.exit_price == pytest.approx(1.09950 - SLIP)


def test_take_profit_fills_at_the_level_before_the_bar_close():
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [
                (1.10000, 1.10020),
                (1.10150, 1.10170),
                (1.10250, 1.10270),
            ],
        ]
    )
    tp = 1.10100
    result = run_backtest(market, Scripted({0: [_buy(tp=tp)]}), EURUSD, _costs())
    trade = result.trades[0]
    assert trade.exit_reason == "tp"
    assert trade.exit_price == pytest.approx(tp)
    assert trade.slippage_exit == pytest.approx(0.0)
    assert trade.exit_time < "2020-01-06T10:01:02"
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission)


def test_sl_wins_when_a_later_tick_would_have_hit_tp():
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [
                (1.10000, 1.10020),
                (1.09940, 1.09960),
                (1.10200, 1.10220),
            ],
        ]
    )
    result = run_backtest(
        market,
        Scripted({0: [_buy(sl=1.09950, tp=1.10150)]}),
        EURUSD,
        _costs(),
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "sl"
    assert result.trades[0].net_pnl < 0.0


def test_short_stop_loss_is_level_plus_slippage():
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.10000, 1.10020), (1.10130, 1.10150)],
        ]
    )
    intent = OrderIntent(type=OrderType.MARKET, side=Side.SELL, lots=0.10, sl=1.10120, tag="short")
    result = run_backtest(market, Scripted({0: [intent]}), EURUSD, _costs())
    trade = result.trades[0]
    assert trade.side == "sell"
    assert trade.entry_price == pytest.approx(1.10000 - SLIP)
    assert trade.exit_reason == "sl"
    assert trade.exit_price == pytest.approx(1.10150 + SLIP)


def test_limit_buy_fills_at_the_limit_when_ask_trades_through():
    intent = OrderIntent(type=OrderType.LIMIT, side=Side.BUY, lots=0.10, price=1.09900, tag="lim")
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.09980, 1.10000), (1.09840, 1.09860)],
        ]
    )
    result = run_backtest(market, Scripted({0: [intent]}), EURUSD, _costs())
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(1.09900)
    assert trade.slippage_entry == pytest.approx(0.0)
    assert trade.spread_entry == pytest.approx(0.00020)


def test_stop_buy_gaps_and_pays_the_ask_plus_slippage():
    intent = OrderIntent(type=OrderType.STOP, side=Side.BUY, lots=0.10, price=1.10100, tag="brk")
    touched = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.10080, 1.10100), (1.10090, 1.10110)],
        ]
    )
    gapped = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.10040, 1.10060), (1.10280, 1.10300)],
        ]
    )
    touch = run_backtest(touched, Scripted({0: [intent]}), EURUSD, _costs()).trades[0]
    gap = run_backtest(gapped, Scripted({0: [intent]}), EURUSD, _costs()).trades[0]
    assert touch.entry_price == pytest.approx(1.10100 + SLIP)
    assert gap.entry_price == pytest.approx(1.10300 + SLIP)
    assert gap.entry_price > 1.10100 + SLIP


def test_signal_cannot_fill_on_the_bar_that_produced_it():
    market = market_from_minutes(
        [
            [(1.20000, 1.20020), (1.20000, 1.20020)],
            [(1.10000, 1.10020), (1.10010, 1.10030)],
        ]
    )
    strategy = Scripted({0: [_buy()]})
    result = run_backtest(market, strategy, EURUSD, _costs())
    assert strategy.seen == [0, 1]
    assert result.trades[0].entry_time.startswith("2020-01-06T10:01:00")
    assert result.trades[0].entry_price == pytest.approx(1.10022)


def test_usdjpy_pnl_and_commission_convert_to_eur():
    usdjpy = market_from_minutes(
        [
            [(150.000, 150.004)],
            [(150.000, 150.004), (150.100, 150.104)],
        ]
    )
    eurusd = market_from_minutes(
        [
            [(1.1000, 1.1002)],
            [(1.1000, 1.1002), (1.1000, 1.1002)],
        ]
    )
    result = run_backtest(
        usdjpy,
        Scripted({0: [_buy(lots=0.10)]}),
        get_instrument("USDJPY"),
        _costs(),
        fx=FxBook(get_instrument("USDJPY"), eurusd),
    )
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(150.006)
    assert trade.exit_price == pytest.approx(150.098)
    quote = (150.098 - 150.006) * 0.10 * 100_000
    gross = convert_to_eur(
        quote,
        "JPY",
        eurusd_bid=1.1000,
        eurusd_ask=1.1002,
        usdjpy_bid=150.100,
        usdjpy_ask=150.104,
    )
    commission = -convert_to_eur(-0.35, "USD", eurusd_bid=1.1000, eurusd_ask=1.1002)
    assert trade.gross_pnl == pytest.approx(gross)
    assert trade.commission == pytest.approx(commission * 2)
    assert trade.net_pnl == pytest.approx(gross - commission * 2)
    # A wrong conversion that treated 150 as the EURUSD rate would not land here.
    assert gross == pytest.approx((quote / 150.104) / 1.1002)


def test_engine_fill_matches_cost_model_for_each_kind():
    cases = [
        (FillKind.MARKET, Side.BUY, None, 1.10000, 1.10020, 1.10022),
        (FillKind.STOP, Side.BUY, 1.10100, 1.10200, 1.10220, 1.10222),
        (FillKind.LIMIT, Side.BUY, 1.09900, 1.09880, 1.09890, 1.09900),
        (FillKind.STOP_LOSS, Side.BUY, 1.09950, 1.09900, 1.09920, 1.09898),
        (FillKind.TAKE_PROFIT, Side.BUY, 1.10100, 1.10140, 1.10160, 1.10100),
    ]
    for kind, side, level, bid, ask, expected in cases:
        got = execution_price(
            side=side, kind=kind, bid=bid, ask=ask, level=level, slippage_price=SLIP
        )
        assert got == pytest.approx(expected)


def test_numba_loop_matches_python_loop():
    market = market_from_minutes(
        [
            [(1.10000, 1.10020)],
            [(1.10000, 1.10020), (1.09900, 1.09920), (1.10150, 1.10170)],
        ]
    )
    plan = {0: [_buy(sl=1.09950, tp=1.10120)]}
    fills._COMPILED = simulate_bar
    python_trade = run_backtest(market, Scripted(plan), EURUSD, _costs()).trades[0]
    fills._COMPILED = None
    compiled = get_simulate()
    if compiled is simulate_bar:
        pytest.skip("numba is not available")
    numba_trade = run_backtest(market, Scripted(plan), EURUSD, _costs()).trades[0]
    assert numba_trade.entry_price == pytest.approx(python_trade.entry_price)
    assert numba_trade.exit_price == pytest.approx(python_trade.exit_price)
    assert numba_trade.exit_reason == python_trade.exit_reason
    assert numba_trade.net_pnl == pytest.approx(python_trade.net_pnl)


def test_take_profit_already_through_the_fill_rejects_the_order():
    # Signal on bar 0 with a TP set from bar-0 prices. The fill on bar 1 is
    # already above that TP (ask + slippage), so a broker would refuse the
    # stops. The order must be dropped, not opened without a target and held
    # until the end of the data.
    market = market_from_minutes(
        [
            [(1.10000, 1.10002)],
            [(1.10010, 1.10012), (1.10011, 1.10013)],
            [(1.10020, 1.10022)],
        ]
    )
    strat = Scripted({0: [_buy(sl=1.09900, tp=1.10005)], 1: [_buy(sl=1.09900, tp=1.10100)]})
    result = run_backtest(market, strat, EURUSD, _costs())
    assert any("take-profit already through" in w for w in result.warnings)
    # the first order is gone; the second (valid TP) fills and is flattened at the end
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(1.10022 + SLIP)
