# Research agenda

Owned by the **Lead Strategist**. Rewritten at the end of every cycle: what each section does next and
why. Researchers (sub-agents) answer agenda items in the run report ("Agenda response"); anything they
need from the lead or another section goes under "Requests".

Last updated: 2026-11-14 (seeded by the main session with Kian's batch).

## Standing rules for picking work
1. Every cycle touches **at least two sections**. Section C gets priority until it has 3 families tested.
2. **Scan first.** Every family starts with `research/scan.py` signals (several variants are fine). Only
   signals that clear about 1.4 pips with t >= 3 in both halves get a Developer and a build test.
3. A family's revision must cite its post-mortem constraint. Three failed builds -> graveyard -> pivot to
   a different mechanism, not a cousin of the dead one.
4. Prefer mechanisms with **few, larger moves** (several pips): the cost line is about 1.1 pips.

## Queue (top = next)

### B: Liquidity / flow
1. **B001 London-open sweep** (lead L001: +1.25 @ 2h, t 2.0). Scan refinements with entry-time filters: tick_count spike
   on the poke, Asian range width percentile, poke depth, minutes after 07:00, news-day exclusion. Target: >= 1.4 pips, t >= 3, both halves.
2. **B004 ORB fade.** Scan 07:00-07:15 UTC and London-time 07:00-07:15 boxes; re-entry after a 3-6 pip break; upticks-downticks stall.
3. **B002 Fix / NY cut.** Scan pre-fix drift then 0-20 min post-fix reversal (London clock) and the NY 10:00 cut (NY clock), separately.
4. **B003 Tick climax.** Scan tick_count > 98th pct of past 2h plus spread_high_raw spike, outside +-30 min of news.
5. **B005 Round numbers.** Scan first touch of .00 / .50 per London day with decelerating tick rate.

### C: Momentum / breakout
1. **C001 Compression -> US-open breakout.** Scan the bottom-20% 06:30-08:00 NY-time range, then first 5-min impulse after 08:00-09:30 NY.
2. **Generate 2+ new C families** grounded in mechanism (session-open continuation at longer horizons, H1-H4 trend
   after news, momentum in other pairs once data exists). F2 says short-horizon breakouts fade: a C idea needs a
   reason why its horizon or trigger is different.

### A: Mean reversion
1. **A001 Grid revival.** Scan regime filters first (range/ATR percentile, trend slope, Asia), time stop instead of basket stop.
2. **R001** has 1 attempt left, but L002 says the edge is weak; only spend it if a post-mortem gives a concrete constraint.

## Requests from researchers
- (none yet)
