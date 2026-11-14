# Research memory (read this first, every cycle)

Distilled knowledge for every role. **Short, factual, with numbers.** Raw results live in `results/`,
`scans/results/` and `ledger.jsonl`; this file is the summary that makes the next idea smarter.

Rules for editing (Lead Strategist at the end of each cycle, using the Post-Mortem Analyst's evidence):
- **Facts**: only harness facts or effects with t >= 3 that hold in BOTH halves (2018-19 and 2020-21).
- **Graveyard**: only families with status graveyard/closed, each with the evidence and *why* it died.
- **Leads**: partial edges. Each needs n, mean, t, **how many slices/signals were searched**, and the
  split check. A lead is a hypothesis, not a fact. Promote to a strategy only after the split check passes.
- Use only features known **at entry** (session, clock, volatility, spread, trend, time-to-news). Never
  conclude from hold time or exit reason; they depend on the outcome.
- Nothing from 2022-2024 ever goes here.

Last updated: 2026-11-14 (seeded by the main session).

## Costs and harness facts
- Capital.com EURUSD: no commission, spread floored at 0.7 pip, 0.2 pip slippage on market/stop fills ->
  **about 1.05-1.1 pips round trip intraday**. Long overnight: about +0.8 pip per night funding; short about 0.
- A strategy needs about **1.4+ pips edge before costs per trade** to pass the build gate (net >= +0.30).
- Limit entries skip slippage and half the spread crossing, but see adverse selection below.
- Decisions happen at bar close (M1 minimum). Features: tick_count, spread_high_raw (raw, pre-floor),
  upticks/downticks (tick-direction proxy; no real buy/sell volume), london_minute / ny_minute (DST-aware),
  weekday, news_next_min / news_last_min (NFP, CPI, FOMC, ECB 2018-2024).
- HistData ticks are sparser than broker feeds (about 1.8M/month vs about 6.5M) and volume is always 0.

## Facts (t >= 3, both halves)
- F1 Short-horizon mean reversion in EURUSD is **real but small**: Bollinger/z-score reversion edge
  +0.07-0.21 pips/trade (M1 t about 7, M5 t about 3-4). About 15x too small for costs on its own.
- F2 Short-term **breakouts reverse**: Donchian breakout edge −0.23 (M1, t −9.9), −0.42 (M5, t −3.5);
  worst in Asia and late NY.
- F3 Grid v1 (basket stop): negative skew; wins +2.3, basket stops −7.4, needs 80-87% win rate; edge negative even before costs.

## Graveyard (do not retest variations of these)
- **mean_anchor_reversion** (as implemented): no edge at any TF, |t| < 2.
- **Raw Donchian breakout M1-M5**: negative edge (F2). Its inverse is only +0.2-0.4 pips.
- **R002 Asia stop-run fade (M5, Asia session)**: limit beyond 4h channel −0.17 (t −1.1); poke-and-reject +0.01 (t 0.2), 3022 trades.
- **R003 16:00 London fix fade (16:05-17:30)**: wrong-signed in all 4 years, edge −1.14 (t −2.1). Do not flip it without a new
  mechanism (B002 tests a different, shorter window plus the NY cut).
- **Resting limits at extremes for fades** (R001a, R002a): adversely selected, edge −0.43 / −0.17. Limits fill mostly on moves
  that keep going. A limit only helps when placed *after* exhaustion is confirmed.

## Leads (hypotheses, not facts)
- **L001 London-open sweep fade** (B001, crude 5m version: poke beyond the Asian range 07:00-08:30 London, close back inside):
  702 events, best horizon 24 bars (2h) +1.25 pips, t 2.0; split 2018-19 +1.91 / 2020-21 +0.55; by year +1.46/+2.37/+0.60/+0.51.
  Searched: 1 signal x 4 horizons. **Status: open**, needs refinement (filters known at entry: tick spike, range size, depth of poke)
  and a split check at the 1.4-pip bar.
- **L002 Close-confirmed Asia band fade M15** (R001b): +0.32 pips (t 1.0), long side +1.39 (t 3.1) vs short −0.68: likely the
  2018-21 drift, not a rule. Searched: 2 variants x side split. **Status: weak**.

## Open questions
- Is there a horizon (H1-H4, holds of hours to a day) where reversion or continuation clears costs? (Funding matters for longs.)
- Do any B/C effects survive on other pairs (GBPUSD, USDJPY, AUDUSD, XAUUSD) once their data is in?
- Which effects concentrate around news (news_next_min / news_last_min), and should those windows be traded or avoided?
