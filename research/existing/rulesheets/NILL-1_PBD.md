# NILL-1 · PbD model (impulse → range → trade the range or the exit) — rule sheet v2

Creator: Patrick Nill (Robbins World Cup, 2× winner, 12× top 5). Model developed by Thomas (Trade the Traders), market/volume
profile part by Serge. Nill: "the PBD logics that you showed me [Thomas] ... that's already half the battle".

## Sources (all in `sources/`)
| Code | File | Value |
|---|---|---|
| IQ1 | `nill_iqcapital_TRANSCRIPT.md` (first IQ Capital interview) | zones, stop/target hints |
| IQ3 | `nill_iqcapital_3touch_TRANSCRIPT.md` (Dubai whiteboard, "3-touch") | **main source: setups, entries, targets** |
| WCE | `nill_wce_embarrassingly_simple_TRANSCRIPT.md` | timeframes, risk, style |
| TTT | `nill_ttt_200pct_TRANSCRIPT.md` | win rate, losing streaks, "levels/incident → business zone" |
| STG | `nill_stuttgart_2tools_TRANSCRIPT.md` | tools, championship holding (swings, fees) |
| MAG | `nill_magnus_commentary_TRANSCRIPT.md` | clips of IQ1 + backtest/DD rules |
| TIT | `nill_titans_PARTIAL_TRANSCRIPT.md` | **incomplete** (the setup part was summarised away) |
| LPT | `nill_lptrades_react_THIRDPARTY_TRANSCRIPT.md` | third-party reaction, index only |

## A. Context rules
| # | Rule | Quote | Status |
|---|---|---|---|
| C1 | Markets are in ranges ~70% of the time, trending 20-30% | IQ3 3:08 | STATED (belief) |
| C2 | P = big impulse up + range; b = big impulse down + range; he trades **after** the impulse, never the impulse | IQ3 19:37, MAG 2:17-3:15 | STATED |
| C3 | **Range is ready after the 3rd touch** ("one two three ... then the probability is bigger for the breakout"); before that "much too early" | IQ3 0:15, 19:37 | STATED; what counts as a touch → **MISSING** |
| C4 | Range/zone drawing rules | IQ3 4:04 "there are some rules to sign the zones ... I don't tell it" | **SECRET** |
| C5 | Range widen/shrink rules as price makes new highs/lows inside | IQ3 20:30 "these rules are secret" | **SECRET** |
| C6 | Size of a "big impulse" | — | **MISSING** |
| C7 | A good P needs half a day to a day on the 15-min chart | IQ3 21:36 | STATED |
| C8 | Context: weekly volume-based market profile, VAH/VAL; best when P/b forms at/under such a zone; two ranges at the same level = extra good | IQ1/MAG 6:05, IQ3 14:05, 16:21 | STATED; profile anchor = INTERPRETED (weekly, Mon-Fri) |
| C9 | "Levels/incident determine the business zone" (price must be in the business zone first) | TTT 4:15; German video summary | STATED, definition → **MISSING** |
| C10 | Small range after a huge move → breakout trades; big range → range trades (breakout not worth it, stop too far) | IQ3 16:21 | STATED |
| C11 | Timeframes: 4h big picture, **15 min decision**, 5/3 min execution only to tighten the stop when the target is small | IQ3 24:42, WCE 5:48 | STATED |

## B. The four setups (all stated in IQ3)
| Setup | Entry | Stop | Target | Status |
|---|---|---|---|---|
| **N1 Counter-return** (his favourite) | P (b) breaks out the "wrong" way (down out of a P); short after confirmation (rule E1) | above the zone, or in its middle | back to the **start of the impulse**; RR ≥ 3 | STATED |
| **N2 Confirmed breakout** (with trend) | E1 confirmation, mostly a **limit order** at the zone edge | range low/high | next fair value; if none (all-time high): **copy the impulse 1:1** from entry | STATED |
| **N3 Failed break / reclaim** ("the perfect trade") | price closes below the range, comes back and closes inside → long | just under the false break (small) | the whole move up, partials at 1/3s | STATED; prefers it on the long side, dislikes the mirrored short after an up-break |
| **N4 Range ping-pong** | limit order at range edge (after 3 touches) | under the range low (over the high) | opposite edge, **no partials** | STATED |

