# CREAMER-1 · Auction Resolution (AR) model: environment → location → path → footprint confirmation (rule sheet v1)

Creator: Chris Creamer ("Thraxx Trades"), Robbins World Cup micro day-trading, July: +100%. Market: NQ/MNQ (also ES, gold).
Platforms: ATAS / DeepCharts (footprint), TradingView, Tanuki Trade (gamma). Stated stats: WR 60-65%, PF ~1.8, 1.5-2R.

## Sources (`sources/`)
| Code | File | Value |
|---|---|---|
| IQW | `creamer_iqcapital_whiteboard_TRANSCRIPT.md` | full 4-step model, fibs, reversal entry, MNQ volume filter, rules |
| AR1 | `creamer_AR_model_course_PART1_TRANSCRIPT.md` | 4h course part 1: framework, environments, no-trade conditions. **Part 2 missing** |
| OFC | `creamer_orderflow_course_condensed_TRANSCRIPT.md` | condensed course: levels stack, delta flips, POC in wick |
| GEX | `creamer_gamma_TRANSCRIPT.md` | gamma regimes, HVL / call wall / put wall as ~40-pt zones |
| WC | `creamer_world_cup_competition_TRANSCRIPT.md` | competition vs funded management, pyramiding, fibs 0.5/0.62 also drawn |
| CONT | `creamer_continuation_entries_PARTIAL_TRANSCRIPT.md` | big-trade continuation entry; **stacked-imbalance entry cut off** |
| ATAS | `creamer_atas_settings_TRANSCRIPT.md` | footprint settings: bid×ask, 400% imbalance, min 10 contracts |
| FEB | `creamer_feb_recap_TRANSCRIPT.md` | prop payouts, sizing by ATR |

## A. Environment (pre-market)
| # | Rule | Source | Status |
|---|---|---|---|
| A1 | Structure HH/HL vs LH/LL on 4h (~30 days), 1h (week), 15m (24h), 5m; value up / down / sideways; balance vs imbalance | IQW, OFC, AR1 | STATED; swing definition → INTERPRETED |
| A2 | Gamma: net positive → dampening, fade edges; negative → amplification, continuation; HVL, call wall, put wall as ~40-pt zones | GEX, IQW | STATED; needs QQQ/NDX options OI history → **MISSING (data)** |
| A3 | Three environments: initiative expansion → continuation models; balance rotation → reversals at extremes; compression → no trade | AR1, OFC | STATED; classifier → INTERPRETED |
| A4 | No-trade if 2-3 of: repeated failed breakouts, overlapping structure, repeated return to value, no HTF alignment, no clean location, no continuation path | AR1 | STATED |

## B. Location
| # | Rule | Source | Status |
|---|---|---|---|
| B1 | Fixed-range volume profile of the active range (swing low → swing high of the impulse) | IQW, AR1, WC | STATED; swing choice → INTERPRETED |
| B2 | Bullish: buy only in discount (below VA); bearish: sell only in premium (above VA) | IQW, AR1 | STATED |
| B3 | Fib from the same swing: zone 0.705-0.886 (0.788 inside; WC also draws 0.5, 0.62) | IQW, AR1, WC | STATED |
| B4 | Fib zone must lie outside the VA; need a swing point / sweep before price enters it; beyond 0.886 = invalid | IQW | STATED |
| B5 | Extra levels: prior RTH/ETH VAH/VAL/POC, weekly VA, 30-day composite, gamma walls; overnight VAL / prior RTH levels as acceptance tests | OFC, WC | STATED; which one counts → INTERPRETED |

## C. Path
| C1 | Daily candle forms over Asia/London/NY; own indicator of average deviation beyond Asia H/L | AR1 | **MISSING (proprietary indicator)** |

## D. Confirmation (5-min footprint)
| # | Rule | Source | Status |
|---|---|---|---|
| D1 | Reversal: in location, aggressive opposite side (delta) with candle POC/volume in the wick, no result; candle closes the other way | IQW, OFC | STATED; thresholds → INTERPRETED |
| D2 | Next candle pulls back, opposite side fails again at a better price, flips → entry | IQW | STATED |
| D3 | Stop beyond the failed participants | IQW | STATED; which failure (1st or 2nd) → INTERPRETED |
| D4 | Imbalance = 400%, stacked 3 levels, min 10 contracts per level | ATAS, IQW | STATED |
| D5 | Continuation: big trade ≥ 300 MNQ inside the candle body, follow-through + strong close; next candle pulls back into it, other side fails, flips → enter; stop below the big trade with room; momentum environments only | CONT | STATED |
| D6 | Stacked-imbalance continuation entry | CONT | **MISSING** (transcript cut) |
| D7 | Participation filter: no trades if MNQ 5-min volume < 20,000 contracts | IQW | STATED (MNQ; NQ equivalent → INTERPRETED) |

## E. Management and risk
| # | Rule | Source | Status |
|---|---|---|---|
| E1 | First check: price must re-enter the value area; if not → break-even or cut | IQW | STATED |
| E2 | Target = swing high/low (full target); trail behind continued aggression; tighter near POC / gamma wall | IQW | STATED; target choice → INTERPRETED |
| E3 | Stop after 2 losses in a row; hard stop 1.5 h after NY open; 0-2 trades/day typical | IQW | STATED |
| E4 | Size down when 5-min ATR is high (50-60 pts → 3-5 micros instead of 10) | FEB | STATED |
| E5 | Competition: small initial risk, add only to winners, average price allows stop to BE; never add to losers | WC | STATED (competition mode only) |

## Verdict
**Partial.** Location (B2-B4), confirmation logic (D1-D5), filters (D7) and management (E1, E3) are concrete and buildable
from our Databento NQ trades (delta, per-candle profile, big trades). Missing: gamma history (A2), the path indicator (C1),
the stacked-imbalance entry (D6), course part 2. Thresholds are MNQ: convert (INTERPRETED) or buy MNQ trades.
Options: (a) build without gamma as a variant, (b) buy QQQ options open-interest history to compute naive GEX, (c) find a gamma
provider with downloadable history. Kian decides; no code before.

## First 12.5-month test (2026-11-16)
See `../audits/PROS_batch_results_2026-11-16.md` (defaults approved by Kian; code `scalping/orderflow/pros.py`).
