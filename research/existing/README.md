# Track 2: existing strategies (tested as published, not refined)

> **Mandatory: `FIDELITY.md`.** Primary source (transcript) → rule sheet (STATED / INTERPRETED / MISSING /
> CONFLICT, with quotes) → Kian approves → code → fidelity grade on every result. No code before approval.

Strategies taken from traders and creators. Each is turned into an **exact rule spec** (every vague
term gets one fixed definition, logged under "Definitions chosen"), implemented once, and run through the
same gates as Track 1 (check → 2018–2021 build → robustness → blind validator → Kian's review). No tuning.
If one passes, it is locked into the engine as an existing strategy. If it fails, the lesson goes to
`research/MEMORY.md`, and a promising piece can become a Track 1 idea.

**Naming:** `<TRADER>-<n>` plus the strategy type. Raw recaps: `sources/`.

Buckets: **Reputable** = verified/audited track record. **Influencer** = course or affiliate marketing,
unverified claims. Both are tested; the bucket is context, not a verdict.

**Testability legend:**
- ✅ Price-only: our quote data tests it faithfully.
- 🟡 Proxy: needs volume or order flow; testable only with proxies (tick count, upticks−downticks, tick-count profile).
- 🔴 Needs data we don't have yet.

## Register

| Name | Type | Bucket | Market (as published) | Testability | Source |
|---|---|---|---|---|---|
| **VALENTINI-1** | Imbalance trend: out-of-balance → pullback to the impulse leg's LVN + big aggression print → 100% exit at prior POC | Reputable | NQ, NY session | 🟡 / 🔴 true version needs futures trades | chart_fanatics_masterclass, deep_dive |
| **VALENTINI-2** | London / low-vol mean reversion: "second drive" back inside balance → LVN pullback → toward POC; stops 1–2 ticks inside extremes | Reputable | NQ, London; Mar/May/Jun/Aug | 🟡 / 🔴 | chart_fanatics_masterclass |
| **VALENTINI-3** | "AAA" VAL/POC absorption: wait 30 min after NY open, absorption at VAL/LVN, buy-stop above it, target VAH, breakeven within 60 s, trail behind aggression | Reputable | NQ, NY | 🟡 / 🔴 | words_of_rizdom, deep_dive |
| **VALENTINI-4** | Momentum squeeze / compression breakout: repeated failures at a protection level, then a full 1M body close beyond the box (stop order) | Reputable | NQ, NY | ✅ core (box + body close) / 🟡 absorption filter | words_of_rizdom, masterclass (live trades) |
| **VALENTINI-5** | NY Power Hour rebalance: after a first-hour expansion, consolidation until 13:30–15:00 NY, then the second leg | Reputable | NQ | ✅ (time + range logic) | words_of_rizdom |
| **VALENTINI-6** | Four-Force dominance (his newest model): value migration + effort/thinness + delta absorption + big orders aligned → scalp every pullback with the dominant side; stand down in a "two-sided cage" | Reputable | NQ | 🟡 / 🔴 (third-party breakdown, IQCapital) | deep_dive |
| **GOH-1** | Market Shift Entry Model: 1H structure → 15M supply/demand in premium/discount → 5M sweep + body-close shift → retrace entry, ≥2R, London only, news blackout, max 3 trades/day | Influencer | XAUUSD (also says works on FX) | ✅ with fixed definitions ("secret indicator" untestable) | goh_market_shift |
| **MAMBAFX-1** | 5M support/resistance exhaustion (≥3 touches) → 1M structure breakout in the bounce direction; short version adds trendline + support break; 1:3–1:5, trail at first zone, pyramiding | Influencer | FX / indices, 1M | ✅ with fixed definitions | mambafx_1min_scalping |
| **DATATRADER-1** | First 4H NY candle (00:00–04:00 NY) range → 5M body close outside → close back inside = fade entry; stop at excursion extreme (capped when wide), 2R target, same-day only | Influencer (faceless clone; original: Data Trader) | BTC, EURUSD, XAUUSD | ✅ fully rule-based | datatrader_4h_ny_range |

## Shared rules worth testing as filters (from VALENTINI sources, his own data)
- No trading in the first 15–30 min of the NY open (whipsaw), and not in the last 10–20 min before it.
- Stand down inside a "two-sided cage" (absorption at both extremes); trade only after an impulsive escape.
- Stop after 3 consecutive losses in a session; 2% daily loss cap; risk 0.25% per trade.
- His Python audits: no Fridays on NQ, no trading after his evening cutoff. Treat these as hypotheses to slice
  on our data, not as copied rules (they were fitted on his own trades).
- Range bars (e.g. 40-range on NQ ≈ 3-8 pip range bars on EURUSD) instead of time bars.

## Data notes
- **Order flow:** VALENTINI-1/2/3/6 depend on executed volume, big-trade prints, delta, CVD and volume profile.
  Spot FX and CFD quotes have none of that (Fabio says so himself for EURUSD). A faithful test needs **NQ futures
  trade data with aggressor side** (e.g. CME tick/trades data from a vendor). Without it we test proxies and
  label the result "proxy".
- **Instruments:** NQ / US100 (Capital.com US100 CFD for execution), XAUUSD, EURUSD (we already have it).

## Status (fidelity pipeline)

| Name | Transcript | Rule sheet | Approved | Code | Grade |
|---|---|---|---|---|---|
| VALENTINI-1, 2, 6 | masterclass + IQCapital needed | – | – | – | – |
| VALENTINI-3 (AAA) | WoR ✓ (masterclass ⏳) | `rulesheets/VALENTINI-3_AAA.md` draft | awaiting Kian | – | – |
| VALENTINI-4 (momentum) | WoR ✓ (masterclass ⏳) | `rulesheets/VALENTINI-4_MOMENTUM.md` draft | awaiting Kian | old Pine = DRAFT, not usable | – |
| VALENTINI-5 (power hour / Model B) | WoR ✓ | `rulesheets/VALENTINI-5_POWERHOUR.md`: entry rules MISSING | – | – | – |
| DATATRADER-1 | needed (recap is detailed) | draft from recap | – | Pine v1 (4 INTERPRETED items, see rule sheet) | pending |
| GOH-1 | needed | – | – | – | – |
| MAMBAFX-1 | needed | – | – | – | – |
