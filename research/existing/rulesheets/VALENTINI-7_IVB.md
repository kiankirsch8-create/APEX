# VALENTINI-7 · IVB: opening-range breakout + range profile + order flow (rule sheet, DRAFT for Kian)

Source: `sources/valentini_ivb_TRANSCRIPT.md` (IVB). Timestamps = video time. Status legend: `../FIDELITY.md`.
He builds on Toby Crabel's opening range breakout (0:08) and says the base ORB alone is already profitable (3:11-3:20).

## R. Range and direction
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| R1 | Range = high/low of the first 15, 30 or 60 min of the NY cash session; examples all use **30 min** | "top and the bottom of the range of the first 15, 30, or 60 minutes" (2:06); "wait for the first 30 minutes" (10:47, 16:30, 20:43) | STATED. 15 vs 30 → **CONFLICT → variants**; main = 30 |
| R2 | The first side that breaks = who won the battle = **direction for the day** | "the first one that break is telling you who won the battle" (2:19); "you are not using discretionality to frame your bias for the day" (14:44) | STATED |
| R3 | Breakout = candle breaking out of the range | "we have a first candle breakout" (12:40); "if we close a candle here... invalidated" (6:12) | STATED; close vs touch → **INTERPRETED** (1m close beyond the range) |
| R4 | Inside the range before a breakout: optional mean reversion, fade top/bottom on absorption/exhaustion | (9:50-10:33) | STATED, separate sub-model, not in v1 |

## L. Location (Model 1, profile)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| L1 | Fixed volume profile of the range (point A = range top, point B = range bottom) → POC, VAH, VAL | "we take this top, we drag down with the fixed profile" (4:42) | STATED |
| L2 | Entry zone after the breakout = between the value area edge and the POC ("real block of orders"), on the retracement | "the level contained between the value area and the POC. This is our real block of orders... when you retrace, you can expect buyers to reload" (5:05-5:27) | STATED. Which edge for a long (VAH side?) → **INTERPRETED**: long = POC..VAH, short = VAL..POC |
| L3 | Invalidation: a 1m close beyond the opposite value-area edge (long: below VAL) | "a value area low that if it gets broken... if we close a candle here, the setup is invalidated" (6:06-6:12) | STATED |
| L4 | Stop = "first protection point"; R:R becomes 1:2-1:2.5 instead of 1:1 | (6:23-6:32) | STATED, exact point → **INTERPRETED** |

## T. Trigger (Model 2, order flow)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| T1 | On the retracement into the zone: **absorption** = aggressive sellers with zero result at the POC → long | "a lot of aggressive sellers here with zero result... big absorption on this level, on the POC... confirmation trigger to go long" (7:25-7:39) | STATED; amount → **INTERPRETED** (same as V3/V4: >= 2 big orders, price holds) |
| T2 | Alternative: exhaustion (sellers stop interacting) + aggressive buyers with momentum → momentum trade, really tight stop | (7:47) | STATED, sub-variant |
| T3 | Buyers protecting the area with aggression → buy, stop below | "when you have aggression of the buyers... protecting the area, you can consider a buy with a stop loss below" (19:56) | STATED |
| T4 | Multiple re-entries at the same absorption area are allowed ("for the fourth time") | (15:08, 22:01-22:17) | STATED |

## X. Targets
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| X1 | TP1 = "protection level" = DeepCharts algorithm (machine learning on years of data), reached 65-70% of the time; TP2 = "maximum extreme average" | (8:15-8:48, 16:54-17:00) | **MISSING (proprietary)** |
| X2 | Practical targets: "better to take 1-to-3, 1-to-2.5 with a really consistent way"; keep a runner to the protection level | (23:00, 14:08) | STATED → usable fallback: fixed 2.5R or 3R |

## Fidelity verdict
Range, direction, profile location, invalidation and order-flow trigger are codable with our Databento trades.
The protection-level target is proprietary → only a **Proxy** with fixed 2.5-3R targets (X2, which he states himself).

## Link to other models (for Kian, not applied without approval)
R2 is a **stated, mechanical day bias** ("not using discretion to frame your bias"). Our suite uses the break of the
pre-NY range (18:00-09:30, CF 1:43:50). In CF 2:02:02 he also names the "initial breakout" of the NY opening range.
Candidate for the missing momentum-day rule of VALENTINI-3 (A4/A6) and as a bias variant for V1/V4.

## Open questions for Kian
1. R1: 30 min as main (all his examples), 15 min as variant?
2. L2: long zone = POC..VAH (the side the breakout came from)?
3. L4: stop = 2 ticks beyond the absorption low (as V3/V4), or beyond the VAL (his invalidation)?
4. X2: fixed target 2.5R or 3R?
5. Use the IVB breakout as the day bias for V3 (AAA), and as a variant for V1/V4?
