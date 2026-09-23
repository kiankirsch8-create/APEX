"""Profile-scoped guardrail state + funded compounding defaults."""

from __future__ import annotations

from datetime import date

import apex_trader_v76_private as v76
import continuous_backtester as cb
import shadow_instruments as si


def test_migrate_flat_guard_state_into_private_slot() -> None:
    st: dict = {
        "guard_activation_date": "2025-01-15",
        "peak_equity": 12500.5,
    }
    v76._migrate_guard_profile_state(st)
    assert st["guard_activation_date_by_profile"]["private"] == "2025-01-15"
    assert st["peak_equity_by_profile"]["private"] == 12500.5
    # Second migrate must not overwrite.
    st["guard_activation_date"] = "2099-01-01"
    st["peak_equity"] = 1.0
    v76._migrate_guard_profile_state(st)
    assert st["guard_activation_date_by_profile"]["private"] == "2025-01-15"
    assert st["peak_equity_by_profile"]["private"] == 12500.5


def test_ensure_activation_and_peak_use_current_profile(monkeypatch) -> None:
    monkeypatch.setattr(v76, "PROFILE", "private")
    st: dict = {}
    act = v76._ensure_guard_activation_date(st, date(2026, 3, 1))
    assert act == date(2026, 3, 1)
    assert st["guard_activation_date_by_profile"]["private"] == "2026-03-01"
    assert "funded" not in st["guard_activation_date_by_profile"]

    peak = v76._update_peak_equity(st, 10000.0)
    assert peak == 10000.0
    peak2 = v76._update_peak_equity(st, 11000.0)
    assert peak2 == 11000.0
    assert st["peak_equity_by_profile"]["private"] == 11000.0
    assert "funded" not in st["peak_equity_by_profile"]

    monkeypatch.setattr(v76, "PROFILE", "funded")
    act_f = v76._ensure_guard_activation_date(st, date(2026, 9, 1))
    assert act_f == date(2026, 9, 1)
    assert st["guard_activation_date_by_profile"]["funded"] == "2026-09-01"
    # Private slot unchanged.
    assert st["guard_activation_date_by_profile"]["private"] == "2026-03-01"

    peak_f = v76._update_peak_equity(st, 50000.0)
    assert peak_f == 50000.0
    assert st["peak_equity_by_profile"]["funded"] == 50000.0
    assert st["peak_equity_by_profile"]["private"] == 11000.0


def test_funded_compounding_defaults() -> None:
    funded = v76.PROFILES["funded"]
    private = v76.PROFILES["private"]
    assert funded["compounding_enabled"] is True
    assert funded["compound_risk_fraction"] == 0.002
    assert private["compounding_enabled"] is False
    assert private.get("compound_risk_fraction") in (None, 0, 0.0)


def test_eurjpy_on_real_curve_not_shadow_part1() -> None:
    assert "EURJPY" not in cb.EXCLUDED_PAIRS
    assert "EURJPY" not in si.PART1_DATA_EXCLUDED_FX
    assert "NZDJPY" in cb.EXCLUDED_PAIRS
    assert "EURJPY" in cb.CHRONO_TICKERS
    assert "EURJPY" in cb._real_chrono_forex_tickers()
