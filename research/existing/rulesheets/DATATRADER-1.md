# DATATRADER-1 rule sheet (draft, from the Gemini recap; transcript still needed)

Source: research/existing/sources/datatrader_4h_ny_range_recap.md (faceless clone of Data Trader's video).
Status legend: STATED / INTERPRETED / MISSING / CONFLICT (see ../FIDELITY.md).

| # | Rule | Recap evidence (timestamp) | Status | Pine v1 |
|---|---|---|---|---|
| 1 | Reference = high/low incl. wicks of the first 4H candle of the calendar day, New York time (00:00-04:00) | 01:13-02:40 | STATED | ✓ |
| 2 | Only draw once that candle has fully closed | 01:13-02:40 | STATED | ✓ |
| 3 | Execution on 5-minute chart | 02:40 | STATED | ✓ (use 5m) |
| 4 | Breakout valid only on a 5m **body close** outside; wick-only pierce = no setup | 02:40, 03:40 | STATED | ✓ |
| 5 | Entry when a later 5m candle closes back inside; enter at that close | 02:40-03:40 | STATED | ✓ |
| 6 | Breakout and re-entry within the same trading day | 02:40 | STATED | ✓ (NY calendar day) |
| 7 | Stop at the exact extreme of the breakout excursion | 03:40 | STATED | ✓ |
| 8 | Wide breakout: stop behind "nearest local key structure / order block" instead | 05:14 (BTC trade 5), 11:46 (gold) | **INTERPRETED**: v1 uses "stop > 2.5 ATR away → highest high / lowest low of last 6 bars". Alternatives: last swing pivot inside the excursion; skip the trade | ✓ (interpreted) |
| 9 | Target fixed 2R | 03:40 | STATED | ✓ |
| 10 | Multiple setups per day allowed | 03:40-05:14 | STATED | ✓ |
| 11 | One position at a time | not addressed (trades in examples are sequential) | **INTERPRETED** | ✓ |
| 12 | Open trade at end of day | not addressed | **INTERPRETED**: v1 closes at 23:55 NY | ✓ |
| 13 | Close straight through the whole range in one bar | not addressed | **INTERPRETED**: v1 treats it as a new breakout on the other side, no trade | ✓ |
| 14 | Markets | BTC, EURUSD, XAUUSD shown; "any asset" | STATED | any |
