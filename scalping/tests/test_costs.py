"""Cost model: real spread, per-side commission, pessimistic conversion."""

from __future__ import annotations

import pytest

from scalping.costs import CostModel, FillKind, Side, convert_to_eur, execution_price


def test_cost_model_has_no_fixed_spread():
    fields = set(CostModel.__dataclass_fields__)
    assert "spread" not in fields
    assert "spread_pips" not in fields
    model = CostModel()
    assert model.commission_per_lot_side == pytest.approx(3.5)
    assert model.account_currency == "EUR"
    assert model.slippage_points == pytest.approx(2.0)


def test_cost_model_rejects_negative_commission():
    with pytest.raises(ValueError):
        CostModel(commission_per_lot_side=-1.0)


def test_market_buy_and_sell_slippage():
    buy = execution_price(
        side=Side.BUY, kind=FillKind.MARKET, bid=1.10000, ask=1.10020, level=None, slippage_price=0.00002
    )
    sell = execution_price(
        side=Side.SELL, kind=FillKind.MARKET, bid=1.10000, ask=1.10020, level=None, slippage_price=0.00002
    )
    assert buy == pytest.approx(1.10022)
    assert sell == pytest.approx(1.09998)


def test_stop_loss_touch_and_gap():
    touch = execution_price(
        side=Side.BUY, kind=FillKind.STOP_LOSS, bid=1.09950, ask=1.09970, level=1.09950, slippage_price=0.00002
    )
    gap = execution_price(
        side=Side.BUY, kind=FillKind.STOP_LOSS, bid=1.09900, ask=1.09920, level=1.09950, slippage_price=0.00002
    )
    assert touch == pytest.approx(1.09948)
    assert gap == pytest.approx(1.09898)
    short = execution_price(
        side=Side.SELL, kind=FillKind.STOP_LOSS, bid=1.10100, ask=1.10140, level=1.10120, slippage_price=0.00002
    )
    assert short == pytest.approx(1.10142)


def test_limit_fills_at_level_without_improvement():
    buy = execution_price(
        side=Side.BUY, kind=FillKind.LIMIT, bid=1.09840, ask=1.09850, level=1.09900, slippage_price=0.00002
    )
    assert buy == pytest.approx(1.09900)
    missed = execution_price(
        side=Side.BUY, kind=FillKind.LIMIT, bid=1.09920, ask=1.09940, level=1.09900, slippage_price=0.00002
    )
    assert missed is None


def test_take_profit_fills_at_level():
    price = execution_price(
        side=Side.BUY, kind=FillKind.TAKE_PROFIT, bid=1.10200, ask=1.10220, level=1.10100, slippage_price=0.00005
    )
    assert price == pytest.approx(1.10100)


def test_convert_usd_and_jpy_pessimistically():
    profit = convert_to_eur(100.0, "USD", eurusd_bid=1.0990, eurusd_ask=1.1010)
    loss = convert_to_eur(-100.0, "USD", eurusd_bid=1.0990, eurusd_ask=1.1010)
    assert profit == pytest.approx(100.0 / 1.1010)
    assert loss == pytest.approx(-100.0 / 1.0990)
    assert abs(loss) > profit

    jpy_profit = convert_to_eur(
        1000.0, "JPY", eurusd_bid=1.0990, eurusd_ask=1.1010, usdjpy_bid=149.90, usdjpy_ask=150.10
    )
    jpy_loss = convert_to_eur(
        -1000.0, "JPY", eurusd_bid=1.0990, eurusd_ask=1.1010, usdjpy_bid=149.90, usdjpy_ask=150.10
    )
    assert jpy_profit == pytest.approx((1000.0 / 150.10) / 1.1010)
    assert jpy_loss == pytest.approx((-1000.0 / 149.90) / 1.0990)
    assert convert_to_eur(12.5, "EUR", eurusd_bid=1.1, eurusd_ask=1.2) == pytest.approx(12.5)


def test_commission_scales_per_lot_per_side():
    model = CostModel(commission_per_lot_side=3.5)
    cash, ccy = model.commission_cash(0.10)
    assert ccy == "USD"
    assert cash == pytest.approx(0.35)