| # | Entry/exit detail | Quote | Status |
|---|---|---|---|
| E1 | Breakout confirmation: candle(s) break out, price comes back to the zone edge, **next candle closes beyond** that candle; or: forms a low, pulls back, breaks the low (2nd lower low). No confirmation = no trade ("forced breakouts") | IQ3 8:11, 14:37 | STATED (needs coding interpretation of "back to the zone") |
| E2 | Order flow/footprint = optional extra confirmation; "it works" without it | IQ3 15:40, 26:42 | STATED; what he looks for → **SECRET** ("I should show you that") |
| E3 | Partials: trades multiples of 3 contracts; TP1 at the zone edge, TP2 at the next range, TP3 at the impulse start | IQ3 6:13, 10:32 | STATED |
| E4 | Stop placement: structure/zone/low or a profile zone; if nothing there, pick a stop that gives RR > 3 (e.g. 200-pt target → 30-40-pt stop) | IQ3 13:17 | STATED |
| E5 | Trail: move the stop under each new P range that forms in the trend | IQ3 18:01 | STATED ("several rules" → partly **MISSING**) |
| E6 | **Never widen a stop** (oil −42 lesson); size from stop distance | IQ3 34:00, 36:10 | STATED |
| E7 | Early exits on 1-5 min signals, "no system" | IQ3 23:30 | DISCRETIONARY → not copied |

## C. Filters and risk
| # | Rule | Quote | Status |
|---|---|---|---|
| F1 | No entry when the P completes in the evening (no overnight watch) | IQ3 21:36 | STATED |
| F2 | No breakout near important news; ~14:00 CET (US open) often changes the market | IQ3 26:42, 23:30 | STATED |
| F3 | Session: mornings (Europe) until ~14:00-15:00 | TIT (Thomas) | STATED |
| F4 | Stop for the day after 3-4 losses | IQ3 44:48, TTT 11:36 | STATED |
| F5 | Avoid strong trends (>20% in two weeks in oil) — his weakness | IQ3 48:32 | STATED (no rule) |
| R1 | Risk: private 0.1-0.3% (German video), "1% rule"; championship 2-4% | WCE 12:00, MAG 11:52 | STATED |
| R2 | Holding: hours to 3-5 days; in the championship often closed in the evening | WCE 8:18, STG 21:36 | STATED |
| R3 | Wants PF ≈ 2; backtest DD < 10%, > 20% = reject; consistency over 5 years | IQ3 41:49, MAG 14:32 | STATED |
| R4 | Win rate: **contradiction** — 30-40% (TTT 10:52) vs 60-75% per championship year (IQ3 44:48) vs 50-60% (IQ1) | — | LOGGED |
| R5 | Markets: DAX, oil, some gold. NQ/ES: "I'm a newbie"; DAX fakes ~10 pts, ES 1-2 → rules must be adjusted per market | IQ3 40:40, 43:45 | STATED → **NQ port is our adaptation** |

## Verdict (v2)
- **Much better than v1.** Entries, stops, targets, partials, trailing and filters for four setups are now stated.
- **Still not a 1:1 copy:** the core object — *how he draws the range/zone* (C4, C5), the impulse size (C6), the business zone
  (C9) and his order-flow confirmation (E2) — are explicitly secret or undefined. Any build needs our own objective range
  definition, labelled INTERPRETED, and that is exactly the part that decides which trades exist.
- He has never traded it on NQ/ES and says the rules must be adapted per market (R5).
- **Not purely swing:** the intraday version (15 min, flat by evening, F1/R2) fits our NQ scalping data. The multi-day version
  (oil/DAX) belongs to the swing engine.

## What would close the gaps
1. The free **World Class Edge masterclass** Nill recorded: "only the structure from the PBD setups ... when they can trade and
   where not" (WCE 23:10) — that is precisely C3-C6.
2. German video **"Patrick Nill: die Geheimnisse hinter dem Trading Weltmeister"** (mentions business zone, relative volume
   filter, limit entry on the retest).
3. The **full** Titans of Tomorrow transcript (youtu.be/_5fP-jFV1tw): the description lists "P- and B-shapes, ledges, the range
   break-in setup" but the attached transcript stops before that part.

## Options for Kian
- **A (strict §0b):** wait for 1-3; don't build yet.
- **B:** build "NILL-1i" now with an objective, clearly labelled interpreted range (e.g. box with ≥3 touches per side after an
  impulse ≥ k × ATR on 15 min), test N1-N4 on the 12.5 months NQ, and treat it as Nill-*inspired*, grade C, with a parameter-plateau
  check on the interpreted parts. Replace the interpretations once 1-3 arrive.
