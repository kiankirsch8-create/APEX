# Section A: first real-data run (agent briefing)

**Status:** baseline run, done 2026-11-12. All five reference strategies lose money
after costs at every timeframe. This is the expected starting point. These
strategies were built to prove the engine, not to trade. This file explains
**what failed, why, and where the data points next**, so the next iteration
starts from evidence instead of guesses.

Machine-readable data in this folder:

| File | Contents |
|---|---|
| `sectionA_diagnostics.json` | per strategy × timeframe: edge vs costs, sliced by session, side, year, exit reason, hold time |
| `sectionA_m1_by_quarter.json` | M1 variants, every quarter: return, trades, win rate, PF, drawdown, costs |
| `sectionA_htf_by_quarter.json` | M5 / M15 / H1 variants, same fields |

Reproduce: `python -m scalping.run --config scalping/config_histdata.json --block <id> --strategy <names> --trades`,
then `python -m scalping.tools_summarize` / `python -m scalping.tools_diagnose` (see each script's docstring).

## Test setup

- **Data:** HistData.com EURUSD ticks, Jan 2018 – Dec 2024, 82 of 84 months (Aug 2018 missing).
  About 1.8M ticks/month (sparser than broker feeds, which run about 6.5M/month), zero volume, median spread 0.2–0.4 pips.
  Import: `python -m scalping.data.histdata DAT_ASCII_EURUSD_T_*.zip`.
- **Blocks:** 29 calendar quarters, scored independently from €100k. Nothing was tuned on any block.
- **Fills:** tick by tick. Market and stop orders cross the spread plus 0.2 pip slippage; limit orders fill at
  their price with no slippage. Commission is $3.5 per lot per side. An order whose take-profit is
  already through the fill is rejected.
- **Variants:** each strategy on M1, plus `@M5`, `@M15`, `@H1` (see `strategies/timeframe.py`): decisions on completed
  higher-timeframe bars, point distances scaled by √minutes, fills still tick-exact.

## Definitions

- **edge**: mid-to-mid price move in the trade's direction, in pips, **before any cost**. This measures signal quality.
- **costs**: half spread in + half spread out + slippage both ways + commission. **net = edge − costs.**
- **t**: edge mean ÷ standard error. |t| ≥ 3 is the bar for "real" here, because many slices are tested.

## Results

| strategy | TF | trades | edge (pips) | t | costs | net | win % | needed win % |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| bollinger_reversion | M1 | 149,656 | **+0.08** | **+7.4** | 1.40 | −1.32 | 31.8 | 50.9 |
| | M5 | 27,340 | **+0.21** | **+3.7** | 1.37 | −1.16 | 38.8 | 46.2 |
| | M15 | 9,689 | +0.32 | +1.9 | 1.36 | −1.04 | 39.8 | 43.6 |
| | H1 | 2,743 | −0.13 | −0.2 | 1.36 | −1.49 | 37.4 | 40.2 |
| ema_zscore_reversion | M1 | 118,679 | **+0.07** | **+7.0** | 1.36 | −1.30 | 35.1 | 57.4 |
| | M5 | 25,903 | **+0.13** | **+3.0** | 1.35 | −1.22 | 44.1 | 54.2 |
| | M15 | 9,627 | +0.14 | +1.1 | 1.34 | −1.21 | 46.2 | 52.0 |
| | H1 | 2,543 | −0.55 | −1.1 | 1.35 | −1.90 | 44.0 | 48.6 |
| mean_anchor_reversion | M1 | 169,871 | 0.00 | +0.5 | 1.35 | −1.35 | 48.2 | 73.8 |
| | M5 / M15 / H1 | | +0.06 / +0.10 / −0.02 | <2 | 1.34 | ≈ −1.3 | | |
| donchian_breakout | M1 | 74,194 | **−0.23** | **−9.9** | 1.80 | −2.03 | 33.9 | 48.8 |
| | M5 | 14,347 | **−0.42** | **−3.5** | 1.58 | −2.00 | 36.5 | 43.2 |
| | M15 / H1 | | −0.04 / −1.32 | ≥ −1 | ~1.5 | −1.5 / −2.8 | | |
| grid_reversion | M1 | 118,867 | **−0.34** | **−24.1** | 1.05 | −1.39 | 72.9 | 86.9 |
| | M5 / M15 / H1 | | −0.40 / −0.42 / −0.23 | −6.2 / −2.4 / −0.4 | 1.06 | ≈ −1.4 | 77 | 82 |

Summed return over 29 quarters: M1 −135% to −205%, M5 −26% to −39%, M15 −7% to −13%, H1 −3.3% to −4.2%.
Zero quarters are positive on M1. At H1, 7–13 of 29 are positive, which is what noise looks like.

## What the data says

1. **Short-horizon mean reversion in EURUSD is real but about 15× too small.**
   Bollinger and z-score reversion have a positive edge with t ≈ 7 on M1 and t ≈ 3–4 on M5.
   The edge holds for longs and shorts, in every session, and in 6 of 7 years (2023 ≈ 0, 2024 ≈ 0).
   It is only **0.07–0.21 pips** per trade against about **1.35 pips** of cost.
   The edge per trade grows from M1 to M15 (0.08 → 0.21 → 0.32), but noise grows faster; by H1 it disappears.

2. **Short-term breakouts reverse.** Donchian has a *negative* edge (t −9.9 on M1, −3.5 on M5), worst in
   Asia (22–07 UTC) and late NY (16–22 UTC). This is the same effect as point 1 seen from the other
   side. Momentum entries on the next few minutes to hours are on the wrong side for EURUSD.

3. **Grid has negative skew and a negative edge before costs.** It wins 72–78% of trades at about +2.3 pips but
   loses about −7.4 pips per basket stop. The needed win rate (80–87% across timeframes) is above the achieved rate even before costs.
   Retire it.

4. **mean_anchor_reversion has no measurable edge** at any timeframe (|t| < 2). Retire it or redesign it from scratch.

5. **Costs are about 1.35 pips per round trip, and commission is the largest part:** commission 0.70, spread
   0.35, slippage 0.32 (market orders). Limit entries carried almost no slippage (grid: 0.05).
   - Over half the cost comes from assumptions (commission, slippage), not from the data. The real account
     (prop firm or MetaQuotes/Capital.com) may differ. **Re-run with the real fee schedule before trusting any result.**
   - Entering with **limit** orders at the signal level, instead of market, removes entry slippage and the
     spread crossing. That is a structural cost cut of about 0.35–0.4 pips (0.2 slippage + about half the spread), not a parameter tweak.

6. **Regime decay.** M1/M5 reversion edge falls to about 0 in 2023–2024 (bollinger M1: +0.10–0.12 in 2018–22,
   0.00 in 2023, +0.04 in 2024). Anything built on point 1 must be checked on 2023–24 separately.

7. **Session pattern (hypothesis, not proven).** Reversion is most consistent in **Asia 22–07 UTC**
   (bollinger M1 t +9.0; ema_zscore M5 +0.32, t +4.9) and weakest in the London/NY overlap.
   Single slices with t ≈ 2 (for example bollinger M15 in NY, +1.57) are expected by chance across this many cuts.

## Traps: do not "learn" these

- **Do not filter on `by_hold` or `by_exit_reason`.** They depend on the outcome: a trade that hits its stop fast is short
  and negative by construction. "Only trades held over 30 minutes win" is hindsight, not a rule you can apply at entry.
- **Multiple testing.** 20 variants × 5 slicings ≈ 400 cuts. Expect several |t| ≈ 2 slices from pure noise. A
  new rule must be decided *before* looking, then pass on quarters it was not built on.
- **Don't tune on these 29 quarters and then report them.** Split them: build on 2018–2021, confirm on 2022–2024, and
  never move the split after seeing results.
- **The data source is not the broker.** HistData spreads may be tighter and ticks are sparser than live. A strategy that
  only works with sub-0.3 pip spreads will not survive live.

## Acceptance bar for a new L1 strategy (proposal)

- edge ≥ costs + margin: **≥ 1.8 pips/trade** at current cost assumptions (or ≥ 1.4× the real round-trip cost);
- edge t ≥ 3 on the build period, **and** positive edge on ≥ 2 of the 3 confirm years (2022–24);
- net positive in ≥ 60% of confirm quarters, max quarterly drawdown inside prop-firm limits;
- a stated mechanism (rubric item 1). "It backtested well" is not a mechanism.

## Where to look next (ranked hypotheses)

1. **Make the real reversion edge bigger per trade, with fewer trades.** Keep the signal family from point 1, but
   only trade when the expected snap-back is several pips: wider bands or z-thresholds on M5–M15, a volatility
   filter, the Asia session, and targets at the mean instead of fixed points. Goal: edge ≥ 1.8 pips with t ≥ 3.
2. **Enter with limit orders at the band or z-level** instead of market orders. This cuts about 0.35–0.4 pips of cost.
   Combine with 1.
3. **Fade breakouts instead of trading them** (the inverse of Donchian), with the same discipline: the raw inverse is
   only +0.2–0.4 pips, so it needs the "bigger move" filter from 1.
4. **Sections B (liquidity vacuum) and C (momentum).** Use point 2 as a warning: short-horizon momentum on EURUSD
   reverses. C needs a horizon or trigger (news, session open, higher-timeframe trend) where the evidence says
   momentum persists.
5. **Cost reality check (blocking for go-live).** Set the target account's real commission and spread, and re-run 1–3.

Retired: `grid_reversion`, `mean_anchor_reversion` (as implemented), raw `donchian_breakout`.
