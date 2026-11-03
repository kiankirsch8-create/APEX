"""
Shared portfolio-construction rules (v7.0 baseline / caps-on production).

Structural anti-clustering rules used by both continuous_backtester chrono and
the live v76 engines (NOT the shadow-only CFG guardrails):

  1. At most one NEW entry per ticker per UTC calendar day
     (dedupe across timeframes / scan slots).
  2. Per-currency cap of 2 open+today positions, with a SEPARATE pool for
     4h/intraday vs 1w/1d (1h checks the 4h pool but records into the major
     pool — matching continuous_backtester record vs check semantics).

Entry *decisions* are unchanged; only which decided trades actually open is
filtered here. Live marks ticker-day on ENTRY; chrono marks on same-day
WIN/LOSS completion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

EXOTIC_PAIRS: frozenset[str] = frozenset({"USDMXN", "USDZAR", "USDNOK", "USDSEK"})
CURRENCY_CAP: int = 2

# Skip reason strings — keep identical to continuous_backtester chrono gates.
SKIP_TICKER_TRADED_TODAY = "Ticker already traded today on another timeframe"
SKIP_CURRENCY_CAP_FMT = "Currency cap: {ccy} already has 2 open trades"

# Chrono phase TF → use_4h_pool at CHECK time (_chrono_v71_phases).
_CHECK_4H_POOL_TFS: frozenset[str] = frozenset({"4h", "15m", "30m", "1h"})
# Record-time pool split (day rebuild + post-trade increment): 1h → major.
_RECORD_4H_POOL_TFS: frozenset[str] = frozenset({"4h", "15m", "30m"})


def get_currencies(ticker: str) -> list[str]:
    """Forex spot symbols as 6-letter AAA/BBB; non-forex → no currencies."""
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return [t[:3], t[3:]]
    return []


def use_4h_currency_pool(timeframe: str) -> bool:
    """CHECK-time pool flag — matches ``_chrono_v71_phases`` ``use4h_pool``."""
    return str(timeframe or "").strip().lower() in _CHECK_4H_POOL_TFS


def record_uses_4h_pool(timeframe: str) -> bool:
    """RECORD-time pool flag — matches BT day-rebuild / post-trade increment."""
    return str(timeframe or "").strip().lower() in _RECORD_4H_POOL_TFS


def currency_cap_blocks(
    ticker: str,
    *,
    use_4h_pool: bool,
    open_currency_count: Mapping[str, int],
    open_currency_count_4h: Mapping[str, int],
    cap: int = CURRENCY_CAP,
) -> tuple[bool, str | None]:
    """
    Production FIX 2 + FIX 9 + FIX 15 — return (blocked, ccy_or_None).

    Identical to continuous_backtester._chrono_currency_cap_blocks body
    (the live-restored semantics; ignores the chrono diagnostic early-return).
    """
    tkr_u = (ticker or "").strip().upper()
    bucket = open_currency_count_4h if use_4h_pool else open_currency_count
    for ccy in get_currencies(tkr_u):
        if tkr_u in EXOTIC_PAIRS and ccy == "USD":
            continue
        if int(bucket.get(ccy, 0) or 0) >= int(cap):
            return True, ccy
    return False, None


def ticker_already_traded_today(ticker: str, traded_tickers_today: Iterable[str]) -> bool:
    tkr_u = (ticker or "").strip().upper()
    if not tkr_u:
        return False
    traded = {str(x).strip().upper() for x in traded_tickers_today}
    return tkr_u in traded


def increment_currency_counts(
    ticker: str,
    timeframe: str,
    open_currency_count: MutableMapping[str, int],
    open_currency_count_4h: MutableMapping[str, int],
) -> None:
    """Record a taken entry into the appropriate currency pool (BT record semantics)."""
    tkr_u = (ticker or "").strip().upper()
    use4 = record_uses_4h_pool(timeframe)
    for ccy in get_currencies(tkr_u):
        if tkr_u in EXOTIC_PAIRS and ccy == "USD":
            continue
        if use4:
            open_currency_count_4h[ccy] = int(open_currency_count_4h.get(ccy, 0) or 0) + 1
        else:
            open_currency_count[ccy] = int(open_currency_count.get(ccy, 0) or 0) + 1


def counts_from_positions(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Build currency pools from open / today rows.

    Each row needs ``ticker`` and ``timeframe`` (record-time pool split).
    """
    major: dict[str, int] = {}
    pool4h: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        increment_currency_counts(
            str(row.get("ticker", "")),
            str(row.get("timeframe", "")),
            major,
            pool4h,
        )
    return major, pool4h


