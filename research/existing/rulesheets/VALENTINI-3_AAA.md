# VALENTINI-3 · "AAA" setup: value-area-low absorption long (rule sheet, DRAFT for Kian's review)

Source: `sources/valentini_words_of_rizdom_TRANSCRIPT.md` (WoR). Timestamps are from that transcript.
Missing source: the Chart Fanatics masterclass, where he says he "went through all the models"
(WoR 28:58). Rows marked ⏳ should be checked against it.
Status: STATED / INTERPRETED / MISSING / CONFLICT (see `../FIDELITY.md`).

## A. Market, session, context
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| A1 | Market: NQ futures (also ES; "volume on indices and commodities amazing"). Not for FX volume | "this is my NASDAQ model account" (16:54); "for forex it's useless because volume is so fragmented" (1:59:25) | STATED |
| A2 | Session: New York open; usually wait the first 30 min for stability | "usually I wait for... the first 30 minutes of New York opening to have a little bit of stability" (4:13) | STATED |
| A3 | Exception: may act earlier if absorption is already brutal | "But I can already see that the sellers are getting absorbed brutally" (4:20) | STATED, but discretionary → **INTERPRETED**: v1 = no entries before 30 min; variant = allow earlier if the absorption trigger (C2) fires |
| A4 | Day type: a **momentum/trend day**; setup goes from VAL to VAH | "going from the value area low in a momentum day to the value area high" (6:52) | STATED. How to tell a momentum day before the fact → **INTERPRETED** (see A5) |
| A5 | Why it's AAA: low on the curve (VAL) + 30 min into session, participants have shown intent | "Value area low. So we are really low on the curve... it's 30 minutes in the session... already they understood what they want to do" (10:17) | STATED |
| A6 | Pre-trade thesis / directional bias (e.g. gap to be squeezed, options pressure) | "my pre-trade model was that today we will push and squeeze this high" (32:49); options "completely long" (30:44) | **MISSING** (discretionary macro read; 0DTE options data not available) |
| A7 | Avoid days with no clear pressure: "CVD not so defined... Don't engage" | (1:15:53–1:16:22) | STATED → needs CVD (order flow) |

## B. Location (where to look for the trade)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| B1 | Volume profile of **the first hours of the session** → VAL, VAH, POC | "We are using the profile distribution of the first hours of the session" (4:48) | STATED. Exact anchor (session start? pre-market? first 30 min?) → **INTERPRETED** ⏳ |
| B2 | For momentum trades, profile is plotted from the **expansion move** (A to B), the first directional move of the session | "the best way to do it for momentum trades is from the expansion movement... I plot from point A to point B" (1:37:54); "you need to get the first directional move of the session" (1:38:52) | STATED. How to detect "expansion start/end" → **INTERPRETED** |
| B3 | First area: **VAL** reaction | "we did an amazing reaction on the value area low" (4:59) | STATED |
| B4 | Second area if VAL breaks: the **POC of the accumulation** that created the up-move | "there is a red level... the POC... the point of maximum interest for the accumulation that created this up movement. So this is the second area" (8:06–8:21) | STATED |
| B5 | If that POC area breaks too → no longs for the day ("consolidation day") | "If this area gets broken, I don't want to be long for the day... a consolidation day" (8:29) | STATED |
| B6 | Bubble filter: big trades = prints ≥30 contracts NY, ≥20 London | "Always the same, it's 30 contracts. If you are in London, I use 20" (27:00) | STATED → needs trade-by-trade data |

## C. Trigger
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| C1 | Don't enter while sellers still get follow-through; cancel if the level is breached | "They breached the level... canceling the buy stop and going to consider another area" (7:45–7:51) | STATED |
| C2 | **Absorption**: big aggressive sell prints at the level but price doesn't go lower ("hitting a wall") | "How can we see absorption? As I explained to you, big trades. It means they are hitting a wall" (6:47); "they are starting to get heavily absorbed" (9:14) | STATED. Quantification (how many bubbles, how little price progress) → **INTERPRETED**; needs order flow (MISSING without it) |
| C3 | Check delta | "We can check also with the delta" (8:35) | STATED, how exactly → **INTERPRETED** |
| C4 | Entry via **buy stop** above the absorption (be "covered below this low"); scale in fractionally | "I can start to build buy stop... if the market gets heavily absorbed here, I want to be covered below this low" (7:24–7:31); "One, two, three contract, going for four" (8:41) | STATED. v1: single entry (scaling → variant) |

