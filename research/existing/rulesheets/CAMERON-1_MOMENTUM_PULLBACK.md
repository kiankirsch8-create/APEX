# CAMERON-1 · Small-cap momentum scalping (5 pillars + first/micro pullback) — rule sheet v1

Creator: Ross Cameron (Warrior Trading). Market: **US small-cap stocks, long only**, 1-min chart (5-min / 10-sec support).
Not a futures strategy. Note: Warrior Trading settled with the FTC in 2022 ($3M) over deceptive marketing/earnings claims;
the FTC said most customers lost money. His own P&L is shown via broker statements; the settlement does not verify or refute it.

## Sources (`sources/`)
| Code | File | Value |
|---|---|---|
| R6 | `cameron_how_to_start_2026_TRANSCRIPT.md` | **newest, main source**: 5 pillars, 1-min pullback, stop/target/exit |
| R3 | `cameron_1min_scalping_training_TRANSCRIPT.md` | patterns (first/micro pullback, half/whole dollar), front/backside, exits |
| R4 | `cameron_entries_exits_TRANSCRIPT.md` | apex point, stops, order types, partials, break-even |
| R1 | `cameron_2017_small_account_review_TRANSCRIPT.md` | stats, sizing (95% buying power), daily max loss |
| R2 | `cameron_2017_midday_recap_TRANSCRIPT.md` | intraday risk "bell curve", 1-min ORB example |
| R5 | `cameron_gme_live_session_TRANSCRIPT.md` | live example (GME pre-market breakout), no new rules |

## A. Stock selection (scanner) — the core of the edge
| # | Rule | Source | Status |
|---|---|---|---|
| S1 | Up ≥ 10% on the day (gapping up) | R6, R3 | STATED |
| S2 | Relative volume ≥ 5× (R6: vs 50-day avg; R3/R4: vs 14-day; R4: ≥2× acceptable) | R6, R3, R4 | STATED, lookback **conflict** → R6 wins |
| S3 | News catalyst (A quality); continuation names / sector catalyst allowed but riskier | R6, R3 | STATED; what qualifies as news → needs a news feed → **MISSING (data)** |
| S4 | Price $2-20, sweet spot $5-10 (2026); $2-5 in 2017; >$20 avoided | R6, R1, R3 | STATED (evolved over time) |
| S5 | Float < 10M shares (lower = better); <20M OK; 50M hard cut-off | R6, R4 | STATED; float data → **MISSING (data)** |
| S6 | High total volume (more volume = more size possible); "most obvious stock in the market" | R6 | STATED / partly discretionary |
| S7 | Daily chart above 9 EMA, room up to 200 EMA | R4 | STATED |
| S8 | Hot sector, recent IPO/SPAC/reverse split, trend keywords = extra | R3, R6 | STATED (bonus, not a filter) |

## B. Time
| T1 | Best 7:00-10:00/11:00 ET (2026: starts pre-market 7:00); 2017: 9:30-11:30 | R6, R4, R1 | STATED |

## C. Entries (1-min chart)
| # | Rule | Source | Status |
|---|---|---|---|
| E1 | **First pullback**: stock surges, pulls back (red candles); entry = **first candle to break the previous candle's high** | R6, R3, R4 | STATED |
| E2 | **Micro pullback**: one-candle dip during a squeeze, entry on break of its high (aggressive) | R3, R4 | STATED |
| E3 | Half/whole-dollar break: enter just below (e.g. 7.99 for the break of 8); avoid apex just *below* a half/whole dollar | R3, R4 | STATED |
| E4 | High-of-day break, bull flag, ABCD/wedge: named as valid patterns | R4 | STATED, no exact rules |
| E5 | Front side only: price above 20 EMA (9 EMA on 5-min), MACD positive; no trades after MACD cross / below 20 EMA | R3, R6 | STATED |
| E6 | Skip if the pullback has a high-volume red candle (sellers in control) | R6 | STATED ("higher volume" not quantified → INTERPRETED) |
| E7 | Skip if spread too wide to keep a tight stop | R3 | STATED |
| E8 | Need ≥ 2:1 to the first target (retest of high of day) | R6, R3 | STATED |
| E9 | Order: marketable limit at ask + 10-15c | R4, R3 | STATED |
| E10 | Early entries on green tape / big bids on Level 2 | R3, R4 | **DISCRETIONARY → not copied** |

## D. Stops, exits, management
| # | Rule | Source | Status |
|---|---|---|---|
| X1 | Stop = low of the pullback (R6); if too far, arbitrary 10-15c (R3) / ~20c (R4) | R6, R3, R4 | STATED, which one when → **CONFLICT** (we test both) |
| X2 | Mental stop, no resting stop order | R4 | STATED (sim: stop at bar touch) |
| X3 | Exit full: **first 1-min candle that closes red** (R6); also topping tail/doji/shooting star, shrinking green candles (R3) | R6, R3 | STATED |
| X4 | Breakout-or-bailout: if it doesn't move immediately, out | R3, R4 | STATED, time not quantified → INTERPRETED |
| X5 | With full size: sell part into the breakout, rest stop at break-even | R4 | STATED |
| X6 | Don't cap winners: hold past target until X3 | R6 | STATED |

