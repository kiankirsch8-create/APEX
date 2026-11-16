# VALENTINI management audit (G-rules from `VALENTINI_channel_review_2026-11-16.md`), 2026-11-16

| # | Rule | Built in valentini2 (`mgmt="his"`)? | Why / how |
|---|---|---|---|
| G1 | No partial profits, trail all the way | yes (nothing partial exists) | CONFLICT CF/WoR resolved in favour of the tested, latest statement |
| G2 | Trail behind the last aggression | yes | after breakeven: each candle closing in direction with a big order in direction OR an aggression candle → stop 2 ticks beyond its low/high |
| G3 | Risk to zero when the other side takes control | partly | existing breakeven rules per model; no extra rule |
| G4 | Position building with 1/3 of locked profit | **no, next step** | needs concurrent positions in the engine (single-position engine today) |
| G5 | Only the day's profit at risk after gains | yes (house money, unchanged) | |
| G6 | Break-even session stop | yes | once the day was positive, no new trades when the day's P&L is back to <= 0 |
| G7 | Daily loss budget | yes (3 losses / 2% cap, unchanged) | |
| G8 | 0.25-0.5% per trade | yes (account view 0.25%) | |
| G9/G10 | KPIs / regime-dependent targets | no | reporting only |
Targets (prior balance levels) stay as in each rule sheet; trailing only protects open profit.

## G4 built (2026-11-16, Kian: "build G4, test on V1 all his rules and V4 range bars")
Trigger (INTERPRETED from ZERO 11:28-12:31): position >= 1R in profit AND a candle closes beyond the extreme since entry,
in direction, with a big order or an aggression candle ("acceptance... big trades buy") → stop of the whole position to the last
invalidation point (lowest low / highest high of the last 5 candles ± 2 ticks), which locks the floating profit → one third of the
locked profit is risked on an add-on at the next candle's first trade (same stop, same target), size floored to MNQ (0.1 NQ), max 2
add-ons, max 3 NQ-equivalent per add-on. Code: `valentini2.py` (`g4=True`), test: `batch_g4.py`.
12.5-month result: V4 range bars 35 add-ons, net/NQ +$7,450 → +$17,762, PF 1.38 → 1.99, $100k +2.8% → +5.5%;
V1 all his rules only 3 add-ons, slightly worse (+$6,134 → +$4,866). Portfolio V1+V4 with G4: +$22,628/NQ, $100k +10.4% (DD 4.5%).
