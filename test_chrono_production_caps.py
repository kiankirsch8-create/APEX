"""Assert PR #99 diagnostic ceiling toggles stay OFF in production chrono config."""
from __future__ import annotations

import inspect

import continuous_backtester as cb


def test_multi_tf_per_day_dedupe_is_enforced() -> None:
    assert cb.DIAGNOSTIC_ALLOW_MULTI_TF_PER_DAY is False


def test_currency_cap_blocks_is_active() -> None:
    import portfolio_construction as pc

    src = inspect.getsource(cb._chrono_currency_cap_blocks)
    assert "portfolio_construction" in src or "_pc.currency_cap_blocks" in src
    assert "return False, None" not in src
    blocked, ccy = cb._chrono_currency_cap_blocks("CADJPY", use_4h_pool=False)
    # Empty pools → not blocked; function must reach real logic (not early-return).
    assert blocked is False and ccy is None
    cb.OPEN_CURRENCY_COUNT.clear()
    cb.OPEN_CURRENCY_COUNT_4H.clear()
    cb.OPEN_CURRENCY_COUNT["JPY"] = 2
    blocked2, ccy2 = cb._chrono_currency_cap_blocks("AUDJPY", use_4h_pool=False)
    assert blocked2 is True and ccy2 == "JPY"
    # Separate 4h pool still empty
    blocked3, _ = cb._chrono_currency_cap_blocks("AUDJPY", use_4h_pool=True)
    assert blocked3 is False
    # Shared helper agrees
    assert pc.currency_cap_blocks(
        "AUDJPY",
        use_4h_pool=False,
        open_currency_count=cb.OPEN_CURRENCY_COUNT,
        open_currency_count_4h=cb.OPEN_CURRENCY_COUNT_4H,
    ) == (True, "JPY")
    cb.OPEN_CURRENCY_COUNT.clear()


def test_r01_short_strong_headwind_diagnostic_removed() -> None:
    src = inspect.getsource(cb.enforce_rules)
    assert "DIAGNOSTIC R01 block" not in src
    assert "slot-stealer test" not in src
