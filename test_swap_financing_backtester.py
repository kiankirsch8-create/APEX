"""Unit tests for estimated swap financing in continuous_backtester."""
from __future__ import annotations

from unittest import mock

import continuous_backtester as cb


def test_swap_audjpy_long_positive_carry() -> None:
    # AUD 4.1% - JPY 0.4% - markup 1.5% = 2.2% annual on $100k exposure, 1 night
    amt = cb._estimate_swap_amount(
        ticker="AUDJPY",
        direction="LONG",
        leveraged_exposure=100_000.0,
        nights_held=1.0,
    )
    expected = 100_000.0 * ((4.1 - 0.4) / 100.0 - 1.5 / 100.0) * 1.0 / 365.0
    assert round(amt, 4) == round(expected, 4)
    assert amt > 0


def test_swap_audjpy_short_flips_carry() -> None:
    long_amt = cb._estimate_swap_amount(
        ticker="AUDJPY",
        direction="LONG",
        leveraged_exposure=100_000.0,
        nights_held=1.0,
    )
    short_amt = cb._estimate_swap_amount(
        ticker="AUDJPY",
        direction="SHORT",
        leveraged_exposure=100_000.0,
        nights_held=1.0,
    )
    assert short_amt < 0
    carry_only_long = 100_000.0 * ((4.1 - 0.4) / 100.0) * 1.0 / 365.0
    carry_only_short = -carry_only_long
    assert round(long_amt, 4) == round(carry_only_long - 100_000.0 * 0.015 / 365.0, 4)
    assert round(short_amt, 4) == round(carry_only_short - 100_000.0 * 0.015 / 365.0, 4)


def test_swap_missing_currency_zero_with_warning() -> None:
    with mock.patch.object(cb, "log") as mock_log:
        amt = cb._estimate_swap_amount(
            ticker="USDMXN",
            direction="LONG",
            leveraged_exposure=50_000.0,
            nights_held=2.0,
        )
    assert amt == 0.0
    mock_log.assert_called_once()
    assert "USDMXN" in str(mock_log.call_args)


def test_swap_included_in_realistic_costs_net_pnl() -> None:
    gross_pct = 0.02
    exp = 100_000.0
    pnl, _pct, _gross, fields = cb._apply_realistic_costs(
        ticker="EURUSD",
        direction="LONG",
        timeframe="1d",
        position_size=100_000.0,
        entry=1.10,
        leveraged_exposure=exp,
        raw_pct=gross_pct,
        candles_to_exit=3,
    )
    swap = fields["swap_amount"]
    assert swap != 0.0
    spread = fields["spread_cost"]
    slip = fields["slippage_cost"]
    comm = fields["commission_cost"]
    gross = exp * gross_pct
    expected_net = gross - spread - slip - comm + swap
    assert abs(pnl - expected_net) < 0.02
    assert fields["total_cost_dollars"] == round(gross - pnl, 2)


def test_swap_scales_with_sizing_mult() -> None:
    full_exp = 100_000.0
    throttle = 0.18
    throttled_exp = full_exp * throttle
    _, _, _, fields_full = cb._apply_realistic_costs(
        ticker="EURUSD",
        direction="LONG",
        timeframe="1d",
        position_size=100_000.0,
        entry=1.10,
        leveraged_exposure=full_exp,
        raw_pct=0.01,
        candles_to_exit=2,
        sizing_mult=1.0,
    )
    _, _, _, fields_throttled = cb._apply_realistic_costs(
        ticker="EURUSD",
        direction="LONG",
        timeframe="1d",
        position_size=18_000.0,
        entry=1.10,
        leveraged_exposure=throttled_exp,
        raw_pct=0.01,
        candles_to_exit=2,
        sizing_mult=throttle,
    )
    assert fields_throttled["swap_amount"] != 0.0
    assert round(fields_throttled["swap_amount"], 2) == round(
        fields_full["swap_amount"] * throttle,
        2,
    )


def test_assert_swap_financing_modeled_errors_on_zero_total() -> None:
    trades = [
        {"outcome": "WIN", "nights_held": 7.0, "swap_amount": 0.0},
        {"outcome": "LOSS", "nights_held": 3.0, "swap_amount": 0.0},
    ]
    with mock.patch.object(cb, "log") as mock_log:
        cb._assert_swap_financing_modeled(trades, context="test")
    assert mock_log.called
    assert "ERROR" in str(mock_log.call_args)


if __name__ == "__main__":
    test_swap_audjpy_long_positive_carry()
    test_swap_audjpy_short_flips_carry()
    test_swap_missing_currency_zero_with_warning()
    test_swap_included_in_realistic_costs_net_pnl()
    test_swap_scales_with_sizing_mult()
    test_assert_swap_financing_modeled_errors_on_zero_total()
    print("ok")