## E. Risk / day management
| # | Rule | Source | Status |
|---|---|---|---|
| D1 | Intraday risk "bell curve": medium size first, increase after a cushion in hour 1, taper from 10:30 | R2 | STATED |
| D2 | Daily max loss (2017: broker cut-off at −$1,000; down > $2,000 → 9 of 10 days end red → stop) | R1 | STATED (absolute $, scale to account) |
| D3 | Small account: 95% of buying power per trade (6× offshore leverage) | R1 | STATED (not used by us) |
| D4 | Stats: accuracy 68-71%, avg win ≈ avg loss (14-18c/share), break-even = 50% | R1, R3, R6 | STATED (benchmark) |

## Verdict
- **Most mechanical creator so far.** Scanner (S1-S7), entry (E1/E2), stop (X1), target (E8), exit (X3) and filters (E5) are
  objective. Only the Level 2/tape part (E10) is discretionary; that's an execution layer we leave out and log.
- **Missing pieces are data, not rules:** news catalyst (S3) and float (S5).
- **Cannot be tested on our NQ data.** The edge is in *which stocks* (low-float news gappers). Running his pattern on NQ would not be
  a copy. A 1:1 test needs US small-cap equity data incl. pre-market from 7:00 ET.
- Fill realism matters: low-float names have wide spreads, halts and slippage; 1-min bars overstate results → test needs
  trades/quotes (or a spread + slippage penalty) and halt handling.

## Data / execution options (for Kian)
- **Price data:** (a) Databento XNAS.ITCH (Nasdaq venue only, incl. pre-market): daily bars for all symbols to find gappers, then
  1-min bars/trades only for candidate days — cheapest route, exact cost from Databento's cost estimate before buying;
  (b) Databento consolidated US equities (all venues, more accurate volume) — more expensive; (c) another vendor with US
  minute/trade flat files (monthly subscription).
- **Float:** free proxy = shares outstanding from SEC EDGAR filings (INTERPRETED, overstates float), or a paid fundamentals feed.
- **News:** proxy = gap ≥10% + RVOL ≥5× (he says high RVOL ≈ news) — INTERPRETED; or a paid historical news feed (exact).
- **Execution later:** US stock broker with pre-market + hotkeys; check the US pattern-day-trader rule (<$25k margin accounts)
  for Kian's account type before demo/live.

## v1.1 update (2026-11-16): three more sources
| Code | File | Value |
|---|---|---|
| R7 | `cameron_scalping_class_zapp_TRANSCRIPT.md` | full scalping class: 5 steps, stats, stop, exit, hotkeys |
| R8 | `cameron_dip_and_rip_recap_TRANSCRIPT.md` | "dip and rip" setup, first-trade size cap, halts as market gauge |
| R9 | `cameron_micro_pullback_candles_TRANSCRIPT.md` | micro pullback anatomy: retrace limit, red-candle count, volume |

| # | Rule | Source | Status |
|---|---|---|---|
| S2b | RVOL ≥ 5× vs **14-day** average (R7); continuation names may sit below 5× because recent volume inflates the average | R7 | STATED → lookback conflict with R6 (50-day) stays; test both |
| S4b | Price $1-20 (R7, R9); best $2-20; risk rises with price (biggest wins *and* losses above $20) | R7 | STATED |
| S9 | **Move potential ≥ 30c** (ideally ≥ 50c): he captures at best half a move, so no trade if the stock can't move 2× the target | R7 | STATED (new filter) |
| E1b | Pullback shape: ≤ 2 red candles (3-4 = not the pattern); red candle not bigger than the prior candles; red-candle volume **lighter** than the prior green; retrace never > 50% of the leg (ideally stays in the top 25%) | R9 | STATED (new, mechanical) |
| E1c | Entry trigger: first candle making a new high, or earlier on green prints on the tape | R7, R9 | STATED; tape part DISCRETIONARY → bar version only |
| E11 | **Dip and rip**: on a fresh squeeze, buy the first quick dip and sell into the first rip near the next half/whole dollar | R8 | STATED; "dip" depth INTERPRETED (same as micro pullback) |
| E12 | Multi-timeframe alignment: 10s, 1m and 5m all look good; skip adds when the 1m shows a doji against a good 10s | R9 | STATED; needs 10s bars (we can build them from trades) |
| X1b | Stop: **arbitrary 10-15c**, not the pullback low ("that's too far"), so average losers stay ≤ ~15-18c (R7); pullback low in R6/R9 | R7, R9 | CONFLICT persists → both variants (R7 = newest class) |
| X3b | First target 15-20c (≈2:1); exit when green candles shrink, topping tail/doji, big seller on Level 2 | R7 | STATED (L2 part discretionary) |
| X7 | Don't trade a stock whose spread is too wide to keep a ~15c stop (he quit ZAPP for that) | R7 | STATED |
| D5 | First trade capped (5,000 shares) until the day is up > $1,000; then size up | R8 | STATED (= D1 "bell curve") |
| D6 | Walk away after the daily goal / when the stock stops working ("lean in on what works, abandon what doesn't") | R7, R8 | STATED |
| D7 | Market gauge: number of circuit-breaker halts in the day (few = weak small-cap tape) | R8 | STATED (context, not a rule) |
| D4b | Stats update: accuracy 68.5% over 9 years, avg win ~$1,400 / avg loss ~$1,500, avg win ~14c, avg loss ~18c | R7 | STATED (benchmark) |
Verdict unchanged: **Copyable** on rules, **not testable on NQ**; needs US small-cap data (see options above). The free PDF he
links ("micro pullback strategy") may settle X1/X1b; it's free in the video description (likely email sign-up).