@dataclass
class PortfolioBookState:
    """Mutable construction state for one UTC day (+ open book)."""

    utc_date: str = ""
    traded_tickers_today: set[str] = field(default_factory=set)
    open_currency_count: dict[str, int] = field(default_factory=dict)
    open_currency_count_4h: dict[str, int] = field(default_factory=dict)

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "portfolio_utc_date": self.utc_date,
            "traded_tickers_today": sorted(self.traded_tickers_today),
        }

    @classmethod
    def from_state_dict(
        cls,
        st: Mapping[str, Any] | None,
        *,
        utc_today: date | None = None,
        open_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> "PortfolioBookState":
        """
        Load persisted ticker-day dedupe; rebuild currency pools from open+today.

        On UTC date rollover, ``traded_tickers_today`` resets. Currency pools are
        always rebuilt from ``open_rows`` (engine's own open positions) so
        overnight swings still consume currency slots.
        """
        today = utc_today or datetime.now(timezone.utc).date()
        today_s = today.isoformat()
        raw = st if isinstance(st, Mapping) else {}
        stored_date = str(raw.get("portfolio_utc_date") or "").strip()
        traded: set[str] = set()
        if stored_date == today_s:
            for x in raw.get("traded_tickers_today") or []:
                u = str(x).strip().upper()
                if u:
                    traded.add(u)
        major, pool4h = counts_from_positions(open_rows or ())
        return cls(
            utc_date=today_s,
            traded_tickers_today=traded,
            open_currency_count=major,
            open_currency_count_4h=pool4h,
        )


@dataclass
class ConstructionResult:
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]

    @property
    def accepted_keys(self) -> set[tuple[str, str]]:
        return {
            (
                str(r.get("ticker", "")).strip().upper(),
                str(r.get("timeframe", "")).strip().lower(),
            )
            for r in self.accepted
        }

    @property
    def accepted_tickers(self) -> set[str]:
        return {str(r.get("ticker", "")).strip().upper() for r in self.accepted}


def filter_portfolio_entries(
    decisions: Sequence[Mapping[str, Any]],
    *,
    book: PortfolioBookState | None = None,
    enforce_ticker_day_dedupe: bool = True,
    enforce_currency_cap: bool = True,
    max_positions: int | None = None,
    current_open_count: int = 0,
) -> ConstructionResult:
    """
    Apply v7.0 portfolio-construction gates to an ordered list of decided trades.

    ``decisions`` items need at least ``ticker`` and ``timeframe``. Extra fields
    are copied through. Order is construction priority (chrono phase order).

    Returns accepted / rejected rows; rejected rows include ``skip_reason``.
    Mutates ``book`` as entries are accepted (mirrors BT TRADED_TICKERS_TODAY +
    OPEN_CURRENCY_COUNT* increments).
    """
    state = book if book is not None else PortfolioBookState()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    open_n = int(current_open_count)

    for raw in decisions:
        row = dict(raw)
        ticker = str(row.get("ticker", "")).strip().upper()
        timeframe = str(row.get("timeframe", "")).strip().lower()
        row["ticker"] = ticker
        row["timeframe"] = timeframe

        if max_positions is not None and open_n >= int(max_positions):
            row = dict(row)
            row["skip_reason"] = "max_positions"
            row["accepted"] = False
            rejected.append(row)
            continue

        if enforce_ticker_day_dedupe and ticker_already_traded_today(
            ticker, state.traded_tickers_today
        ):
            row = dict(row)
            row["skip_reason"] = SKIP_TICKER_TRADED_TODAY
            row["accepted"] = False
            rejected.append(row)
            continue

        if enforce_currency_cap:
            blocked, ccy = currency_cap_blocks(
                ticker,
                use_4h_pool=use_4h_currency_pool(timeframe),
                open_currency_count=state.open_currency_count,
                open_currency_count_4h=state.open_currency_count_4h,
            )
            if blocked:
                row = dict(row)
                row["skip_reason"] = SKIP_CURRENCY_CAP_FMT.format(ccy=ccy)
                row["accepted"] = False
                row["currency_cap_ccy"] = ccy
                rejected.append(row)
                continue

        # Accept
        state.traded_tickers_today.add(ticker)
        increment_currency_counts(
            ticker,
            timeframe,
            state.open_currency_count,
            state.open_currency_count_4h,
        )
        open_n += 1
        ok = dict(row)
        ok["accepted"] = True
        ok.pop("skip_reason", None)
        accepted.append(ok)

    return ConstructionResult(accepted=accepted, rejected=rejected)


def chrono_phase_sort_key(
    ticker: str,
    timeframe: str,
    *,
    ticker_order: Sequence[str] | None = None,
) -> tuple[int, int]:
    """Sort key matching chrono phases: 1w → 1d → 1h → 4h → intraday, then ticker list."""
    tf_rank = {"1w": 0, "1d": 1, "1h": 2, "4h": 3, "15m": 4, "30m": 5}
    order = list(ticker_order) if ticker_order is not None else []
    t_rank = {str(t).strip().upper(): i for i, t in enumerate(order)}
    tu = (ticker or "").strip().upper()
    tf = (timeframe or "").strip().lower()
    return (tf_rank.get(tf, 99), t_rank.get(tu, 10_000))


def sort_decisions_chrono_order(
    decisions: Sequence[Mapping[str, Any]],
    *,
    ticker_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    rows = [dict(d) for d in decisions]
    rows.sort(
        key=lambda r: chrono_phase_sort_key(
            str(r.get("ticker", "")),
            str(r.get("timeframe", "")),
            ticker_order=ticker_order,
        )
    )
    return rows
