# VALENTINI-6 · "NASDAQ Effort" model (value migration + delta + Effort areas) (rule sheet)

Source: `sources/valentini_iqcapital_TRANSCRIPT.md` (IQC). Status legend: `../FIDELITY.md`.

## ⛔ Verdict first: cannot be copied 1:1
The core confirmation is his **proprietary "NASDAQ Effort" model**. He describes what it measures
(imbalance, absorption, aggression, path of least resistance) but **not how it is calculated** (7:41-8:06),
and the trailing rule depends on it ("two NASDAQ Effort boxes", 30:44). Under the 1:1 rule this model is
**not implementable** as his strategy. What *is* fully described is a sub-model, the per-candle
**value-migration 1:1 scalp ("Order Track")**, row group B. That part can be a faithful copy of *that* sub-model only.

## A. Framework (stated)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| A1 | Market: NQ only ("I took the time only to test NASDAQ") | (20:05) | STATED |
| A2 | Direction of the day from **value migration**; scalp every move in that direction | (0:22, 14:34) | STATED |
| A3 | Don't trade inside the **"cage"**: session profile balanced from the open (absorption by sellers, then by buyers) → buyers and sellers at fair value; engage only on directional auction **outside value** | "This is what I call 'the cage'... I want always to engage when I have directional auction outside the value" (3:26-3:52) | STATED. Detection → INTERPRETED |
| A4 | Confirmations: value migration + delta/absorption + large orders + NASDAQ Effort area; take only setups with **all** confirmations | (0:40, 0:56, 22:44) | STATED, Effort part **MISSING (proprietary)** |
| A5 | Trend-following; suffers in consolidation; accepted | (22:05, 24:17) | STATED |
| A6 | Validated by him on 3 years of data + 3 months out-of-sample (~300 trades) | (26:27) | STATED (context) |
| A7 | He would **not automate** it: management needs discretion | (18:46-19:10) | STATED, caution for any coded version |

## B. Sub-model "Order Track": per-candle value-migration 1:1 scalp (fully described)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| B1 | For **each candle**, compute its own value area / POC ("who is dominating the migration of the value area" in each single candle) | (13:41-13:47, 16:43-16:56) | STATED → needs per-candle volume at price (footprint) |
| B2 | **Long signal**: value area switches higher vs the previous candle AND buyers dominate (candle closes buy / green) | "value area now is switching higher... look here who is dominating: buyers. This one is what I use to load my 1 to 1" (14:06-14:20) | STATED |
| B3 | **No trade** if value switches one way but the candle closes the other way ("we switch lower with this, but it's a green candle, I don't take") | (16:20) | STATED |
| B4 | Entry **at the value area**, stop beyond the protected value area ("position at the value area, covering below") | (15:15, 15:45, 16:05) | STATED. Exact level (VAL? POC?) → **INTERPRETED** |
| B5 | Target **1:1** ("machine gun 1 to 1"); optional 1:2 when strong absorption shows | (14:20, 15:45, 17:09) | STATED (1:2 is discretionary → variant) |
| B6 | After a target, **wait for the market to realign**; re-enter on the next switch in direction | (15:52-16:05) | STATED |
| B7 | Can flip direction when value flips (short after value switches lower with sellers dominating) | (15:38-16:28) | STATED |
| B8 | **Candle timeframe** of the Order Track | not stated | **MISSING**: required for a 1:1 copy |
| B9 | Chop: win rate 40-50% → breakeven after commission | (19:44) | STATED (expectation) |

## C. Management (needs his proprietary layer)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| C1 | Trail below the last aggression; risk to zero when big absorption appears | (8:55, 9:43, 18:58) | STATED, needs big-order data |
| C2 | Trend model trailing: breakeven when the 2nd Effort box prints, then stop below the previous box | (30:44-31:12) | **MISSING (proprietary)** |
| C3 | **No partial profits on NQ** (tested, hurts); partials on currencies | (31:22-31:49) | STATED |
| C4 | Position building on sustained drift (several positions on initial risk of one) | (19:39) | STATED, discretionary → not in v1 |

## What is needed for a 1:1 copy of sub-model B
1. **B8: the candle timeframe** of the Order Track. Not in this video. If he states it elsewhere, we need that source.
2. **Per-candle footprint data** (volume at each price inside each candle): TradingView **Premium** (`request.footprint`, gives per-bar POC/VAH/VAL and delta) or Databento NQ trades.
3. B4 confirmation from the chart: entry at VAL (long) / VAH (short) of the signal candle, or at its POC?

Until 1 and 2 are available: **no code.**
