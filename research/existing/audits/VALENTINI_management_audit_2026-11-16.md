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