## D. Stop and management
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| D1 | Stop below the absorption low / level | "We are going to cover ourselves below here" (8:51) | STATED |
| D2 | Risk sizing: fixed $ risk (~$2,000) for ~$7–10k potential (≈3.5–5R) | "It's only $2,000 risk... profit of $7,000" (8:51); "risking 2,000 to make a potential $10,000" (9:55) | STATED (R terms) |
| D3 | Go **risk-free fast** (≈1 minute) | "In 1 minute the position is risk-free" (10:37); "if you can put your stop loss to zero in the first minute, it means that you are joining the battle on the momentum side" (1:16:53) | STATED. Mechanism (time vs. price) → **INTERPRETED**: breakeven once price moves X ticks in favour within N minutes, else exit? ⏳ |
| D4 | Trail the stop behind each new wave of aggressive buyers | "trail our stop loss... As soon as we see buyers getting in the position" (11:25); "I want to be covered here" (13:20) | STATED, needs order flow → proxy: trail behind last higher low |
| D5 | Take partial / lock profit near risk points (e.g. the gap) | "The point of risk that we have is the potential close of the gap" (10:57); "I took 5,000 out" (12:26) | STATED, discretionary → **INTERPRETED** |

## E. Exit
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| E1 | Target: **VAH** | "Going to target the value area high" (6:26) | STATED |
| E2 | Exit early at a **round number** when absorption shows at the top | "a little bit of absorption here because it's a round number... 25,000" (16:16) | STATED → round number (1000 / 500 / 100?) **INTERPRETED** |
| E3 | Never hold for the extreme; take profit at walls (3–5R), "70% of time market is stationary" | (36:05–39:00) | STATED |

## F. Discipline (applies to all his models)
| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|
| F1 | Stop after **3 losing trades** (4–5 early in session) | "My rule was three losing trades to stop... In the starting of the session I give more room, I have four or five" (1:19:10–1:19:16) | STATED |
| F2 | After a big first wave with zero retracement → done for the day (sideways follows) | "after a wave like this, zero retracement, the market will just go sideways" (28:18); "if I get the 20,000 profit for the day, my rules say it's done" (25:02) | STATED. "Big wave" threshold → **INTERPRETED** |
| F3 | Daily max drawdown; risk only session profit after a cushion | (3:15, 54:31–55:17) | STATED |
| F4 | Don't trade inside contraction ("punching a wall on both sides") | (1:27:18–1:27:36, 1:01:41) | STATED → detection **INTERPRETED** |

## Summary
- Price-only version possible for: A1, A2, A4/A5 (approx.), B1–B5 (with a **volume profile** from NQ bar volume), C1, C4, D1–D3, E1–E3, F1–F4.
- Needs order flow (bubbles, delta, CVD): B6, C2, C3, D4, A7 → **proxy** without footprint data.
- Fidelity achievable: **Proxy** on TradingView Essential/Plus; much closer on **Premium** (footprint: per-price buy/sell volume, delta, POC/VA) or with Databento NQ trades.

## Open questions for Kian
1. B1: what is "the first hours of the session" profile in practice: from 09:30 NY, or including pre-market? (masterclass ⏳)
2. A3: allow entries before 09:30+30 min if absorption is strong, or stick to the 30-min rule?
3. D3: breakeven "within 1 minute": implement as "move to breakeven after +N ticks", or "exit if not in profit within N minutes"?

## Masterclass check (2026-11-14)
- Risk-free mechanism (open question 3): "when you see that you get an additional breakout, immediately stop to breakeven"
  (CF 17:38-20:01); CVD-based early breakeven (CF 26:19); "full body candle accepting the high" (CF 2:54:21). Still discretionary.
- 30-min rule context (open question 2): "direction for the day [clear] from 15 to 30 minutes inside the opening" (CF 1:23:34).
- Daily cap 2%, 0.25% per trade (CF 2:09:19, 2:23:06) confirm F-rows.
