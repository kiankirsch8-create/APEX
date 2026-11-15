# VALENTINI audit #2 · V1, V2, V4 for the Python port on Databento trades (2026-11-15)

Why a second audit (§0b): new code (Python harness) and new data (every trade with size and aggressor side). Several rows
that were MISSING or proxied on TradingView can now be built as he states them. Read again for this audit:
`sources/valentini_chart_fanatics_TRANSCRIPT.md` start to finish (CF), and every V4 passage of
`sources/valentini_words_of_rizdom_TRANSCRIPT.md` (WoR: protection level, buy stop, stop, trail, failed auction, compression,
contract filter). Rule sheets: VALENTINI-1_TREND, -2_MEANREV, -4_MOMENTUM. Previous audit: `VALENTINI_audit_2026-11-15.md`.

## A. Misread / would contradict his words if ported as in Pine v5
| # | Model | What he says (time) | Pine v5 | Port does |
|---|---|---|---|---|
| A5 | V2 | Big-order filter **20 contracts in London**, 30 in NY (CF 1:29:36; WoR 27:00 "If you are in London, I use 20") | delta proxy, one threshold | BIG = 20 for V2 (03:00-09:10), 30 for V1/V4 |
| A6 | V4 | Stop "behind the level of aggressive sellers" / "covered below aggressive sellers' failure" (WoR 19:06, 21:12, 26:32); live "cover ourselves below the big trade" (CF 2:52:42) | beyond the box (approved proxy) | Two variants: **box** (approved) and **big** = beyond the farthest absorbed trapped-side big order in the box. Not swapped silently |
| A7 | V4 | Protection level = **repeated** aggressive orders absorbed ("again aggressive, again absorbed... bum, bum, bum", WoR 57:52) | sign of box delta | ≥ 2 big orders of the trapped side inside the box while price holds the box half in bias direction (count = INTERPRETED) |

## B. Forgotten in Pine v5, STATED, now added
| # | Model | Rule (time) | Port |
|---|---|---|---|
| B6 | all | Stop **1-2 ticks inside** an obvious high/low (prev. day/week H/L) to beat the slippage cascade (CF 42:51; rule sheets V1 E6, V2 E7) | if prev-day or prev-week H/L lies between entry and stop, stop moves to 2 ticks inside that level (min-stop floor still applies) |
| B7 | V4 | Fill risk on the squeeze stop entry (WoR 19:23, sheet M12) | stop entries fill at the first trade through the level + 1 tick |

## CONFLICT found in this read (both stay documented, live wording used)
| # | Topic | Source 1 | Source 2 | Port |
|---|---|---|---|---|
| K1 | Early breakeven via CVD | "CVD pushing up and leg building, you can already put to breakeven" (CF 26:19) | live: don't go risk-free too fast near an LVN; "you can go risk-free when you see a candle accepting the high" (CF 2:15:30, 2:54:21) | live rule (full-body close beyond the leg extreme). CVD-BE off (v5 version put 185/292 trades at BE) |
| K2 | Entry timing | "you can be fast and use only big orders" (CF 1:02:18) | anticipating on the first ball = "trading the noise... not the same win rate" (CF 1:28:00); live entries wait for the close (2:52:42, 3:11:31) | main = close of the bubble bar (variant A); fast = test only |

## C. Still not copyable (unchanged unless noted)
- C1 big orders: **now copyable** (same timestamp + same side = one order; matches his live day, `VALENTINI_fidelity_2025-06-23.md`).
  How Deepcharts groups a sweep stays INTERPRETED.
- C2 step-3 reading "you cannot just automate it" (CF 10:55): our trigger is the mechanical part he describes (direction + location + big order).
- C3 management read live: half off at weakness, re-open with profit, add on new highs (CF 1:10:17, 2:17:29, 2:21:11), exit when
  big opposite orders show follow-through and a candle closes with them (CF 1:03:32), house money for "noise" trades (CF 1:38:26).
  **Codable now with trade data** but belongs to the separate management module (to be audited and approved as one block). Not in this test.
- Noise filter ideas found: bubbles flooding both sides = no clear direction, stay out (CF 1:25:47); no trades 10-20 min
  before the open (CF 1:26:52); direction clear 15-30 min after the open (CF 1:23:34). For the noise-filter audit, not in this test.
- VALENTINI-3 (AAA): rule sheet not approved yet, A6 MISSING (macro read). Not built. VALENTINI-5: P5 MISSING. VALENTINI-6: proprietary.

## D. Confirmed correct in v5 and kept
V1: NY 09:45-13:30, bias from the pre-NY range break, outside today's developing VA, CVD-supported break, LVN of the impulse
leg, stop behind the big order, nearest-level target skip < 2.5R (fallback 5R = "trail for the day" proxy), 1m execution, flat 15:55.
V2: London 03:00-09:10, previous-day profile, 1m close back inside the VA (Kian), retracement to the LVN of that swing, big order
in direction, target prev POC (full exit, INTERPRETED), BE after the next breakout of the swing extreme ("additional breakout", CF 17:38).
V4: compression box, stop order on the first attempt then full-body close (Kian), BE on full-body close or first failed test,
exit after 3 failed tests (A2), nearest-level target.
Shared: one position at a time, max 3 losses per day, re-entry after a win only with the day's profit (house money).

## Open for Kian (not blocking this test)
1. A6: which V4 stop should become the main one after the test, box or big order?
2. Min stop 10 pts (from the v1 bug fix) is wider than his stated London risk ($160 = 8 pts, CF 1:08:26). Keep the floor?
3. V2 month filter (summer months) still unanswered; Jun-Aug is inside his "perfect" months anyway.

## Found while checking the first run (same day)
| # | Model | What he says (time) | Pine v5 | Port does |
|---|---|---|---|---|
| A8 | V4 | Entered T2 on a full-body close and said "I will still not go risk-free... you can go risk-free when you see a candle accepting the high; this one is still not accepting the high" (CF 2:54:21); "break this high, we are risk-free" (3:12:42, 3:18:42) | breakeven already on the ENTRY candle if it is full-body (12 of 19 V4 trades ended at BE) | breakeven only on a LATER candle closing full-body beyond the high/low reached since entry, or after a failed test (A2). Pine version kept as variant `v4be="pine"` |
